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
        # verifying raises the staff claim automatically, so it is pending approval, not "due"
        claim = r.reimbursement
        self.assertEqual((claim.kind, claim.amount, claim.requested_by), ("reimburse", D("2000"), self.staff))
        f = svc.line_figures(self.venue)
        self.assertEqual((f.reimbursements_due, f.pending), (0, D("2000")))

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
        self.assertEqual(r.status, S.ENDORSE)  # kasun reports to tharindu in the demo
        self.client.login(username="tharindu", password="demo1234")
        self.client.post(f"/requests/{r.pk}/endorse/", {"comment": ""})
        r.refresh_from_db()
        self.assertEqual(r.status, S.SUBMITTED)
        self.client.login(username="md", password="demo1234")
        self.client.post(f"/requests/{r.pk}/approve/", {"comment": ""})
        r.refresh_from_db()
        self.assertEqual(r.status, S.APPROVED)

    def test_seed_closing_campaign_is_ready(self):
        _, ready, _ = svc.closure_checklist(Campaign.objects.get(code="CMP-2609"))
        self.assertTrue(ready)


# --------------------------------------------------------------------------- AI document reading
from unittest import mock

from django.test import override_settings

from . import ai


def fake_doc(**kw):
    d = {"document_type": "invoice", "vendor_name": "Acme (Pvt) Ltd", "document_number": "INV-1",
         "document_date": date.today().isoformat(), "currency": "LKR", "total_amount": 30000,
         "tax_amount": None, "line_items": [], "legibility": "good", "handwritten": False, "notes": ""}
    d.update(kw)
    return d


@override_settings(OPENAI_API_KEY="test-key", AI_PROVIDER="openai", AI_MODEL="", AI_AUTO_READ=True)
class AITests(Base):
    def upload(self, r, kind, doc):
        att = Attachment.objects.create(request=r, kind=kind, uploaded_by=self.staff,
                                        file=ContentFile(b"Total Rs. 30,000", name="doc.txt"))
        with mock.patch.object(ai, "call_model", return_value=doc):
            svc.run_ai_read(self.fin, att)
        att.refresh_from_db()
        return att

    def levels(self, r):
        return [lvl for lvl, _ in svc.ai_checks(r)]

    def test_defaults_to_cheap_openai_model(self):
        self.assertTrue(ai.enabled())
        self.assertEqual(ai.model_name(), "gpt-6-luna")

    def test_strict_schema_requires_every_field(self):
        sc = ai._strict(ai.DOCUMENT_SCHEMA)
        self.assertEqual(set(sc["required"]), set(sc["properties"]))
        self.assertFalse(sc["additionalProperties"])
        item = sc["properties"]["line_items"]["items"]
        self.assertEqual(set(item["required"]), {"description", "quantity", "amount"})

    def test_matching_bill_passes(self):
        r = self.req("30000")
        svc.decide_request(self.md, r, "approve")
        self.pay(r)
        att = self.upload(r, "bill", fake_doc())
        self.assertEqual(att.ai_total, D("30000"))
        self.assertNotIn("warn", self.levels(r))
        self.assertIn("ok", self.levels(r))
        self.assertTrue(AuditLog.objects.filter(request=r, action="ai.read").exists())

    def test_bill_amount_and_vendor_mismatch_flagged(self):
        r = self.req("30000")
        svc.decide_request(self.md, r, "approve")
        self.pay(r)
        self.upload(r, "bill", fake_doc(total_amount=33000, vendor_name="Totally Different Traders"))
        msgs = " ".join(m for lvl, m in svc.ai_checks(r) if lvl == "warn")
        self.assertIn("Bill total", msgs)
        self.assertIn("payee", msgs)

    def test_duplicate_bill_across_requests(self):
        r1, r2 = self.req("30000"), self.req("30000")
        self.upload(r1, "bill", fake_doc())
        self.upload(r2, "bill", fake_doc())
        msgs = " ".join(m for lvl, m in svc.ai_checks(r2) if lvl == "warn")
        self.assertIn("possible duplicate", msgs)
        self.assertIn(r1.reference, msgs)

    def test_receipts_prefill_and_check_settlement(self):
        r = self.req("50000", kind="advance")
        svc.decide_request(self.md, r, "approve")
        self.pay(r)
        self.upload(r, "receipt", fake_doc(document_type="receipt", total_amount=20000, document_number="R1"))
        self.upload(r, "receipt", fake_doc(document_type="receipt", total_amount=25000, document_number="R2"))
        self.assertEqual(svc.receipts_total(r), D("45000"))
        svc.submit_settlement(self.staff, r, spent_amount=D("48000"), cash_returned=D("2000"))
        r = PaymentRequest.objects.get(pk=r.pk)
        msgs = " ".join(m for lvl, m in svc.ai_checks(r) if lvl == "warn")
        self.assertIn("settlement claims", msgs)

    def test_quote_vs_contract_and_wrong_type(self):
        r = self.req("40000", new_contract_total=D("90000"), instalment="first")
        self.upload(r, "quote", fake_doc(document_type="quotation", total_amount=90000))
        self.assertNotIn("warn", self.levels(r))
        self.upload(r, "bill", fake_doc(document_type="quotation", total_amount=90000, document_number="Q9"))
        msgs = " ".join(m for lvl, m in svc.ai_checks(r) if lvl == "warn")
        self.assertIn("looks like a quotation", msgs)

    def test_api_failure_is_recorded_not_raised(self):
        r = self.req("1000")
        att = Attachment.objects.create(request=r, kind="bill", uploaded_by=self.staff,
                                        file=ContentFile(b"x", name="b.txt"))
        with mock.patch.object(ai, "call_model", side_effect=RuntimeError("401 invalid key")):
            svc.run_ai_read(self.fin, att)
        att.refresh_from_db()
        self.assertEqual(att.ai_status, "error")
        self.assertIn("info", self.levels(r))

    def test_upload_view_auto_reads(self):
        r = self.req("30000")
        self.client.force_login(self.staff)
        with mock.patch.object(ai, "call_model", return_value=fake_doc(document_type="quotation")) as m:
            self.client.post(f"/requests/{r.pk}/upload/", {
                "kind": "quote", "file": ContentFile(b"quote", name="q.txt"), "description": ""})
        m.assert_called_once()
        self.assertEqual(r.attachments.get().ai_status, "done")

    def test_image_is_downscaled_to_jpeg(self):
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (4000, 3000), "white").save(buf, format="PNG")
        block = ai.content_block("photo.png", buf.getvalue())
        self.assertEqual(block["source"]["media_type"], "image/jpeg")
        part = ai._to_openai(block)
        self.assertTrue(part["image_url"].startswith("data:image/jpeg;base64,"))


