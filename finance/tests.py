from datetime import date
from decimal import Decimal as D

from django.contrib.auth.models import Group, User
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.test import TestCase

from . import services as svc
from .models import Attachment, AuditLog, Campaign, PaymentRequest, Supplier

S = PaymentRequest.Status


def make_user(name, role):
    u = User.objects.create_user(name, password="pw-12345678")
    u.groups.add(Group.objects.get_or_create(name=role)[0])
    return u


class Base(TestCase):
    def setUp(self):
        self.md = make_user("md", svc.ROLE_MD)
        self.fin = make_user("fin", svc.ROLE_FINANCE)
        self.staff = make_user("staff", svc.ROLE_STAFF)
        self.c = svc.create_campaign(self.md, code="T1", name="Test", lines=[("Venue", D("100000")), ("Print", D("50000"))])
        self.venue = self.c.budget_lines.get(category="Venue")
        self.print = self.c.budget_lines.get(category="Print")
        self.sup = Supplier.objects.create(name="Acme")

    def req(self, amount, line=None, kind="supplier", **kw):
        return svc.submit_request(
            kw.pop("user", self.staff), campaign=self.c, budget_line=line or self.venue, kind=kind,
            amount=D(amount), purpose="x", supplier=None if kind == "advance" else kw.pop("supplier", self.sup), **kw)

    def pay(self, r, amount=None):
        svc.record_payment(self.fin, r, amount=D(amount) if amount else r.amount, paid_on=date.today(),
                           method="transfer", reference="REF1")

    def attach(self, r, kind):
        Attachment.objects.create(request=r, kind=kind, uploaded_by=self.staff, file=ContentFile(b"x", name="f.txt"))


class BudgetMathTests(Base):
    def test_pending_committed_paid_flow(self):
        r = self.req("30000")
        f = svc.line_figures(self.venue)
        self.assertEqual((f.pending, f.committed, f.cash_out, f.available), (D("30000"), 0, 0, D("70000")))
        svc.decide_request(self.md, r, "approve")
        f = svc.line_figures(self.venue)
        self.assertEqual((f.pending, f.committed, f.available), (0, D("30000"), D("70000")))
        self.pay(r)
        f = svc.line_figures(self.venue)
        self.assertEqual((f.committed, f.cash_out, f.verified, f.available), (0, D("30000"), 0, D("70000")))
        self.attach(r, "bill")
        svc.verify_bill(self.fin, r)
        self.assertEqual(svc.line_figures(self.venue).verified, D("30000"))

    def test_rejected_and_cancelled_release_budget(self):
        r1, r2 = self.req("20000"), self.req("10000")
        svc.decide_request(self.md, r1, "reject", "no")
        svc.cancel_request(self.staff, r2)
        self.assertEqual(svc.line_figures(self.venue).available, D("100000"))

    def test_partial_payment_releases_balance(self):
        r = self.req("30000")
        svc.decide_request(self.md, r, "approve")
        self.pay(r, "25000")
        f = svc.line_figures(self.venue)
        self.assertEqual((f.cash_out, f.committed, f.available), (D("25000"), 0, D("75000")))

    def test_instalments_are_not_double_counted(self):
        first = self.req("40000", new_contract_total=D("90000"), instalment="first")
        # before approval the whole quote is pending exposure
        self.assertEqual(svc.line_figures(self.venue).pending, D("90000"))
        svc.decide_request(self.md, first, "approve")
        f = svc.line_figures(self.venue)
        self.assertEqual((f.committed, f.pending, f.available), (D("90000"), 0, D("10000")))
        self.pay(first)
        second = self.req("30000", contract=first.contract, instalment="second")
        f = svc.line_figures(self.venue)
        self.assertEqual((f.cash_out, f.committed, f.pending, f.available), (D("40000"), D("50000"), 0, D("10000")))
        svc.decide_request(self.md, second, "approve")
        self.pay(second)
        final = self.req("20000", contract=first.contract, instalment="final")
        svc.decide_request(self.md, final, "approve")
        self.pay(final)
        f = svc.line_figures(self.venue)
        self.assertEqual((f.cash_out, f.committed, f.available), (D("90000"), 0, D("10000")))

    def test_instalment_cannot_exceed_contract(self):
        first = self.req("40000", new_contract_total=D("90000"), instalment="first")
        self.req("40000", contract=first.contract, instalment="second")
        with self.assertRaises(ValidationError):
            self.req("20000", contract=first.contract, instalment="final")

    def test_advance_settlement_with_return(self):
        r = self.req("50000", kind="advance")
        svc.decide_request(self.md, r, "approve")
        self.pay(r)
        f = svc.line_figures(self.venue)
        self.assertEqual((f.cash_out, f.advances_outstanding, f.verified), (D("50000"), D("50000"), 0))
        with self.assertRaises(ValidationError):  # receipts required
            svc.submit_settlement(self.staff, r, spent_amount=D("45000"), cash_returned=D("5000"))
        self.attach(r, "receipt")
        svc.submit_settlement(self.staff, r, spent_amount=D("45000"), cash_returned=D("5000"))
        self.assertEqual(svc.line_figures(self.venue).advances_outstanding, D("50000"))  # until verified
        svc.verify_settlement(self.fin, r)
        r.refresh_from_db()
        f = svc.line_figures(self.venue)
        self.assertEqual(r.status, S.SETTLED)
        self.assertEqual((f.cash_out, f.verified, f.advances_outstanding, f.available),
                         (D("45000"), D("45000"), 0, D("55000")))

    def test_advance_overspend_shows_reimbursement(self):
        r = self.req("10000", kind="advance")
        svc.decide_request(self.md, r, "approve")
        self.pay(r)
        self.attach(r, "receipt")
        svc.submit_settlement(self.staff, r, spent_amount=D("12000"), cash_returned=D("0"))
        svc.verify_settlement(self.fin, r)
        self.assertEqual(svc.line_figures(self.venue).reimbursements_due, D("2000"))

    def test_campaign_totals(self):
        self.req("10000")
        self.req("5000", line=self.print)
        _, total = svc.campaign_position(self.c)
        self.assertEqual((total.budget, total.pending, total.available), (D("150000"), D("15000"), D("135000")))


