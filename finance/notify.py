"""Email notifications: Outlook (Microsoft Graph), SMTP, or simulated.

Every workflow event that needs someone's attention sends a short email with a
link back to the request. Approvers still approve inside the app (one tap after
the link opens), so the email itself never carries authority.

Providers (``EMAIL_PROVIDER``):

* ``simulated`` (default) – nothing leaves the server. Each message is stored in
  ``OutboundEmail`` and shown in the in-app *Inbox (simulated)*, so a demo shows
  exactly what each person would receive.
* ``outlook`` – sends through Microsoft Graph ``/users/{sender}/sendMail`` with an
  Entra ID (Azure AD) app registration using the client-credentials flow. Needs
  ``MS_GRAPH_TENANT_ID``, ``MS_GRAPH_CLIENT_ID``, ``MS_GRAPH_CLIENT_SECRET`` and
  ``MS_GRAPH_SENDER`` (the shared mailbox, e.g. finance@agency.lk). The app needs
  the *Mail.Send* application permission, ideally limited to that one mailbox with
  an Exchange application access policy.
* ``smtp`` – Django's normal SMTP backend (``EMAIL_HOST`` etc.), e.g. for Gmail.

Sending happens after the database transaction commits, and a failed send never
undoes an approval: the failure is recorded on the ``OutboundEmail`` row.
"""
from __future__ import annotations

import base64
import json
import logging
import time
import urllib.parse
import urllib.request
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone

from .models import EmailAttachment, OutboundEmail, PaymentRequest

logger = logging.getLogger(__name__)
GRAPH_ATTACHMENT_LIMIT = 3 * 1024 * 1024   # sendMail accepts ~4 MB per request including base64 overhead


# --------------------------------------------------------------------------- helpers

def provider() -> str:
    return (getattr(settings, "EMAIL_PROVIDER", "simulated") or "simulated").lower()


def absolute(path: str) -> str:
    return getattr(settings, "SITE_URL", "http://127.0.0.1:8000").rstrip("/") + path


def money(amount) -> str:
    return f"{settings.CURRENCY} {Decimal(amount):,.2f}"


def display(user) -> str:
    return user.get_full_name() or user.username


def wants_mail(user) -> bool:
    profile = getattr(user, "profile", None)
    return bool(user.is_active and user.email and (profile is None or profile.email_notifications))


def users_in_role(role):
    return list(get_user_model().objects.filter(groups__name=role, is_active=True).distinct())


# --------------------------------------------------------------------------- sending

def send(event, *, users=(), addresses=(), cc_users=(), subject, heading, intro, rows=(), button=None, note="",
         request=None, campaign=None, attachments=()):
    """Create the OutboundEmail row and send it once the current transaction commits.

    ``rows`` are (label, value) pairs shown as a small table; ``button`` is (label, url);
    ``attachments`` are (filename, bytes, content_type).
    """
    users = [u for u in dict.fromkeys(users) if u is not None]
    to = list(dict.fromkeys([u.email for u in users if wants_mail(u)] + [a for a in addresses if a]))
    cc = [u.email for u in cc_users if u is not None and wants_mail(u) and u.email not in to]
    context = {"heading": heading, "intro": intro, "rows": rows, "button": button, "note": note,
               "org": getattr(settings, "EMAIL_ORG_NAME", "Campaign Finance")}
    html = render_to_string("finance/email/notification.html", context)
    text = render_to_string("finance/email/notification.txt", context)

    email = OutboundEmail.objects.create(
        event=event, to=to, cc=cc, subject=subject, body_text=text, body_html=html, provider=provider(),
        status=OutboundEmail.Status.SKIPPED if not to else OutboundEmail.Status.SIMULATED,
        request=request, campaign=campaign or (request.campaign if request else None),
    )
    if users or cc_users:
        email.recipients.set(list(users) + [u for u in cc_users if u is not None])
    for filename, data, ctype in attachments:
        EmailAttachment.objects.create(email=email, file=ContentFile(data, name=filename), filename=filename,
                                       content_type=ctype, size=len(data))
    if to:
        transaction.on_commit(lambda: deliver(email.pk))
    return email


