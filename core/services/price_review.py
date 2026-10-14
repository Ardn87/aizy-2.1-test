"""Shared calculations for the review screen; no duplicate financial state in JS."""
import hashlib
import json
from collections import Counter
from core.supplier_logos import supplier_logo
from decimal import Decimal, InvalidOperation

from django.db.models import Prefetch, Q

from core.models import Facture, LigneFacture, PriceReviewDecision, CatalogueFournisseur, CataloguePriceHistory, ProductAssociation, InvoiceControlCache, SupplierClaimLine
from .catalogue_control import (control_invoice_against_catalogue, control_invoice_line, build_catalogue_indexes,
                               select_control_catalogues, CONTROL_VERSION, PRICE_TOLERANCE)


def snapshot(detail):
    line = detail['invoice_line']
    invoice = line.facture
    catline = detail.get('catalogue_line')
    cat = catline.catalogue if catline else None
    result = {
        'line_id': line.pk, 'invoice_id': invoice.pk, 'invoice_number': invoice.numero_facture,
        'invoice_date': str(invoice.date_facture or ''), 'delivery_date': str(invoice.date_livraison_detectee or ''),
        'supplier_id': invoice.fournisseur_id, 'invoice_state': invoice.statut, 'product': line.designation,
        'reference': line.reference_produit, 'ean': line.ean, 'quantity': value(line.quantite),
        'unit': line.unite, 'conversion_factor': value(detail.get('conversion_factor')), 'verified_at': str(invoice.verified_at or ''), 'invoice_price': value(line.prix_unitaire_ht), 'currency': invoice.devise,
        'catalogue_line_id': catline.pk if catline else None,
        'catalogue_id': cat.pk if cat else None,
        'catalogue_start': str(cat.date_debut or '') if cat else '',
        'catalogue_end': str(cat.date_fin or '') if cat else '',
        'catalogue_currency': cat.devise if cat else '',
        'catalogue_reference': catline.reference_produit if catline else '',
        'catalogue_ean': catline.ean if catline else '',
        'catalogue_unit': catline.unite if catline else '',
        'catalogue_price': value(detail.get('prix_catalogue')),
        'status': detail['status'],
    }
    if cat and cat.is_initial_reference:
        result.update(initial_reference=True, price_source=catline.price_source)
    return result


def value(number):
    return str(number) if number is not None else None


def fingerprint(detail):
    data = snapshot(detail)
    for key in ('invoice_state','verified_at'):
        data.pop(key, None)
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def decorate(detail, catalogue=None, verified=None):
    line = detail['invoice_line']
    detail = dict(detail)
    detail.update(fingerprint=fingerprint(detail), catalogue=catalogue, line=line,
                  invoice=line.facture, supplier=line.facture.fournisseur, impact=None,
                  initial_reference=bool(catalogue and catalogue.is_initial_reference))
    if (detail['status'] == 'ecart_prix' and line.quantite is not None
            and line.quantite >= 0 and line.prix_unitaire_ht is not None
            and detail.get('prix_catalogue') is not None
            and line.prix_unitaire_ht >= 0 and detail['prix_catalogue'] >= 0):
        detail['impact'] = (detail['ecart'] * line.quantite).quantize(Decimal('0.01'))
    from .quality import document_is_verified
    if verified is None:
        verified = document_is_verified(line.facture)
    detail['data_unverified'] = not verified
    detail['claim_blocked'] = detail['status'] == 'ecart_prix' and not (line.quantite is not None and line.quantite > 0 and line.facture.numero_facture and line.facture.date_facture and line.unite.strip() and detail.get('catalogue_line') and detail['catalogue_line'].unite.strip())
    detail['can_prepare_claim'] = bool(detail['impact'] is not None and detail['impact'] > 0
                               and line.facture.numero_facture and line.facture.date_facture
                               and line.unite.strip()
                               and detail.get('catalogue_line') and detail['catalogue_line'].unite.strip())
    detail['can_claim'] = bool(verified and detail['can_prepare_claim'])
    detail['requires_claim_confirmation'] = bool(detail['can_prepare_claim'] and not verified)
    detail['show_claim_action'] = bool(detail['status'] == 'ecart_prix' and detail['ecart'] is not None and detail['ecart'] > 0)
    missing = []
    if not line.facture.numero_facture: missing.append('le numéro de facture')
    if not line.facture.date_facture: missing.append('la date de facture')
    if line.quantite is None or line.quantite <= 0: missing.append('la quantité facturée positive')
    if not line.unite.strip() or not detail.get('catalogue_line') or not detail['catalogue_line'].unite.strip():
        missing.append('les unités de prix')
    detail['claim_missing_fields'] = ', '.join(missing)
    # EAN alone is also a usable identifier.
    detail['can_price'] = bool(catalogue and detail['status'] in ('ecart_prix', 'prix_manquant', 'produit_non_trouve')
                              and (line.reference_produit.strip() or line.ean.strip())
                              and (not detail.get('catalogue_line') or line.unite.strip().lower() == detail['catalogue_line'].unite.strip().lower()))
    detail['logo'] = supplier_logo(line.facture.fournisseur.nom)
    detail['can_associate'] = bool(catalogue and (line.reference_produit.strip() or line.ean.strip()))
    return detail


