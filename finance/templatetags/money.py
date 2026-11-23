from decimal import Decimal

from django import template

register = template.Library()


@register.filter
def lkr(value):
    """Format a number as 'LKR 1,234,567' (2dp only when there are cents)."""
    if value in (None, ""):
        return "—"
    d = Decimal(str(value))
    s = f"{d:,.2f}"
    if s.endswith(".00"):
        s = s[:-3]
    return f"LKR {s}"


@register.filter
def num(value):
    if value in (None, ""):
        return "—"
    d = Decimal(str(value))
    s = f"{d:,.2f}"
    return s[:-3] if s.endswith(".00") else s


@register.filter
def neg(value):
    try:
        return Decimal(str(value)) < 0
    except Exception:
        return False


@register.filter
def pct_bar(value):
    try:
        return max(0, min(100, float(value)))
    except Exception:
        return 0


@register.filter
def simple_md(text):
    """Tiny, safe markdown for assistant answers: **bold**, `code`, '- ' bullets, line breaks."""
    import re

    from django.utils.html import escape
    from django.utils.safestring import mark_safe

    out, in_list = [], False
    for raw in escape(text or "").splitlines():
        line = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", raw)
        line = re.sub(r"`([^`]+)`", r"<code>\1</code>", line)
        m = re.match(r"^\s*(?:[-*•]|\d+\.)\s+(.*)", line)
        if m:
            if not in_list:
                out.append("<ul style='margin:.3rem 0;padding-left:1.2rem'>")
                in_list = True
            out.append(f"<li>{m.group(1)}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        out.append(line + "<br>" if line.strip() else "<br>")
    if in_list:
        out.append("</ul>")
    return mark_safe("".join(out).rstrip("<br>"))