def deliver(email_pk: int) -> OutboundEmail:
    email = OutboundEmail.objects.get(pk=email_pk)
    mode = email.provider
    try:
        if mode == "simulated":
            email.status = OutboundEmail.Status.SIMULATED
        elif mode == "outlook":
            email.provider_message_id = GraphMailer().send(email)
            email.status = OutboundEmail.Status.SENT
        elif mode == "smtp":
            _send_smtp(email)
            email.status = OutboundEmail.Status.SENT
        else:
            raise ValueError(f"Unknown EMAIL_PROVIDER {mode!r}")
        email.sent_at = timezone.now()
        email.error = ""
    except Exception as exc:  # noqa: BLE001 – record any provider failure, never break the workflow
        logger.exception("Email %s failed", email_pk)
        email.status = OutboundEmail.Status.FAILED
        email.error = str(exc)[:2000]
    email.save(update_fields=["status", "sent_at", "error", "provider_message_id"])
    return email


def _send_smtp(email):
    msg = EmailMultiAlternatives(email.subject, email.body_text, settings.DEFAULT_FROM_EMAIL, email.to, cc=email.cc)
    msg.attach_alternative(email.body_html, "text/html")
    for a in email.attachments.all():
        msg.attach(a.filename, a.file.read(), a.content_type)
    msg.send()


class GraphMailer:
    """Minimal Microsoft Graph client: client-credentials token + sendMail. No SDK needed."""

    _token = None
    _token_expiry = 0.0
    timeout = 20

    def __init__(self, opener=None):
        self.opener = opener or urllib.request.urlopen
        for name in ("MS_GRAPH_TENANT_ID", "MS_GRAPH_CLIENT_ID", "MS_GRAPH_CLIENT_SECRET", "MS_GRAPH_SENDER"):
            if not getattr(settings, name, ""):
                raise RuntimeError(f"{name} is not set; Outlook sending needs an Entra ID app registration")

    def _post(self, url, data, headers):
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with self.opener(req, timeout=self.timeout) as resp:
            return resp.status, resp.read(), dict(resp.headers)

    def token(self):
        if GraphMailer._token and time.time() < GraphMailer._token_expiry - 60:
            return GraphMailer._token
        body = urllib.parse.urlencode({
            "client_id": settings.MS_GRAPH_CLIENT_ID,
            "client_secret": settings.MS_GRAPH_CLIENT_SECRET,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        }).encode()
        url = f"https://login.microsoftonline.com/{settings.MS_GRAPH_TENANT_ID}/oauth2/v2.0/token"
        status, raw, _ = self._post(url, body, {"Content-Type": "application/x-www-form-urlencoded"})
        payload = json.loads(raw or b"{}")
        if status != 200 or "access_token" not in payload:
            raise RuntimeError(f"Graph token request failed ({status}): {payload.get('error_description', raw[:300])}")
        GraphMailer._token = payload["access_token"]
        GraphMailer._token_expiry = time.time() + int(payload.get("expires_in", 3599))
        return GraphMailer._token

    def message_json(self, email):
        message = {
            "subject": email.subject,
            "body": {"contentType": "HTML", "content": email.body_html or email.body_text},
            "toRecipients": [{"emailAddress": {"address": a}} for a in email.to],
            "ccRecipients": [{"emailAddress": {"address": a}} for a in email.cc],
        }
        files = list(email.attachments.all())
        if files and sum(a.size for a in files) <= GRAPH_ATTACHMENT_LIMIT:
            message["attachments"] = [{
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": a.filename, "contentType": a.content_type,
                "contentBytes": base64.b64encode(a.file.read()).decode(),
            } for a in files]
        elif files:
            # Too large for one sendMail call: link to the files in the app instead of attaching them.
            links = "".join(f'<li><a href="{absolute(f"/outbox/attachment/{a.pk}/")}">{a.filename}</a></li>' for a in files)
            message["body"]["content"] += f"<p>Attachments (too large to email, sign in to download):</p><ul>{links}</ul>"
        return {"message": message, "saveToSentItems": True}

    def send(self, email):
        sender = urllib.parse.quote(settings.MS_GRAPH_SENDER)
        status, raw, headers = self._post(
            f"https://graph.microsoft.com/v1.0/users/{sender}/sendMail",
            json.dumps(self.message_json(email)).encode(),
            {"Authorization": f"Bearer {self.token()}", "Content-Type": "application/json"},
        )
        if status != 202:
            raise RuntimeError(f"Graph sendMail failed ({status}): {raw[:500]!r}")
        return headers.get("request-id", "")


