"""Immutable claim snapshots shared by the letter, preview and mail draft."""
from decimal import Decimal
from urllib.parse import quote, urlencode

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.template.loader import render_to_string

from .price_review import claim_total


def document_context(claim):
    invoices, deliveries = {}, {}
    for line in claim.lines.all():
        data = line.snapshot
        if line.category == 'delivery':
            deliveries.setdefault(data['delivery_id'], {
                'number': data.get('delivery_number'), 'date': data.get('delivery_date'), 'lines': []
            })['lines'].append(data)
            continue
        group = invoices.setdefault(data['invoice_id'], {
            'number': data['invoice_number'], 'date': data['invoice_date'], 'lines': [], 'total': Decimal('0')
        })
        group['lines'].append(data)
        group['total'] += Decimal(data['impact'])
    return {'claim': claim, 'invoices': list(invoices.values()), 'deliveries': list(deliveries.values()),
            'has_priced_lines': bool(invoices), 'document_total': claim_total(claim)}


def mail_content(claim):
    recipient = claim.fournisseur.email.strip()
    try:
        validate_email(recipient)
    except ValidationError:
        recipient = ''
    # Headers must stay on one line, even for a supplier name edited outside forms.
    subject = ' '.join(f'Réclamation n° {claim.pk} — {claim.fournisseur.nom}'.splitlines())
    body = render_to_string('core/claim_email.txt', document_context(claim)).strip()
    query = urlencode({'subject': subject, 'body': body.replace('\r\n', '\n').replace('\n', '\r\n')}, quote_via=quote)
    return {'recipient': recipient, 'subject': subject, 'body': body,
            'url': f'mailto:{quote(recipient, safe="@")}?{query}' if recipient else '',
            'test_address': recipient.lower().endswith('.invalid')}
