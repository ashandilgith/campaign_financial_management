"""Budget math, role checks and workflow transitions.

Views stay thin: every state change goes through a function here, which validates
the transition, applies it inside a transaction and writes an AuditLog entry.
"""
from dataclasses import asdict, dataclass, field
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from . import notify
from .models import (
    ApprovalDecision,
    AuditLog,
    BudgetLine,
    BudgetRevision,
    Campaign,
    Payment,
    PaymentRequest,
    Settlement,
    StaffProfile,
    SupplierContract,
)

ZERO = Decimal("0")
S = PaymentRequest.Status

# --------------------------------------------------------------------------- roles

ROLE_STAFF = "Field Staff"
ROLE_MD = "MD"
ROLE_FINANCE = "Finance"
ROLE_ADMIN = "Admin"
ALL_ROLES = (ROLE_STAFF, ROLE_MD, ROLE_FINANCE, ROLE_ADMIN)


def roles_of(user):
    if not user.is_authenticated:
        return set()
    cached = getattr(user, "_cfm_roles", None)
    if cached is None:
        cached = set(user.groups.values_list("name", flat=True))
        if user.is_superuser:
            cached.add(ROLE_ADMIN)
        user._cfm_roles = cached
    return cached


def is_md(user):
    return ROLE_MD in roles_of(user)


def is_finance(user):
    return ROLE_FINANCE in roles_of(user)


def is_admin(user):
    return ROLE_ADMIN in roles_of(user)


def can_manage_campaigns(user):
    return bool(roles_of(user) & {ROLE_MD, ROLE_FINANCE, ROLE_ADMIN})


def can_see_all(user):
    """Field staff only see their own requests; everyone else sees everything."""
    return can_manage_campaigns(user)


def require(condition, message="You do not have permission to do that."):
    if not condition:
        raise PermissionDenied(message)


def supervisor_of(user):
    """The person who endorses this user's requests before the MD, or None (straight to MD)."""
    profile = StaffProfile.objects.filter(user=user).select_related("supervisor").first()
    sup = profile.supervisor if profile else None
    if sup is None or not sup.is_active or sup.pk == user.pk or is_md(user):
        return None
    return sup


def is_supervisor(user):
    return StaffProfile.objects.filter(supervisor=user).exists()


def log(actor, action, message, campaign=None, request=None):
    if request is not None and campaign is None:
        campaign = request.campaign
    return AuditLog.objects.create(
        actor=actor, action=action, message=message, campaign=campaign, request=request
    )


def fmt(amount):
    return f"LKR {amount:,.2f}"


# --------------------------------------------------------------------------- budget math


@dataclass
class Figures:
    """Money position of one budget line (or a whole campaign when summed).

    available = budget - cash_out - committed - pending
    """

    budget: Decimal = ZERO
    cash_out: Decimal = ZERO  # paid / advanced by finance, net of cash returned
    committed: Decimal = ZERO  # approved but not yet paid (incl. unpaid contract balance)
    pending: Decimal = ZERO  # awaiting MD decision
    verified: Decimal = ZERO  # spend backed by verified bills / settlements
    advances_outstanding: Decimal = ZERO  # advances paid but not yet settled & verified
    reimbursements_due: Decimal = ZERO  # staff overspent an advance; company owes them
    pending_requests: int = 0
    unpaid_approved: int = 0
    unverified_bills: int = 0
    unsettled_advances: int = 0

    @property
    def available(self):
        return self.budget - self.cash_out - self.committed - self.pending

    @property
    def utilisation_pct(self):
        if not self.budget:
            return 0
        return round(float((self.cash_out + self.committed) / self.budget * 100), 1)

    @property
    def variance(self):
        """Budget minus verified spend (positive = under budget)."""
        return self.budget - self.verified

    def __add__(self, other):
        out = Figures()
        for name in self.__dataclass_fields__:
            setattr(out, name, getattr(self, name) + getattr(other, name))
        return out

    def as_dict(self):
        d = {k: (str(v) if isinstance(v, Decimal) else v) for k, v in asdict(self).items()}
        d["available"] = str(self.available)
        return d


def _active(requests):
    return [r for r in requests if r.status not in (S.REJECTED, S.CANCELLED)]


