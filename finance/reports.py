"""Campaign closure report as a PDF, ready to send to the client.

Replaces the manual step Arun described: pasting settled bills into a report so
the client can close the activation and the agency can invoice its commission.

The PDF has: a summary against the client PO, budget versus actual by category,
an expense schedule (every payment with its evidence), advance settlements, an
invoice summary (verified spend plus agency commission), the closure checklist
and an appendix with a copy of every supporting bill and receipt (photos are
placed on the page, PDF bills are merged in after their cover page).

All figures come from ``services.campaign_position`` – the same numbers as the
screens – so the report can never disagree with the app.
"""
from __future__ import annotations

import io
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (Flowable, Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer,
                                Table, TableStyle)
from xml.sax.saxutils import escape

from . import services as svc
from .models import Attachment, PaymentRequest, Settlement

ZERO = Decimal("0")
INK = colors.HexColor("#1f2933")
MUTED = colors.HexColor("#6b7280")
RULE = colors.HexColor("#d9dde3")
HEAD_BG = colors.HexColor("#eef2f7")
ACCENT = colors.HexColor("#1d4ed8")
WARN_BG = colors.HexColor("#fff4e0")
EVIDENCE_KINDS = (Attachment.Kind.BILL, Attachment.Kind.RECEIPT)

styles = getSampleStyleSheet()
S_BODY = ParagraphStyle("body", parent=styles["Normal"], fontName="Helvetica", fontSize=9, leading=12, textColor=INK)
S_SMALL = ParagraphStyle("small", parent=S_BODY, fontSize=7.5, leading=9.5, textColor=MUTED)
S_CELL = ParagraphStyle("cell", parent=S_BODY, fontSize=8, leading=10)
S_CELL_R = ParagraphStyle("cellr", parent=S_CELL, alignment=TA_RIGHT)
S_H1 = ParagraphStyle("h1", parent=S_BODY, fontName="Helvetica-Bold", fontSize=17, leading=21, spaceAfter=2)
S_H2 = ParagraphStyle("h2", parent=S_BODY, fontName="Helvetica-Bold", fontSize=11.5, leading=15, spaceBefore=10,
                      spaceAfter=5)
S_KPI_L = ParagraphStyle("kpil", parent=S_BODY, fontSize=7.5, leading=9, textColor=MUTED)
S_KPI_V = ParagraphStyle("kpiv", parent=S_BODY, fontName="Helvetica-Bold", fontSize=12, leading=15)


def n(amount) -> str:
    """1234567.5 -> '1,234,567.50'; negatives in brackets, the accountant's way."""
    d = Decimal(amount or 0).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    s = f"{abs(d):,.2f}"
    return f"({s})" if d < 0 else s


def p(text, style=S_CELL):
    return Paragraph(escape(str(text)), style)


