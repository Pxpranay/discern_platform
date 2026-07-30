"""Indian numbering for currency.

₹55,00,000 rather than ₹5,500,000 — the lakh/crore grouping every reader of
these figures actually uses. Django's ``intcomma`` groups in thousands, which
is wrong for this audience.
"""

from decimal import Decimal, InvalidOperation

from django import template

register = template.Library()


@register.filter
def rupees(value):
    """Format a number with Indian digit grouping. No decimals — these are
    project figures, not invoice lines."""
    if value in (None, ""):
        return "—"
    try:
        amount = Decimal(str(value)).quantize(Decimal("1"))
    except (InvalidOperation, ValueError):
        return value

    sign = "-" if amount < 0 else ""
    digits = str(abs(amount))

    if len(digits) <= 3:
        return f"{sign}₹{digits}"

    last3, rest = digits[-3:], digits[:-3]
    groups = []
    while len(rest) > 2:
        groups.insert(0, rest[-2:])
        rest = rest[:-2]
    if rest:
        groups.insert(0, rest)
    return f"{sign}₹{','.join(groups)},{last3}"


@register.filter
def qty(value):
    """Trim trailing zeros from a quantity without lapsing into exponent form."""
    if value is None:
        return "—"
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return value
