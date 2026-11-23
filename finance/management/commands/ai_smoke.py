"""Live test of AI document reading against generated documents with known answers.

    python manage.py ai_smoke --out eval_reports

Generates a phone-photo-style receipt (JPEG) and a supplier invoice (PDF), attaches them to
seeded demo requests, has the configured model read them, and scores every extracted field.
Needs a real OPENAI_API_KEY (or ANTHROPIC_API_KEY with AI_PROVIDER=anthropic) and demo data.
"""
import io
import json
import time
from datetime import date, timedelta
from pathlib import Path

from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError

from finance import ai, services as svc
from finance.models import Attachment, PaymentRequest


def _font(size):
    from PIL import ImageFont

    for name in ("DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def make_receipt_jpeg(vendor, number, when, items, rotate=2.5):
    """A narrow thermal-printer style receipt, slightly rotated like a phone photo."""
    from PIL import Image, ImageDraw, ImageFilter

    w, line = 560, 34
    h = 380 + line * len(items)
    img = Image.new("RGB", (w, h), (250, 248, 240))
    d = ImageDraw.Draw(img)
    f, fb = _font(22), _font(26)
    y = 20
    for text, font in [(vendor.upper(), fb), ("No. 45, Galle Road, Colombo 03", f), ("Tel: 011 255 7788", f)]:
        d.text((w // 2, y), text, fill="black", font=font, anchor="mt"); y += line
    y += 10
    d.text((20, y), f"Receipt No: {number}", fill="black", font=f); y += line
    d.text((20, y), f"Date: {when:%d/%m/%Y}  14:32", fill="black", font=f); y += line + 6
    d.line((20, y, w - 20, y), fill="black", width=2); y += 12
    total = 0
    for desc, qty, unit in items:
        amt = qty * unit
        total += amt
        d.text((20, y), f"{desc} x{qty}", fill="black", font=f)
        d.text((w - 20, y), f"{amt:,.2f}", fill="black", font=f, anchor="ra"); y += line
    d.line((20, y + 4, w - 20, y + 4), fill="black", width=2); y += 18
    d.text((20, y), "TOTAL Rs.", fill="black", font=fb)
    d.text((w - 20, y), f"{total:,.2f}", fill="black", font=fb, anchor="ra"); y += line + 10
    d.text((w // 2, y), "Thank you! Come again", fill="black", font=f, anchor="mt")
    canvas = Image.new("RGB", (w + 160, h + 160), (120, 110, 95))  # table background
    canvas.paste(img, (80, 80))
    canvas = canvas.rotate(rotate, expand=True, fillcolor=(120, 110, 95)).filter(ImageFilter.GaussianBlur(0.6))
    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=80)
    return buf.getvalue(), total


def make_invoice_pdf(vendor, number, when, items, vat_rate=0.18):
    """An A4 supplier tax invoice rendered to PDF (image-based, like a scanned invoice)."""
    from PIL import Image, ImageDraw

    w, h = 1240, 1754
    img = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)
    f, fb, ft = _font(28), _font(34), _font(48)
    d.text((80, 80), vendor, fill="black", font=ft)
    d.text((80, 150), "Event Staging, Sound & Lighting", fill="black", font=f)
    d.text((80, 190), "VAT Reg: 114567890-7000", fill="black", font=f)
    d.text((80, 280), "TAX INVOICE", fill="black", font=fb)
    d.text((80, 335), f"Invoice No: {number}", fill="black", font=f)
    d.text((80, 375), f"Date: {when:%d %B %Y}", fill="black", font=f)
    d.text((80, 435), "Bill to: Brand Activation Agency (Pvt) Ltd, Colombo 05", fill="black", font=f)
    y = 520
    d.rectangle((80, y, w - 80, y + 54), outline="black", width=2)
    d.text((100, y + 12), "Description", fill="black", font=f)
    d.text((680, y + 12), "Qty", fill="black", font=f)
    d.text((900, y + 12), "Rate", fill="black", font=f, anchor="ra")
    d.text((w - 100, y + 12), "Amount", fill="black", font=f, anchor="ra")
    y += 80
    sub = 0
    for desc, qty, unit in items:
        amt = qty * unit
        sub += amt
        d.text((100, y), desc, fill="black", font=f)
        d.text((680, y), str(qty), fill="black", font=f)
        d.text((900, y), f"{unit:,.2f}", fill="black", font=f, anchor="ra")
        d.text((w - 100, y), f"{amt:,.2f}", fill="black", font=f, anchor="ra")
        y += 50
    vat = round(sub * vat_rate, 2)
    total = sub + vat
    y += 30
    for label, val, font in [("Sub total", sub, f), (f"VAT {int(vat_rate*100)}%", vat, f), ("TOTAL (LKR)", total, fb)]:
        d.text((640, y), label, fill="black", font=font)
        d.text((w - 100, y), f"{val:,.2f}", fill="black", font=font, anchor="ra")
        y += 55
    d.text((80, h - 200), "Authorised signature: ____________________", fill="black", font=f)
    buf = io.BytesIO()
    img.save(buf, format="PDF", resolution=150)
    return buf.getvalue(), sub, vat, total


class Command(BaseCommand):
    help = "Live-test AI document reading on generated receipts/invoices with known answers."

    def add_arguments(self, parser):
        parser.add_argument("--out", default="eval_reports")

    def handle(self, *args, out="eval_reports", **opts):
        if not ai.enabled():
            raise CommandError("No API key configured for the AI provider.")
        from django.contrib.auth.models import User

        fin = User.objects.get(username="finance")
        venue = PaymentRequest.objects.get(purpose="Mall atrium rental – 2 days")
        lorry = PaymentRequest.objects.get(purpose="Lorry hire & fuel – setup day")
        today = date.today()
        cases = []

        # 1) Invoice PDF for the venue payment: 300,000 + 18% VAT = 354,000 (payment was 320,000 → should flag)
        inv_date = today - timedelta(days=6)
        pdf, sub, vat, total = make_invoice_pdf("Lanka Stage & Sound (Pvt) Ltd", "LSS/INV/4471", inv_date,
                                                [("Mall atrium rental - Day 1", 1, 150000), ("Mall atrium rental - Day 2", 1, 150000)])
        cases.append(dict(name="Supplier tax invoice (PDF)", request=venue, kind="bill", filename="lss-invoice-4471.pdf", raw=pdf,
                          truth={"document_type": {"invoice"}, "vendor_contains": "lanka stage", "document_number": "LSS/INV/4471",
                                 "document_date": inv_date.isoformat(), "currency": "LKR", "total_amount": total, "tax_amount": vat},
                          expect_warning="Bill total"))

        # 2) Receipt photo for the lorry advance (two receipts expected; this one = fuel)
        rc_date = today - timedelta(days=2)
        jpg, rtotal = make_receipt_jpeg("Ceypetco Fuel Station Kiribathgoda", "R-008812", rc_date,
                                        [("Diesel (L)", 40, 290), ("Engine oil 1L", 1, 2800), ("Coolant", 1, 4000)])
        cases.append(dict(name="Thermal receipt photo (JPEG, rotated)", request=lorry, kind="receipt", filename="fuel-receipt.jpg", raw=jpg,
                          truth={"document_type": {"receipt", "invoice"}, "vendor_contains": "ceypetco", "document_number": "R-008812",
                                 "document_date": rc_date.isoformat(), "currency": "LKR", "total_amount": rtotal, "tax_amount": None},
                          expect_warning=None))

        results = []
        for c in cases:
            att = Attachment.objects.create(request=c["request"], kind=c["kind"], uploaded_by=fin,
                                            file=ContentFile(c["raw"], name=c["filename"]))
            t0 = time.time()
            svc.run_ai_read(fin, att)
            secs = round(time.time() - t0, 1)
            att.refresh_from_db()
            got, t = att.ai_data, c["truth"]
            fields = {}
            if att.ai_status == "done":
                fields["document_type"] = got.get("document_type") in t["document_type"]
                fields["vendor"] = t["vendor_contains"] in (got.get("vendor_name") or "").lower()
                fields["document_number"] = (got.get("document_number") or "").replace(" ", "") == t["document_number"]
                fields["document_date"] = got.get("document_date") == t["document_date"]
                fields["currency"] = (got.get("currency") or "").upper() == "LKR"
                fields["total_amount"] = got.get("total_amount") is not None and abs(float(got["total_amount"]) - t["total_amount"]) < 1
                if t["tax_amount"] is not None:
                    fields["tax_amount"] = got.get("tax_amount") is not None and abs(float(got["tax_amount"]) - t["tax_amount"]) < 1
            checks = svc.ai_checks(c["request"])
            warn_ok = None
            if c["expect_warning"]:
                warn_ok = any(lvl == "warn" and c["expect_warning"] in m for lvl, m in checks)
            results.append({"case": c["name"], "status": att.ai_status, "error": att.ai_error, "seconds": secs,
                            "model": att.ai_model, "fields": fields, "fields_correct": sum(fields.values()),
                            "fields_total": len(fields), "expected_warning_raised": warn_ok, "extracted": got,
                            "truth": {k: (sorted(v) if isinstance(v, set) else v) for k, v in t.items()},
                            "checks": checks})
            self.stdout.write(f"{c['name']}: {att.ai_status} {sum(fields.values())}/{len(fields)} fields in {secs}s")

        Path(out).mkdir(parents=True, exist_ok=True)
        (Path(out) / "ai_smoke.json").write_text(json.dumps(results, indent=2, default=str))
        md = ["# AI document reading – live smoke test", "", f"Model: `{ai.model_name()}` (provider `{ai.provider()}`)", "",
              "| Case | Status | Fields correct | Expected flag | Time |", "|---|---|---|---|---|"]
        for r in results:
            flag = "n/a" if r["expected_warning_raised"] is None else ("✅" if r["expected_warning_raised"] else "❌")
            md.append(f"| {r['case']} | {r['status']} | {r['fields_correct']}/{r['fields_total']} | {flag} | {r['seconds']}s |")
        for r in results:
            md += ["", f"## {r['case']}", ""]
            if r["error"]:
                md.append(f"Error: `{r['error']}`")
            md += [f"- {k}: {'✅' if v else '❌'} (got `{r['extracted'].get(k if k != 'vendor' else 'vendor_name')}`)"
                   for k, v in r["fields"].items()]
            md += ["", "Checks raised:"] + [f"- [{lvl}] {m}" for lvl, m in r["checks"]]
        (Path(out) / "ai_smoke.md").write_text("\n".join(md) + "\n")