def line_figures(line, requests=None, contracts=None):
    """Compute the money position for a budget line.

    ``requests``/``contracts`` may be passed pre-fetched (all for the campaign) to avoid
    repeated queries; they are filtered to this line here.
    """
    if requests is None:
        requests = list(
            line.requests.select_related("payment", "settlement").all()
        )
    else:
        requests = [r for r in requests if r.budget_line_id == line.id]
    if contracts is None:
        contracts = list(line.contracts.all())
    else:
        contracts = [c for c in contracts if c.budget_line_id == line.id]

    f = Figures(budget=line.approved_amount)
    live = _active(requests)

    # Supplier contracts: the whole quoted value is committed once any instalment is
    # approved; instalments draw it down rather than being counted again.
    for c in contracts:
        if c.cancelled:
            continue
        inst = [r for r in live if r.contract_id == c.id]
        paid_c = sum((r.paid_amount for r in inst), ZERO)
        if any(r.status in (S.APPROVED, S.PAID, S.SETTLED) for r in inst):
            f.committed += max(c.total_amount - paid_c, ZERO)
        elif any(r.status in PaymentRequest.OPEN_STATUSES for r in inst):
            # Approving the first instalment commits the full quote, so show that exposure.
            f.pending += c.total_amount

    returned = ZERO
    for r in live:
        if r.status in PaymentRequest.OPEN_STATUSES:
            f.pending_requests += 1
        if r.status == S.APPROVED:
            f.unpaid_approved += 1

        f.cash_out += r.paid_amount
        if r.contract_id is None:
            if r.status in PaymentRequest.OPEN_STATUSES:
                f.pending += r.amount
            elif r.status == S.APPROVED:
                f.committed += r.amount

        if r.kind == PaymentRequest.Kind.ADVANCE and r.status in (S.PAID, S.SETTLED):
            s = getattr(r, "settlement", None)
            if s is not None and s.status == Settlement.Status.VERIFIED:
                returned += s.cash_returned
                f.verified += s.spent_amount
                if s.balance < 0:
                    f.reimbursements_due += -s.balance
            else:
                f.advances_outstanding += r.paid_amount
                f.unsettled_advances += 1
        elif r.kind == PaymentRequest.Kind.SUPPLIER and r.status == S.PAID:
            if r.bill_verified:
                f.verified += r.paid_amount
            else:
                f.unverified_bills += 1

    f.cash_out -= returned
    return f


def campaign_position(campaign):
    """Return ([(line, Figures), ...], total Figures) for a campaign using 3 queries."""
    lines = list(campaign.budget_lines.all())
    requests = list(
        campaign.requests.select_related("payment", "settlement", "requested_by").all()
    )
    contracts = list(campaign.contracts.all())
    rows = [(line, line_figures(line, requests, contracts)) for line in lines]
    total = Figures()
    for _, fig in rows:
        total = total + fig
    return rows, total


def contract_position(contract):
    inst = _active(contract.instalments.select_related("payment").all())
    paid = sum((r.paid_amount for r in inst), ZERO)
    requested = sum((r.amount for r in inst), ZERO)
    return {
        "total": contract.total_amount,
        "requested": requested,
        "paid": paid,
        "unrequested": contract.total_amount - requested,
        "unpaid": contract.total_amount - paid,
        "instalments": inst,
    }


def approval_context(req):
    """Everything the MD needs on one screen before deciding."""
    fig = line_figures(req.budget_line)
    # What 'available' would become if this request were approved: it already sits in
    # pending, and approval moves it to committed, so line availability is unchanged –
    # the meaningful number is availability *excluding* this request.
    if req.contract_id:
        cp = contract_position(req.contract)
        own_pending = (
            req.contract.total_amount
            if not any(r.status in (S.APPROVED, S.PAID, S.SETTLED) for r in cp["instalments"])
            else ZERO
        )
    else:
        cp = None
        own_pending = req.amount if req.status in PaymentRequest.OPEN_STATUSES else ZERO
    available_before = fig.available + own_pending
    impact = own_pending if req.status in PaymentRequest.OPEN_STATUSES else ZERO

    prior = PaymentRequest.objects.filter(campaign=req.campaign).exclude(pk=req.pk)
    if req.kind == PaymentRequest.Kind.SUPPLIER and req.supplier_id:
        prior = prior.filter(supplier=req.supplier)
    else:
        prior = prior.filter(budget_line=req.budget_line)
    prior = prior.exclude(status__in=[S.REJECTED, S.CANCELLED]).select_related("payment")

    return {
        "figures": fig,
        "available_before": available_before,
        "impact": impact,
        "available_after": available_before - impact,
        "other_pending": fig.pending - own_pending,
        "over_budget": available_before - impact < 0,
        "contract": cp,
        "prior_requests": list(prior[:20]),
    }


