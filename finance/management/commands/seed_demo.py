"""Load sample users, campaigns and requests in every workflow state.

    python manage.py seed_demo          # adds demo data (idempotent on users)
    python manage.py seed_demo --reset  # wipes finance data first

All demo users have the password ``demo1234``.
"""
from datetime import date, timedelta
from decimal import Decimal as D

from django.contrib.auth.models import Group, User
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from django.db import transaction

from finance import services as svc
from finance.models import (
    Attachment, AuditLog, Campaign, Supplier,
)

PASSWORD = "demo1234"


def _file(name, text):
    return ContentFile(f"SAMPLE DOCUMENT – demo data only\n\n{text}\n".encode(), name=name)


class Command(BaseCommand):
    help = "Seed demo users, campaigns, suppliers and payment requests."

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="Delete existing finance data first")

    @transaction.atomic
    def handle(self, *args, reset=False, **opts):
        if reset:
            AuditLog.objects.all().delete()
            from finance import models as fm
            for model in (fm.Attachment, fm.Payment, fm.Settlement, fm.ApprovalDecision, fm.PaymentRequest,
                          fm.SupplierContract, fm.BudgetRevision, fm.BudgetLine, fm.Campaign, fm.Supplier):
                model.objects.all().delete()

        groups = {name: Group.objects.get_or_create(name=name)[0] for name in svc.ALL_ROLES}
        users = {}
        for username, first, last, role in [
            ("md", "Nimal", "Perera", svc.ROLE_MD),
            ("finance", "Shalini", "Fernando", svc.ROLE_FINANCE),
            ("kasun", "Kasun", "Silva", svc.ROLE_STAFF),
            ("dilini", "Dilini", "Jayawardena", svc.ROLE_STAFF),
            ("admin", "System", "Admin", svc.ROLE_ADMIN),
        ]:
            u, created = User.objects.get_or_create(username=username, defaults={"first_name": first, "last_name": last})
            if created:
                u.set_password(PASSWORD)
                if username == "admin":
                    u.is_staff = u.is_superuser = True
                u.save()
            u.groups.add(groups[role])
            users[username] = u
        md, fin, kasun, dilini = users["md"], users["finance"], users["kasun"], users["dilini"]

        if Campaign.objects.exists():
            self.stdout.write("Campaigns already exist – users ensured, skipping sample data (use --reset).")
            return

        sup = {n: Supplier.objects.create(name=n, contact_person=c, phone=p) for n, c, p in [
            ("Lanka Stage & Sound", "Ruwan", "077 123 4567"),
            ("PrintHub Colombo", "Mahesh", "071 555 0101"),
            ("Island Transport Services", "Fazal", "076 222 3344"),
            ("Green Leaf Caterers", "Anoma", "070 888 9900"),
            ("Metro Fabricators", "Sunil", "072 444 1212"),
        ]}
        today = date.today()

        # ---------------------------------------------------------------- campaign 1: mid-flight
        c1 = svc.create_campaign(
            md, code="CMP-2611", name="Sunleaf Tea – Mall Sampling Weekend", client="Sunleaf Beverages (Pvt) Ltd",
            start_date=today - timedelta(days=10), end_date=today + timedelta(days=5),
            lines=[("Venue & permits", D("350000")), ("Production & fabrication", D("600000")),
                   ("Printing & branding", D("280000")), ("Promoters & staff", D("420000")),
                   ("Transport & logistics", D("180000")), ("Food & refreshments", D("90000"))],
        )
        L = {l.category: l for l in c1.budget_lines.all()}

        # Instalment contract: stage build 540k → first paid, second approved, final pending later
        r = svc.submit_request(kasun, campaign=c1, budget_line=L["Production & fabrication"], kind="supplier",
                               supplier=sup["Metro Fabricators"], amount=D("216000"), purpose="Booth fabrication – 40% advance",
                               new_contract_total=D("540000"), new_contract_description="Sampling booth build (3 units)",
                               instalment="first")
        Attachment.objects.create(request=r, kind="quote", uploaded_by=kasun, file=_file("metro-quote.txt", "Quote 540,000"))
        svc.decide_request(md, r, "approve")
        svc.record_payment(fin, r, amount=D("216000"), paid_on=today - timedelta(days=8), method="transfer", reference="BOC-TT-88123")
        contract = r.contract
        r2 = svc.submit_request(kasun, campaign=c1, budget_line=L["Production & fabrication"], kind="supplier",
                                contract=contract, amount=D("216000"), purpose="Booth fabrication – 40% on delivery",
                                instalment="second")
        svc.decide_request(md, r2, "approve", "Booths delivered and checked on site.")

        # Printing: paid, bill uploaded, verified
        r = svc.submit_request(dilini, campaign=c1, budget_line=L["Printing & branding"], kind="supplier",
                               supplier=sup["PrintHub Colombo"], amount=D("185000"), purpose="Banners, wobblers, sampling cups print")
        svc.decide_request(md, r, "approve")
        svc.record_payment(fin, r, amount=D("185000"), paid_on=today - timedelta(days=6), method="cheque", reference="CHQ 004512")
        Attachment.objects.create(request=r, kind="bill", uploaded_by=fin, file=_file("printhub-inv-2291.txt", "Invoice 185,000"))
        svc.verify_bill(fin, r)

        # Venue: paid, bill not yet verified
        r = svc.submit_request(kasun, campaign=c1, budget_line=L["Venue & permits"], kind="supplier",
                               supplier=sup["Lanka Stage & Sound"], amount=D("320000"), purpose="Mall atrium rental – 2 days")
        svc.decide_request(md, r, "approve")
        svc.record_payment(fin, r, amount=D("320000"), paid_on=today - timedelta(days=5), method="transfer", reference="COM-TT-55410")

        # Staff advance: paid and settled with cash returned
        r = svc.submit_request(dilini, campaign=c1, budget_line=L["Promoters & staff"], kind="advance",
                               amount=D("150000"), purpose="Promoter daily payments – day 1")
        svc.decide_request(md, r, "approve")
        svc.record_payment(fin, r, amount=D("150000"), paid_on=today - timedelta(days=3), method="cash", reference="PV-1182")
        Attachment.objects.create(request=r, kind="receipt", uploaded_by=dilini, file=_file("promoter-sheet-day1.txt", "Signed sheet 142,500"))
        svc.submit_settlement(dilini, r, spent_amount=D("142500"), cash_returned=D("7500"), notes="12 promoters × 11,875")
        svc.verify_settlement(fin, r)

        # Staff advance: paid, settlement submitted awaiting finance
        r = svc.submit_request(kasun, campaign=c1, budget_line=L["Transport & logistics"], kind="advance",
                               amount=D("60000"), purpose="Lorry hire & fuel – setup day")
        svc.decide_request(md, r, "approve")
        svc.record_payment(fin, r, amount=D("60000"), paid_on=today - timedelta(days=2), method="cash", reference="PV-1190")
        Attachment.objects.create(request=r, kind="receipt", uploaded_by=kasun, file=_file("fuel-receipts.txt", "Fuel 18,400; lorry 45,000"))
        svc.submit_settlement(kasun, r, spent_amount=D("63400"), cash_returned=D("0"), notes="Overspent 3,400 – extra trip")

        # Advance paid, not settled
        r = svc.submit_request(dilini, campaign=c1, budget_line=L["Promoters & staff"], kind="advance",
                               amount=D("150000"), purpose="Promoter daily payments – day 2")
        svc.decide_request(md, r, "approve")
        svc.record_payment(fin, r, amount=D("150000"), paid_on=today - timedelta(days=1), method="cash", reference="PV-1195")

        # Pending MD: within budget
        svc.submit_request(kasun, campaign=c1, budget_line=L["Food & refreshments"], kind="supplier",
                           supplier=sup["Green Leaf Caterers"], amount=D("72000"), purpose="Crew lunch & tea – 2 days",
                           needed_by=today + timedelta(days=1))
        # Pending MD: over budget (promoters line)
        svc.submit_request(dilini, campaign=c1, budget_line=L["Promoters & staff"], kind="advance",
                           amount=D("165000"), purpose="Promoter payments – extended day 3",
                           details="Client asked to extend sampling by one day.")
        # Clarification requested
        r = svc.submit_request(kasun, campaign=c1, budget_line=L["Transport & logistics"], kind="supplier",
                               supplier=sup["Island Transport Services"], amount=D("95000"), purpose="Return logistics")
        svc.decide_request(md, r, "clarify", "Why 95k? Setup was 60k. Get a second quote.")
        # Rejected
        r = svc.submit_request(dilini, campaign=c1, budget_line=L["Printing & branding"], kind="supplier",
                               supplier=sup["PrintHub Colombo"], amount=D("45000"), purpose="Extra flyers")
        svc.decide_request(md, r, "reject", "Use leftover stock from last month.")
        # Pending budget revision
        svc.propose_revision(fin, L["Promoters & staff"], D("600000"), "Client approved 1 extra sampling day (PO amendment #2)")

        # ---------------------------------------------------------------- campaign 2: just started
        c2 = svc.create_campaign(
            md, code="CMP-2612", name="Hilltop Mobile – Up-country Roadshow", client="Hilltop Telecom",
            start_date=today + timedelta(days=7), end_date=today + timedelta(days=21),
            lines=[("Venue & permits", D("250000")), ("Production & fabrication", D("900000")),
                   ("Printing & branding", D("200000")), ("Promoters & staff", D("300000")),
                   ("Transport & logistics", D("400000"))],
        )
        L2 = {l.category: l for l in c2.budget_lines.all()}
        svc.submit_request(kasun, campaign=c2, budget_line=L2["Production & fabrication"], kind="supplier",
                           supplier=sup["Lanka Stage & Sound"], amount=D("300000"), purpose="Truck stage & sound – 1st instalment",
                           new_contract_total=D("850000"), new_contract_description="Roadshow truck stage, LED & sound",
                           instalment="first")
        svc.submit_request(dilini, campaign=c2, budget_line=L2["Transport & logistics"], kind="advance",
                           amount=D("80000"), purpose="Recce trip – Kandy, Nuwara Eliya")

        # ---------------------------------------------------------------- campaign 3: ready to close
        c3 = svc.create_campaign(
            md, code="CMP-2609", name="Ocean Breeze Soap – School Hygiene Drive", client="Ocean Breeze Consumer",
            start_date=today - timedelta(days=40), end_date=today - timedelta(days=25),
            lines=[("Printing & branding", D("150000")), ("Promoters & staff", D("200000")),
                   ("Transport & logistics", D("120000"))],
        )
        L3 = {l.category: l for l in c3.budget_lines.all()}
        r = svc.submit_request(kasun, campaign=c3, budget_line=L3["Printing & branding"], kind="supplier",
                               supplier=sup["PrintHub Colombo"], amount=D("138000"), purpose="Posters & handbooks")
        svc.decide_request(md, r, "approve")
        svc.record_payment(fin, r, amount=D("138000"), paid_on=today - timedelta(days=38), method="transfer", reference="BOC-TT-80011")
        Attachment.objects.create(request=r, kind="bill", uploaded_by=fin, file=_file("printhub-inv-2201.txt", "Invoice 138,000"))
        svc.verify_bill(fin, r)
        r = svc.submit_request(dilini, campaign=c3, budget_line=L3["Promoters & staff"], kind="advance",
                               amount=D("180000"), purpose="Presenter fees – 12 schools")
        svc.decide_request(md, r, "approve")
        svc.record_payment(fin, r, amount=D("180000"), paid_on=today - timedelta(days=36), method="cash", reference="PV-1101")
        Attachment.objects.create(request=r, kind="receipt", uploaded_by=dilini, file=_file("presenter-receipts.txt", "Fees 171,000"))
        svc.submit_settlement(dilini, r, spent_amount=D("171000"), cash_returned=D("9000"))
        svc.verify_settlement(fin, r)
        svc.set_campaign_status(fin, c3, Campaign.Status.CLOSING)

        self.stdout.write(self.style.SUCCESS(
            "Demo data loaded. Log in as md / finance / kasun / dilini / admin with password 'demo1234'."))
