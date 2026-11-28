"""Data model for campaign budgets, payment requests, approvals, payments and settlements.

Three money concepts are kept deliberately separate (concept note, "Design principles"):

* **Budget**     – what the MD approved per campaign category (BudgetLine.approved_amount).
* **Cash out**   – what finance actually paid or advanced (Payment), net of cash returned.
* **Verified expenditure** – spend backed by a verified bill or a verified advance settlement.

All money math lives in ``finance.services`` so that it can be tested in isolation.
"""
from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.urls import reverse

MONEY = dict(max_digits=14, decimal_places=2)
POSITIVE = [MinValueValidator(Decimal("0.01"))]
NON_NEGATIVE = [MinValueValidator(Decimal("0"))]


class Campaign(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        ACTIVE = "active", "Active"
        CLOSING = "closing", "Closing"
        CLOSED = "closed", "Closed"

    code = models.CharField(max_length=30, unique=True)
    name = models.CharField(max_length=200)
    client = models.CharField(max_length=200, blank=True)
    client_po_number = models.CharField("Client PO number", max_length=60, blank=True,
                                        help_text="The client-approved PO that sets this campaign's budget")
    client_email = models.EmailField("Client contact email", blank=True,
                                     help_text="Where the closure report is sent")
    commission_pct = models.DecimalField("Agency commission %", max_digits=5, decimal_places=2,
                                         default=Decimal("0"), validators=NON_NEGATIVE,
                                         help_text="Added to verified spend on the closure invoice summary")
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.code} – {self.name}"

    def get_absolute_url(self):
        return reverse("campaign_detail", args=[self.pk])

    @property
    def accepts_requests(self):
        return self.status in (self.Status.DRAFT, self.Status.ACTIVE, self.Status.CLOSING)

    @property
    def total_budget(self):
        return sum((line.approved_amount for line in self.budget_lines.all()), Decimal("0"))


class BudgetLine(models.Model):
    """One approved budget category of a campaign (e.g. Venue, Printing, Promoters)."""

    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="budget_lines")
    category = models.CharField(max_length=80)
    approved_amount = models.DecimalField(**MONEY, validators=NON_NEGATIVE)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "id"]
        unique_together = [("campaign", "category")]

    def __str__(self):
        return f"{self.campaign.code} / {self.category}"