# --------------------------------------------------------------------------- campaigns & budgets


@transaction.atomic
def create_campaign(user, *, code, name, client="", start_date=None, end_date=None, notes="", lines=(),
                    client_po_number="", client_email="", commission_pct=ZERO):
    require(can_manage_campaigns(user))
    campaign = Campaign.objects.create(
        code=code, name=name, client=client, start_date=start_date, end_date=end_date,
        notes=notes, created_by=user, client_po_number=client_po_number, client_email=client_email,
        commission_pct=commission_pct or ZERO,
    )
    for i, (category, amount) in enumerate(lines):
        BudgetLine.objects.create(campaign=campaign, category=category, approved_amount=amount, sort_order=i)
    log(user, "campaign.create", f"Created campaign with budget {fmt(campaign.total_budget)}", campaign=campaign)
    return campaign


@transaction.atomic
def add_budget_line(user, campaign, category, amount):
    require(can_manage_campaigns(user))
    if campaign.status == Campaign.Status.CLOSED:
        raise ValidationError("Campaign is closed.")
    if campaign.status != Campaign.Status.DRAFT and not is_md(user):
        # After launch, new money goes through the revision workflow instead.
        line = BudgetLine.objects.create(
            campaign=campaign, category=category, approved_amount=ZERO,
            sort_order=campaign.budget_lines.count(),
        )
        log(user, "budget.line_add", f"Added category '{category}' (LKR 0, revision pending)", campaign=campaign)
        propose_revision(user, line, amount, f"New category '{category}'")
        return line
    line = BudgetLine.objects.create(
        campaign=campaign, category=category, approved_amount=amount,
        sort_order=campaign.budget_lines.count(),
    )
    log(user, "budget.line_add", f"Added category '{category}' at {fmt(amount)}", campaign=campaign)
    return line


@transaction.atomic
def propose_revision(user, line, new_amount, reason):
    """MD revisions apply immediately; others go to the MD for approval.

    Draft campaigns can be edited freely by finance/admin (budget not yet approved).
    """
    require(can_manage_campaigns(user))
    campaign = line.campaign
    if campaign.status == Campaign.Status.CLOSED:
        raise ValidationError("Campaign is closed; budget cannot be revised.")
    if not reason.strip():
        raise ValidationError("A reason is required for every budget revision.")
    fig = line_figures(line)
    floor = fig.cash_out + fig.committed
    if new_amount < floor:
        raise ValidationError(
            f"Cannot reduce below what is already paid or committed ({fmt(floor)})."
        )
    rev = BudgetRevision.objects.create(
        budget_line=line, old_amount=line.approved_amount, new_amount=new_amount,
        reason=reason, proposed_by=user,
    )
    if is_md(user) or campaign.status == Campaign.Status.DRAFT:
        _apply_revision(user, rev)
    else:
        log(user, "budget.revision_proposed",
            f"Proposed {line.category}: {fmt(rev.old_amount)} → {fmt(new_amount)}. Reason: {reason}",
            campaign=campaign)
        notify.revision_proposed(rev)
    return rev


def _apply_revision(user, rev):
    line = rev.budget_line
    rev.old_amount = line.approved_amount
    line.approved_amount = rev.new_amount
    line.save(update_fields=["approved_amount"])
    rev.status = BudgetRevision.Status.APPLIED
    rev.decided_by = user
    rev.decided_at = timezone.now()
    rev.save()
    log(user, "budget.revision_applied",
        f"{line.category}: {fmt(rev.old_amount)} → {fmt(rev.new_amount)}. Reason: {rev.reason}",
        campaign=line.campaign)


@transaction.atomic
def decide_revision(user, rev, approve, comment=""):
    require(is_md(user), "Only the MD can approve budget revisions.")
    if rev.status != BudgetRevision.Status.PENDING:
        raise ValidationError("This revision has already been decided.")
    if approve:
        _apply_revision(user, rev)
    else:
        rev.status = BudgetRevision.Status.REJECTED
        rev.decided_by = user
        rev.decided_at = timezone.now()
        rev.save()
        log(user, "budget.revision_rejected",
            f"Rejected {rev.budget_line.category} → {fmt(rev.new_amount)}. {comment}".strip(),
            campaign=rev.budget_line.campaign)


