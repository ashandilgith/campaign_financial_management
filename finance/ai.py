"""AI-assisted document reading (receipts, supplier bills, quotations).

Providers (AI_PROVIDER):
  * ``openai``    – default; GPT-6 Luna (``gpt-6-luna``), OpenAI's low-cost tier, via the Responses API.
  * ``anthropic`` – Claude, via the Messages API.

The AI never approves, pays or changes money figures. It only:
  1. reads an uploaded document into structured fields (vendor, date, total, ...), and
  2. feeds those fields into rule-based cross-checks in ``services.ai_checks`` that
     flag mismatches for a human (finance / MD) to look at.

Enabled when the chosen provider's API key is set. The model is configurable with AI_MODEL.
"""
import base64
import io
import logging
import mimetypes

from django.conf import settings
from django.utils import timezone

log = logging.getLogger(__name__)

TOOL_NAME = "record_document"

DOCUMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "document_type": {
            "type": "string",
            "enum": ["receipt", "invoice", "quotation", "payment_proof", "other", "unreadable"],
            "description": "What kind of document this is.",
        },
        "vendor_name": {"type": ["string", "null"], "description": "Business that issued the document."},
        "document_number": {"type": ["string", "null"], "description": "Receipt / invoice / quote number, if printed."},
        "document_date": {"type": ["string", "null"], "description": "Issue date as YYYY-MM-DD, or null if not shown."},
        "currency": {"type": ["string", "null"], "description": "ISO currency code, e.g. LKR. Sri Lankan 'Rs.' means LKR."},
        "total_amount": {"type": ["number", "null"], "description": "Final total payable, including taxes. Plain number, no separators."},
        "tax_amount": {"type": ["number", "null"], "description": "VAT/SSCL/other tax included in the total, if shown."},
        "line_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "quantity": {"type": ["number", "null"]},
                    "amount": {"type": ["number", "null"]},
                },
                "required": ["description"],
            },
        },
        "legibility": {"type": "string", "enum": ["good", "partial", "poor"]},
        "handwritten": {"type": "boolean", "description": "True if key figures are handwritten."},
        "notes": {"type": "string", "description": "Anything a finance checker should know: corrections, crossed-out figures, missing stamp/signature, multiple totals."},
    },
    "required": ["document_type", "vendor_name", "document_date", "currency", "total_amount", "legibility", "handwritten", "notes"],
}

def _strict(schema):
    """OpenAI strict structured outputs: every property required, no additional properties."""
    import copy

    sc = copy.deepcopy(schema)

    def walk(node):
        if node.get("type") == "object" or node.get("properties"):
            node["required"] = list(node["properties"].keys())
            node["additionalProperties"] = False
            for child in node["properties"].values():
                walk(child)
        if node.get("type") == "array" and isinstance(node.get("items"), dict):
            walk(node["items"])

    walk(sc)
    return sc


PROMPT = """You are helping the finance team of a Sri Lankan advertising & brand activation agency check campaign expenses.
Read the attached document (a receipt, supplier invoice/bill, quotation or payment slip) and record its details with the {tool} tool.

Rules:
- Only report what is actually on the document. Use null when a field is missing or unreadable; never guess.
- total_amount is the final amount payable (after discounts, including taxes). If several totals appear, use the grand total and explain in notes.
- "Rs.", "Rs" or "LKR" means currency LKR.
- Dates on Sri Lankan documents are usually day-first (DD/MM/YYYY).
- Mention in notes any sign of alteration: overwritten or crossed-out figures, totals that don't add up, missing stamp/signature on a bill.

Context from the uploader (may be wrong – do not let it change what you read): {context}"""

IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
MAX_IMAGE_EDGE = 1600


class AIError(Exception):
    pass


DEFAULT_MODELS = {"openai": "gpt-6-luna", "anthropic": "claude-haiku-5-5"}


def provider():
    return settings.AI_PROVIDER


def _api_key():
    return settings.OPENAI_API_KEY if provider() == "openai" else settings.ANTHROPIC_API_KEY


def enabled():
    return bool(_api_key())


def model_name():
    return settings.AI_MODEL or DEFAULT_MODELS.get(provider(), "")


