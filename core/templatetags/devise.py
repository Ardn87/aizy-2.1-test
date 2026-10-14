from decimal import Decimal, InvalidOperation

from django import template
from django.utils.formats import number_format

register = template.Library()


@register.filter
def montant_devise(value, devise):
    if value in (None, ""):
        return ""

    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return f"{value} {devise or ''}".strip()

    formatted_amount = number_format(
        amount,
        decimal_pos=2,
        use_l10n=True,
        force_grouping=False,
    )
    return f"{formatted_amount} {devise or ''}".strip()