def closure_checklist(campaign):
    rows, total = campaign_position(campaign)
    open_contracts = []
    for c in campaign.contracts.filter(cancelled=False):
        pos = contract_position(c)
        if pos["unpaid"] > 0:
            open_contracts.append((c, pos["unpaid"]))
    pending_revisions = BudgetRevision.objects.filter(
        budget_line__campaign=campaign, status=BudgetRevision.Status.PENDING
    ).count()
    checks = [
        ("No requests awaiting approval", total.pending_requests == 0, total.pending_requests),
        ("No approved requests waiting for payment", total.unpaid_approved == 0, total.unpaid_approved),
        ("All staff advances settled and verified", total.unsettled_advances == 0, total.unsettled_advances),
        ("All supplier bills verified", total.unverified_bills == 0, total.unverified_bills),
        ("All supplier contracts fully paid or cancelled", not open_contracts, len(open_contracts)),
        ("No budget revisions pending", pending_revisions == 0, pending_revisions),
    ]
    return checks, all(ok for _, ok, _ in checks), open_contracts


@transaction.atomic
def set_campaign_status(user, campaign, status):
    require(is_md(user) or is_finance(user) or is_admin(user))
    if status == Campaign.Status.CLOSED:
        _, ready, _ = closure_checklist(campaign)
        if not ready:
            raise ValidationError("Campaign cannot be closed until every closure check passes.")
        campaign.closed_at = timezone.now()
    old = campaign.get_status_display()
    campaign.status = status
    campaign.save()
    log(user, "campaign.status", f"Status {old} → {campaign.get_status_display()}", campaign=campaign)


# --------------------------------------------------------------------------- payment requests


def _validate_contract_amount(contract, amount, exclude_pk=None):
    others = contract.instalments.exclude(status__in=[S.REJECTED, S.CANCELLED])
    if exclude_pk:
        others = others.exclude(pk=exclude_pk)
    already = sum((r.amount for r in others), ZERO)
    remaining = contract.total_amount - already
    if amount > remaining:
        raise ValidationError(
            f"Instalment exceeds the unrequested balance of this contract ({fmt(remaining)}). "
            "Revise the contract value first if the supplier's quote changed."
        )


@transaction.atomic
def submit_request(user, *, campaign, budget_line, kind, amount, purpose, details="",
                   supplier=None, contract=None, new_contract_total=None,
                   new_contract_description="", instalment=PaymentRequest.Instalment.SINGLE,
                   needed_by=None):
    if not campaign.accepts_requests:
        raise ValidationError("This campaign is closed to new requests.")
    if budget_line.campaign_id != campaign.id:
        raise ValidationError("Budget category does not belong to this campaign.")
    if kind == PaymentRequest.Kind.SUPPLIER and supplier is None and contract is not None:
        supplier = contract.supplier
    if kind == PaymentRequest.Kind.SUPPLIER and supplier is None:
        raise ValidationError("Choose a supplier for a supplier payment.")
    if kind == PaymentRequest.Kind.ADVANCE:
        supplier = contract = None
        instalment = PaymentRequest.Instalment.SINGLE

    if contract is None and new_contract_total:
        if instalment == PaymentRequest.Instalment.SINGLE:
            instalment = PaymentRequest.Instalment.FIRST
        contract = SupplierContract.objects.create(
            campaign=campaign, budget_line=budget_line, supplier=supplier,
            description=new_contract_description or purpose,
            total_amount=new_contract_total, created_by=user,
        )
        log(user, "contract.create",
            f"Contract with {supplier}: {contract.description} – total {fmt(contract.total_amount)}",
            campaign=campaign)
    if contract is not None:
        if contract.campaign_id != campaign.id or contract.budget_line_id != budget_line.id:
            raise ValidationError("Contract does not match this campaign/category.")
        if contract.cancelled:
            raise ValidationError("This contract has been cancelled.")
        supplier = contract.supplier
        if instalment == PaymentRequest.Instalment.SINGLE:
            raise ValidationError("Pick which instalment this is (first / second / final).")
        _validate_contract_amount(contract, amount)

    supervisor = supervisor_of(user)
    req = PaymentRequest.objects.create(
        campaign=campaign, budget_line=budget_line, kind=kind, supplier=supplier,
        contract=contract, instalment=instalment, amount=amount, purpose=purpose,
        details=details, needed_by=needed_by, requested_by=user,
        status=S.ENDORSE if supervisor else S.SUBMITTED,
    )
    route = f" → supervisor {supervisor.get_full_name() or supervisor.username}" if supervisor else " → MD"
    log(user, "request.submit",
        f"{req.get_kind_display()} {fmt(amount)} for '{purpose}' ({budget_line.category}){route}", request=req)
    notify.request_needs_approval(req)
    return req


