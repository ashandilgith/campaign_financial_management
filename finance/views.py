import csv

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q
from django.http import FileResponse, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from django.conf import settings

from . import ai
from . import services as svc
from .forms import (
    AttachmentForm,
    BudgetLineForm,
    CampaignForm,
    ClarifyResponseForm,
    DecisionForm,
    PaymentForm,
    PaymentRequestForm,
    RevisionForm,
    SettlementForm,
)
from .models import (
    Attachment,
    AuditLog,
    BudgetLine,
    BudgetRevision,
    Campaign,
    PaymentRequest,
    Settlement,
    Supplier,
)

S = PaymentRequest.Status


def _errors(request, exc):
    msgs = exc.messages if isinstance(exc, ValidationError) else [str(exc)]
    for m in msgs:
        messages.error(request, m)


def _maybe_ai_read(user, att):
    if ai.enabled() and settings.AI_AUTO_READ and att.kind in Attachment.AI_KINDS:
        svc.run_ai_read(user, att)


def _visible_requests(user):
    qs = PaymentRequest.objects.select_related(
        "campaign", "budget_line", "supplier", "requested_by", "payment", "settlement"
    )
    return qs if svc.can_see_all(user) else qs.filter(requested_by=user)


def _get_request_for(user, pk):
    req = get_object_or_404(_visible_requests(user), pk=pk)
    return req


# --------------------------------------------------------------------------- dashboard & queues


@login_required
def dashboard(request):
    user = request.user
    ctx = {
        "my_requests": _visible_requests(user).filter(requested_by=user)[:8],
        "active_campaigns": Campaign.objects.exclude(status=Campaign.Status.CLOSED)[:10],
    }
    if svc.can_manage_campaigns(user):
        summaries = []
        for c in ctx["active_campaigns"]:
            _, total = svc.campaign_position(c)
            summaries.append((c, total))
        ctx["campaign_summaries"] = summaries
    return render(request, "finance/dashboard.html", ctx)


@login_required
def md_queue(request):
    svc.require(svc.is_md(request.user), "MD only.")
    items = []
    qs = _visible_requests(request.user).filter(status=S.SUBMITTED).exclude(requested_by=request.user)
    for req in qs.order_by("created_at"):
        c = svc.approval_context(req)
        c["ai_warnings"] = [m for lvl, m in svc.ai_checks(req) if lvl == "warn"]
        items.append((req, c))
    revisions = BudgetRevision.objects.filter(status=BudgetRevision.Status.PENDING).select_related(
        "budget_line__campaign", "proposed_by"
    )
    clarifying = _visible_requests(request.user).filter(status=S.CLARIFY)
    return render(request, "finance/md_queue.html",
                  {"items": items, "revisions": revisions, "clarifying": clarifying})


@login_required
def finance_queue(request):
    svc.require(svc.is_finance(request.user), "Finance only.")
    qs = _visible_requests(request.user)
    return render(request, "finance/finance_queue.html", {
        "to_pay": qs.filter(status=S.APPROVED).order_by("needed_by", "created_at"),
        "settlements": qs.filter(settlement__status=Settlement.Status.SUBMITTED),
        "bills": qs.filter(kind=PaymentRequest.Kind.SUPPLIER, status=S.PAID, bill_verified=False),
        "advances": qs.filter(kind=PaymentRequest.Kind.ADVANCE, status=S.PAID).exclude(
            settlement__status=Settlement.Status.SUBMITTED),
    })


@login_required
def request_list(request):
    qs = _visible_requests(request.user)
    status = request.GET.get("status")
    campaign = request.GET.get("campaign")
    q = request.GET.get("q", "").strip()
    if status:
        qs = qs.filter(status=status)
    if campaign:
        qs = qs.filter(campaign_id=campaign)
    if q:
        qs = qs.filter(Q(purpose__icontains=q) | Q(supplier__name__icontains=q) | Q(details__icontains=q))
    return render(request, "finance/request_list.html", {
        "requests": qs[:200], "statuses": S.choices, "campaigns": Campaign.objects.all(),
        "f_status": status or "", "f_campaign": campaign or "", "q": q,
    })