def current_detail(line):
    result = control_invoice_against_catalogue(line.facture)
    detail = next((d for d in result['details'] if d['invoice_line'].pk == line.pk), None)
    return decorate(detail, result['catalogue']) if detail else None


def is_price_review_issue(detail):
    """Cards concern prices only; uncertain source data stays in document checks."""
    price = detail['prix_facture']
    if price is None or price < 0:
        return False
    if detail['status'] == 'ecart_prix':
        reference = detail['prix_catalogue']
        return reference is not None and abs(price - reference) > PRICE_TOLERANCE
    return (detail['status'] == 'produit_non_trouve'
            or (detail['status'] == 'prix_manquant' and detail['prix_catalogue'] is None))


def price_data_alert(details):
    labels = {'unite_a_verifier': 'unité ou conditionnement à préciser',
              'produit_ambigu': 'correspondance produit ambiguë',
              'devise_differente': 'devise à vérifier',
              'prix_manquant': 'prix du document absent'}
    counts = Counter(labels.get(d['status'], 'donnée du document à vérifier') for d in details)
    return 'Comparaison des prix à compléter : ' + ' ; '.join(f'{label} ({count})' for label, count in counts.items()) + '. Ces lignes ne sont pas présentées comme des écarts de prix.'


def claim_covers_price(detail, item):
    """A restored catalogue may change provenance without changing the claimed gap."""
    if item.category != 'price':
        return False
    current, saved = snapshot(detail), item.snapshot
    for key in ('invoice_id', 'supplier_id', 'invoice_number', 'invoice_date', 'delivery_date',
                'product', 'reference', 'ean', 'currency', 'catalogue_currency'):
        if current.get(key) != saved.get(key):
            return False
    for key in ('unit', 'catalogue_unit'):
        if str(current.get(key) or '').strip().casefold() != str(saved.get(key) or '').strip().casefold():
            return False
    for key in ('quantity', 'invoice_price', 'catalogue_price', 'conversion_factor'):
        left, right = current.get(key), saved.get(key)
        if left is None or right is None:
            if left != right:
                return False
        else:
            try:
                if Decimal(str(left)) != Decimal(str(right)):
                    return False
            except (InvalidOperation, ValueError, TypeError):
                return False
    try:
        return detail['impact'] is not None and detail['impact'] == Decimal(str(saved.get('impact')))
    except (InvalidOperation, ValueError, TypeError):
        return False