@transaction.atomic
def endorse_request(user, req, decision, comment=""):
    """First approval layer: the requester's supervisor / cost-centre head endorses before the MD."""
    require(req.status == S.ENDORSE or (req.status == S.CLARIFY and _clarify_stage(req) == "supervisor"),
            "This request is not awaiting a supervisor.")
    require(supervisor_of(req.requested_by) == user or is_admin(user),
            "Only the requester's supervisor can endorse this request.")
    require(req.requested_by_id != user.id, "You cannot endorse your own request.")
    D = ApprovalDecision.Decision
    if decision not in (D.ENDORSE, D.REJECT, D.CLARIFY):
        raise ValidationError("Unknown decision.")
    if decision in (D.REJECT, D.CLARIFY) and not comment.strip():
        raise ValidationError("Please add a comment explaining the rejection or what needs clarifying.")
    ctx = approval_context(req)
    snapshot = ctx["figures"].as_dict()
    snapshot.update(available_before=str(ctx["available_before"]), available_after=str(ctx["available_after"]),
                    request_amount=str(req.amount))
    ApprovalDecision.objects.create(request=req, stage=ApprovalDecision.Stage.SUPERVISOR, decision=decision,
                                    comment=comment, snapshot=snapshot, decided_by=user)
    req.status = {D.ENDORSE: S.SUBMITTED, D.REJECT: S.REJECTED, D.CLARIFY: S.CLARIFY}[decision]
    req.save(update_fields=["status", "updated_at"])
    log(user, f"request.supervisor_{decision}",
        f"Supervisor {dict(D.choices)[decision].lower()} {fmt(req.amount)}. {comment}".strip(), request=req)
    if decision == D.ENDORSE:
        notify.request_needs_approval(req, endorsed_by=user)
    else:
        notify.request_decided(req, decision, user, comment, stage="supervisor")


def _clarify_stage(req):
    last = req.decisions.filter(decision=ApprovalDecision.Decision.CLARIFY).order_by("-decided_at", "-pk").first()
    return last.stage if last else ApprovalDecision.Stage.MD


@transaction.atomic
def decide_request(user, req, decision, comment=""):
    require(is_md(user), "Only the MD can approve payment requests.")
    require(req.requested_by_id != user.id, "You cannot approve your own request.")
    if req.status == S.ENDORSE or (req.status == S.CLARIFY and _clarify_stage(req) == "supervisor"):
        raise ValidationError("This request is still with the requester's supervisor.")
    if req.status not in PaymentRequest.OPEN_STATUSES:
        raise ValidationError("This request is not awaiting a decision.")
    ctx = approval_context(req)
    D = ApprovalDecision.Decision
    if decision == D.APPROVE and ctx["over_budget"] and not comment.strip():
        raise ValidationError("This request exceeds the available budget – add a comment to approve it.")
    if decision in (D.REJECT, D.CLARIFY) and not comment.strip():
        raise ValidationError("Please add a comment explaining the rejection or what needs clarifying.")

    snapshot = ctx["figures"].as_dict()
    snapshot.update(
        available_before=str(ctx["available_before"]),
        available_after=str(ctx["available_after"]),
        request_amount=str(req.amount),
    )
    ApprovalDecision.objects.create(
        request=req, decision=decision, comment=comment, snapshot=snapshot, decided_by=user
    )
    req.status = {D.APPROVE: S.APPROVED, D.REJECT: S.REJECTED, D.CLARIFY: S.CLARIFY}[decision]
    req.save(update_fields=["status", "updated_at"])
    label = dict(D.choices)[decision]
    log(user, f"request.{decision}", f"{label} {fmt(req.amount)}. {comment}".strip(), request=req)
    notify.request_decided(req, decision, user, comment)


@transaction.atomic
def respond_to_clarification(user, req, response, new_amount=None):
    require(req.requested_by_id == user.id, "Only the requester can respond.")
    if req.status != S.CLARIFY:
        raise ValidationError("No clarification has been requested.")
    if not response.strip():
        raise ValidationError("Please write a response for the MD.")
    last = req.decisions.filter(decision=ApprovalDecision.Decision.CLARIFY).order_by("-decided_at", "-pk").first()
    msg = f"Clarification: {response}"
    if new_amount is not None and new_amount != req.amount:
        if req.contract_id:
            _validate_contract_amount(req.contract, new_amount, exclude_pk=req.pk)
        msg += f" Amount changed {fmt(req.amount)} → {fmt(new_amount)}."
        req.amount = new_amount
    back_to_supervisor = last is not None and last.stage == ApprovalDecision.Stage.SUPERVISOR
    req.status = S.ENDORSE if back_to_supervisor else S.SUBMITTED
    req.save()
    log(user, "request.clarified", msg, request=req)
    notify.clarification_answered(req, last.decided_by if last else None)


