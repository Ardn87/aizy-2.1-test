"""Prepare one reviewable invoice proposal; only an explicit decision writes links."""
from collections import Counter
from decimal import Decimal
from itertools import combinations

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from core.models import Fournisseur, Facture, BusinessAudit
from . import reconciliation as service


def _choose(choices, remaining):
    """Only choose a unique combination; dates/order never break an ambiguous tie."""
    best = max(c['rank'] for c in choices)
    choices = [c for c in choices if c['rank'] == best]
    if len(choices) == 1:
        return choices
    total = sum(c['available'] * c['factor'] for c in choices)
    if total <= remaining:
        return choices
    # Bounded exhaustive search, not a truncated candidate list presented as certain.
    if len(choices) > 12:
        return []
    solution = None
    for size in range(1, len(choices) + 1):
        for subset in combinations(choices, size):
            if sum(c['available'] * c['factor'] for c in subset) == remaining:
                if solution is not None:
                    return []
                solution = list(subset)
    return solution or []


def build_plan(invoice, state=None):
    state = state or service.reconciliation_state(invoice)
    basis = service.review_token(invoice, include_decision=False)
    plan = {'entries': [], 'unresolved': [], 'documents': [], 'line_count': 0,
            'complete': False, 'rejected': False, 'blocked': '', 'warnings': []}
    if invoice.kind != 'invoice' or invoice.document_role != 'active':
        plan['blocked'] = 'Ce document est exclu du rapprochement.'
    elif invoice.statut in ('analyse_en_cours', 'analyse_echouee') or not state['total']:
        plan['blocked'] = 'Les données de la facture doivent être analysées ou vérifiées.'
    elif state['duplicates']:
        plan['blocked'] = 'Doublon possible : vérifiez les documents avant de valider.'
    elif state['source_stale']:
        plan['blocked'] = 'Des données ou des associations sont à vérifier avant de valider.'
    elif state['mode'] in ('invoice_only', 'combined'):
        plan['blocked'] = 'Sans BL distinct, la livraison ne peut pas être vérifiée automatiquement.'
    proposed = {} if plan['blocked'] else {
        p['line'].pk: p for p in service.suggestions(invoice, state, limit=None)}
    for row in state['issues']:
        line = row['line']
        candidates = proposed.get(line.pk, {}).get('choices', [])
        usable = [c for c in candidates if c['can_confirm'] and c['factor'] and c['quantity']
                  and not (invoice.file_sha256 and invoice.file_sha256 == c['delivery'].bon_livraison.file_sha256)]
        remaining = line.quantite - row['allocated'] - row['credited'] if line.quantite is not None else None
        selected = _choose(usable, remaining) if usable and remaining and remaining > 0 else []
        if selected:
            for choice in selected:
                amount = min(choice['available'], remaining / choice['factor'])
                if amount <= 0 or amount != amount.quantize(Decimal('.001')):
                    continue
                plan['entries'].append({**choice, 'line': line, 'quantity': amount,
                    'invoice_quantity': amount * choice['factor'],
                    'partial_delivery': amount < choice['available']})
            continue
        if not line.unite.strip() or line.quantite is None or line.quantite <= 0:
            reason = 'Une quantité ou une unité manque dans la facture ; les données doivent être vérifiées.'
        elif plan['blocked']:
            reason = plan['blocked']
        elif usable:
            reason = 'Plusieurs BL peuvent correspondre ; aucune association choisie automatiquement.'
        elif any(c['data_warning'] or not c['factor'] for c in candidates):
            reason = 'Une quantité ou une unité manque, ou le conditionnement doit être vérifié.'
        elif candidates:
            reason = 'Dates ou données des documents à vérifier avant de proposer un lien.'
        elif not state['delivery_count']:
            reason = 'Quantité reçue inconnue : aucun BL importé pour ce fournisseur.'
        else:
            reason = 'Aucune correspondance suffisamment fiable dans les BL disponibles.'
        plan['unresolved'].append({'line': line, 'reason': reason})
    # Do not allocate the same available quantity twice inside a single proposal.
    usage = Counter(e['delivery'].pk for e in plan['entries'])
    conflicts = {e['line'].pk for e in plan['entries'] if usage[e['delivery'].pk] > 1}
    for row in state['issues']:
        if row['line'].pk in conflicts:
            plan['unresolved'].append({'line': row['line'], 'reason': 'Le même BL correspond à plusieurs lignes de facture ; répartition à vérifier.'})
    plan['entries'] = [e for e in plan['entries'] if e['line'].pk not in conflicts]
    covered = {}
    documents = {}
    for entry in plan['entries']:
        covered[entry['line'].pk] = covered.get(entry['line'].pk, 0) + entry['invoice_quantity']
        doc = entry['delivery'].bon_livraison
        documents[doc.pk] = doc
        if entry['partial_delivery']:
            plan['warnings'].append('Une partie seulement du BL est utilisée ; le solde reste disponible.')
        if entry['rank'] == 4:
            plan['warnings'].append('Certaines désignations sont proches : vérifiez le détail avant de valider.')
    for row in state['issues']:
        amount = covered.get(row['line'].pk)
        if amount is not None and amount + row['allocated'] + row['credited'] != row['line'].quantite:
            plan['unresolved'].append({'line': row['line'], 'reason': 'Différence de quantité : les BL trouvés ne couvrent pas toute la quantité facturée.'})
    plan['warnings'] = list(dict.fromkeys(plan['warnings']))
    plan['documents'] = list(documents.values())
    plan['line_count'] = len(covered)
    plan['complete'] = bool(plan['entries'] and not plan['unresolved'] and not plan['blocked'])
    plan['signature'] = service.digest({'basis': basis, 'entries': [
        [e['line'].pk, e['delivery'].pk, e['quantity'], e['factor']] for e in plan['entries']]})
    plan['rejected'] = invoice.reconciliation_decision.get('state') == 'rejected' and invoice.reconciliation_decision.get('signature') == plan['signature']
    if plan['rejected']:
        plan['label'] = 'Proposition refusée'
    elif plan['entries']:
        plan['label'] = 'Rapprochement prêt à valider' if plan['complete'] else 'Rapprochement partiel à valider'
    else:
        plan['label'] = state['label']
    return plan