# --------------------------------------------------------------------------- campaigns


@login_required
def campaign_list(request):
    return render(request, "finance/campaign_list.html", {"campaigns": Campaign.objects.all()})


@login_required
def campaign_create(request):
    svc.require(svc.can_manage_campaigns(request.user))
    form = CampaignForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            c = svc.create_campaign(
                request.user, code=d["code"], name=d["name"], client=d["client"],
                start_date=d["start_date"], end_date=d["end_date"], notes=d["notes"],
                lines=d["budget_lines"],
            )
            if d["status"] != c.status:
                c.status = d["status"]
                c.save(update_fields=["status"])
        except ValidationError as e:
            _errors(request, e)
        else:
            messages.success(request, "Campaign created.")
            return redirect(c)
    return render(request, "finance/campaign_form.html", {"form": form})


@login_required
def campaign_detail(request, pk):
    campaign = get_object_or_404(Campaign, pk=pk)
    if not svc.can_see_all(request.user):
        return redirect("request_new", campaign_id=pk)
    rows, total = svc.campaign_position(campaign)
    contracts = [(c, svc.contract_position(c)) for c in campaign.contracts.select_related("supplier", "budget_line")]
    checks, ready, _ = svc.closure_checklist(campaign)
    return render(request, "finance/campaign_detail.html", {
        "campaign": campaign, "rows": rows, "total": total, "contracts": contracts,
        "requests": campaign.requests.select_related("budget_line", "supplier", "requested_by", "payment")[:100],
        "revisions": BudgetRevision.objects.filter(budget_line__campaign=campaign).select_related(
            "budget_line", "proposed_by", "decided_by")[:30],
        "line_form": BudgetLineForm(), "revision_form": RevisionForm(),
        "checks": checks, "ready_to_close": ready, "statuses": Campaign.Status.choices,
    })


@login_required
@require_POST
def campaign_add_line(request, pk):
    campaign = get_object_or_404(Campaign, pk=pk)
    form = BudgetLineForm(request.POST)
    if form.is_valid():
        try:
            svc.add_budget_line(request.user, campaign, form.cleaned_data["category"], form.cleaned_data["amount"])
            messages.success(request, "Budget category added.")
        except (ValidationError, PermissionDenied) as e:
            _errors(request, e)
    else:
        messages.error(request, "Enter a category and amount.")
    return redirect(campaign)


@login_required
@require_POST
def line_revise(request, pk):
    line = get_object_or_404(BudgetLine, pk=pk)
    form = RevisionForm(request.POST)
    if form.is_valid():
        try:
            rev = svc.propose_revision(request.user, line, form.cleaned_data["new_amount"], form.cleaned_data["reason"])
            messages.success(request, "Budget revised." if rev.status == "applied" else "Revision sent to MD.")
        except (ValidationError, PermissionDenied) as e:
            _errors(request, e)
    else:
        messages.error(request, "Enter the new amount and a reason.")
    return redirect(line.campaign)


@login_required
@require_POST
def revision_decide(request, pk):
    rev = get_object_or_404(BudgetRevision, pk=pk)
    try:
        svc.decide_revision(request.user, rev, request.POST.get("decision") == "approve",
                            request.POST.get("comment", ""))
        messages.success(request, "Revision decided.")
    except (ValidationError, PermissionDenied) as e:
        _errors(request, e)
    return redirect(request.POST.get("next") or "md_queue")


@login_required
@require_POST
def campaign_status(request, pk):
    campaign = get_object_or_404(Campaign, pk=pk)
    try:
        svc.set_campaign_status(request.user, campaign, request.POST["status"])
        messages.success(request, f"Campaign is now {campaign.get_status_display()}.")
    except (ValidationError, PermissionDenied, KeyError) as e:
        _errors(request, e)
    return redirect(campaign)