class ControlTests(Base):
    def test_only_md_approves_and_not_own(self):
        r = self.req("1000")
        with self.assertRaises(PermissionDenied):
            svc.decide_request(self.fin, r, "approve")
        own = self.req("1000", user=self.md)
        with self.assertRaises(PermissionDenied):
            svc.decide_request(self.md, own, "approve")

    def test_over_budget_approval_needs_comment(self):
        r = self.req("120000")
        ctx = svc.approval_context(r)
        self.assertTrue(ctx["over_budget"])
        self.assertEqual(ctx["available_before"], D("100000"))
        with self.assertRaises(ValidationError):
            svc.decide_request(self.md, r, "approve")
        svc.decide_request(self.md, r, "approve", "Client covering extra")
        self.assertEqual(r.decisions.get().snapshot["available_before"], "100000.00")

    def test_finance_only_pays_approved(self):
        r = self.req("1000")
        with self.assertRaises(ValidationError):
            self.pay(r)
        svc.decide_request(self.md, r, "approve")
        with self.assertRaises(PermissionDenied):
            svc.record_payment(self.md, r, amount=D("1000"), paid_on=date.today(), method="cash", reference="x")
        with self.assertRaises(ValidationError):
            self.pay(r, "2000")

    def test_clarify_cycle(self):
        r = self.req("1000")
        with self.assertRaises(ValidationError):
            svc.decide_request(self.md, r, "clarify")  # comment required
        svc.decide_request(self.md, r, "clarify", "Why?")
        svc.respond_to_clarification(self.staff, r, "Because", D("800"))
        r.refresh_from_db()
        self.assertEqual((r.status, r.amount), (S.SUBMITTED, D("800")))

    def test_revision_controls(self):
        rev = svc.propose_revision(self.fin, self.venue, D("120000"), "client PO")
        self.venue.refresh_from_db()
        self.assertEqual((rev.status, self.venue.approved_amount), ("pending", D("100000")))
        svc.decide_revision(self.md, rev, True)
        self.venue.refresh_from_db()
        self.assertEqual(self.venue.approved_amount, D("120000"))
        r = self.req("50000")
        svc.decide_request(self.md, r, "approve")
        with self.assertRaises(ValidationError):  # can't cut below committed
            svc.propose_revision(self.md, self.venue, D("40000"), "cut")
        with self.assertRaises(ValidationError):
            svc.propose_revision(self.md, self.venue, D("90000"), "  ")

    def test_closure_blocked_until_clean(self):
        r = self.req("1000")
        with self.assertRaises(ValidationError):
            svc.set_campaign_status(self.fin, self.c, Campaign.Status.CLOSED)
        svc.decide_request(self.md, r, "approve")
        self.pay(r)
        _, ready, _ = svc.closure_checklist(self.c)
        self.assertFalse(ready)  # bill not verified
        self.attach(r, "bill")
        svc.verify_bill(self.fin, r)
        svc.set_campaign_status(self.fin, self.c, Campaign.Status.CLOSED)
        with self.assertRaises(ValidationError):
            self.req("10")

    def test_every_transition_is_audited(self):
        r = self.req("1000")
        svc.decide_request(self.md, r, "approve")
        self.pay(r)
        actions = list(AuditLog.objects.filter(request=r).values_list("action", flat=True))
        self.assertEqual(sorted(actions), ["payment.record", "request.approve", "request.submit"])


class ViewSmokeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def test_pages_render_for_each_role(self):
        c = Campaign.objects.get(code="CMP-2611")
        r = c.requests.first()
        for username in ("md", "finance", "admin"):
            self.client.login(username=username, password="demo1234")
            for url in ["/", "/campaigns/", "/requests/", f"/campaigns/{c.pk}/", f"/campaigns/{c.pk}/report/",
                        f"/campaigns/{c.pk}/payments.csv", f"/campaigns/{c.pk}/request/", f"/requests/{r.pk}/", "/audit/"]:
                self.assertEqual(self.client.get(url).status_code, 200, f"{username} {url}")
        self.client.login(username="md", password="demo1234")
        self.assertEqual(self.client.get("/approvals/").status_code, 200)
        self.client.login(username="finance", password="demo1234")
        self.assertEqual(self.client.get("/finance/").status_code, 200)

    def test_staff_only_sees_own_requests(self):
        self.client.login(username="kasun", password="demo1234")
        other = PaymentRequest.objects.exclude(requested_by__username="kasun").first()
        mine = PaymentRequest.objects.filter(requested_by__username="kasun").first()
        self.assertEqual(self.client.get(f"/requests/{other.pk}/").status_code, 404)
        self.assertEqual(self.client.get(f"/requests/{mine.pk}/").status_code, 200)
        self.assertEqual(self.client.get("/audit/").status_code, 403)
        self.assertEqual(self.client.get("/approvals/").status_code, 403)

    def test_submit_and_approve_via_http(self):
        c = Campaign.objects.get(code="CMP-2612")
        line = c.budget_lines.get(category="Printing & branding")
        self.client.login(username="kasun", password="demo1234")
        resp = self.client.post(f"/campaigns/{c.pk}/request/", {
            "kind": "supplier", "budget_line": line.pk, "purpose": "Roadshow banners", "amount": "50000",
            "new_supplier": "New Print Co", "instalment": "single"})
        self.assertEqual(resp.status_code, 302)
        r = PaymentRequest.objects.get(purpose="Roadshow banners")
        self.client.login(username="md", password="demo1234")
        self.client.post(f"/requests/{r.pk}/approve/", {"comment": ""})
        r.refresh_from_db()
        self.assertEqual(r.status, S.APPROVED)

    def test_seed_closing_campaign_is_ready(self):
        _, ready, _ = svc.closure_checklist(Campaign.objects.get(code="CMP-2609"))
        self.assertTrue(ready)
