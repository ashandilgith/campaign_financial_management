"""Budget math, role checks and workflow transitions.

Views stay thin: every state change goes through a function here, which validates
the transition, applies it inside a transaction and writes an AuditLog entry.
"""
from dataclasses import asdict, dataclass, field
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from .models import (
    ApprovalDecision,
    AuditLog,
    BudgetLine,
    BudgetRevision,
    Campaign,
    Payment,
    PaymentRequest,
    Settlement,
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
def create_campaign(user, *, code, name, client="", start_date=None, end_date=None, notes="", lines=()):
    require(can_manage_campaigns(user))
    campaign = Campaign.objects.create(
        code=code, name=name, client=client, start_date=start_date, end_date=end_date,
        notes=notes, created_by=user,
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
        ("No requests awaiting MD decision", total.pending_requests == 0, total.pending_requests),
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

    req = PaymentRequest.objects.create(
        campaign=campaign, budget_line=budget_line, kind=kind, supplier=supplier,
        contract=contract, instalment=instalment, amount=amount, purpose=purpose,
        details=details, needed_by=needed_by, requested_by=user,
    )
    log(user, "request.submit",
        f"{req.get_kind_display()} {fmt(amount)} for '{purpose}' ({budget_line.category})", request=req)
    return req


@transaction.atomic
def decide_request(user, req, decision, comment=""):
    require(is_md(user), "Only the MD can approve payment requests.")
    require(req.requested_by_id != user.id, "You cannot approve your own request.")
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


@transaction.atomic
def respond_to_clarification(user, req, response, new_amount=None):
    require(req.requested_by_id == user.id, "Only the requester can respond.")
    if req.status != S.CLARIFY:
        raise ValidationError("No clarification has been requested.")
    if not response.strip():
        raise ValidationError("Please write a response for the MD.")
    msg = f"Clarification: {response}"
    if new_amount is not None and new_amount != req.amount:
        if req.contract_id:
            _validate_contract_amount(req.contract, new_amount, exclude_pk=req.pk)
        msg += f" Amount changed {fmt(req.amount)} → {fmt(new_amount)}."
        req.amount = new_amount
    req.status = S.SUBMITTED
    req.save()
    log(user, "request.clarified", msg, request=req)


@transaction.atomic
def cancel_request(user, req, reason=""):
    require(req.requested_by_id == user.id or is_finance(user) or is_admin(user))
    if req.status not in (S.SUBMITTED, S.CLARIFY, S.APPROVED):
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
    else:
        if not comment.strip():
            raise ValidationError("Explain what needs correcting.")
        s.status = Settlement.Status.RETURNED
        s.save()
        log(user, "settlement.return", f"Settlement returned for correction: {comment}", request=req)


def record_attachment(user, req, attachment):
    log(user, "attachment.upload",
        f"Uploaded {attachment.get_kind_display()}: {attachment.filename}", request=req)