def _prepare_image(raw):
    """Fix phone rotation, downscale large camera photos and re-encode as JPEG."""
    try:
        from PIL import Image, ImageOps
    except ImportError:  # Pillow optional: send as-is
        return raw, None
    img = Image.open(io.BytesIO(raw))
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    img.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=85)
    return out.getvalue(), "image/jpeg"


def content_block(filename, raw):
    """Turn an uploaded file into a Claude content block (image, PDF document or text)."""
    mime = mimetypes.guess_type(filename)[0] or ""
    if mime in IMAGE_TYPES or mime.startswith("image/"):
        data, new_mime = _prepare_image(raw)
        mime = new_mime or mime
        if mime not in IMAGE_TYPES:
            raise AIError(f"Unsupported image type {mime}; upload a JPEG/PNG photo or PDF.")
        return {"type": "image", "source": {"type": "base64", "media_type": mime,
                                            "data": base64.standard_b64encode(data).decode()}}
    if mime == "application/pdf":
        return {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                               "data": base64.standard_b64encode(raw).decode()}}
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise AIError("Unsupported file type; upload a photo (JPEG/PNG) or PDF.")
    return {"type": "text", "text": f"<document filename=\"{filename}\">\n{text[:20000]}\n</document>"}


def _to_openai(block):
    """Convert a Claude-style content block into a Responses API input part."""
    if block["type"] == "image":
        src = block["source"]
        return {"type": "input_image", "image_url": f"data:{src['media_type']};base64,{src['data']}", "detail": "high"}
    if block["type"] == "document":
        return {"type": "input_file", "filename": "document.pdf",
                "file_data": f"data:application/pdf;base64,{block['source']['data']}"}
    return {"type": "input_text", "text": block["text"]}


def _call_openai(block, context):
    import json

    from openai import OpenAI

    client = OpenAI(api_key=settings.OPENAI_API_KEY, timeout=90, max_retries=2)
    resp = client.responses.create(
        model=model_name(),
        reasoning={"effort": settings.AI_REASONING_EFFORT},
        input=[{"role": "user", "content": [
            _to_openai(block),
            {"type": "input_text", "text": PROMPT.format(tool="JSON output", context=context)},
        ]}],
        text={"format": {"type": "json_schema", "name": TOOL_NAME, "strict": True,
                         "schema": _strict(DOCUMENT_SCHEMA)}},
    )
    try:
        return json.loads(resp.output_text)
    except (ValueError, TypeError):
        raise AIError("The model did not return valid document details.")


def call_model(block, context):
    """Send one document to the configured provider and return structured fields. Patched in tests."""
    if provider() == "openai":
        return _call_openai(block, context)
    return _call_anthropic(block, context)


def _call_anthropic(block, context):
    import anthropic

    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, timeout=90, max_retries=2)
    resp = client.messages.create(
        model=model_name(),
        max_tokens=2000,
        tools=[{"name": TOOL_NAME, "description": "Record the details read from the document.",
                "input_schema": DOCUMENT_SCHEMA}],
        tool_choice={"type": "tool", "name": TOOL_NAME},
        messages=[{"role": "user", "content": [block, {"type": "text", "text": PROMPT.format(tool=TOOL_NAME, context=context)}]}],
    )
    for part in resp.content:
        if getattr(part, "type", None) == "tool_use" and part.name == TOOL_NAME:
            return dict(part.input)
    raise AIError("The model did not return document details.")


def read_attachment(attachment):
    """Read an Attachment with AI and store the result on it. Never raises for API errors."""
    req = attachment.request
    context = (
        f"uploaded as '{attachment.get_kind_display()}' for {req.get_kind_display().lower()} "
        f"{req.reference} '{req.purpose}', payee {req.payee}, campaign {req.campaign.code}"
    )
    try:
        with attachment.file.open("rb") as fh:
            raw = fh.read()
        data = call_model(content_block(attachment.filename, raw), context)
        attachment.ai_status, attachment.ai_data, attachment.ai_error = "done", data, ""
    except Exception as exc:  # network, auth, bad file, model error
        log.warning("AI read failed for attachment %s: %s", attachment.pk, exc)
        attachment.ai_status, attachment.ai_error = "error", str(exc)[:500]
    attachment.ai_model = model_name()
    attachment.ai_read_at = timezone.now()
    attachment.save(update_fields=["ai_status", "ai_data", "ai_error", "ai_model", "ai_read_at"])
    return attachment