@override_settings(OPENAI_API_KEY="", ANTHROPIC_API_KEY="", AI_PROVIDER="openai")
class AIDisabledTests(Base):
    def test_disabled_without_key(self):
        self.assertFalse(ai.enabled())
        r = self.req("1000")
        self.client.force_login(self.staff)
        with mock.patch.object(ai, "call_model") as m:
            self.client.post(f"/requests/{r.pk}/upload/", {"kind": "quote", "file": ContentFile(b"q", name="q.txt")})
        m.assert_not_called()


# --------------------------------------------------------------------------- query agent (offline)
import json as _json

from langchain_core.messages import AIMessage, ToolMessage

from . import agent


class ScriptedLLM:
    """Stands in for the chat model: returns scripted AIMessages, may inspect tool results."""

    def __init__(self, *steps):
        self.steps, self.seen = list(steps), []

    def bind_tools(self, tools):
        self.tool_names = [t.name for t in tools]
        return self

    def invoke(self, messages):
        self.seen.append(messages)
        step = self.steps.pop(0)
        return step(messages) if callable(step) else step


def call(name, args, i="c1"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": i, "type": "tool_call"}])


def last_tool(messages):
    return _json.loads([m for m in messages if isinstance(m, ToolMessage)][-1].content)


@override_settings(OPENAI_API_KEY="test-key")
class AgentTests(Base):
    def test_budget_question_uses_tool_figures(self):
        self.req("30000")
        llm = ScriptedLLM(call("get_campaign_budget", {"campaign": "T1"}),
                          lambda m: AIMessage(content=f"Venue available: {last_tool(m)['categories'][0]['available']}"))
        res = agent.ask(self.md, "How much is left for venue?", llm=llm)
        self.assertEqual(res["answer"], "Venue available: 70000.0")
        self.assertEqual([c["name"] for c in res["tool_calls"]], ["get_campaign_budget"])
        self.assertTrue(res["tool_calls"][0]["ok"])
        self.assertIn("Today is", llm.seen[0][0].content)  # system prompt sent

    def test_bad_campaign_returns_error_then_recovers(self):
        llm = ScriptedLLM(call("get_campaign_budget", {"campaign": "nope"}, "a"),
                          call("list_campaigns", {}, "b"),
                          AIMessage(content="Did you mean T1?"))
        res = agent.ask(self.md, "budget for nope", llm=llm)
        self.assertFalse(res["tool_calls"][0]["ok"])
        self.assertTrue(res["tool_calls"][1]["ok"])
        self.assertEqual(res["answer"], "Did you mean T1?")

    def test_staff_scoping(self):
        other = make_user("other", svc.ROLE_STAFF)
        self.req("1000")
        self.req("2000", user=other)
        names = [t.name for t in agent.build_tools(self.staff)]
        self.assertNotIn("recent_activity", names)
        self.assertIn("recent_activity", [t.name for t in agent.build_tools(self.md)])
        tools = {t.name: t for t in agent.build_tools(self.staff)}
        out = _json.loads(tools["search_requests"].invoke({}))
        self.assertEqual((out["count"], out["total_amount"]), (1, 1000.0))
        theirs = PaymentRequest.objects.get(requested_by=other)
        self.assertIn("error", _json.loads(tools["get_request"].invoke({"reference": theirs.reference})))

    def test_every_tool_runs_on_demo_shaped_data(self):
        r = self.req("40000", new_contract_total=D("90000"), instalment="first")
        svc.decide_request(self.md, r, "approve")
        self.pay(r)
        adv = self.req("5000", kind="advance")
        svc.decide_request(self.md, adv, "approve")
        self.pay(adv)
        tools = {t.name: t for t in agent.build_tools(self.md)}
        calls = {"list_campaigns": {}, "get_campaign_budget": {"campaign": "Test"},
                 "search_requests": {"status": "paid", "kind": "supplier"}, "get_request": {"reference": r.reference},
                 "supplier_summary": {"supplier": "acme"}, "pending_work": {}, "closure_status": {"campaign": "T1"},
                 "document_flags": {}, "recent_activity": {"days": 3}}
        self.assertEqual(set(calls), set(tools))
        for name, args in calls.items():
            out = _json.loads(tools[name].invoke(args))
            self.assertNotIn("error", out, name)
        self.assertEqual(_json.loads(tools["pending_work"].invoke({}))["advances_not_settled"][0]["ref"], adv.reference)
        self.assertEqual(_json.loads(tools["supplier_summary"].invoke({"supplier": "acme"}))["contracts"][0]["value"], 90000.0)

    def test_model_failure_is_reported(self):
        class Broken(ScriptedLLM):
            def invoke(self, messages):
                raise RuntimeError("429 rate limited")
        res = agent.ask(self.md, "hi", llm=Broken())
        self.assertIn("429", res["error"])

    def test_ask_page(self):
        self.client.force_login(self.md)
        self.assertEqual(self.client.get("/ask/").status_code, 200)
        fake = {"answer": "LKR 70,000 left", "error": "", "tool_calls": [{"name": "get_campaign_budget", "args": {}, "ok": True}]}
        with mock.patch.object(agent, "ask", return_value=fake):
            self.client.post("/ask/", {"question": "venue left?"})
        page = self.client.get("/ask/").content.decode()
        self.assertIn("LKR 70,000 left", page)
        self.assertIn("get_campaign_budget", page)
        self.assertTrue(AuditLog.objects.filter(action="agent.query").exists())


class SimpleMarkdownTests(TestCase):
    def test_bold_lists_and_escaping(self):
        from .templatetags.money import simple_md
        html = simple_md("**LKR 5** left\n- a <script>\n- `SUP-1`")
        self.assertIn("<strong>LKR 5</strong>", html)
        self.assertIn("<li>a &lt;script&gt;</li>", html)
        self.assertIn("<code>SUP-1</code>", html)


class FormRenderingRegressionTests(Base):
    def test_budget_preview_data_is_machine_readable(self):
        self.client.force_login(self.staff)
        html = self.client.get(f"/campaigns/{self.c.pk}/request/").content.decode()
        self.assertIn('data-avail="100000.00"', html)  # no thousand separators for JS parseFloat

    def test_payment_date_prefilled_in_iso(self):
        r = self.req("1000")
        svc.decide_request(self.md, r, "approve")
        self.client.force_login(self.fin)
        html = self.client.get(f"/requests/{r.pk}/").content.decode()
        self.assertIn(f'value="{date.today().isoformat()}"', html)



class StorageTests(Base):
    def _upload(self):
        r = self.req("1000")
        self.client.force_login(self.staff)
        self.client.post(f"/requests/{r.pk}/upload/", {"kind": "receipt", "file": ContentFile(b"%PDF-1.4 receipt", name="r.pdf")})
        return r, r.attachments.get()

    def test_database_storage_round_trip(self):
        from django.core.files.storage import storages

        from .models import StoredFile
        with override_settings(STORAGES={**__import__("django.conf").conf.settings.STORAGES,
                                         "default": {"BACKEND": "finance.storage.DatabaseStorage"}}):
            storages._storages = {}
            try:
                r, att = self._upload()
                self.assertEqual(StoredFile.objects.count(), 1)
                resp = self.client.get(f"/attachments/{att.pk}/")
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(b"".join(resp.streaming_content), b"%PDF-1.4 receipt")
                # a second upload with the same name gets a unique name, not an overwrite
                self.client.post(f"/requests/{r.pk}/upload/", {"kind": "receipt", "file": ContentFile(b"two", name="r.pdf")})
                self.assertEqual(StoredFile.objects.count(), 2)
            finally:
                storages._storages = {}

    def test_missing_file_is_404_not_500(self):
        r, att = self._upload()
        att.file.storage.delete(att.file.name)
        self.assertEqual(self.client.get(f"/attachments/{att.pk}/").status_code, 404)


# --------------------------------------------------------------------------- supervisor layer
from .models import ApprovalDecision, EmailAttachment, OutboundEmail, StaffProfile
from . import notify


class SupervisorTests(Base):
    def setUp(self):
        super().setUp()
        self.boss = make_user("boss", svc.ROLE_STAFF)
        StaffProfile.objects.create(user=self.staff, supervisor=self.boss)

    def test_request_goes_to_supervisor_first(self):
        r = self.req("20000")
        self.assertEqual(r.status, S.ENDORSE)
        with self.assertRaises(ValidationError):
            svc.decide_request(self.md, r, "approve")
        svc.endorse_request(self.boss, r, "endorse")
        r.refresh_from_db()
        self.assertEqual(r.status, S.SUBMITTED)
        svc.decide_request(self.md, r, "approve")
        r.refresh_from_db()
        self.assertEqual(r.status, S.APPROVED)
        stages = list(r.decisions.values_list("stage", "decision"))
        self.assertEqual(stages, [("supervisor", "endorse"), ("md", "approve")])

    def test_awaiting_supervisor_counts_as_pending_budget(self):
        self.req("30000")
        f = svc.line_figures(self.venue)
        self.assertEqual(f.pending, D("30000"))
        self.assertEqual(f.available, D("70000"))

    def test_only_the_supervisor_can_endorse(self):
        r = self.req("20000")
        other = make_user("other", svc.ROLE_STAFF)
        with self.assertRaises(PermissionDenied):
            svc.endorse_request(other, r, "endorse")
        with self.assertRaises(PermissionDenied):
            svc.endorse_request(self.md, r, "endorse")

    def test_supervisor_clarification_returns_to_supervisor(self):
        r = self.req("20000")
        svc.endorse_request(self.boss, r, "clarify", "Which venue?")
        r.refresh_from_db()
        self.assertEqual(r.status, S.CLARIFY)
        with self.assertRaises(ValidationError):
            svc.decide_request(self.md, r, "approve")  # MD can't jump in on the supervisor's question
        svc.respond_to_clarification(self.staff, r, "Atrium B")
        r.refresh_from_db()
        self.assertEqual(r.status, S.ENDORSE)

    def test_supervisor_reject_needs_comment(self):
        r = self.req("20000")
        with self.assertRaises(ValidationError):
            svc.endorse_request(self.boss, r, "reject")
        svc.endorse_request(self.boss, r, "reject", "Not in plan")
        r.refresh_from_db()
        self.assertEqual(r.status, S.REJECTED)

    def test_md_and_unsupervised_staff_skip_the_layer(self):
        StaffProfile.objects.create(user=self.md, supervisor=self.boss)
        r = self.req("10000", user=self.md)
        self.assertEqual(r.status, S.SUBMITTED)


# --------------------------------------------------------------------------- email notifications
class EmailTests(Base):
    def setUp(self):
        super().setUp()
        for u, e in ((self.md, "md@x.lk"), (self.fin, "fin@x.lk"), (self.staff, "staff@x.lk")):
            u.email = e
            u.save()

    def test_workflow_sends_simulated_emails(self):
        with self.captureOnCommitCallbacks(execute=True):
            r = self.req("20000")
        e = OutboundEmail.objects.get(event="request.needs_approval")
        self.assertEqual(e.to, ["md@x.lk"])
        self.assertEqual(e.status, OutboundEmail.Status.SIMULATED)
        self.assertIn(r.reference, e.subject)
        self.assertIn(f"/requests/{r.pk}/", e.body_html)
        with self.captureOnCommitCallbacks(execute=True):
            svc.decide_request(self.md, r, "approve")
        self.assertTrue(OutboundEmail.objects.filter(event="request.approve", to=["staff@x.lk"]).exists())
        self.assertTrue(OutboundEmail.objects.filter(event="request.ready_to_pay", to=["fin@x.lk"]).exists())
        with self.captureOnCommitCallbacks(execute=True):
            self.pay(r)
        self.assertTrue(OutboundEmail.objects.filter(event="payment.recorded").exists())

    def test_user_without_email_is_skipped_not_an_error(self):
        self.md.email = ""
        self.md.save()
        self.req("20000")
        self.assertEqual(OutboundEmail.objects.get(event="request.needs_approval").status, OutboundEmail.Status.SKIPPED)

    def test_opt_out(self):
        StaffProfile.objects.create(user=self.md, email_notifications=False)
        self.req("20000")
        self.assertEqual(OutboundEmail.objects.get(event="request.needs_approval").to, [])

    @override_settings(EMAIL_PROVIDER="outlook", MS_GRAPH_TENANT_ID="t", MS_GRAPH_CLIENT_ID="c",
                       MS_GRAPH_CLIENT_SECRET="s", MS_GRAPH_SENDER="finance@agency.lk")
    def test_outlook_graph_payload(self):
        import json as _json
        calls = []

        class Resp:
            def __init__(self, status, body, headers=None):
                self.status, self._b, self.headers = status, body, headers or {}

            def read(self):
                return self._b

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def opener(req, timeout=None):
            calls.append(req)
            if "oauth2" in req.full_url:
                return Resp(200, b'{"access_token": "tok", "expires_in": 3600}')
            return Resp(202, b"", {"request-id": "abc-123"})

        notify.GraphMailer._token = None
        e = notify.send("test", addresses=["client@x.lk"], subject="Hi", heading="H", intro="I",
                        attachments=[("r.pdf", b"%PDF-1.4 test", "application/pdf")])
        with mock.patch("finance.notify.urllib.request.urlopen", opener):
            notify.deliver(e.pk)
        e.refresh_from_db()
        self.assertEqual(e.status, OutboundEmail.Status.SENT, e.error)
        self.assertEqual(e.provider_message_id, "abc-123")
        send_call = calls[-1]
        self.assertTrue(send_call.full_url.endswith("/users/finance%40agency.lk/sendMail"))
        self.assertEqual(send_call.headers["Authorization"], "Bearer tok")
        body = _json.loads(send_call.data)
        self.assertEqual(body["message"]["toRecipients"][0]["emailAddress"]["address"], "client@x.lk")
        self.assertEqual(body["message"]["attachments"][0]["name"], "r.pdf")

    @override_settings(EMAIL_PROVIDER="outlook", MS_GRAPH_TENANT_ID="", MS_GRAPH_CLIENT_ID="",
                       MS_GRAPH_CLIENT_SECRET="", MS_GRAPH_SENDER="")
    def test_outlook_misconfigured_is_recorded_not_raised(self):
        with self.captureOnCommitCallbacks(execute=True):
            r = self.req("20000")
        e = OutboundEmail.objects.get(event="request.needs_approval")
        self.assertEqual(e.status, OutboundEmail.Status.FAILED)
        self.assertIn("MS_GRAPH_TENANT_ID", e.error)
        r.refresh_from_db()
        self.assertEqual(r.status, S.SUBMITTED)  # the workflow carried on


# --------------------------------------------------------------------------- PDF report
class ReportTests(Base):
    def _paid_with_bill(self, filename, content):
        r = self.req("40000")
        svc.decide_request(self.md, r, "approve")
        self.pay(r)
        Attachment.objects.create(request=r, kind="bill", uploaded_by=self.fin, file=ContentFile(content, name=filename))
        svc.verify_bill(self.fin, r)
        return r

    def test_report_merges_pdf_bills_and_places_images(self):
        import io

        from PIL import Image
        from pypdf import PdfReader
        from reportlab.pdfgen import canvas

        from .reports import build_campaign_report
        buf = io.BytesIO()
        c = canvas.Canvas(buf)
        c.drawString(100, 700, "Invoice")
        c.showPage()
        c.drawString(100, 700, "page 2")
        c.showPage()
        c.save()
        self._paid_with_bill("inv.pdf", buf.getvalue())
        png = io.BytesIO()
        Image.new("RGB", (300, 200), "white").save(png, format="PNG")
        self._paid_with_bill("bill.png", png.getvalue())
        self.c.commission_pct = D("15")
        self.c.save()
        pdf = build_campaign_report(self.c)
        self.assertTrue(pdf.startswith(b"%PDF"))
        reader = PdfReader(io.BytesIO(pdf))
        text = "\n".join(p.extract_text() for p in reader.pages)
        self.assertIn("Budget versus actual", text)
        self.assertIn("92,000.00", text)          # 80,000 verified + 15% commission
        self.assertIn("page 2", text)             # the PDF bill's pages were merged in
        self.assertGreaterEqual(len(reader.pages), 4)

    def test_report_marked_draft_until_closable(self):
        import io

        from pypdf import PdfReader

        from .reports import build_campaign_report
        self.req("1000")
        text = PdfReader(io.BytesIO(build_campaign_report(self.c))).pages[0].extract_text()
        self.assertIn("not ready for closure", text)

    def test_email_report_to_client(self):
        self._paid_with_bill("inv.txt", b"Invoice 40,000")
        with self.captureOnCommitCallbacks(execute=True):
            e = svc.send_campaign_report(self.fin, self.c, "client@brand.lk")
        e.refresh_from_db()
        self.assertEqual(e.to, ["client@brand.lk"])
        self.assertEqual(e.status, OutboundEmail.Status.SIMULATED)
        att = e.attachments.get()
        self.assertTrue(att.filename.endswith("closure-report.pdf"))
        with self.assertRaises(PermissionDenied):
            svc.send_campaign_report(self.staff, self.c, "client@brand.lk")
        with self.assertRaises(ValidationError):
            svc.send_campaign_report(self.fin, self.c, "not-an-email")


class ReportViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def test_pdf_inbox_and_team_pages(self):
        c = Campaign.objects.get(code="CMP-2609")
        self.client.login(username="finance", password="demo1234")
        resp = self.client.get(f"/campaigns/{c.pk}/report.pdf")
        self.assertEqual(resp["Content-Type"], "application/pdf")
        self.assertTrue(resp.content.startswith(b"%PDF"))
        resp = self.client.post(f"/campaigns/{c.pk}/report/email/", {"to": "x@client.lk"})
        self.assertEqual(resp.status_code, 302)
        e = next(m for m in OutboundEmail.objects.filter(event="report.sent") if "x@client.lk" in m.to)
        self.assertIsNotNone(e)
        self.assertEqual(self.client.get("/inbox/?all=1").status_code, 200)
        self.assertEqual(self.client.get(f"/inbox/{e.pk}/").status_code, 200)
        att = e.attachments.first()
        self.assertEqual(self.client.get(f"/outbox/attachment/{att.pk}/").status_code, 200)
        self.client.login(username="tharindu", password="demo1234")
        resp = self.client.get("/team-approvals/")
        self.assertContains(resp, "Roadshow banners")
        self.assertEqual(self.client.get(f"/outbox/attachment/{att.pk}/").status_code, 403)
        self.client.login(username="kasun", password="demo1234")
        self.assertEqual(self.client.get("/team-approvals/").status_code, 403)
        self.assertEqual(self.client.get("/inbox/").status_code, 200)


# --------------------------------------------------------------------------- local (llama.cpp) AI provider
@override_settings(AI_PROVIDER="local", AI_BASE_URL="http://127.0.0.1:8080/v1")
class LocalAITests(Base):
    def test_enabled_without_api_key(self):
        self.assertTrue(ai.enabled())

    def test_pdf_sent_as_text_and_image_as_image(self):
        import base64
        import io

        from reportlab.pdfgen import canvas
        buf = io.BytesIO()
        c = canvas.Canvas(buf)
        c.drawString(100, 700, "TOTAL 30,000")
        c.save()
        block = ai.content_block("x.pdf", buf.getvalue())
        parts = ai._local_parts(block)
        self.assertEqual(parts[0]["type"], "text")
        self.assertIn("TOTAL 30,000", parts[0]["text"])
        img = {"type": "image", "source": {"media_type": "image/jpeg", "data": base64.b64encode(b"x").decode()}}
        self.assertTrue(ai._local_parts(img)[0]["image_url"]["url"].startswith("data:image/jpeg;base64,"))

    def test_local_call_uses_base_url_and_json_schema(self):
        seen = {}

        class FakeCompletions:
            def create(self, **kw):
                seen.update(kw)
                msg = mock.Mock(content='{"document_type": "invoice", "total_amount": 30000}')
                return mock.Mock(choices=[mock.Mock(message=msg)])

        class FakeOpenAI:
            def __init__(self, **kw):
                seen["client"] = kw
                self.chat = mock.Mock(completions=FakeCompletions())

        with mock.patch("openai.OpenAI", FakeOpenAI):
            out = ai.call_model({"type": "text", "text": "Invoice 30,000"}, "ctx")
        self.assertEqual(out["total_amount"], 30000)
        self.assertEqual(seen["client"]["base_url"], "http://127.0.0.1:8080/v1")
        self.assertEqual(seen["response_format"]["type"], "json_schema")


class ReimbursementTests(Base):
    def overspent(self):
        r = self.req("10000", kind="advance")
        svc.decide_request(self.md, r, "approve")
        self.pay(r)
        self.attach(r, "receipt")
        svc.submit_settlement(self.staff, r, spent_amount=D("12500"), cash_returned=D("0"))
        svc.verify_settlement(self.fin, r)
        r.refresh_from_db()
        return r

    def test_claim_is_approved_and_paid_like_any_request(self):
        adv = self.overspent()
        claim = adv.reimbursement
        self.assertTrue(claim.reference.startswith("RMB-"))
        svc.decide_request(self.md, claim, "approve")
        self.pay(claim)
        claim.refresh_from_db()
        self.assertEqual(claim.status, S.PAID)
        checks = {c[0]: c[1] for c in svc.closure_checklist(self.c)[0]}
        self.assertTrue(checks["Staff overspends claimed (reimbursement raised)"])

    def test_only_one_claim_per_advance(self):
        adv = self.overspent()
        with self.assertRaises(ValidationError):
            svc.request_reimbursement(self.fin, adv)

    def test_reimburse_needs_an_advance_and_finance(self):
        with self.assertRaises(ValidationError):
            self.req("500", kind="reimburse", supplier=None)
        adv = self.overspent()
        with self.assertRaises(PermissionDenied):
            svc.request_reimbursement(self.staff, adv)

    def test_staff_cannot_pick_reimburse_in_the_form(self):
        from .forms import PaymentRequestForm
        choices = [c[0] for c in PaymentRequestForm(campaign=self.c).fields["kind"].choices]
        self.assertNotIn("reimburse", choices)


class ClientPOTests(Base):
    def test_attach_po_and_it_leads_the_report_appendix(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from . import reports
        up = SimpleUploadedFile("PO-778.pdf", _tiny_pdf(), content_type="application/pdf")
        svc.attach_client_po(self.md, self.c, up, po_number="PO-778")
        self.c.refresh_from_db()
        self.assertEqual(self.c.client_po_number, "PO-778")
        self.assertTrue(self.c.client_po_file.name.endswith(".pdf"))
        pdf = reports.build_campaign_report(self.c)
        self.assertTrue(pdf.startswith(b"%PDF"))

    def test_po_rejects_unknown_types_and_staff(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        with self.assertRaises(ValidationError):
            svc.attach_client_po(self.md, self.c, SimpleUploadedFile("x.exe", b"MZ"))
        with self.assertRaises(PermissionDenied):
            svc.attach_client_po(self.staff, self.c, SimpleUploadedFile("po.pdf", _tiny_pdf()))

    def test_po_download_view(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        svc.attach_client_po(self.md, self.c, SimpleUploadedFile("po.pdf", _tiny_pdf()))
        self.client.force_login(self.md)
        r = self.client.get(f"/campaigns/{self.c.pk}/po/file")
        self.assertEqual(r.status_code, 200)
        self.client.force_login(self.staff)
        self.assertIn(self.client.get(f"/campaigns/{self.c.pk}/po/file").status_code, (403, 404))


def _tiny_pdf():
    import io
    from reportlab.pdfgen import canvas
    b = io.BytesIO(); c = canvas.Canvas(b); c.drawString(72, 720, "Client PO 778"); c.save()
    return b.getvalue()


@override_settings(EMAIL_BACKEND="finance.mail_backend.OutboxEmailBackend", EMAIL_PROVIDER="simulated")
class AccountTests(Base):
    def test_password_reset_email_goes_through_outbox(self):
        from .models import OutboundEmail
        self.staff.email = "staff@example.com"; self.staff.save()
        with self.captureOnCommitCallbacks(execute=True):
            r = self.client.post("/password-reset/", {"email": "staff@example.com"})
        self.assertEqual(r.status_code, 302)
        e = OutboundEmail.objects.get(event="auth")
        self.assertIn("/reset/", e.body_text)
        self.assertEqual(e.to, ["staff@example.com"])

    def test_invite_users_command(self):
        import os, tempfile
        from .models import OutboundEmail, StaffProfile
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as f:
            f.write("username,first_name,last_name,email,role,supervisor\n"
                    "nimal,Nimal,P,nimal@example.com,Field Staff,md\n")
        with self.captureOnCommitCallbacks(execute=True):
            call_command("invite_users", f.name)
        os.unlink(f.name)
        u = User.objects.get(username="nimal")
        self.assertFalse(u.has_usable_password())
        self.assertEqual(StaffProfile.objects.get(user=u).supervisor, self.md)
        self.assertTrue(OutboundEmail.objects.filter(event="user.invite", to=["nimal@example.com"]).exists())

    def test_two_factor_setup_and_login(self):
        import pyotp
        self.client.force_login(self.md)
        self.client.get("/security/setup/")
        secret = self.client.session["totp_pending"]
        r = self.client.post("/security/setup/", {"code": "000000"})
        self.assertEqual(r.status_code, 200)
        r = self.client.post("/security/setup/", {"code": pyotp.TOTP(secret).now()})
        self.assertEqual(r.status_code, 302)
        # a fresh session now has to give a code
        self.client.logout(); self.client.force_login(self.md)
        r = self.client.get("/")
        self.assertTrue(r.url.startswith("/security/verify/"))
        r = self.client.post("/security/verify/?next=/", {"code": pyotp.TOTP(secret).now()})
        self.assertEqual(r.url, "/")
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_two_factor_lockout_after_five_wrong_codes(self):
        from .models import StaffProfile
        import pyotp
        StaffProfile.objects.update_or_create(user=self.md, defaults={"totp_secret": pyotp.random_base32(),
                                                                      "totp_enabled": True})
        self.client.force_login(self.md)
        for _ in range(5):
            r = self.client.post("/security/verify/", {"code": "000000"})
        self.assertEqual(r.url, "/login/")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_required_mode_forces_enrolment_for_md_not_staff(self):
        with self.settings(TWO_FACTOR_REQUIRED=True):
            self.client.force_login(self.md)
            self.assertEqual(self.client.get("/").url, "/security/setup/")
            self.client.force_login(self.staff)
            self.assertEqual(self.client.get("/").status_code, 200)