# --------------------------------------------------------------------------- workflow events

def _request_rows(req, ctx=None):
    rows = [
        ("Campaign", f"{req.campaign.code} · {req.campaign.name}"),
        ("Category", req.budget_line.category),
        ("Type", req.get_kind_display() + ("" if req.instalment == "single" else f" ({req.get_instalment_display()})")),
        ("Payee", req.payee),
        ("Amount", money(req.amount)),
        ("Purpose", req.purpose),
        ("Requested by", display(req.requested_by)),
    ]
    if req.needed_by:
        rows.append(("Needed by", req.needed_by.strftime("%d %b %Y")))
    if ctx:
        rows.append(("Available before → after", f"{money(ctx['available_before'])} → {money(ctx['available_after'])}"))
    return rows


def _approvers_for(req):
    from . import services as svc
    if req.status == PaymentRequest.Status.ENDORSE:
        sup = svc.supervisor_of(req.requested_by)
        return [sup] if sup else []
    return [u for u in users_in_role(svc.ROLE_MD) if u.pk != req.requested_by_id]


def request_needs_approval(req, endorsed_by=None):
    from . import services as svc
    ctx = svc.approval_context(req)
    stage = "endorsement" if req.status == PaymentRequest.Status.ENDORSE else "approval"
    over = " · OVER BUDGET" if ctx["over_budget"] else ""
    intro = (f"{display(endorsed_by)} has endorsed this request; it now needs MD approval." if endorsed_by else
             f"{display(req.requested_by)} has submitted a request that needs your {stage}.")
    note = "This request exceeds the available budget for the category." if ctx["over_budget"] else ""
    warnings = [m for lvl, m in svc.ai_checks(req) if lvl == "warn"]
    if warnings:
        note = (note + " " if note else "") + f"Document checks raised {len(warnings)} warning(s); see the request."
    send("request.needs_" + stage, users=_approvers_for(req),
         subject=f"[{'Endorse' if stage == 'endorsement' else 'Approve'}] {req.reference} · {money(req.amount)} · {req.campaign.code}{over}",
         heading=f"{req.reference} needs your {stage}", intro=intro, rows=_request_rows(req, ctx),
         button=("Review request", absolute(req.get_absolute_url())), note=note, request=req)


def request_decided(req, decision, actor, comment="", stage="md"):
    from . import services as svc
    labels = {"approve": "approved", "reject": "rejected", "clarify": "returned for clarification",
              "endorse": "endorsed"}
    who = "Your supervisor" if stage == "supervisor" else "The MD"
    rows = _request_rows(req) + ([("Comment", comment)] if comment else [])
    send(f"request.{decision}", users=[req.requested_by],
         subject=f"{req.reference} {labels[decision]} · {money(req.amount)}",
         heading=f"{req.reference} was {labels[decision]}",
         intro=f"{who} ({display(actor)}) {labels[decision]} your request.",
         rows=rows, button=("Open request", absolute(req.get_absolute_url())),
         note="Please answer the question in the app; the request returns to the same approver." if decision == "clarify" else "",
         request=req)
    if decision == "approve":
        send("request.ready_to_pay", users=users_in_role(svc.ROLE_FINANCE),
             subject=f"[To pay] {req.reference} · {money(req.amount)} · {req.payee}",
             heading=f"{req.reference} is approved and ready to pay", intro="The MD has approved this request.",
             rows=_request_rows(req), button=("Record payment", absolute(req.get_absolute_url())), request=req)