@login_required
def campaign_report(request, pk):
    svc.require(svc.can_see_all(request.user))
    campaign = get_object_or_404(Campaign, pk=pk)
    rows, total = svc.campaign_position(campaign)
    payments = campaign.requests.filter(payment__isnull=False).select_related(
        "payment", "budget_line", "supplier", "requested_by", "settlement").order_by("payment__paid_on")
    checks, ready, _ = svc.closure_checklist(campaign)
    return render(request, "finance/campaign_report.html", {
        "campaign": campaign, "rows": rows, "total": total, "payments": payments,
        "checks": checks, "ready_to_close": ready,
    })


@login_required
def campaign_export(request, pk):
    """CSV of every payment – also the hand-off format for QuickBooks import later."""
    svc.require(svc.can_see_all(request.user))
    campaign = get_object_or_404(Campaign, pk=pk)
    resp = HttpResponse(content_type="text/csv")
    resp["Content-Disposition"] = f'attachment; filename="{campaign.code}-payments.csv"'
    w = csv.writer(resp)
    w.writerow(["Date", "Reference", "Type", "Category", "Payee", "Purpose", "Instalment",
                "Approved", "Paid", "Method", "Txn ref", "Bill verified", "Settled spent", "Cash returned"])
    for r in campaign.requests.filter(payment__isnull=False).select_related(
            "payment", "budget_line", "supplier", "requested_by", "settlement").order_by("payment__paid_on"):
        s = getattr(r, "settlement", None)
        verified_s = s if s and s.status == Settlement.Status.VERIFIED else None
        w.writerow([
            r.payment.paid_on, r.reference, r.get_kind_display(), r.budget_line.category, r.payee, r.purpose,
            r.get_instalment_display(), r.amount, r.payment.amount, r.payment.get_method_display(),
            r.payment.reference, "yes" if r.bill_verified else "",
            verified_s.spent_amount if verified_s else "", verified_s.cash_returned if verified_s else "",
        ])
    return resp


@login_required
def audit_log(request):
    svc.require(svc.can_see_all(request.user))
    qs = AuditLog.objects.select_related("actor", "campaign", "request")
    if request.GET.get("campaign"):
        qs = qs.filter(campaign_id=request.GET["campaign"])
    return render(request, "finance/audit_log.html", {"entries": qs[:300], "campaigns": Campaign.objects.all(),
                                                      "f_campaign": request.GET.get("campaign", "")})


# --------------------------------------------------------------------------- payment requests


@login_required
def request_new(request, campaign_id):
    campaign = get_object_or_404(Campaign, pk=campaign_id)
    if not campaign.accepts_requests:
        messages.error(request, "This campaign is closed to new requests.")
        return redirect("campaign_list")
    form = PaymentRequestForm(request.POST or None, request.FILES or None, campaign=campaign)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        supplier = d.get("supplier")
        if d["kind"] == PaymentRequest.Kind.SUPPLIER and not supplier and d.get("new_supplier"):
            supplier, _ = Supplier.objects.get_or_create(name=d["new_supplier"].strip())
        try:
            req = svc.submit_request(
                request.user, campaign=campaign, budget_line=d["budget_line"], kind=d["kind"],
                amount=d["amount"], purpose=d["purpose"], details=d["details"], supplier=supplier,
                contract=d.get("contract"), new_contract_total=d.get("new_contract_total"),
                instalment=d["instalment"], needed_by=d.get("needed_by"),
            )
            if d.get("quote"):
                att = Attachment.objects.create(request=req, kind=Attachment.Kind.QUOTE, file=d["quote"],
                                                uploaded_by=request.user)
                svc.record_attachment(request.user, req, att)
                _maybe_ai_read(request.user, att)
        except (ValidationError, PermissionDenied) as e:
            _errors(request, e)
        else:
            messages.success(request, f"{req.reference} sent to the MD for approval.")
            return redirect(req)
    rows, _ = svc.campaign_position(campaign)
    return render(request, "finance/request_form.html", {"form": form, "campaign": campaign, "rows": rows})


