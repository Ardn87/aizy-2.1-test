"""Counts based on the actual, restaurant-scoped business queues."""
from core.models import Facture, BonLivraison, CatalogueFournisseur, DeliveryIssue, SupplierClaim
from .price_review import review_queue
from .automatic_reconciliation import queue_bucket
from .reconciliation import reconciliation_state


def home_summary():
    tasks, alerts = review_queue('all')
    blocked = {('facture',alert['invoice'].pk) for alert in alerts}
    reconcile = 0
    for invoice in Facture.objects.filter(kind='invoice',document_role='active').select_related('fournisseur__restaurant').prefetch_related('lignes'):
        state = reconciliation_state(invoice)
        reconcile += queue_bucket(invoice,state) == 'attention'
        if state['state'] == 'review' and invoice.statut not in ('uploade','analyse_en_cours','analyse_echouee'):
            blocked.add(('facture',invoice.pk))
    failed = set()
    for kind, model in [('facture',Facture),('bon_livraison',BonLivraison),('catalogue',CatalogueFournisseur)]:
        failed.update((kind,pk) for pk in model.objects.filter(statut='analyse_echouee').values_list('pk',flat=True))
    blocked -= failed
    return {
        'price':sum(task['queue_state']=='pending' for task in tasks),
        'reconcile':reconcile,
        'delivery_issues':DeliveryIssue.objects.filter(
            state=DeliveryIssue.State.PENDING, delivery__archived_at__isnull=True).count(),
        'claims':SupplierClaim.objects.exclude(state__in=['closed','cancelled']).filter(lines__isnull=False).distinct().count(),
        'failed':len(failed), 'blocked':len(blocked), 'attention':len(failed | blocked),
    }