@transaction.atomic
def cancel_request(user, req, reason=""):
    require(req.requested_by_id == user.id or is_finance(user) or is_admin(user))
    if req.status not in (S.ENDORSE, S.SUBMITTED, S.CLARIFY, S.APPROVED):
        raise ValidationError("Only unpaid requests can be cancelled.")
    req.status = S.CANCELLED
    req.save(update_fields=["status", "updated_at"])
    log(user, "request.cancel", f"Cancelled. {reason}".strip(), request=req)


@transaction.atomic
def record_payment(user, req, *, amount, paid_on, method, reference):
    require(is_finance(user), "Only finance can record payments.")
    if req.status != S.APPROVED:
        raise ValidationError("Payments can only be recorded against MD-approved requests.")
    if not req.decisions.filter(decision=ApprovalDecision.Decision.APPROVE).exists():
        raise ValidationError("No MD approval on record for this request.")
    if amount > req.amount:
        raise ValidationError(f"Payment cannot exceed the approved amount ({fmt(req.amount)}).")
    if not reference.strip():
        raise ValidationError("A transaction reference is required.")
    Payment.objects.create(
        request=req, amount=amount, paid_on=paid_on, method=method, reference=reference, recorded_by=user
    )
    req.status = S.PAID
    req.save(update_fields=["status", "updated_at"])
    note = "" if amount == req.amount else f" (approved {fmt(req.amount)}; balance released)"
    log(user, "payment.record",
        f"Paid {fmt(amount)} by {dict(Payment.Method.choices)[method]}, ref {reference}{note}", request=req)
    notify.payment_recorded(req)


@transaction.atomic
def verify_bill(user, req):
    require(is_finance(user), "Only finance can verify bills.")
    if req.kind != PaymentRequest.Kind.SUPPLIER or req.status != S.PAID:
        raise ValidationError("Bills are verified on paid supplier requests.")
    if not req.attachments.filter(kind="bill").exists():
        raise ValidationError("Upload the supplier bill/invoice before verifying.")
    req.bill_verified = True
    req.save(update_fields=["bill_verified", "updated_at"])
    log(user, "bill.verify", "Supplier bill verified", request=req)


@transaction.atomic
def submit_settlement(user, req, *, spent_amount, cash_returned, notes=""):
    require(req.requested_by_id == user.id or is_finance(user), "Only the advance holder or finance can settle.")
    if req.kind != PaymentRequest.Kind.ADVANCE or req.status != S.PAID:
        raise ValidationError("Only paid advances can be settled.")
    if not req.attachments.filter(kind="receipt").exists() and spent_amount > 0:
        raise ValidationError("Upload at least one receipt before settling.")
    if cash_returned > req.paid_amount:
        raise ValidationError("Cash returned cannot exceed the advance paid.")
    existing = getattr(req, "settlement", None)
    if existing and existing.status == Settlement.Status.SUBMITTED:
        raise ValidationError("A settlement is already awaiting finance.")
    if existing:  # returned for correction → resubmit
        existing.spent_amount, existing.cash_returned, existing.notes = spent_amount, cash_returned, notes
        existing.status = Settlement.Status.SUBMITTED
        existing.submitted_by = user
        existing.save()
        s = existing
    else:
        s = Settlement.objects.create(
            request=req, spent_amount=spent_amount, cash_returned=cash_returned, notes=notes, submitted_by=user
        )
    log(user, "settlement.submit",
        f"Settlement: spent {fmt(spent_amount)}, returned {fmt(cash_returned)}, balance {fmt(s.balance)}",
        request=req)
    notify.settlement_submitted(req)
    return s