@login_required
def request_detail(request, pk):
    req = _get_request_for(request.user, pk)
    user = request.user
    ctx = {
        "req": req,
        "ctx": svc.approval_context(req),
        "decisions": req.decisions.select_related("decided_by"),
        "attachments": req.attachments.select_related("uploaded_by"),
        "history": req.audit.select_related("actor"),
        "settlement": getattr(req, "settlement", None),
        "payment": getattr(req, "payment", None),
        "attachment_form": AttachmentForm(),
        "is_owner": req.requested_by_id == user.id,
        "ai_enabled": ai.enabled(),
        "ai_checks": svc.ai_checks(req),
    }
    if svc.is_md(user) and req.status == S.SUBMITTED and not ctx["is_owner"]:
        ctx["decision_form"] = DecisionForm()
    if ctx["is_owner"] and req.status == S.CLARIFY:
        ctx["clarify_form"] = ClarifyResponseForm(initial={"new_amount": req.amount})
    if svc.is_finance(user) and req.status == S.APPROVED:
        ctx["payment_form"] = PaymentForm(initial={"amount": req.amount})
    if (req.kind == PaymentRequest.Kind.ADVANCE and req.status == S.PAID
            and (ctx["is_owner"] or svc.is_finance(user))
            and (ctx["settlement"] is None or ctx["settlement"].status == Settlement.Status.RETURNED)):
        s = ctx["settlement"]
        read_total = svc.receipts_total(req)
        if s:
            spent, returned = s.spent_amount, s.cash_returned
        elif read_total is not None:
            spent, returned = read_total, max(req.paid_amount - read_total, 0)
        else:
            spent, returned = req.paid_amount, 0
        ctx["settlement_form"] = SettlementForm(initial={"spent_amount": spent, "cash_returned": returned})
        ctx["prefilled_from_ai"] = s is None and read_total is not None
    ctx["can_cancel"] = req.status in (S.SUBMITTED, S.CLARIFY, S.APPROVED) and (
        ctx["is_owner"] or svc.is_finance(user) or svc.is_admin(user))
    return render(request, "finance/request_detail.html", ctx)