@transaction.atomic
def decide(invoice, signature, action, user):
    Fournisseur.objects.select_for_update().get(pk=invoice.fournisseur_id)
    invoice = Facture.objects.select_for_update().select_related('fournisseur__restaurant').get(pk=invoice.pk)
    service.ensure_invoice(invoice)
    plan = build_plan(invoice)
    if signature != plan['signature']:
        raise ValidationError('La proposition a changé. Vérifiez le nouveau rapprochement avant de décider.')
    if action == 'restore_auto':
        if not plan['rejected']:
            raise ValidationError('Cette proposition n’est plus refusée.')
        invoice.reconciliation_decision = {}
    else:
        if plan['blocked'] or not plan['entries'] or plan['rejected']:
            raise ValidationError('Aucun rapprochement disponible à valider ou à refuser.')
        if action == 'approve_auto':
            for entry in plan['entries']:
                service.allocate_delivery(invoice, entry['line'].pk, entry['delivery'].pk,
                    str(entry['quantity']), user, str(entry['factor']),
                    entry['reason'] or 'Rapprochement automatique vérifié et validé sur la facture')
        elif action != 'reject_auto':
            raise ValidationError('Décision inconnue.')
        invoice.reconciliation_decision = {'state': 'approved' if action == 'approve_auto' else 'rejected',
            'signature': signature, 'actor': user.pk, 'at': timezone.now().isoformat()}
    invoice.save(update_fields=['reconciliation_decision'])
    BusinessAudit.objects.create(restaurant=invoice.fournisseur.restaurant, actor=user,
        model_label='core.facture', object_id=invoice.pk, action=action,
        after={'decision': invoice.reconciliation_decision, 'associations': [
            {'invoice_line': e['line'].pk, 'delivery_line': e['delivery'].pk,
             'quantity': str(e['quantity']), 'factor': str(e['factor'])} for e in plan['entries']]})
    return plan


def queue_bucket(invoice, state, plan=None):
    """Classify without building a proposal when the actionable bucket is known."""
    bucket = state['state']
    if bucket not in ('waiting','matched','unverified'):
        return 'attention'
    plan = plan if plan is not None else build_plan(invoice,state)
    return 'attention' if plan['entries'] else bucket


def queue_entry(invoice):
    """The same actionable bucket is used by reconciliation and home counters."""
    state = service.reconciliation_state(invoice)
    plan = build_plan(invoice, state)
    bucket = queue_bucket(invoice,state,plan)
    if plan['entries']:
        state = {**state, 'label':plan['label'], 'state':'pending'}
    return {'invoice':invoice, 'state':state, 'bucket':bucket}