class BudgetRevision(models.Model):
    """Controlled change to a budget line. Proposals by finance/admin need MD approval."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending MD"
        APPLIED = "applied", "Applied"
        REJECTED = "rejected", "Rejected"

    budget_line = models.ForeignKey(BudgetLine, on_delete=models.CASCADE, related_name="revisions")
    old_amount = models.DecimalField(**MONEY)
    new_amount = models.DecimalField(**MONEY, validators=NON_NEGATIVE)
    reason = models.TextField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    proposed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def delta(self):
        return self.new_amount - self.old_amount


class Supplier(models.Model):
    name = models.CharField(max_length=200, unique=True)
    contact_person = models.CharField(max_length=120, blank=True)
    phone = models.CharField(max_length=40, blank=True)
    bank_details = models.TextField(blank=True, help_text="Account name / bank / branch for transfers")

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class SupplierContract(models.Model):
    """A quoted supplier job paid in instalments (first / second / final).

    The full quoted value becomes *committed* against the budget line once the MD
    approves any instalment. Later instalments draw down that commitment instead of
    being counted again, which is what prevents double counting.
    """

    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="contracts")
    budget_line = models.ForeignKey(BudgetLine, on_delete=models.PROTECT, related_name="contracts")
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="contracts")
    description = models.CharField(max_length=255)
    total_amount = models.DecimalField(**MONEY, validators=POSITIVE)
    cancelled = models.BooleanField(default=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.supplier} – {self.description}"


class PaymentRequest(models.Model):
    class Kind(models.TextChoices):
        ADVANCE = "advance", "Staff advance"
        SUPPLIER = "supplier", "Supplier payment"

    class Instalment(models.TextChoices):
        SINGLE = "single", "Single payment"
        FIRST = "first", "First instalment"
        SECOND = "second", "Second instalment"
        FINAL = "final", "Final instalment"

    class Status(models.TextChoices):
        ENDORSE = "endorse", "Awaiting supervisor"
        SUBMITTED = "submitted", "Awaiting MD"
        CLARIFY = "clarify", "Clarification requested"
        APPROVED = "approved", "Approved – to pay"
        REJECTED = "rejected", "Rejected"
        PAID = "paid", "Paid"
        SETTLED = "settled", "Settled"
        CANCELLED = "cancelled", "Cancelled"

    OPEN_STATUSES = (Status.ENDORSE, Status.SUBMITTED, Status.CLARIFY)

    campaign = models.ForeignKey(Campaign, on_delete=models.PROTECT, related_name="requests")
    budget_line = models.ForeignKey(BudgetLine, on_delete=models.PROTECT, related_name="requests")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, null=True, blank=True, related_name="requests")
    contract = models.ForeignKey(
        SupplierContract, on_delete=models.PROTECT, null=True, blank=True, related_name="instalments"
    )
    instalment = models.CharField(max_length=10, choices=Instalment.choices, default=Instalment.SINGLE)
    amount = models.DecimalField(**MONEY, validators=POSITIVE)
    purpose = models.CharField(max_length=255)
    details = models.TextField(blank=True)
    needed_by = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.SUBMITTED)
    bill_verified = models.BooleanField(default=False, help_text="Finance has checked the supplier bill/invoice")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="payment_requests")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.reference} {self.purpose}"

    @property
    def reference(self):
        prefix = "ADV" if self.kind == self.Kind.ADVANCE else "SUP"
        return f"{prefix}-{self.pk:05d}" if self.pk else prefix

    def get_absolute_url(self):
        return reverse("request_detail", args=[self.pk])

    @property
    def payee(self):
        if self.kind == self.Kind.ADVANCE:
            return self.requested_by.get_full_name() or self.requested_by.username
        return str(self.supplier) if self.supplier else "—"

    @property
    def paid_amount(self):
        payment = getattr(self, "payment", None)
        return payment.amount if payment else Decimal("0")


class ApprovalDecision(models.Model):
    """A supervisor or MD decision. ``snapshot`` stores the budget figures the approver saw."""

    class Stage(models.TextChoices):
        SUPERVISOR = "supervisor", "Supervisor"
        MD = "md", "MD"

    class Decision(models.TextChoices):
        ENDORSE = "endorse", "Endorsed"
        APPROVE = "approve", "Approved"
        REJECT = "reject", "Rejected"
        CLARIFY = "clarify", "Clarification requested"

    request = models.ForeignKey(PaymentRequest, on_delete=models.CASCADE, related_name="decisions")
    stage = models.CharField(max_length=10, choices=Stage.choices, default=Stage.MD)
    decision = models.CharField(max_length=10, choices=Decision.choices)
    comment = models.TextField(blank=True)
    snapshot = models.JSONField(default=dict, blank=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    decided_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["decided_at"]


class Payment(models.Model):
    """Actual cash out recorded by finance after verifying MD approval."""

    class Method(models.TextChoices):
        TRANSFER = "transfer", "Bank transfer"
        CHEQUE = "cheque", "Cheque"
        CASH = "cash", "Cash"
        OTHER = "other", "Other"

    request = models.OneToOneField(PaymentRequest, on_delete=models.PROTECT, related_name="payment")
    amount = models.DecimalField(**MONEY, validators=POSITIVE)
    paid_on = models.DateField()
    method = models.CharField(max_length=10, choices=Method.choices, default=Method.TRANSFER)
    reference = models.CharField(max_length=120, help_text="Bank / cheque / voucher reference")
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    recorded_at = models.DateTimeField(auto_now_add=True)


class Settlement(models.Model):
    """Settlement of a staff advance: what was spent (with receipts) and any cash returned."""

    class Status(models.TextChoices):
        SUBMITTED = "submitted", "Submitted – awaiting finance"
        VERIFIED = "verified", "Verified"
        RETURNED = "returned", "Returned for correction"

    request = models.OneToOneField(PaymentRequest, on_delete=models.PROTECT, related_name="settlement")
    spent_amount = models.DecimalField(**MONEY, validators=NON_NEGATIVE)
    cash_returned = models.DecimalField(**MONEY, default=Decimal("0"), validators=NON_NEGATIVE)
    notes = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.SUBMITTED)
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    submitted_at = models.DateTimeField(auto_now_add=True)
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    verified_at = models.DateTimeField(null=True, blank=True)

    @property
    def balance(self):
        """Positive: staff still holds cash. Negative: company owes staff a reimbursement."""
        return self.request.paid_amount - self.spent_amount - self.cash_returned


def attachment_path(instance, filename):
    return f"campaigns/{instance.request.campaign.code}/{instance.request.reference}/{filename}"


class Attachment(models.Model):
    class Kind(models.TextChoices):
        QUOTE = "quote", "Quotation / estimate"
        BILL = "bill", "Supplier bill / invoice"
        RECEIPT = "receipt", "Receipt"
        PAYMENT_PROOF = "payment_proof", "Payment proof"
        OTHER = "other", "Other"

    request = models.ForeignKey(PaymentRequest, on_delete=models.CASCADE, related_name="attachments")
    kind = models.CharField(max_length=15, choices=Kind.choices)
    file = models.FileField(upload_to=attachment_path)
    description = models.CharField(max_length=200, blank=True)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    uploaded_at = models.DateTimeField(auto_now_add=True)

    # AI document reading (see finance/ai.py). Advisory only – never changes money figures.
    AI_KINDS = ("quote", "bill", "receipt")
    ai_status = models.CharField(max_length=10, blank=True, choices=[("done", "Read"), ("error", "Failed")])
    ai_data = models.JSONField(default=dict, blank=True)
    ai_error = models.TextField(blank=True)
    ai_model = models.CharField(max_length=60, blank=True)
    ai_read_at = models.DateTimeField(null=True, blank=True)

    @property
    def ai_total(self):
        from decimal import Decimal, InvalidOperation

        v = self.ai_data.get("total_amount") if self.ai_status == "done" else None
        try:
            return Decimal(str(v)) if v is not None else None
        except InvalidOperation:
            return None

    @property
    def filename(self):
        return self.file.name.rsplit("/", 1)[-1]


class AuditLog(models.Model):
    """Append-only trail of every state change. Never edited or deleted by the app."""

    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    action = models.CharField(max_length=40)
    message = models.TextField()
    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, null=True, blank=True, related_name="audit")
    request = models.ForeignKey(PaymentRequest, on_delete=models.CASCADE, null=True, blank=True, related_name="audit")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]


class StoredFile(models.Model):
    """Bytes of an uploaded file when FILE_STORAGE=database (see finance/storage.py)."""

    name = models.CharField(max_length=255, unique=True)
    data = models.BinaryField()
    size = models.PositiveIntegerField()
    content_type = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class StaffProfile(models.Model):
    """Per-user settings: the supervisor who endorses this person's requests before the MD."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    supervisor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="team",
        help_text="Cost-centre head / supervisor who endorses requests before they reach the MD. Blank = straight to MD.",
    )
    email_notifications = models.BooleanField(default=True)

    def __str__(self):
        return f"Profile of {self.user}"