@transaction.atomic
def verify_settlement(user, req, approve=True, comment=""):
    require(is_finance(user), "Only finance can verify settlements.")
    s = getattr(req, "settlement", None)
    if s is None or s.status != Settlement.Status.SUBMITTED:
        raise ValidationError("No settlement awaiting verification.")
    if approve:
        s.status = Settlement.Status.VERIFIED
        s.verified_by = user
        s.verified_at = timezone.now()
        s.save()
        req.status = S.SETTLED
        req.save(update_fields=["status", "updated_at"])
        extra = ""
        if s.balance > 0:
            extra = f" Staff still holds {fmt(s.balance)} – follow up."
        elif s.balance < 0:
            extra = f" Reimbursement due to staff: {fmt(-s.balance)}."
        log(user, "settlement.verify", f"Settlement verified.{extra} {comment}".strip(), request=req)
        notify.settlement_checked(req, True, comment)
    else:
        if not comment.strip():
            raise ValidationError("Explain what needs correcting.")
        s.status = Settlement.Status.RETURNED
        s.save()
        log(user, "settlement.return", f"Settlement returned for correction: {comment}", request=req)
        notify.settlement_checked(req, False, comment)


def record_attachment(user, req, attachment):
    log(user, "attachment.upload",
        f"Uploaded {attachment.get_kind_display()}: {attachment.filename}", request=req)


# --------------------------------------------------------------------------- AI-assisted checks


def _norm(name):
    import re

    name = (name or "").lower()
    name = re.sub(r"\b(pvt|private|ltd|limited|co|company|\(pvt\)|plc|enterprises?)\b", " ", name)
    return re.sub(r"[^a-z0-9]+", " ", name).strip()


def _similar(a, b):
    from difflib import SequenceMatcher

    a, b = _norm(a), _norm(b)
    if not a or not b:
        return True
    return a in b or b in a or SequenceMatcher(None, a, b).ratio() >= 0.6


def ai_checks(req):
    """Rule-based cross-checks on AI-read documents. Returns [(level, message)], level ∈ {warn, info, ok}.

    These are flags for a human to look at; they never block or change anything.
    """
    from datetime import date as _date, timedelta

    from .models import Attachment

    tol = Decimal("1.00")
    out = []
    atts = [a for a in req.attachments.all() if a.kind in Attachment.AI_KINDS]
    read = [a for a in atts if a.ai_status == "done"]
    for a in atts:
        if a.ai_status == "error":
            out.append(("info", f"{a.filename}: AI could not read this file ({a.ai_error[:120]})."))

    expected_type = {"bill": {"invoice", "receipt"}, "receipt": {"receipt", "invoice"}, "quote": {"quotation"}}
    for a in read:
        d = a.ai_data
        dtype = d.get("document_type")
        if dtype == "unreadable" or d.get("legibility") == "poor":
            out.append(("warn", f"{a.filename}: hard to read – check the original manually."))
        elif dtype and dtype not in expected_type.get(a.kind, {dtype}):
            out.append(("warn", f"{a.filename}: uploaded as {a.get_kind_display().lower()} but looks like a {dtype}."))
        cur = (d.get("currency") or "LKR").upper()
        if cur not in ("LKR", "RS", "RS."):
            out.append(("warn", f"{a.filename}: currency is {cur}, not LKR."))
        if req.kind == PaymentRequest.Kind.SUPPLIER and req.supplier and d.get("vendor_name"):
            if not _similar(d["vendor_name"], req.supplier.name):
                out.append(("warn", f"{a.filename}: issued by '{d['vendor_name']}', but the payee is '{req.supplier.name}'."))
        doc_date = d.get("document_date")
        if doc_date:
            try:
                dd = _date.fromisoformat(doc_date)
            except ValueError:
                dd = None
            if dd:
                c = req.campaign
                if dd > timezone.localdate() + timedelta(days=1):
                    out.append(("warn", f"{a.filename}: dated {dd:%d %b %Y}, which is in the future."))
                elif c.start_date and dd < c.start_date - timedelta(days=45):
                    out.append(("warn", f"{a.filename}: dated {dd:%d %b %Y}, well before the campaign started."))
                elif c.end_date and dd > c.end_date + timedelta(days=45):
                    out.append(("warn", f"{a.filename}: dated {dd:%d %b %Y}, well after the campaign ended."))
        if d.get("handwritten"):
            out.append(("info", f"{a.filename}: key figures are handwritten."))
        if d.get("notes"):
            out.append(("info", f"{a.filename}: {d['notes']}"))

        # Possible duplicates anywhere in the system (same document claimed twice).
        dupes = Attachment.objects.filter(ai_status="done").exclude(pk=a.pk).exclude(request__status__in=[S.REJECTED, S.CANCELLED])
        num, total = d.get("document_number"), a.ai_total
        hits = []
        for other in dupes.select_related("request"):
            od = other.ai_data
            same_no = num and od.get("document_number") and str(od["document_number"]).strip() == str(num).strip() \
                and _similar(od.get("vendor_name"), d.get("vendor_name"))
            same_amt = total is not None and other.ai_total == total and od.get("document_date") == d.get("document_date") \
                and d.get("document_date") and _similar(od.get("vendor_name"), d.get("vendor_name"))
            if same_no or same_amt:
                hits.append(other.request.reference)
        if hits:
            out.append(("warn", f"{a.filename}: possible duplicate – same document appears on {', '.join(sorted(set(hits)))}."))

    def total_of(kind):
        vals = [a.ai_total for a in read if a.kind == kind and a.ai_total is not None]
        return sum(vals, ZERO) if vals else None

    # Amount cross-checks
    quote_total = total_of("quote")
    if quote_total is not None:
        target = req.contract.total_amount if req.contract_id else req.amount
        label = "contract value" if req.contract_id else "requested amount"
        if abs(quote_total - target) > tol:
            out.append(("warn", f"Quotation total {fmt(quote_total)} ≠ {label} {fmt(target)}."))
        else:
            out.append(("ok", f"Quotation total matches the {label}."))

    if req.kind == PaymentRequest.Kind.SUPPLIER:
        bill_total = total_of("bill")
        if bill_total is not None:
            paid = req.paid_amount or req.amount
            if req.contract_id:
                # an instalment's bill may be for the whole job; compare with contract too
                if abs(bill_total - paid) > tol and abs(bill_total - req.contract.total_amount) > tol:
                    out.append(("warn", f"Bill total {fmt(bill_total)} matches neither this instalment ({fmt(paid)}) nor the contract ({fmt(req.contract.total_amount)})."))
                else:
                    out.append(("ok", "Bill total matches the instalment or contract value."))
            elif abs(bill_total - paid) > tol:
                out.append(("warn", f"Bill total {fmt(bill_total)} ≠ amount {'paid' if req.paid_amount else 'requested'} {fmt(paid)}."))
            else:
                out.append(("ok", "Bill total matches the payment."))

    if req.kind == PaymentRequest.Kind.ADVANCE:
        receipts = [a for a in atts if a.kind == "receipt"]
        rec_total = total_of("receipt")
        if rec_total is not None:
            unread = len(receipts) - len([a for a in receipts if a.ai_total is not None])
            s = getattr(req, "settlement", None)
            note = f" ({unread} receipt{'s' if unread != 1 else ''} not read)" if unread else ""
            if s is not None:
                if abs(rec_total - s.spent_amount) > tol:
                    out.append(("warn", f"Receipts add up to {fmt(rec_total)}{note}, but the settlement claims {fmt(s.spent_amount)} spent."))
                else:
                    out.append(("ok", f"Receipts add up to the claimed spend ({fmt(rec_total)})."))
            else:
                out.append(("info", f"Receipts read so far add up to {fmt(rec_total)}{note}."))
    order = {"warn": 0, "info": 1, "ok": 2}
    return sorted(out, key=lambda x: order[x[0]])