@login_required
@require_POST
def request_action(request, pk, action):
    req = _get_request_for(request.user, pk)
    user, P = request.user, request.POST
    try:
        if action in ("approve", "reject", "clarify"):
            svc.decide_request(user, req, action, P.get("comment", ""))
            messages.success(request, f"{req.reference}: {req.get_status_display()}.")
            if P.get("next"):
                return redirect(P["next"])
        elif action == "respond":
            form = ClarifyResponseForm(P)
            if not form.is_valid():
                raise ValidationError("Write a response for the MD.")
            svc.respond_to_clarification(user, req, form.cleaned_data["response"], form.cleaned_data["new_amount"])
            messages.success(request, "Response sent – the request is back with the MD.")
        elif action == "cancel":
            svc.cancel_request(user, req, P.get("reason", ""))
            messages.success(request, "Request cancelled.")
        elif action == "pay":
            form = PaymentForm(P, request.FILES)
            if not form.is_valid():
                raise ValidationError([f"{k}: {v[0]}" for k, v in form.errors.items()])
            d = form.cleaned_data
            svc.record_payment(user, req, amount=d["amount"], paid_on=d["paid_on"], method=d["method"],
                               reference=d["reference"])
            if d.get("proof"):
                att = Attachment.objects.create(request=req, kind=Attachment.Kind.PAYMENT_PROOF, file=d["proof"],
                                                uploaded_by=user)
                svc.record_attachment(user, req, att)
            messages.success(request, "Payment recorded.")
        elif action == "verify_bill":
            svc.verify_bill(user, req)
            messages.success(request, "Bill verified.")
        elif action == "settle":
            form = SettlementForm(P)
            if not form.is_valid():
                raise ValidationError("Check the settlement amounts.")
            d = form.cleaned_data
            svc.submit_settlement(user, req, spent_amount=d["spent_amount"], cash_returned=d["cash_returned"],
                                  notes=d["notes"])
            messages.success(request, "Settlement submitted to finance.")
        elif action in ("verify_settlement", "return_settlement"):
            svc.verify_settlement(user, req, approve=action == "verify_settlement", comment=P.get("comment", ""))
            messages.success(request, "Settlement updated.")
        elif action == "upload":
            svc.require(req.requested_by_id == user.id or svc.can_see_all(user))
            form = AttachmentForm(P, request.FILES)
            if not form.is_valid():
                raise ValidationError("Choose a file and document type.")
            att = form.save(commit=False)
            att.request, att.uploaded_by = req, user
            att.save()
            svc.record_attachment(user, req, att)
            _maybe_ai_read(user, att)
            if att.ai_status == "done":
                messages.success(request, "File uploaded and read by AI – see the checks panel.")
            elif att.ai_status == "error":
                messages.warning(request, "File uploaded, but AI could not read it. Check it manually.")
            else:
                messages.success(request, "File uploaded.")
        elif action == "ai_read":
            svc.require(ai.enabled(), "AI is not configured (set OPENAI_API_KEY in .env).")
            svc.require(req.requested_by_id == user.id or svc.can_see_all(user))
            att = get_object_or_404(Attachment, pk=P.get("attachment"), request=req)
            svc.run_ai_read(user, att)
            if att.ai_status == "done":
                messages.success(request, f"AI read {att.filename}.")
            else:
                messages.warning(request, f"AI could not read {att.filename}: {att.ai_error[:150]}")
        else:
            raise ValidationError("Unknown action.")
    except (ValidationError, PermissionDenied) as e:
        _errors(request, e)
    return redirect(req)


@login_required
def attachment_download(request, pk):
    att = get_object_or_404(Attachment, pk=pk)
    _get_request_for(request.user, att.request_id)  # permission check
    try:
        fh = att.file.open("rb")
    except FileNotFoundError:
        from django.http import Http404

        raise Http404("This file is no longer stored on the server. Please upload it again.")
    return FileResponse(fh, as_attachment=False, filename=att.filename)


# --------------------------------------------------------------------------- AI assistant


ASK_EXAMPLES = [
    "How much is left in the Promoters budget for CMP-2611?",
    "What is waiting for my approval and how much is it in total?",
    "Which advances are still not settled, and for how long?",
    "How much have we paid Metro Fabricators, and what is still owed on their contract?",
    "Is CMP-2609 ready to close? If not, what's blocking it?",
    "Any AI document warnings I should look at?",
]


@login_required
def ask_view(request):
    from . import agent

    history = request.session.get("ask_history", [])
    if request.method == "POST":
        if request.POST.get("clear"):
            request.session["ask_history"] = []
            return redirect("ask")
        q = request.POST.get("question", "").strip()[:1000]
        if q and agent.enabled():
            res = agent.ask(request.user, q, [(h["role"], h["text"]) for h in history])
            history.append({"role": "user", "text": q})
            history.append({"role": "assistant", "text": res["answer"] or "", "error": res.get("error", ""),
                            "tools": [{"name": c["name"], "args": c["args"], "ok": c["ok"]} for c in res["tool_calls"]]})
            request.session["ask_history"] = history[-20:]
            svc.log(request.user, "agent.query",
                    f"Asked: {q[:200]} · tools: {', '.join(c['name'] for c in res['tool_calls']) or 'none'}")
        return redirect("ask")
    return render(request, "finance/ask.html", {"history": history, "examples": ASK_EXAMPLES,
                                                "agent_enabled": agent.enabled()})
