from typing import Any

from django import template

register = template.Library()


@register.filter
def risk_display(finding: Any) -> str:
    if finding.risk_amount_minor is None:
        return "—"
    factor = 10**finding.exponent
    whole, remainder = divmod(finding.risk_amount_minor, factor)
    amount = f"{whole:,}" + (f".{remainder:0{finding.exponent}d}" if finding.exponent else "")
    return f"{finding.currency} {amount}"