def receipts_total(req):
    """Sum of AI-read receipt totals, used to pre-fill a settlement. None if nothing read."""
    vals = [a.ai_total for a in req.attachments.filter(kind="receipt", ai_status="done") if a.ai_total is not None]
    return sum(vals, ZERO) if vals else None


def run_ai_read(user, attachment):
    from . import ai

    ai.read_attachment(attachment)
    if attachment.ai_status == "done":
        d = attachment.ai_data
        total = attachment.ai_total
        log(user, "ai.read",
            f"AI read {attachment.filename}: {d.get('vendor_name') or 'unknown vendor'}, "
            f"{d.get('document_date') or 'no date'}, total {fmt(total) if total is not None else 'not found'}",
            request=attachment.request)
    else:
        log(user, "ai.error", f"AI could not read {attachment.filename}", request=attachment.request)
    return attachment


# --------------------------------------------------------------------------- reports & email


def send_campaign_report(user, campaign, to_address, message=""):
    """Build the closure report PDF and email it to the client (or simulate that)."""
    from django.core.validators import validate_email

    from .reports import build_campaign_report

    require(can_see_all(user), "Only finance, the MD or an admin can send reports.")
    to_address = (to_address or "").strip()
    validate_email(to_address)
    pdf = build_campaign_report(campaign, prepared_by=user)
    email = notify.campaign_report(campaign, user, to_address, pdf, message)
    log(user, "report.email", f"Closure report emailed to {to_address} ({len(pdf) // 1024} KB)", campaign=campaign)
    return email
