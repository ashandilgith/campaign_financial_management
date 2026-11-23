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