def review_queue(state='pending', restaurant='', supplier=''):
    qs = Facture.objects.filter(document_role='active', kind='invoice', statut__in=[Facture.Statut.A_VALIDER, Facture.Statut.VALIDEE, Facture.Statut.LITIGE])
    if restaurant:
        qs = qs.filter(fournisseur__restaurant_id=restaurant)
    if supplier:
        qs = qs.filter(fournisseur_id=supplier)
    qs = qs.select_related('fournisseur__restaurant').prefetch_related(
        Prefetch('lignes', queryset=LigneFacture.objects.select_related('price_decision').order_by('ordre', 'pk')))
    suppliers = qs.values_list('fournisseur_id', flat=True).distinct()
    catalogues = CatalogueFournisseur.objects.filter(fournisseur_id__in=suppliers).filter(
        Q(statut='valide') | Q(origine='documents', statut='a_valider', date_debut__isnull=True, date_fin__isnull=True)
    ).prefetch_related('lignes').order_by('-date_debut', '-date_import', 'pk')
    aliases = {}
    for a in ProductAssociation.objects.filter(fournisseur_id__in=suppliers):
        aliases.setdefault(a.fournisseur_id, {})[a.identifier] = a
    by_supplier, indexes = {}, {}
    for cat in catalogues:
        by_supplier.setdefault(cat.fournisseur_id, []).append(cat)
        indexes[cat.pk] = build_catalogue_indexes(cat, aliases=aliases.get(cat.fournisseur_id, {}))
    claims_by_line = {}
    for item in SupplierClaimLine.objects.exclude(claim__state='cancelled').filter(invoice_line__facture__in=qs).select_related('claim').order_by('-pk'):
        claims_by_line.setdefault(item.invoice_line_id, []).append(item)
    tasks, alerts = [], []
    cache_writes = []
    cache = {c.invoice_id: c for c in InvoiceControlCache.objects.filter(invoice_id__in=qs.values('pk'))}
    for invoice in qs.order_by('date_facture', 'pk'):
        control_date = invoice.date_livraison_detectee or invoice.date_facture
        eligible = select_control_catalogues(by_supplier.get(invoice.fournisseur_id, []), control_date)
        saved = cache.get(invoice.pk)
        cached = saved.result if saved and saved.revision == invoice.fournisseur.control_revision and saved.result.get('version') == CONTROL_VERSION else None
        result = control_invoice_against_catalogue(invoice, catalogues=eligible,
            catalogue_indexes=indexes[eligible[0].pk] if len(eligible) == 1 else None) if cached is None else None
        if result is not None:
            data = {'version': CONTROL_VERSION, 'available': result['control_available'], 'message': result['message'],
                'details': [{'line_id': d['invoice_line'].pk, 'catalogue_line_id': d['catalogue_line'].pk if d['catalogue_line'] else None,
                            'status': d['status'], 'prix_catalogue': value(d['prix_catalogue']), 'ecart': value(d['ecart']),
                            'conversion_factor': value(d.get('conversion_factor'))} for d in result['problem_details']]}
            cache_writes.append(InvoiceControlCache(invoice=invoice, revision=invoice.fournisseur.control_revision, result=data))
        else:
            from .catalogue_control import line_detail
            inv_lines = {l.pk: l for l in invoice.lignes.all()}
            cat_lines = {l.pk: l for l in eligible[0].lignes.all()} if len(eligible) == 1 else {}
            details = []
            for d in cached['details']:
                if d['line_id'] not in inv_lines:
                    continue
                row = line_detail(inv_lines[d['line_id']], status=d['status'], catalogue_line=cat_lines.get(d['catalogue_line_id']),
                    catalogue_price=Decimal(d['prix_catalogue']) if d['prix_catalogue'] is not None else None,
                    ecart=Decimal(d['ecart']) if d['ecart'] is not None else None)
                row['conversion_factor'] = Decimal(d['conversion_factor']) if d['conversion_factor'] else None
                details.append(row)
            result = {'control_available': cached['available'], 'message': cached['message'], 'catalogue': eligible[0] if len(eligible)==1 else None, 'problem_details': details}
        if not result['control_available']:
            alerts.append({'invoice': invoice, 'message': result['message']})
            continue
        data_issues = [d for d in result['problem_details'] if not is_price_review_issue(d)]
        if data_issues:
            alerts.append({'invoice': invoice, 'message': price_data_alert(data_issues)})
        from .quality import document_is_verified
        verified = document_is_verified(invoice)
        for detail in result['problem_details']:
            if not is_price_review_issue(detail):
                continue
            row = decorate(detail, result['catalogue'], verified=verified)
            existing = claims_by_line.get(row['line'].pk, [])
            if any(claim_covers_price(row, item) for item in existing):
                continue
            if existing:
                row['existing_claim'] = existing[0].claim
                row['existing_claim_category'] = existing[0].category
                row['show_claim_action'] = False
            decision = getattr(row['line'], 'price_decision', None)
            effective = decision if decision and decision.fingerprint == row['fingerprint'] else None
            row['queue_state'] = 'deferred' if effective and effective.state == PriceReviewDecision.State.DEFERRED else 'pending'
            if state == 'all':
                if effective and effective.state != PriceReviewDecision.State.DEFERRED:
                    continue
            elif state == 'deferred':
                if not effective or effective.state != PriceReviewDecision.State.DEFERRED:
                    continue
            elif effective:
                continue
            row['decision'] = effective
            tasks.append(row)
    if cache_writes:
        InvoiceControlCache.objects.bulk_create(cache_writes, update_conflicts=True, update_fields=["revision","result"], unique_fields=["invoice"])
    return tasks, alerts


def claim_total(claim):
    return sum((Decimal(line.snapshot['impact']) for line in claim.lines.all()
                if line.snapshot.get('impact') is not None), Decimal('0.00'))


def record_price(line, user, old):
    cat = line.catalogue
    if line.corrigee_manuellement and line.price_source:
        line.price_source = {}
        line.save(update_fields=['price_source'])
    CataloguePriceHistory.objects.create(line=line, actor=user, old_price=old,
        new_price=line.prix_unitaire_ht, catalogue_snapshot={
            'catalogue_id': cat.pk, 'supplier_id': cat.fournisseur_id,
            'reference': line.reference_produit, 'ean': line.ean, 'currency': cat.devise,
            'start': str(cat.date_debut or ''), 'end': str(cat.date_fin or ''), 'price_source': line.price_source})
