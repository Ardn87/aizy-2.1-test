"""Rapprochements proposés puis confirmés. Aucun lien n'est créé par une lecture."""
import hashlib
import json
import unicodedata
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from core.models import (
    DeliveryMode, Fournisseur, Facture, BonLivraison, LigneFacture, LigneBonLivraison,
    InvoiceDeliveryLink, DeliveryAllocation, DeliveryReceipt, InvoiceReceipt,
    DeliveryProductMapping, QuantityCredit, SupplierClaim, SupplierClaimLine,
    BusinessAudit,
)
from .catalogue_control import normalized_unit

ZERO = Decimal('0')


def digest(data):
    return hashlib.sha256(json.dumps(data, default=str, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def normalized(value):
    return ' '.join(''.join(c for c in unicodedata.normalize('NFKD', value or '') if not unicodedata.combining(c)).casefold().split())


def product_key(line):
    return ('ref:' + normalized(line.reference_produit)) if line.reference_produit.strip() else 'name:' + normalized(line.designation)


def line_fingerprint(line):
    parent = line.facture if isinstance(line, LigneFacture) else line.bon_livraison
    fields = ['reference_produit', 'designation', 'unite', 'archived_at']
    fields += ['quantite', 'prix_unitaire_ht', 'kind'] if isinstance(line, LigneFacture) else ['quantite_livree', 'statut_ligne']
    doc_fields = ['fournisseur_id', 'archived_at'] + (['numero_facture', 'date_facture', 'devise', 'kind', 'document_role'] if isinstance(parent, Facture) else ['numero_bon_livraison', 'date_livraison'])
    def stable_value(k):
        value=getattr(line,k)
        return format(Decimal(str(value)).normalize(),'f') if value is not None and line._meta.get_field(k).get_internal_type()=='DecimalField' else value
    return digest({'line': {k:stable_value(k) for k in fields}, 'parent': {k:getattr(parent,k) for k in doc_fields}})


def pair_fingerprint(a, b):
    return digest([line_fingerprint(a), line_fingerprint(b)])


def mode_for(invoice):
    return invoice.delivery_mode or invoice.fournisseur.delivery_mode


def quantity(value, *, zero=False, places=3):
    try:
        result = Decimal(str(value).replace(',', '.'))
        if not result.is_finite() or result < 0 or (not zero and result == 0) or result > Decimal('999999.999') or result.as_tuple().exponent < -places:
            raise ValueError
        return result
    except (InvalidOperation, ValueError, TypeError):
        raise ValidationError(f'Indiquez une quantité {"positive ou nulle" if zero else "positive"}, avec {places} décimales maximum.')


def reason_text(value):
    result = (value or '').strip()
    if len(result) < 5 or len(result) > 2000:
        raise ValidationError('Expliquez la décision (5 à 2 000 caractères).')
    return result


def ensure_invoice(invoice):
    if invoice.archived_at or invoice.kind != 'invoice' or invoice.document_role != 'active':
        raise ValidationError('Ce document est exclu du rapprochement. Rétablissez son rôle avant de continuer.')
    if invoice.statut == 'analyse_en_cours':
        raise ValidationError('Attendez la fin de l’analyse de cette facture.')


def usable_delivery(line):
    return not line.archived_at and not line.bon_livraison.archived_at and line.statut_ligne != 'ignoree' and line.bon_livraison.statut != 'analyse_en_cours'


def received_quantity(line):
    receipt = DeliveryReceipt.objects.filter(line=line).first()
    if receipt:
        if receipt.source_fingerprint != line_fingerprint(line):
            return None, 'Réception à reconfirmer après modification du BL'
        return receipt.received - receipt.returned, ''
    return line.quantite_livree, ''


def allocated_delivery_quantity(line, excluding=None):
    qs = DeliveryAllocation.all_objects.filter(delivery_line=line)
    if excluding is not None:
        qs = qs.exclude(invoice_line_id=excluding)
    return qs.aggregate(total=Sum('quantity'))['total'] or ZERO


def unit_factor(invoice_line, other, value, reason):
    a, b = normalized_unit(invoice_line.unite), normalized_unit(other.unite)
    if not a or not b:
        raise ValidationError('Renseignez les unités avant le rapprochement.')
    factor = quantity(value, places=6)
    if a == b and factor != 1:
        raise ValidationError('Pour des unités identiques, le facteur doit être 1.')
    if a != b:
        reason_text(reason)
    return factor


@transaction.atomic
def confirm_link(invoice, delivery, user):
    Fournisseur.objects.select_for_update().get(pk=invoice.fournisseur_id)
    ensure_invoice(invoice)
    if mode_for(invoice) == DeliveryMode.MIXED:
        invoice.delivery_mode=DeliveryMode.SEPARATE
        invoice.save(update_fields=['delivery_mode'])
    if mode_for(invoice) != DeliveryMode.SEPARATE:
        raise ValidationError('Choisissez le fonctionnement « BL et facture séparés » pour associer un BL.')
    if delivery.fournisseur_id != invoice.fournisseur_id or delivery.archived_at or delivery.statut == 'analyse_en_cours':
        raise ValidationError('Choisissez un BL disponible de ce fournisseur et de cet établissement.')
    if Facture.objects.filter(source_delivery=delivery, document_role='active').exists():
        raise ValidationError('Ce BL sert déjà de facture ; il ne doit pas être facturé deux fois.')
    if invoice.file_sha256 and invoice.file_sha256 == delivery.file_sha256:
        raise ValidationError('Ces documents sont le même fichier. Utilisez le mode document BL et facture.')
    if InvoiceReceipt.objects.filter(line__facture=invoice).exists():
        raise ValidationError('Retirez les confirmations sans BL avant de rattacher une livraison.')
    return InvoiceDeliveryLink.objects.get_or_create(invoice=invoice, delivery=delivery, defaults={'confirmed_by':user})[0]


@transaction.atomic
def allocate_delivery(invoice, invoice_line_id, delivery_line_id, amount, user, factor='1', reason='', remember=False):
    Fournisseur.objects.select_for_update().get(pk=invoice.fournisseur_id)
    ensure_invoice(invoice)
    line = LigneFacture.objects.select_for_update().get(pk=invoice_line_id, facture=invoice, kind='product')
    delivery = LigneBonLivraison.objects.select_for_update().select_related('bon_livraison').get(pk=delivery_line_id, bon_livraison__fournisseur=invoice.fournisseur)
    if not usable_delivery(delivery):
        raise ValidationError('Cette ligne de BL n’est plus disponible.')
    confirm_link(invoice, delivery.bon_livraison, user)
    if not (line.reference_produit.strip() or line.designation.strip()) or not (delivery.reference_produit.strip() or delivery.designation.strip()):
        raise ValidationError('Identifiez les deux produits avant de les associer.')
    amount = quantity(amount)
    factor = unit_factor(line, delivery, factor, reason)
    available, warning = received_quantity(delivery)
    if warning or available is None or available < 0:
        raise ValidationError(warning or 'La quantité du BL est à vérifier.')
    if allocated_delivery_quantity(delivery, excluding=line.pk) + amount > available:
        raise ValidationError('Cette quantité dépasse le disponible du BL ; une autre facture peut déjà en utiliser une partie.')
    other = sum((a.quantity*a.factor for a in DeliveryAllocation.all_objects.filter(invoice_line=line).exclude(delivery_line=delivery)), ZERO)
    credits = sum((a.quantity*a.factor for a in QuantityCredit.all_objects.filter(invoice_line=line)), ZERO)
    if line.quantite is None or line.quantite <= 0 or other + amount*factor + credits > line.quantite:
        raise ValidationError('La quantité rapprochée dépasse la quantité facturée restant à rapprocher.')
    allocation, _ = DeliveryAllocation.objects.update_or_create(invoice_line=line, delivery_line=delivery, defaults={
        'quantity':amount, 'factor':factor, 'reason':reason[:2000], 'confirmed_by':user,
        'source_fingerprint':pair_fingerprint(line, delivery)})
    if remember:
        why = reason_text(reason)
        DeliveryProductMapping.objects.update_or_create(fournisseur=invoice.fournisseur,
            invoice_key=product_key(line), delivery_key=product_key(delivery),
            invoice_unit=normalized_unit(line.unite), delivery_unit=normalized_unit(delivery.unite),
            defaults={'factor':factor, 'reason':why, 'confirmed_by':user})
    return allocation


def duplicate_pair_signature(a, b):
    def data(doc):
        return [doc.pk,doc.fournisseur_id,doc.kind,doc.numero_facture,doc.date_facture,doc.devise,
                doc.montant_ht,doc.montant_ttc,doc.file_sha256]
    return digest(sorted([data(a),data(b)],key=lambda row:row[0]))


def duplicate_candidates(invoice):
    condition = Q()
    if invoice.numero_facture.strip():
        condition |= Q(numero_facture__iexact=invoice.numero_facture.strip())
    if invoice.file_sha256:
        condition |= Q(file_sha256=invoice.file_sha256)
    if not condition:
        return Facture.objects.none()
    candidates=Facture.objects.filter(condition, fournisseur=invoice.fournisseur, kind=invoice.kind, document_role='active').exclude(pk=invoice.pk)
    dismissed=[]
    for other in candidates:
        signature=duplicate_pair_signature(invoice,other)
        if any(decision.get('signature')==signature for decision in [invoice.duplicate_dismissals.get(str(other.pk),{}),other.duplicate_dismissals.get(str(invoice.pk),{})]):
            dismissed.append(other.pk)
    return candidates.exclude(pk__in=dismissed)


def reconciliation_state(invoice):
    """Les quantités liées à des sources modifiées restent visibles mais ne valident rien."""
    mode = mode_for(invoice)
    delivery_count=BonLivraison.objects.filter(fournisseur=invoice.fournisseur).count()
    lines = list(invoice.lignes.filter(kind='product'))
    allocations = list(DeliveryAllocation.objects.filter(invoice_line__facture=invoice).select_related('delivery_line__bon_livraison', 'invoice_line__facture'))
    credits = list(QuantityCredit.objects.filter(invoice_line__facture=invoice).select_related('credit_line__facture', 'invoice_line__facture'))
    receipts = {r.line_id:r for r in InvoiceReceipt.objects.filter(line__facture=invoice)}
    claim_ids = set(SupplierClaimLine.objects.exclude(claim__state='cancelled').filter(invoice_line__facture=invoice, category='quantity').values_list('invoice_line_id', flat=True))
    rows=[]; matched=0; stale=False; source_stale=False
    for line in lines:
        warnings=[]; delivered=ZERO; credited=ZERO; documents=[]; has_evidence=False
        for a in allocations:
            if a.invoice_line_id != line.pk:
                continue
            dl=a.delivery_line
            documents.append(str(dl.bon_livraison))
            capacity, warning = received_quantity(dl)
            valid = usable_delivery(dl) and a.source_fingerprint == pair_fingerprint(line, dl)
            if not valid or warning or capacity is None or allocated_delivery_quantity(dl) > capacity:
                warnings.append(warning or 'Association à revoir : source modifiée ou quantité disponible dépassée')
            else:
                delivered += a.quantity*a.factor; has_evidence=True
        receipt=receipts.get(line.pk)
        if receipt:
            if receipt.source_fingerprint != line_fingerprint(line):
                warnings.append('Réception à reconfirmer après modification de la facture')
            elif mode == DeliveryMode.SEPARATE or allocations:
                warnings.append('La confirmation sans BL ne peut pas être cumulée avec des BL')
            else:
                delivered=receipt.quantity; has_evidence=True
        for c in credits:
            if c.invoice_line_id != line.pk:
                continue
            cl=c.credit_line
            used=QuantityCredit.all_objects.filter(credit_line=cl).aggregate(total=Sum('quantity'))['total'] or ZERO
            if (cl.archived_at or cl.facture.archived_at or cl.facture.document_role != 'active' or cl.facture.kind != 'credit'
                or c.source_fingerprint != pair_fingerprint(line,cl) or cl.quantite is None or used>abs(cl.quantite)):
                warnings.append('Avoir à reconfirmer après modification de sa source')
            else:
                credited += c.quantity*c.factor
        if warnings:
            source_stale=True
        if not line.unite.strip() or line.quantite is None or line.quantite <= 0:
            warnings.append('Quantité ou unité facturée à vérifier')
        gap = line.quantite-delivered-credited if line.quantite is not None else None
        state='matched' if gap==0 and not warnings and (has_evidence or credited>0) else 'difference'
        if not has_evidence and not credited and mode != DeliveryMode.SEPARATE:
            gap=None;state='unverified'
        if warnings:
            state='review';stale=True
        if state=='matched': matched+=1
        rows.append({'has_evidence':has_evidence, 'line':line,'allocated':delivered,'credited':credited,'gap':gap,'state':state,
            'warnings':list(dict.fromkeys(warnings)),'documents':sorted(set(documents)), 'claimed':line.pk in claim_ids,
            'can_claim':bool(has_evidence and state=='difference' and gap is not None and gap>0 and not warnings and invoice.numero_facture and invoice.date_facture and invoice.devise!='UNK' and line.prix_unitaire_ht is not None and line.prix_unitaire_ht>=0)})
    if any(line_id not in {l.pk for l in lines} for line_id in receipts):
        stale=True; source_stale=True
    if allocations and mode != DeliveryMode.SEPARATE:
        stale=True; source_stale=True
    # Allocations pointing to an archived/non-product invoice row must be reviewed too.
    if any(a.invoice_line_id not in {l.pk for l in lines} for a in allocations+credits):
        stale=True; source_stale=True
    due=invoice.delivery_wait_until or (invoice.date_import.date()+timedelta(days=invoice.fournisseur.delivery_wait_days))
    duplicates=list(duplicate_candidates(invoice))
    if invoice.document_role!='active': state,label='excluded',invoice.get_document_role_display()
    elif invoice.kind=='credit': state,label='credit','Avoir · à associer à une facture'
    elif invoice.statut in ('analyse_en_cours','analyse_echouee') or not lines: state,label='review','Données à vérifier'
    elif duplicates: state,label='review','Doublon possible · à vérifier'
    elif stale: state,label='review','Associations à revoir'
    elif mode==DeliveryMode.MIXED: state,label=('pending','BL disponibles · à rapprocher') if delivery_count else ('setup','Fonctionnement à confirmer')
    elif matched==len(lines): state,label='matched','Livraisons rapprochées'
    elif mode in (DeliveryMode.INVOICE_ONLY,DeliveryMode.COMBINED):
        state,label=('difference','Différences à traiter') if any(r['state']=='difference' for r in rows) else ('unverified','Livraison non vérifiée')
    else: state,label=('waiting','En attente de document') if due>=timezone.localdate() else ('difference','Différences à traiter')
    # Keep known discrepancies visible even before the waiting deadline.
    if state=='waiting' and not invoice.delivery_wait_until and any(r['allocated']>0 and r['gap'] not in (None,ZERO) for r in rows):
        state,label='difference','Différences à traiter'
    result={'state':state,'label':label,'mode':mode,'mode_label':dict(DeliveryMode.choices)[mode],
        'rows':rows,'issues':[r for r in rows if r['state']!='matched'],'matched':matched,'total':len(lines),
        'due':due,'delivery_count':delivery_count,'duplicates':duplicates,'stale':stale,'source_stale':source_stale,'excluded_lines':invoice.lignes.exclude(kind='product')}
    if state=='waiting' and not invoice.delivery_wait_until and delivery_count and suggestions(invoice,result):
        result.update(state='pending',label='BL proposés · à confirmer')
    return result


def review_token(invoice, *, include_decision=True):
    """Empêche un POST ancien d'écraser une décision ou une correction concurrente."""
    return digest({
        'invoice': [invoice.pk,invoice.kind,invoice.numero_facture,invoice.date_facture,invoice.date_livraison_detectee,invoice.devise,invoice.source_delivery_id,invoice.delivery_mode,invoice.delivery_wait_until,invoice.document_role,invoice.replaces_with_id, invoice.fournisseur.delivery_mode,invoice.fournisseur.delivery_wait_days,invoice.statut,invoice.file_sha256],
        'decision':invoice.reconciliation_decision if include_decision else None,
        'delivery_documents':list(BonLivraison.objects.filter(fournisseur=invoice.fournisseur).values('pk','statut','file_sha256','date_livraison')),
        'mappings':list(DeliveryProductMapping.objects.filter(fournisseur=invoice.fournisseur).values()),
        'lines':[line_fingerprint(l) for l in invoice.lignes.all()],
        'deliveries':[line_fingerprint(l) for l in LigneBonLivraison.objects.filter(bon_livraison__fournisseur=invoice.fournisseur).select_related('bon_livraison')],
        'supplier_allocations':list(DeliveryAllocation.objects.filter(delivery_line__bon_livraison__fournisseur=invoice.fournisseur).values()),
        'supplier_credits':list(QuantityCredit.objects.filter(credit_line__facture__fournisseur=invoice.fournisseur).values()),
        'credit_sources':[line_fingerprint(l) for l in LigneFacture.objects.filter(facture__fournisseur=invoice.fournisseur,facture__kind='credit').select_related('facture')],
        'allocations':list(DeliveryAllocation.objects.filter(invoice_line__facture=invoice).values()),
        'receipts':list(InvoiceReceipt.objects.filter(line__facture=invoice).values()),
        'delivery_receipts':list(DeliveryReceipt.objects.filter(line__bon_livraison__fournisseur=invoice.fournisseur).values()),
        'credits':list(QuantityCredit.objects.filter(invoice_line__facture=invoice).values()),
        'duplicates':list(Facture.objects.filter(fournisseur=invoice.fournisseur).values('pk','numero_facture','date_facture','devise','file_sha256','document_role','duplicate_dismissals')),
        'claims':list(SupplierClaimLine.objects.exclude(claim__state='cancelled').filter(invoice_line__facture=invoice).values('pk','fingerprint'))})


def suggestions(invoice, state=None, *, limit=5):
    state=state or reconciliation_state(invoice)
    if mode_for(invoice) not in (DeliveryMode.SEPARATE,DeliveryMode.MIXED) or invoice.document_role!='active':
        return []
    date=invoice.date_livraison_detectee or invoice.date_facture
    qs=LigneBonLivraison.objects.filter(bon_livraison__fournisseur=invoice.fournisseur).exclude(statut_ligne='ignoree').exclude(bon_livraison__statut='analyse_en_cours').select_related('bon_livraison')
    qs=qs.exclude(bon_livraison__combined_invoice__document_role='active')
    candidates=list(qs.order_by('-bon_livraison__date_livraison','pk'))
    mappings=list(DeliveryProductMapping.objects.filter(fournisseur=invoice.fournisseur))
    receipts={r.line_id:r for r in DeliveryReceipt.objects.filter(line__in=candidates)}
    consumed=dict(DeliveryAllocation.all_objects.filter(delivery_line__in=candidates).values('delivery_line_id').annotate(total=Sum('quantity')).values_list('delivery_line_id','total'))
    existing=set(DeliveryAllocation.objects.filter(invoice_line__facture=invoice).values_list('invoice_line_id','delivery_line_id'))
    prepared=[]
    for dl in candidates:
        receipt=receipts.get(dl.pk)
        warning=bool(receipt and receipt.source_fingerprint!=line_fingerprint(dl))
        capacity=receipt.received-receipt.returned if receipt and not warning else dl.quantite_livree
        available=None if capacity is None or warning else capacity-consumed.get(dl.pk,ZERO)
        if available is not None and available<=0: continue
        in_period=bool(date and dl.bon_livraison.date_livraison and date-timedelta(days=62)<=dl.bon_livraison.date_livraison<=date+timedelta(days=7))
        prepared.append((dl,available,in_period,warning))
    result=[]
    for row in state['issues']:
        line=row['line']; choices=[]
        remaining=line.quantite-row['allocated']-row['credited'] if line.quantite is not None else None
        if remaining is not None and remaining<=0: continue
        for dl,available,in_period,warning in prepared:
            if (line.pk,dl.pk) in existing: continue
            a,b=product_key(line),product_key(dl)
            mapping=next((m for m in mappings if m.invoice_key==a and m.delivery_key==b and m.invoice_unit==normalized_unit(line.unite) and m.delivery_unit==normalized_unit(dl.unite)),None)
            exact=bool(line.reference_produit.strip() and normalized(line.reference_produit)==normalized(dl.reference_produit))
            same_name=bool(line.designation.strip() and normalized(line.designation)==normalized(dl.designation))
            similarity=SequenceMatcher(None,normalized(line.designation),normalized(dl.designation)).ratio() if line.designation and dl.designation else 0
            if not (mapping or exact or same_name or (in_period and similarity>=.72)): continue
            factor=mapping.factor if mapping else Decimal('1') if normalized_unit(line.unite)==normalized_unit(dl.unite) and line.unite.strip() else None
            proposed=min(available,remaining/factor) if factor and available is not None and remaining is not None else None
            if proposed is not None and proposed != proposed.quantize(Decimal('.001')): proposed=None
            can_confirm=bool(in_period and available is not None and line.quantite is not None and line.quantite>0 and line.unite.strip() and dl.unite.strip() and not row['warnings'] and not warning)
            choices.append({'delivery':dl,'available':available,'factor':factor,'quantity':proposed,
                'reason':mapping.reason if mapping else '', 'confidence':'Association mémorisée' if mapping else 'Référence identique' if exact else 'Désignation identique' if same_name else 'Désignation proche · à vérifier',
                'rank':(3 if mapping else 2 if exact else 1 if same_name else 0)+(4 if in_period else 0),
                'can_confirm':can_confirm,'date_warning':not in_period,'data_warning':not line.unite.strip() or not dl.unite.strip() or line.quantite is None or available is None})
        choices.sort(key=lambda c:c['rank'],reverse=True)
        if choices: result.append({'line':line,'choices':choices if limit is None else choices[:limit],'count':len(choices)})
    return result


@transaction.atomic
def save_delivery_receipt(invoice, dl, received, returned, reason, user):
    Fournisseur.objects.select_for_update().get(pk=invoice.fournisseur_id)
    if dl.bon_livraison.fournisseur_id != invoice.fournisseur_id or not usable_delivery(dl):
        raise ValidationError('Choisissez une ligne de livraison disponible de ce fournisseur.')
    received=quantity(received,zero=True);returned=quantity(returned,zero=True)
    if returned>received: raise ValidationError('La quantité retournée dépasse la quantité reçue.')
    return DeliveryReceipt.objects.update_or_create(line=dl, defaults={'received':received,'returned':returned,
        'reason':reason_text(reason),'source_fingerprint':line_fingerprint(dl),'confirmed_by':user})[0]


@transaction.atomic
def save_invoice_receipt(invoice, line, amount, reason, user):
    Fournisseur.objects.select_for_update().get(pk=invoice.fournisseur_id)
    ensure_invoice(invoice)
    if mode_for(invoice) not in (DeliveryMode.INVOICE_ONLY,DeliveryMode.COMBINED) or DeliveryAllocation.objects.filter(invoice_line__facture=invoice).exists():
        raise ValidationError('La confirmation directe est réservée aux factures sans BL séparé.')
    if line.facture_id != invoice.pk or line.kind!='product' or not line.unite.strip():
        raise ValidationError('Choisissez un produit de cette facture avec une unité connue.')
    return InvoiceReceipt.objects.update_or_create(line=line, defaults={'quantity':quantity(amount,zero=True),
        'reason':reason_text(reason),'source_fingerprint':line_fingerprint(line),'confirmed_by':user})[0]


@transaction.atomic
def save_credit(invoice, line, credit, amount, factor, reason, user):
    Fournisseur.objects.select_for_update().get(pk=invoice.fournisseur_id)
    ensure_invoice(invoice)
    if line.facture_id!=invoice.pk or line.kind!='product' or credit.kind!='product' or credit.facture.kind!='credit' or credit.facture.document_role!='active' or credit.facture.fournisseur_id!=invoice.fournisseur_id:
        raise ValidationError('Choisissez une ligne d’avoir de ce fournisseur et une ligne de cette facture.')
    if credit.facture.statut=='analyse_en_cours' or invoice.devise=='UNK' or invoice.devise!=credit.facture.devise:
        raise ValidationError('L’avoir doit être disponible et dans la même devise connue que la facture.')
    amount=quantity(amount);factor=unit_factor(line,credit,factor,reason)
    used=QuantityCredit.all_objects.filter(credit_line=credit).exclude(invoice_line=line).aggregate(total=Sum('quantity'))['total'] or ZERO
    if credit.quantite is None or used+amount>abs(credit.quantite):
        raise ValidationError('Cette quantité dépasse le disponible de l’avoir.')
    others=sum((c.quantity*c.factor for c in QuantityCredit.all_objects.filter(invoice_line=line).exclude(credit_line=credit)),ZERO)
    if line.quantite is None or others+amount*factor>line.quantite:
        raise ValidationError('Les quantités créditées dépassent la quantité facturée.')
    return QuantityCredit.objects.update_or_create(invoice_line=line,credit_line=credit,defaults={
        'quantity':amount,'factor':factor,'reason':reason_text(reason),'confirmed_by':user,'source_fingerprint':pair_fingerprint(line,credit)})[0]


@transaction.atomic
def quantity_claim(invoice, line, user):
    Fournisseur.objects.select_for_update().get(pk=invoice.fournisseur_id)
    ensure_invoice(invoice)
    row=next((r for r in reconciliation_state(invoice)['issues'] if r['line'].pk==line.pk),None)
    if duplicate_candidates(invoice).exists():
        raise ValidationError('Résolvez le doublon possible avant de réclamer une correction.')
    if not row or not row['can_claim']:
        raise ValidationError('Confirmez les quantités, l’unité, la devise et les informations de facture avant de préparer une réclamation.')
    # A price claim and a quantity claim on the same row would double-count part of the amount.
    if SupplierClaimLine.objects.exclude(claim__state='cancelled').filter(invoice_line=line).exists():
        raise ValidationError('Cette ligne figure déjà dans une réclamation. Vérifiez ce dossier pour éviter une double demande.')
    impact=(row['gap']*line.prix_unitaire_ht).quantize(Decimal('.01'))
    if impact<=0: raise ValidationError('Le montant réclamé doit être positif.')
    claim,_=SupplierClaim.objects.get_or_create(fournisseur=invoice.fournisseur,currency=invoice.devise,state='draft',defaults={'created_by':user})
    item=SupplierClaimLine.objects.create(claim=claim,invoice_line=line,category='quantity',fingerprint=digest(['quantity',review_token(invoice),line.pk]),snapshot={
        'category':'quantity','invoice_id':invoice.pk,'invoice_number':invoice.numero_facture,'invoice_date':str(invoice.date_facture),
        'product':line.designation,'reference':line.reference_produit,'ean':line.ean,'unit':line.unite,'quantity':str(line.quantite),
        'delivered':str(row['allocated']),'credited':str(row['credited']),'difference':str(row['gap']),
        'delivery_numbers':row['documents'],'invoice_price':str(line.prix_unitaire_ht),'impact':str(impact)})
    BusinessAudit.objects.create(restaurant=invoice.fournisseur.restaurant,actor=user,model_label='core.supplierclaimline',object_id=item.pk,action='quantity_claim',after=item.snapshot)
    return claim


def check_quantity_claims(claim):
    """Un brouillon devenu obsolète ne doit pas réclamer un écart déjà régularisé."""
    for item in claim.lines.filter(category='quantity').select_related('invoice_line__facture__fournisseur'):
        line=item.invoice_line
        if not line or line.archived_at or line.facture.archived_at or line.facture.document_role!='active':
            raise ValidationError('Une ligne de quantité de cette réclamation n’est plus active. Retirez-la du brouillon depuis le rapprochement.')
        row=next((r for r in reconciliation_state(line.facture)['issues'] if r['line'].pk==line.pk),None)
        if (not row or not row['can_claim'] or duplicate_candidates(line.facture).exists()
            or str(line.facture.numero_facture)!=item.snapshot['invoice_number']
            or str(line.facture.date_facture)!=item.snapshot['invoice_date']
            or line.facture.devise!=claim.currency
            or row['gap']!=Decimal(item.snapshot['difference'])
            or (row['gap']*line.prix_unitaire_ht).quantize(Decimal('.01'))!=Decimal(item.snapshot['impact'])
            or row['documents']!=item.snapshot['delivery_numbers']):
            raise ValidationError('Les quantités ou justificatifs de cette réclamation ont changé. Retirez la ligne du brouillon depuis le rapprochement, puis préparez une nouvelle demande si nécessaire.')