def table(data, widths, header=True, total=False, align_right_from=None):
    t = Table(data, colWidths=widths, repeatRows=1 if header else 0)
    st = [("FONTNAME", (0, 0), (-1, -1), "Helvetica"), ("FONTSIZE", (0, 0), (-1, -1), 8),
          ("TEXTCOLOR", (0, 0), (-1, -1), INK), ("VALIGN", (0, 0), (-1, -1), "TOP"),
          ("LINEBELOW", (0, 0), (-1, -1), 0.4, RULE), ("TOPPADDING", (0, 0), (-1, -1), 3),
          ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
    if header:
        st += [("BACKGROUND", (0, 0), (-1, 0), HEAD_BG), ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold")]
    if total:
        st += [("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"), ("LINEABOVE", (0, -1), (-1, -1), 0.8, INK)]
    if align_right_from is not None:
        st += [("ALIGN", (align_right_from, 0), (-1, -1), "RIGHT")]
    t.setStyle(TableStyle(st))
    return t


class PageMarker(Flowable):
    """Zero-size flowable that records the page it lands on, so PDF bills can be merged after it."""

    def __init__(self, key, registry):
        super().__init__()
        self.key, self.registry = key, registry

    def wrap(self, *_):
        return 0, 0

    def draw(self):
        self.registry[self.key] = self.canv.getPageNumber()


def _evidence_label(req):
    docs = [a for a in req.attachments.all() if a.kind in EVIDENCE_KINDS]
    numbers = [a.ai_data.get("document_number") for a in docs if a.ai_status == "done" and a.ai_data.get("document_number")]
    parts = []
    if req.kind == PaymentRequest.Kind.SUPPLIER:
        parts.append("Bill verified" if req.bill_verified else "Bill not verified")
    elif req.kind == PaymentRequest.Kind.REIMBURSE:
        parts.append(f"Reimburses overspend on {req.linked_advance.reference}" if req.linked_advance else "Reimbursement")
    else:
        s = getattr(req, "settlement", None)
        parts.append("Settled, verified" if s and s.status == Settlement.Status.VERIFIED else "Not settled")
    if numbers:
        parts.append("No. " + ", ".join(numbers[:2]))
    if docs:
        parts.append(f"{len(docs)} doc{'s' if len(docs) != 1 else ''} (App. {req.reference})")
    return "; ".join(parts)


def _verified_spend(req):
    if req.kind == PaymentRequest.Kind.SUPPLIER:
        return req.paid_amount if req.bill_verified else ZERO
    s = getattr(req, "settlement", None)
    return s.spent_amount if s and s.status == Settlement.Status.VERIFIED else ZERO


def build_campaign_report(campaign, prepared_by=None) -> bytes:
    rows, total = svc.campaign_position(campaign)
    checks, ready, _ = svc.closure_checklist(campaign)
    paid = list(campaign.requests.filter(payment__isnull=False)
                .select_related("payment", "budget_line", "supplier", "requested_by", "settlement")
                .prefetch_related("attachments").order_by("payment__paid_on", "pk"))
    cur = settings.CURRENCY
    agency = getattr(settings, "REPORT_AGENCY_NAME", "")
    generated = timezone.localtime().strftime("%d %b %Y %H:%M")
    draft = not ready

    def on_page(canv, doc):
        canv.saveState()
        w, h = A4
        canv.setFont("Helvetica", 7.5)
        canv.setFillColor(MUTED)
        canv.drawString(15 * mm, h - 10 * mm, f"{agency} · Campaign closure report · {campaign.code}")
        canv.drawRightString(w - 15 * mm, h - 10 * mm, f"Generated {generated}")
        canv.drawString(15 * mm, 9 * mm, f"{campaign.client or ''}{' · PO ' + campaign.client_po_number if campaign.client_po_number else ''}")
        canv.drawRightString(w - 15 * mm, 9 * mm, f"Page {doc.page}")
        if draft:
            canv.setFont("Helvetica-Bold", 60)
            canv.setFillColor(colors.Color(0.85, 0.2, 0.2, alpha=0.08))
            canv.translate(w / 2, h / 2)
            canv.rotate(45)
            canv.drawCentredString(0, 0, "DRAFT")
        canv.restoreState()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=16 * mm,
                            bottomMargin=15 * mm, title=f"{campaign.code} closure report", author=agency)
    W = A4[0] - 30 * mm
    story = []

    # ---------------------------------------------------------------- title + KPIs
    story += [Paragraph("Campaign closure report", S_SMALL), Paragraph(escape(campaign.name), S_H1)]
    period = " – ".join(d.strftime("%d %b %Y") for d in (campaign.start_date, campaign.end_date) if d) or "—"
    meta = [["Client", campaign.client or "—", "Campaign code", campaign.code],
            ["Client PO", campaign.client_po_number or "—", "Period", period],
            ["Status", campaign.get_status_display(), "Prepared by", (prepared_by.get_full_name() or prepared_by.username) if prepared_by else agency]]
    mt = Table(meta, colWidths=[22 * mm, 68 * mm, 26 * mm, W - 116 * mm])
    mt.setStyle(TableStyle([("FONTSIZE", (0, 0), (-1, -1), 8.5), ("TEXTCOLOR", (0, 0), (0, -1), MUTED),
                            ("TEXTCOLOR", (2, 0), (2, -1), MUTED), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
    story += [mt, Spacer(1, 6)]
    if draft:
        open_items = "; ".join(f"{label} ({cnt} open)" for label, ok, cnt in checks if not ok)
        warn = Table([[Paragraph(f"<b>Draft – not ready for closure.</b> Checks not yet met: {escape(open_items)}.",
                                 S_CELL)]], colWidths=[W])
        warn.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), WARN_BG), ("BOX", (0, 0), (-1, -1), 0.5,
                                                                                    colors.HexColor("#f0c36d"))]))
        story += [warn, Spacer(1, 6)]

    commission_pct = campaign.commission_pct or ZERO
    commission = (total.verified * commission_pct / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    kpis = [("Approved budget (PO)", total.budget), ("Cash paid, net", total.cash_out),
            ("Verified spend", total.verified), ("Under / (over) budget", total.variance)]
    cells = [[Paragraph(label, S_KPI_L) for label, _ in kpis], [Paragraph(f"{cur} {n(v)}", S_KPI_V) for _, v in kpis]]
    kt = Table(cells, colWidths=[W / 4] * 4)
    kt.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.5, RULE), ("INNERGRID", (0, 0), (-1, -1), 0.5, RULE),
                            ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    story += [kt]

    # ---------------------------------------------------------------- budget vs actual
    story.append(Paragraph(f"Budget versus actual ({cur})", S_H2))
    data = [["Category", "Budget", "Cash paid (net)", "Verified spend", "Under / (over)", "Still committed"]]
    for line, f in rows:
        data.append([p(line.category), n(f.budget), n(f.cash_out), n(f.verified), n(f.variance),
                     n(f.committed + f.pending)])
    data.append(["Total", n(total.budget), n(total.cash_out), n(total.verified), n(total.variance),
                 n(total.committed + total.pending)])
    story.append(table(data, [W - 5 * 26 * mm] + [26 * mm] * 5, total=True, align_right_from=1))
    notes = []
    if total.advances_outstanding:
        notes.append(f"Advances not yet settled: {cur} {n(total.advances_outstanding)}.")
    if total.reimbursements_due:
        notes.append(f"Reimbursements due to staff for advance overspends: {cur} {n(total.reimbursements_due)}.")
    notes.append("Verified spend counts supplier payments with a checked bill and staff advances with verified receipts.")
    story.append(Paragraph(" ".join(notes), S_SMALL))

    # ---------------------------------------------------------------- expense schedule
    story.append(Paragraph("Expense schedule", S_H2))
    data = [["Ref", "Paid on", "Payee", "Category / purpose", "Paid", "Verified", "Evidence"]]
    for r in paid:
        data.append([r.reference, r.payment.paid_on.strftime("%d %b"), p(r.payee),
                     p(f"{r.budget_line.category}: {r.purpose}"), n(r.payment.amount), n(_verified_spend(r)),
                     p(_evidence_label(r), S_SMALL)])
    if len(data) == 1:
        data.append(["", "", "No payments recorded.", "", "", "", ""])
    else:
        data.append(["Total", "", "", "", n(sum((r.payment.amount for r in paid), ZERO)),
                     n(sum((_verified_spend(r) for r in paid), ZERO)), ""])
    sched = table(data, [18 * mm, 13 * mm, 30 * mm, W - 140 * mm, 22 * mm, 22 * mm, 35 * mm], total=len(data) > 2)
    sched.setStyle(TableStyle([("ALIGN", (4, 0), (5, -1), "RIGHT")]))
    story.append(sched)

    # ---------------------------------------------------------------- advances
    advances = [r for r in paid if r.kind == PaymentRequest.Kind.ADVANCE]
    if advances:
        story.append(Paragraph("Staff advance settlements", S_H2))
        data = [["Ref", "Holder", "Purpose", "Advanced", "Spent", "Returned", "Status"]]
        for r in advances:
            s = getattr(r, "settlement", None)
            data.append([r.reference, p(r.payee), p(r.purpose), n(r.payment.amount),
                         n(s.spent_amount) if s else "—", n(s.cash_returned) if s else "—",
                         p(s.get_status_display() if s else "Not submitted")])
        adv = table(data, [18 * mm, 30 * mm, W - 151 * mm, 22 * mm, 22 * mm, 22 * mm, 37 * mm])
        adv.setStyle(TableStyle([("ALIGN", (3, 0), (5, -1), "RIGHT")]))
        story.append(adv)

    # ---------------------------------------------------------------- invoice summary
    inv = [["Verified campaign spend", f"{cur} {n(total.verified)}"],
           [f"Agency commission ({commission_pct.normalize():f}%)", f"{cur} {n(commission)}"],
           ["Amount to invoice (before taxes)", f"{cur} {n(total.verified + commission)}"]]
    it = table(inv, [W - 50 * mm, 50 * mm], header=False, total=True, align_right_from=1)
    story += [KeepTogether([Paragraph("Invoice summary", S_H2), it,
                            Paragraph("For the client's approval. The tax invoice itself is raised from the accounting "
                                      "system; VAT and SSCL are added there.", S_SMALL)])]

    # ---------------------------------------------------------------- closure checklist
    data = [["Closure check", "Status"]] + [[p(label), "Done" if ok else f"{cnt} open"] for label, ok, cnt in checks]
    story += [KeepTogether([Paragraph("Closure checklist", S_H2), table(data, [W - 30 * mm, 30 * mm])])]

    # ---------------------------------------------------------------- appendix: supporting documents
    markers, pdf_inserts = {}, []
    evidence = [(r, a) for r in paid for a in r.attachments.all() if a.kind in EVIDENCE_KINDS]
    if campaign.client_po_file:
        evidence.insert(0, (None, campaign.client_po_file))
    if evidence:
        story += [PageBreak(), Paragraph("Appendix: supporting documents", S_H2),
                  Paragraph("The client PO, then a copy of every bill and receipt on file, in the order of the "
                            "expense schedule.", S_SMALL),
                  Spacer(1, 6)]
        for i, (r, a) in enumerate(evidence):
            if r is None:   # the client PO itself
                fname = a.name.rsplit("/", 1)[-1]
                caption = f"Client PO {campaign.client_po_number or ''} · {fname}"
                fileobj = a
            else:
                fname = a.filename
                caption = f"{r.reference} · {r.payee} · {a.get_kind_display()} · {fname}"
                fileobj = a.file
            block = [Paragraph(f"<b>{escape(caption)}</b>", S_CELL), Spacer(1, 3)]
            name = fname.lower()
            try:
                with fileobj.open("rb") as fh:
                    raw = fh.read()
            except Exception:  # noqa: BLE001 – a missing file must not stop the report
                raw = None
            if raw is None:
                block.append(Paragraph("File missing from storage.", S_SMALL))
                story += [KeepTogether(block), Spacer(1, 10)]
            elif name.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
                img = _image_flowable(raw, W, A4[1] - 70 * mm)
                block.append(img if img else Paragraph("Image could not be read.", S_SMALL))
                story += [KeepTogether(block), Spacer(1, 10)]
            elif name.endswith(".pdf"):
                key = f"pdf{i}"
                block.append(Paragraph("The PDF document follows on the next page(s).", S_SMALL))
                story += [KeepTogether(block), PageMarker(key, markers), PageBreak()]
                pdf_inserts.append((key, raw))
            elif not name.endswith((".txt", ".csv")):
                block.append(Paragraph("This file type can't be reproduced in the PDF; it is kept in the system "
                                       "and available on request.", S_SMALL))
                story += [KeepTogether(block), Spacer(1, 10)]
            else:
                text = raw.decode("utf-8", "replace")[:3000]
                block.append(Paragraph(escape(text).replace("\n", "<br/>"), S_SMALL))
                story += [KeepTogether(block), Spacer(1, 10)]

    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    out = buf.getvalue()
    return _merge_pdfs(out, markers, pdf_inserts) if pdf_inserts else out


