"""Construire une collection produits sans transformer des prix relevés en tarifs."""
import hashlib
import unicodedata
from collections import defaultdict
from django.db import transaction
from django.db.models import Max
from core.models import (CatalogueFournisseur, Fournisseur, LigneCatalogue,
                         LigneFacture, LigneBonLivraison, Devise)
from .catalogue_control import normalized_unit


def normalized_text(value):
    return ' '.join(unicodedata.normalize('NFKC', str(value or '')).strip().casefold().split())


def product_key(reference, ean, designation, unit):
    identity = ('ref:' + reference.strip() if reference else
                'ean:' + ean.strip() if ean else 'name:' + normalized_text(designation))
    return 'product:' + hashlib.sha256((identity + '|' + normalized_unit(unit)).encode()).hexdigest()


def supplier_products(supplier):
    groups = defaultdict(list)
    invoices = LigneFacture.objects.filter(facture__fournisseur=supplier, facture__kind='invoice', facture__document_role='active', kind='product').select_related('facture')
    deliveries = LigneBonLivraison.objects.filter(bon_livraison__fournisseur=supplier).exclude(statut_ligne='ignoree').select_related('bon_livraison')
    for kind, lines in [('facture', invoices), ('bon_livraison', deliveries)]:
        for line in lines:
            if not (line.designation.strip() or line.reference_produit.strip() or getattr(line, 'ean', '').strip()):
                continue
            doc = line.facture if kind == 'facture' else line.bon_livraison
            payload=doc.donnees_ia if isinstance(doc.donnees_ia,dict) else {}
            header=payload.get('delivery_note') if isinstance(payload.get('delivery_note'),dict) else {}
            currency = doc.devise if kind == 'facture' else str(payload.get('devise') or header.get('currency') or '').upper()
            if currency not in Devise.values: currency = Devise.UNKNOWN
            price = line.prix_unitaire_ht if kind == 'facture' else line.prix_unitaire
            date = doc.date_facture if kind == 'facture' else doc.date_livraison
            observation = {
                'kind':kind, 'document_id':doc.pk, 'line_id':line.pk,
                'number':doc.numero_facture if kind == 'facture' else doc.numero_bon_livraison,
                'date':date.isoformat() if date else None,
                'imported_at':doc.date_import.isoformat(),
                'designation':line.designation, 'reference':line.reference_produit,
                'ean':getattr(line,'ean',''), 'unit':line.unite,
                'packaging':getattr(line,'conditionnement',''),
                'price':str(price) if price is not None else None, 'currency':currency,
                'basis':'HT' if kind == 'facture' else 'À vérifier',
                'verified':bool(doc.verified_at), 'source_page':line.source_page,
            }
            groups[product_key(line.reference_produit, getattr(line,'ean',''), line.designation, line.unite)].append(observation)
    for observations in groups.values():
        observations.sort(key=lambda o:(o['date'] or '',o['imported_at'],o['kind'],o['document_id'],o['line_id']),reverse=True)
    return dict(groups)


@transaction.atomic
def build_supplier_catalogue(supplier):
    supplier = Fournisseur.objects.select_for_update().get(pk=supplier.pk)
    groups = supplier_products(supplier)
    catalogue = CatalogueFournisseur.objects.filter(fournisseur=supplier,origine='documents').first()
    currencies = {o['currency'] for rows in groups.values() for o in rows if o['currency'] != Devise.UNKNOWN}
    created = catalogue is None
    if created:
        catalogue = CatalogueFournisseur.objects.create(
            fournisseur=supplier, origine='documents', nom=f'Catalogue {supplier.nom}',
            devise=next(iter(currencies)) if len(currencies)==1 else Devise.UNKNOWN,
            statut='a_valider', note='Produits regroupés depuis les BL et factures. Les prix relevés sont conservés comme observations ; les prix catalogue HT et leur période doivent être confirmés.')
    all_lines=list(LigneCatalogue.all_objects.filter(catalogue=catalogue))
    existing = {l.source_key:l for l in all_lines if l.source_key.startswith('product:')}
    manual={}
    for line in all_lines:
        if not line.source_key:
            manual.setdefault(product_key(line.reference_produit,line.ean,line.designation,line.unite),[]).append(line)
    next_order=(LigneCatalogue.all_objects.filter(catalogue=catalogue).aggregate(last=Max('ordre'))['last'] or 0)+1
    added = updated = 0
    for key, observations in sorted(groups.items(),key=lambda pair:pair[1][0]['designation'].casefold()):
        line = existing.get(key)
        if line is None and key in manual:
            if len(manual[key]) != 1:
                continue  # Aucun choix arbitraire parmi des produits manuels ambigus.
            line=manual[key][0]
            if not line.archived_at:
                line.source_key=key;line.observations=observations
                line.save(update_fields=['source_key','observations']);updated += 1
        if line is not None and line.archived_at:
            continue  # Un archivage volontaire ne doit pas être annulé par un enrichissement.
        if line is None:
            latest = observations[0]
            line = LigneCatalogue(catalogue=catalogue,source_key=key,
                reference_produit=latest['reference'],ean=next((o['ean'] for o in observations if o['ean']),''),
                designation=latest['designation'],unite=latest['unit'],conditionnement=latest['packaging'],
                ordre=next_order,observations=observations,prix_unitaire_ht=None)
            line.save(); added += 1; next_order += 1
        elif line.observations != observations:
            line.observations = observations
            line.save(update_fields=['observations']); updated += 1
    return catalogue, {'created':created,'added':added,'updated':updated,'products':catalogue.lignes.count()}


def usable_observation(observations):
    """Choisir un vrai prix unitaire ; ne pas traiter un total ou une quantité comme un prix."""
    from decimal import Decimal, InvalidOperation
    for observation in sorted(observations, key=lambda o: (o.get('kind') != 'facture', o.get('date') or '', o.get('imported_at') or '')):
        try:
            price = Decimal(str(observation.get('price')))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if price.is_finite() and 0 <= price <= Decimal('9999999999.99') and price == price.quantize(Decimal('.01')):
            return observation, price
    return None


@transaction.atomic
def fill_catalogue_prices(catalogue, actor):
    """Initialisation demandée, avec origine conservée et sans écraser les tarifs présents."""
    from .price_review import record_price
    Fournisseur.objects.select_for_update().get(pk=catalogue.fournisseur_id)
    groups = supplier_products(catalogue.fournisseur)
    result = {'filled': 0, 'missing': 0, 'existing': 0}
    for line in catalogue.lignes.select_for_update():
        if line.prix_unitaire_ht is not None:
            result['existing'] += 1
            continue
        observations = line.observations or groups.get(product_key(line.reference_produit, line.ean, line.designation, line.unite), [])
        chosen = usable_observation(observations)
        if not chosen:
            result['missing'] += 1
            continue
        source, price = chosen
        line.prix_unitaire_ht = price
        line.price_source = {**source, 'initialised_from_document': True}
        line.corrigee_manuellement = False
        line.statut_ligne = 'a_verifier'
        line.save()
        record_price(line, actor, None)
        result['filled'] += 1
    return result