def email_attachment_path(instance, filename):
    return f"outbox/{instance.email_id}/{filename}"


class OutboundEmail(models.Model):
    """Every notification and report the system sends (or, in simulated mode, would send).

    In ``EMAIL_PROVIDER=simulated`` mode nothing leaves the server and this table is the
    demo "inbox". With ``outlook`` the message is sent through Microsoft Graph and the
    row records the outcome, so there is always an audit trail of what was emailed.
    """

    class Status(models.TextChoices):
        SIMULATED = "simulated", "Simulated (not sent)"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped (no address)"

    event = models.CharField(max_length=40)
    to = models.JSONField(default=list)
    cc = models.JSONField(default=list, blank=True)
    recipients = models.ManyToManyField(settings.AUTH_USER_MODEL, blank=True, related_name="emails_received")
    subject = models.CharField(max_length=255)
    body_text = models.TextField()
    body_html = models.TextField(blank=True)
    provider = models.CharField(max_length=20)
    status = models.CharField(max_length=10, choices=Status.choices)
    error = models.TextField(blank=True)
    provider_message_id = models.CharField(max_length=255, blank=True)
    request = models.ForeignKey(PaymentRequest, on_delete=models.SET_NULL, null=True, blank=True, related_name="emails")
    campaign = models.ForeignKey(Campaign, on_delete=models.SET_NULL, null=True, blank=True, related_name="emails")
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]

    def __str__(self):
        return f"{self.subject} → {', '.join(self.to)}"


class EmailAttachment(models.Model):
    email = models.ForeignKey(OutboundEmail, on_delete=models.CASCADE, related_name="attachments")
    file = models.FileField(upload_to=email_attachment_path)
    filename = models.CharField(max_length=200)
    content_type = models.CharField(max_length=100, default="application/pdf")
    size = models.PositiveIntegerField(default=0)