def clarification_answered(req, asked_by):
    send("request.clarified", users=[asked_by] if asked_by else _approvers_for(req),
         subject=f"{req.reference} – clarification answered", heading=f"{display(req.requested_by)} answered your question",
         intro="The request is back in your queue.", rows=_request_rows(req),
         button=("Review request", absolute(req.get_absolute_url())), request=req)


def payment_recorded(req):
    p = req.payment
    note = ("This is an advance: upload your receipts and submit the settlement in the app within 7 days of the event."
            if req.kind == PaymentRequest.Kind.ADVANCE else "")
    send("payment.recorded", users=[req.requested_by], subject=f"{req.reference} paid · {money(p.amount)}",
         heading=f"{req.reference} has been paid",
         intro=f"Finance paid {money(p.amount)} by {p.get_method_display().lower()}, reference {p.reference}.",
         rows=_request_rows(req), button=("Open request", absolute(req.get_absolute_url())), note=note, request=req)


def settlement_submitted(req):
    from . import services as svc
    s = req.settlement
    send("settlement.submitted", users=users_in_role(svc.ROLE_FINANCE),
         subject=f"[Verify settlement] {req.reference} · spent {money(s.spent_amount)}",
         heading=f"Settlement to verify for {req.reference}",
         intro=f"{display(req.requested_by)} submitted receipts for an advance of {money(req.paid_amount)}.",
         rows=_request_rows(req) + [("Spent", money(s.spent_amount)), ("Cash returned", money(s.cash_returned))],
         button=("Verify settlement", absolute(req.get_absolute_url())), request=req)


def settlement_checked(req, approved, comment=""):
    word = "verified" if approved else "returned for correction"
    send(f"settlement.{'verified' if approved else 'returned'}", users=[req.requested_by],
         subject=f"{req.reference} settlement {word}", heading=f"Your settlement was {word}",
         intro="Finance has checked the receipts for your advance.",
         rows=_request_rows(req) + ([("Comment", comment)] if comment else []),
         button=("Open request", absolute(req.get_absolute_url())), request=req)


def revision_proposed(rev):
    from . import services as svc
    line = rev.budget_line
    send("budget.revision", users=users_in_role(svc.ROLE_MD),
         subject=f"[Approve] Budget revision · {line.campaign.code} {line.category}",
         heading="A budget revision needs your approval",
         intro=f"{display(rev.proposed_by)} proposed a change to an approved budget line.",
         rows=[("Campaign", f"{line.campaign.code} · {line.campaign.name}"), ("Category", line.category),
               ("Current", money(rev.old_amount)), ("Proposed", money(rev.new_amount)), ("Reason", rev.reason)],
         button=("Review", absolute("/approvals/")), campaign=line.campaign)


def campaign_report(campaign, actor, to_address, pdf_bytes, message=""):
    filename = f"{campaign.code}-closure-report.pdf"
    return send("report.sent", addresses=[to_address], cc_users=[actor],
                subject=f"{campaign.code} {campaign.name} – campaign closure report",
                heading=f"Closure report: {campaign.name}",
                intro=message or (f"Please find attached the closure report for {campaign.name}"
                                  f"{' (PO ' + campaign.client_po_number + ')' if campaign.client_po_number else ''}: "
                                  "budget against actual spend, the expense schedule and copies of the supporting bills."),
                rows=[("Campaign", f"{campaign.code} · {campaign.name}"), ("Client", campaign.client or "—"),
                      ("Sent by", display(actor))],
                campaign=campaign, attachments=[(filename, pdf_bytes, "application/pdf")])