def _image_flowable(raw, max_w, max_h):
    try:
        from PIL import Image as PILImage, ImageOps
        img = ImageOps.exif_transpose(PILImage.open(io.BytesIO(raw)))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.thumbnail((1600, 1600))
        data = io.BytesIO()
        img.save(data, format="JPEG", quality=82)
        data.seek(0)
        w, h = img.size
        scale = min(max_w / w, max_h / h, 1.5)
        return Image(data, width=w * scale, height=h * scale)
    except Exception:  # noqa: BLE001
        return None


def _merge_pdfs(main_pdf, markers, inserts):
    """Insert each attached PDF's pages right after the page that introduced it."""
    from pypdf import PdfReader, PdfWriter

    main = PdfReader(io.BytesIO(main_pdf))
    after = {}
    for key, raw in inserts:
        page_no = markers.get(key)
        if page_no:
            after.setdefault(page_no, []).append(raw)
    writer = PdfWriter()
    for number, page in enumerate(main.pages, start=1):
        writer.add_page(page)
        for raw in after.get(number, []):
            try:
                for extra in PdfReader(io.BytesIO(raw)).pages:
                    writer.add_page(extra)
            except Exception:  # noqa: BLE001 – a corrupt bill PDF is skipped, the report still builds
                continue
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()
