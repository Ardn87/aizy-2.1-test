from django.utils import timezone
from core.models import Facture, BonLivraison, CatalogueFournisseur
from .catalogue_control import select_control_catalogues


def catalogue_health(supplier):
    today = timezone.localdate()
    catalogues = list(supplier.catalogues.all())
    valid = select_control_catalogues(catalogues, today)
    if not catalogues: label, state = 'Catalogue absent', 'missing'
    elif not valid:
        if any(c.statut == 'analyse_echouee' for c in catalogues): label, state = 'Analyse à relancer', 'failed'
        elif any(not c.date_debut or c.devise == 'UNK' for c in catalogues): label, state = 'Période ou devise à compléter', 'incomplete'
        elif any(c.statut == 'valide' and c.date_debut and c.date_debut > today for c in catalogues): label, state = 'Catalogue à venir', 'future'
        elif any(c.statut == 'valide' for c in catalogues): label, state = 'Catalogue expiré', 'expired'
        else: label, state = 'Catalogue à valider', 'pending'
    elif len(valid) > 1: label, state = 'Périodes ambiguës', 'ambiguous'
    else:
        lines = list(valid[0].lignes.all())
        identifiers = []; incomplete = valid[0].devise == 'UNK' or not any(l.statut_ligne != 'ignoree' for l in lines)
        for line in lines:
            if line.statut_ligne == 'ignoree': continue
            identifiers.extend(('ref:' + line.reference_produit.strip(),) if line.reference_produit else ())
            identifiers.extend(('ean:' + line.ean.strip(),) if line.ean else ())
            incomplete |= line.requires_verification() or not line.unite.strip()
        if len(identifiers) != len(set(identifiers)): label, state = 'Références ambiguës', 'ambiguous'
        elif incomplete: label, state = 'Produits à compléter', 'incomplete'
        elif valid[0].is_initial_reference: label, state = 'Référence initiale active', 'ready'
        else: label, state = 'Catalogue prêt', 'ready'
    blocked = 0
    for invoice in supplier.factures.all():
        if invoice.statut == 'analyse_en_cours' or invoice.kind == 'credit': continue
        date = invoice.date_livraison_detectee or invoice.date_facture
        eligible = select_control_catalogues(catalogues, date)
        if len(eligible) != 1 or not date or invoice.devise == 'UNK' or eligible[0].devise != invoice.devise: blocked += 1
    return {'label': label, 'state': state, 'blocked': blocked, 'catalogue': valid[0] if len(valid)==1 else None}


def archive_document(document):
    if getattr(document, 'archived_at', None): return
    document.archived_at = timezone.now()
    document.save(update_fields=['archived_at', 'date_modification'])
    from core.models import AnalysisJob
    kind = {'facture':'facture','bonlivraison':'bon_livraison','cataloguefournisseur':'catalogue'}[document._meta.model_name]
    AnalysisJob.objects.filter(document_type=kind, document_id=document.pk, state='queued').update(state='failed', error='Document archivé', finished_at=timezone.now())


def allocate(invoice, invoice_line_id, delivery_line_id, quantity, user):
    from .reconciliation import allocate_delivery
    return allocate_delivery(invoice, invoice_line_id, delivery_line_id, quantity, user)


def reconciliation(invoice):
    from .reconciliation import reconciliation_state
    return reconciliation_state(invoice)['issues']
