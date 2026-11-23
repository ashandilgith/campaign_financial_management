from django import forms
from django.utils import timezone

from .models import Attachment, BudgetLine, Campaign, Payment, PaymentRequest, Supplier, SupplierContract

DEFAULT_CATEGORIES = [
    "Venue & permits", "Production & fabrication", "Printing & branding", "Promoters & staff",
    "Transport & logistics", "Food & refreshments", "Media & digital", "Miscellaneous",
]


class DateInput(forms.DateInput):
    input_type = "date"


class CampaignForm(forms.ModelForm):
    budget_lines = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 8}),
        help_text="One category per line as: Category, amount  (e.g. 'Venue & permits, 450000')",
        initial="\n".join(f"{c}, 0" for c in DEFAULT_CATEGORIES),
    )

    class Meta:
        model = Campaign
        fields = ["code", "name", "client", "start_date", "end_date", "status", "notes"]
        widgets = {"start_date": DateInput(), "end_date": DateInput(), "notes": forms.Textarea(attrs={"rows": 2})}

    def clean_budget_lines(self):
        from decimal import Decimal, InvalidOperation

        rows, seen = [], set()
        for n, raw in enumerate(self.cleaned_data["budget_lines"].splitlines(), 1):
            raw = raw.strip()
            if not raw:
                continue
            if "," not in raw:
                raise forms.ValidationError(f"Line {n}: expected 'Category, amount'.")
            cat, amt = raw.rsplit(",", 1)
            cat = cat.strip()
            try:
                amount = Decimal(amt.strip().replace(" ", "").replace("_", ""))
            except InvalidOperation:
                raise forms.ValidationError(f"Line {n}: '{amt.strip()}' is not a number.")
            if amount < 0:
                raise forms.ValidationError(f"Line {n}: amount cannot be negative.")
            if cat.lower() in seen:
                raise forms.ValidationError(f"Line {n}: duplicate category '{cat}'.")
            seen.add(cat.lower())
            rows.append((cat, amount))
        if not rows:
            raise forms.ValidationError("Add at least one budget category.")
        return rows


class BudgetLineForm(forms.Form):
    category = forms.CharField(max_length=80)
    amount = forms.DecimalField(max_digits=14, decimal_places=2, min_value=0)


class RevisionForm(forms.Form):
    new_amount = forms.DecimalField(max_digits=14, decimal_places=2, min_value=0)
    reason = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}))


class PaymentRequestForm(forms.Form):
    kind = forms.ChoiceField(choices=PaymentRequest.Kind.choices, widget=forms.RadioSelect,
                             initial=PaymentRequest.Kind.ADVANCE, label="Request type")
    budget_line = forms.ModelChoiceField(queryset=BudgetLine.objects.none(), label="Budget category")
    purpose = forms.CharField(max_length=255, label="What is it for?")
    amount = forms.DecimalField(max_digits=14, decimal_places=2, min_value=0.01, label="Amount (LKR)")
    supplier = forms.ModelChoiceField(queryset=Supplier.objects.all(), required=False)
    new_supplier = forms.CharField(max_length=200, required=False, label="…or new supplier name")
    contract = forms.ModelChoiceField(queryset=SupplierContract.objects.none(), required=False,
                                      label="Existing instalment contract")
    instalment = forms.ChoiceField(choices=PaymentRequest.Instalment.choices,
                                   initial=PaymentRequest.Instalment.SINGLE)
    new_contract_total = forms.DecimalField(
        max_digits=14, decimal_places=2, min_value=0.01, required=False,
        label="Full quoted value (if paying in instalments)",
        help_text="Enter the supplier's total quote when this is the first of several instalments.",
    )
    needed_by = forms.DateField(required=False, widget=DateInput())
    details = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), required=False)
    quote = forms.FileField(required=False, label="Quotation / supporting document")

    def __init__(self, *args, campaign, **kwargs):
        super().__init__(*args, **kwargs)
        self.campaign = campaign
        self.fields["budget_line"].queryset = campaign.budget_lines.all()
        self.fields["contract"].queryset = campaign.contracts.filter(cancelled=False).select_related("supplier")

    def clean(self):
        data = super().clean()
        if data.get("kind") == PaymentRequest.Kind.SUPPLIER:
            if data.get("contract"):
                data["supplier"] = data["contract"].supplier
            elif not data.get("supplier") and not data.get("new_supplier"):
                self.add_error("supplier", "Choose a supplier or type a new supplier name.")
            if data.get("new_contract_total") and data.get("amount") and data["amount"] > data["new_contract_total"]:
                self.add_error("amount", "Instalment cannot exceed the full quoted value.")
        return data


class DecisionForm(forms.Form):
    comment = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}), required=False)


class ClarifyResponseForm(forms.Form):
    response = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}))
    new_amount = forms.DecimalField(max_digits=14, decimal_places=2, min_value=0.01, required=False,
                                    label="Revised amount (optional)")


class PaymentForm(forms.Form):
    amount = forms.DecimalField(max_digits=14, decimal_places=2, min_value=0.01)
    paid_on = forms.DateField(widget=DateInput(), initial=timezone.localdate)
    method = forms.ChoiceField(choices=Payment.Method.choices)
    reference = forms.CharField(max_length=120, label="Transaction reference")
    proof = forms.FileField(required=False, label="Payment proof (optional)")


class SettlementForm(forms.Form):
    spent_amount = forms.DecimalField(max_digits=14, decimal_places=2, min_value=0, label="Total spent (LKR)")
    cash_returned = forms.DecimalField(max_digits=14, decimal_places=2, min_value=0, initial=0)
    notes = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}), required=False)


class AttachmentForm(forms.ModelForm):
    class Meta:
        model = Attachment
        fields = ["kind", "file", "description"]
