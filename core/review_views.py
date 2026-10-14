from decimal import Decimal, InvalidOperation
import logging
from urllib.parse import urlencode

from django.contrib import messages
from django.core.exceptions import ValidationError, PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.utils import timezone

from .review_forms import ReviewLineForm
from .operations_forms import ClaimFollowupForm
from .access import ROLE_ADMIN, ROLE_GESTIONNAIRE, ROLE_REVUE, role_required, user_has_role
from .models import (CatalogueFournisseur, CataloguePriceHistory, Fournisseur, Facture, LigneCatalogue,
                     LigneFacture, PriceReviewDecision, PriceReviewEvent, ProductAssociation,
                     Restaurant, SupplierClaim, SupplierClaimLine, BusinessAudit)
from .services.price_review import review_queue, current_detail, snapshot, claim_total, record_price
from .supplier_logos import supplier_logo
from .services.claim_documents import document_context, mail_content

logger = logging.getLogger(__name__)

manage = role_required(ROLE_GESTIONNAIRE, ROLE_ADMIN)
review = role_required(ROLE_GESTIONNAIRE, ROLE_ADMIN, ROLE_REVUE)


def filters(request):
    source = request.POST if request.method == 'POST' else request.GET
    return {key: source.get(key, '') if source.get(key, '').isdigit() else ''
            for key in ('restaurant', 'supplier')}


def return_review(request):
    source = request.POST if request.method == 'POST' else request.GET
    params = filters(request)
    params['state'] = 'deferred' if source.get('state') == 'deferred' else 'pending'
    params['position'] = source.get('position', '1') if source.get('position', '1').isdigit() else '1'
    return redirect(reverse('price_review') + '?' + urlencode(params))


@review
@require_http_methods(['GET'])
def price_review(request):
    state = 'deferred' if request.GET.get('state') == 'deferred' else 'pending'
    scope = filters(request)
    all_tasks, alerts = review_queue('all', **scope)
    pending = [t for t in all_tasks if t['queue_state'] == 'pending']
    deferred = [t for t in all_tasks if t['queue_state'] == 'deferred']
    tasks = deferred if state == 'deferred' else pending
    try:
        position = max(1, min(int(request.GET.get('position', 1)), len(tasks)))
    except ValueError:
        position = 1
    page = Paginator(tasks, 40).get_page((position - 1) // 40 + 1)
    current = tasks[position - 1] if tasks else None
    # Numbered navigation stays compact even for thousands of anomalies.
    numbers = sorted({1, len(tasks), *range(max(1, position - 2), min(len(tasks), position + 2) + 1)}) if tasks else []
    nav, previous = [], 0
    for n in numbers:
        if previous and n - previous > 1:
            nav.append(None)
        nav.append(n)
        previous = n
    choices = []
    if current and current['catalogue']:
        choices = current['catalogue'].lignes.exclude(statut_ligne=LigneCatalogue.StatutLigne.IGNOREE).order_by('designation')
    if current:
        current['is_image'] = current['invoice'].fichier.name.lower().endswith(('.png', '.jpg', '.jpeg', '.webp'))
    history = PriceReviewEvent.objects.select_related('actor').filter(line=current['line'])[:15] if current else []
    for event in history:
        event.label = {'defer': 'Mise en attente', 'claim': 'Ajout à une réclamation', 'price': 'Modification du catalogue', 'associate': 'Association produit', 'correct': 'Correction de la facture'}.get(event.action, event.action)
    suppliers = Fournisseur.objects.all()
    if scope['restaurant']:
        suppliers = suppliers.filter(restaurant_id=scope['restaurant'])
    return render(request, 'core/price_review.html', {
        'current': current, 'previous': tasks[position - 2] if position > 1 else None,
        'next': tasks[position] if position < len(tasks) else None,
        'position': position, 'total': len(tasks), 'page': page, 'numbers': nav,
        'pending_count': len(pending), 'deferred_count': len(deferred), 'state': state,
        'scope': scope, 'restaurants': Restaurant.objects.all(), 'suppliers': suppliers,
        'alerts': alerts, 'choices': choices, 'history': history,
        'correction_form': ReviewLineForm(instance=current['line']) if current else None,
        'last_event': PriceReviewEvent.objects.filter(actor=request.user, undone=False, line__in=LigneFacture.objects.all()).first(),
        'claims_count': SupplierClaim.objects.exclude(state__in=['closed', 'cancelled']).count(),
        'base_query': urlencode({**scope, 'state': state}),
    })


def decision_data(line):
    d = PriceReviewDecision.objects.filter(line=line).first()
    return {'fingerprint': d.fingerprint, 'state': d.state, 'reason': d.reason,
            'actor_id': d.actor_id} if d else {}


def catalogue_record(line):
    return {'id': line.pk, 'price': str(line.prix_unitaire_ht) if line.prix_unitaire_ht is not None else None,
            'manual': line.corrigee_manuellement, 'status': line.statut_ligne, 'price_source': line.price_source}


def decimal_price(raw):
    try:
        price = Decimal(raw.replace(',', '.'))
        if not price.is_finite() or price < 0 or price > Decimal('9999999999.99') or price.as_tuple().exponent < -2:
            raise ValueError
        return price
    except (InvalidOperation, ValueError, AttributeError):
        raise ValidationError('Indiquez un prix positif ou nul, avec au maximum deux décimales.')


@review
@require_http_methods(['POST'])
def review_action(request, pk):
    try:
        with transaction.atomic():
            # Supplier serializes catalogue, association and claim operations across invoices.
            supplier_id = get_object_or_404(LigneFacture, pk=pk).facture.fournisseur_id
            supplier = Fournisseur.objects.select_for_update().get(pk=supplier_id)
            line = LigneFacture.objects.select_for_update().select_related('facture__fournisseur').get(pk=pk)
            if line.facture.statut not in ('a_valider', 'validee', 'litige'):
                raise ValidationError('Cette facture n’est pas encore prête pour le contrôle.')
            detail = current_detail(line)
            if not detail or detail['status'] == 'conforme' or request.POST.get('fingerprint') != detail['fingerprint']:
                raise ValidationError('Les données de cette fiche ont changé. Vérifiez les valeurs actualisées.')
            if detail['catalogue']:
                CatalogueFournisseur.objects.select_for_update().get(pk=detail['catalogue'].pk)
            if detail.get('catalogue_line'):
                LigneCatalogue.objects.select_for_update().get(pk=detail['catalogue_line'].pk)
            detail = current_detail(line)
            if not detail or request.POST.get('fingerprint') != detail['fingerprint']:
                raise ValidationError('Les données de cette fiche ont changé. Vérifiez les valeurs actualisées.')
            prior = PriceReviewDecision.objects.filter(line=line, fingerprint=detail['fingerprint']).first()
            if prior and prior.state != 'deferred':
                raise ValidationError('Cette fiche a déjà été traitée par un autre utilisateur.')
            action = request.POST.get('action')
            before, after = {'decision': decision_data(line)}, {}
            if action == 'defer':
                reason = request.POST.get('reason', '').strip()
                if not reason or len(reason) > 2000:
                    raise ValidationError('Indiquez un motif de report (2 000 caractères maximum).')
                state = 'deferred'
            elif action == 'claim':
                if not detail['can_prepare_claim']:
                    raise ValidationError('Vérifiez le numéro de facture, la date, les quantités et les unités avant de réclamer un surcoût positif.')
                if detail['requires_claim_confirmation'] and request.POST.get('confirm_line') != '1':
                    raise ValidationError('Confirmez les prix et la quantité de cette ligne avant de l’ajouter au brouillon.')
                if SupplierClaimLine.objects.exclude(claim__state='cancelled').filter(invoice_line=line).exists():
                    raise ValidationError('Cette ligne figure déjà dans une réclamation.')
                claim, _ = SupplierClaim.objects.get_or_create(fournisseur=supplier, currency=line.facture.devise,
                    state='draft', defaults={'created_by': request.user})
                data = snapshot(detail)
                data['impact'] = str(detail['impact'])
                if detail['requires_claim_confirmation']:
                    data['line_confirmation'] = {'actor': request.user.pk, 'at': timezone.now().isoformat(),
                        'scope': 'Prix et quantité de cette ligne uniquement'}
                    after['line_confirmation'] = data['line_confirmation']
                item = SupplierClaimLine.objects.create(claim=claim, invoice_line=line,
                    fingerprint=detail['fingerprint'], snapshot=data)
                after.update(claim_id=claim.pk, claim_line_id=item.pk)
                state, reason = 'claimed', ''
            elif action == 'correct':
                form = ReviewLineForm(request.POST, instance=line)
                before['invoice_line'] = {key: str(getattr(line, key)) if getattr(line, key) is not None else None for key in form.Meta.fields}
                before['invoice_line']['manual'] = line.corrigee_manuellement
                if not form.is_valid():
                    raise ValidationError([str(error) for errors in form.errors.values() for error in errors])
                line = form.save(commit=False)
                line.corrigee_manuellement = True
                line.save()
                after['invoice_line'] = {key: str(getattr(line, key)) if getattr(line, key) is not None else None for key in form.Meta.fields}
                PriceReviewDecision.objects.filter(line=line).delete()
                state, reason = None, ''
            elif action in ('price', 'associate'):
                cat = detail['catalogue']
                if not cat:
                    raise ValidationError('Un catalogue valide est nécessaire.')
                CatalogueFournisseur.objects.select_for_update().get(pk=cat.pk)
                if action == 'associate':
                    if not detail['can_associate']:
                        raise ValidationError('Renseignez une référence ou un EAN sur la facture.')
                    target = get_object_or_404(LigneCatalogue, pk=request.POST.get('target', '0') if request.POST.get('target', '').isdigit() else 0,
                        catalogue=cat)
                    if target.statut_ligne == 'ignoree' or not (target.reference_produit or target.ean):
                        raise ValidationError('Choisissez un produit catalogue identifiable et actif.')
                    identifier = 'ref:' + line.reference_produit.strip() if line.reference_produit.strip() else 'ean:' + line.ean.strip()
                    alias = ProductAssociation.objects.filter(fournisseur=supplier, identifier=identifier).first()
                    before['association'] = {'reference': alias.target_reference, 'ean': alias.target_ean} if alias else None
                    alias, _ = ProductAssociation.objects.update_or_create(fournisseur=supplier, identifier=identifier,
                        defaults={'target_reference': target.reference_produit.strip(), 'target_ean': target.ean.strip()})
                    after['association'] = {'id': alias.pk, 'reference': alias.target_reference, 'ean': alias.target_ean}
                else:
                    if not detail['can_price']:
                        raise ValidationError('Vérifiez l’identifiant du produit et les unités avant de modifier le catalogue.')
                    price = decimal_price(request.POST.get('price', ''))
                    target = detail.get('catalogue_line')
                    before['catalogue'] = catalogue_record(target) if target else None
                    if target:
                        target = LigneCatalogue.objects.select_for_update().get(pk=target.pk)
                        old = target.prix_unitaire_ht
                    else:
                        target = LigneCatalogue(catalogue=cat, reference_produit=line.reference_produit.strip(),
                            ean=line.ean.strip(), designation=line.designation, unite=line.unite)
                        old = None
                    target.prix_unitaire_ht, target.corrigee_manuellement = price, True
                    target.save()
                    record_price(target, request.user, old)
                    after['catalogue'] = catalogue_record(target)
                updated = current_detail(line)
                # Association may expose a real price discrepancy: leave it in the queue.
                if updated and updated['status'] == 'conforme':
                    state, reason = 'resolved', ''
                    detail = updated
                else:
                    PriceReviewDecision.objects.filter(line=line).delete()
                    state, reason = None, ''
            else:
                raise ValidationError('Action inconnue.')
            if state:
                PriceReviewDecision.objects.update_or_create(line=line, defaults={
                    'fingerprint': detail['fingerprint'], 'state': state, 'reason': reason, 'actor': request.user})
            after['decision'] = decision_data(line)
            after['fingerprint'] = current_detail(line)['fingerprint']
            PriceReviewEvent.objects.create(line=line, actor=request.user, action=action, before=before, after=after)
            logger.info('Revue prix: action=%s ligne=%s utilisateur=%s', action, line.pk, request.user.pk)
        messages.success(request, {'defer': 'Fiche mise en attente.', 'claim': 'Ligne ajoutée à la réclamation du fournisseur.',
                                  'price': 'Catalogue mis à jour et contrôle recalculé.', 'associate': 'Association enregistrée et contrôle recalculé.', 'correct': 'Ligne corrigée et contrôle recalculé.'}[action])
    except ValidationError as exc:
        messages.warning(request, ' '.join(exc.messages))
    return return_review(request)


@review
@require_http_methods(['POST'])
def review_undo(request):
    try:
        with transaction.atomic():
            event_id = request.POST.get('event', '')
            event = get_object_or_404(PriceReviewEvent.objects.filter(line__in=LigneFacture.objects.all()), pk=event_id if event_id.isdigit() else 0, actor=request.user)
            if not event.line_id or event.undone:
                raise ValidationError('Cette décision ne peut plus être annulée.')
            supplier = Fournisseur.objects.select_for_update().get(pk=event.line.facture.fournisseur_id)
            event = PriceReviewEvent.objects.select_for_update().get(pk=event.pk)
            line = LigneFacture.objects.select_for_update().select_related('facture__fournisseur').get(pk=event.line_id)
            detail = current_detail(line)
            if event.undone or not detail or detail['fingerprint'] != event.after['fingerprint'] or decision_data(line) != event.after['decision']:
                raise ValidationError('Les données ont changé depuis cette décision. L’annulation est indisponible.')
            if PriceReviewEvent.objects.filter(line=line, date_creation__gt=event.date_creation, undone=False).exists():
                raise ValidationError('Une décision plus récente existe sur cette fiche.')
            if event.action == 'claim':
                item = SupplierClaimLine.objects.select_related('claim').get(pk=event.after['claim_line_id'])
                if item.claim.state != 'draft':
                    raise ValidationError('La réclamation est déjà préparée. Son contenu est conservé.')
                claim = item.claim
                item.delete()
                if not claim.lines.exists():
                    claim.delete()
            if event.action == 'price':
                target = LigneCatalogue.objects.select_for_update().get(pk=event.after['catalogue']['id'])
                if catalogue_record(target) != event.after['catalogue']:
                    raise ValidationError('Ce tarif a été modifié depuis. L’annulation est indisponible.')
                old = target.prix_unitaire_ht
                record = event.before['catalogue']
                if record:
                    target.prix_unitaire_ht = Decimal(record['price']) if record['price'] is not None else None
                    target.corrigee_manuellement, target.statut_ligne = record['manual'], record['status']
                    target.price_source = record.get('price_source', {})
                else:
                    target.statut_ligne = 'ignoree'
                target.save()
                record_price(target, request.user, old)
            if event.action == 'correct':
                prior_line = event.before['invoice_line']
                for key in ReviewLineForm.Meta.fields:
                    val = prior_line[key]
                    setattr(line, key, Decimal(val) if key in ('quantite', 'prix_unitaire_ht') and val is not None else val)
                line.corrigee_manuellement = prior_line['manual']
                line.save()
            if event.action == 'associate':
                alias = ProductAssociation.objects.get(pk=event.after['association']['id'])
                if (alias.target_reference, alias.target_ean) != (event.after['association']['reference'], event.after['association']['ean']):
                    raise ValidationError('Cette association a été modifiée depuis.')
                prior = event.before['association']
                if prior:
                    alias.target_reference, alias.target_ean = prior['reference'], prior['ean']
                    alias.save()
                else:
                    alias.delete()
            prior = event.before['decision']
            if prior:
                PriceReviewDecision.objects.update_or_create(line=line, defaults=prior)
            else:
                PriceReviewDecision.objects.filter(line=line).delete()
            event.undone = True
            event.save(update_fields=['undone'])
        messages.success(request, 'Dernière décision annulée.')
    except (ValidationError, SupplierClaimLine.DoesNotExist, LigneCatalogue.DoesNotExist, ProductAssociation.DoesNotExist) as exc:
        messages.warning(request, ' '.join(exc.messages) if isinstance(exc, ValidationError) else 'Les données ont changé. L’annulation est indisponible.')
    return return_review(request)


@manage
@require_http_methods(['GET'])
def supplier_claims(request):
    claims = SupplierClaim.objects.select_related('fournisseur__restaurant').prefetch_related('lines__delivery_issue__delivery').order_by('-date_creation','-pk')
    filter_definitions = [
        ('all','Toutes',None),
        ('active','En cours',('draft','prepared','sent','replied','credit_due','credit_received')),
        ('draft','Brouillons',('draft',)),
        ('prepared','À envoyer',('prepared',)),
        ('followup','Suivi',('sent','replied','credit_due','credit_received')),
        ('closed','Clôturées',('closed',)),
        ('cancelled','Annulées',('cancelled',)),
    ]
    claim_filter=request.GET.get('filter','all')
    if claim_filter not in {key for key,_,_ in filter_definitions}: claim_filter='all'
    totals={row['state']:row['total'] for row in claims.values('state').order_by().annotate(total=Count('pk'))}
    quick_filters=[{'key':key,'label':label,'count':sum(totals.values()) if states is None else sum(totals.get(state,0) for state in states)} for key,label,states in filter_definitions]
    selected_states=next(states for key,_,states in filter_definitions if key==claim_filter)
    if selected_states is not None: claims=claims.filter(state__in=selected_states)
    ids = list(claims.values_list('pk',flat=True))
    requested = request.GET.get('claim','')
    if requested.isdigit() and int(requested) in ids:
        position = ids.index(int(requested)) + 1
    else:
        try:
            position = max(1,min(int(request.GET.get('position',1)),len(ids)))
        except ValueError:
            position = 1
    claim = claims[position-1] if ids else None
    if claim:
        claim.mail = mail_content(claim) if claim.state not in ('draft', 'cancelled') else None
        claim.total = claim_total(claim)
        claim.has_unpriced_delivery = any(item.category == 'delivery' and item.snapshot.get('impact') is None for item in claim.lines.all())
        claim.has_priced_lines = any(item.snapshot.get('impact') is not None for item in claim.lines.all())
        claim.logo = supplier_logo(claim.fournisseur.nom)
        claim.can_cancel = claim.state != 'cancelled' and not claim.recovered_amount and not claim.credit_invoice_id
        # Suggest the original claimed differences without recording a recovery.
        # Keep a recovery already entered, including a partial credit.
        suggested_recovery = claim.recovered_amount or claim.total
        claim.followup_form = ClaimFollowupForm(initial={'recovered_amount':suggested_recovery,'due_date':claim.due_date,'followup_note':claim.followup_note,'credit_invoice':claim.credit_invoice_id})
        claim.followup_form.fields['credit_invoice'].queryset = claim.fournisseur.factures.filter(kind='credit')
    numbers, last = [], 0
    for n in sorted({1,len(ids),*range(max(1,position-2),min(len(ids),position+2)+1)}) if ids else []:
        if last and n-last>1:
            numbers.append(None)
        numbers.append(n)
        last=n
    return render(request, 'core/supplier_claims.html', {
        'claim':claim, 'position':position, 'total':len(ids), 'numbers':numbers,
        'claim_filter':claim_filter,'quick_filters':quick_filters,
        'previous':claims[position-2] if position>1 else None,
        'next':claims[position] if position<len(ids) else None,
    })


def claim_return_url(request, pk=None):
    params={'filter':request.POST.get('claim_filter','all')}
    if pk is not None: params['claim']=pk
    return reverse('supplier_claims')+'?'+urlencode(params)


def withdraw_claim(claim, user, delete=False):
    """Called with the supplier and claim locked inside a transaction."""
    if delete and not user_has_role(user, ROLE_ADMIN):
        raise PermissionDenied
    if delete and claim.state != 'draft':
        raise ValidationError('Seul un brouillon peut être supprimé. Utilisez l’annulation pour les autres réclamations.')
    if claim.state == 'cancelled':
        raise ValidationError('Cette réclamation est déjà annulée.')
    if claim.recovered_amount > 0 or claim.credit_invoice_id:
        raise ValidationError('Cette réclamation est liée à un avoir ou à un montant récupéré. Conservez son suivi et utilisez la clôture.')
    items = list(claim.lines.all())
    before = {'state':claim.state, 'supplier_id':claim.fournisseur_id, 'currency':claim.currency,
              'lines':[{'id':item.pk, 'invoice_line_id':item.invoice_line_id, 'delivery_issue_id':item.delivery_issue_id, 'category':item.category,
                        'fingerprint':item.fingerprint, 'snapshot':item.snapshot} for item in items]}
    ids = [item.invoice_line_id for item in items if item.invoice_line_id]
    still_claimed = SupplierClaimLine.objects.exclude(claim=claim).exclude(claim__state='cancelled').filter(invoice_line_id__in=ids).values_list('invoice_line_id',flat=True)
    PriceReviewDecision.objects.filter(line_id__in=ids,state='claimed').exclude(line_id__in=still_claimed).delete()
    PriceReviewEvent.objects.filter(action='claim',after__claim_id=claim.pk,undone=False).update(undone=True)
    action = 'claim_delete' if delete else 'claim_cancel'
    BusinessAudit.objects.create(restaurant=claim.fournisseur.restaurant, actor=user,
        model_label='core.supplierclaim',object_id=claim.pk,action=action,before=before,
        after={'state':'deleted' if delete else 'cancelled'})
    PriceReviewEvent.objects.create(actor=user,action=action,before={'claim_id':claim.pk,'state':claim.state},
                                   after={'claim_state':'deleted' if delete else 'cancelled'})
    from .services.delivery_claims import reopen_delivery_issues
    reopen_delivery_issues(claim)
    if delete:
        claim.delete()
    else:
        claim.state = 'cancelled'
        claim.save(update_fields=['state','date_modification'])


@manage
@require_http_methods(['POST'])
def claim_state(request, pk):
    try:
        with transaction.atomic():
            supplier_id = get_object_or_404(SupplierClaim, pk=pk).fournisseur_id
            Fournisseur.objects.select_for_update().get(pk=supplier_id)
            claim = SupplierClaim.objects.select_for_update().get(pk=pk)
            action = request.POST.get('action')
            old_state = claim.state
            if action in ('cancel','delete'):
                if request.POST.get('confirmed') != '1':
                    raise ValidationError('Validez l’annulation ou la suppression dans le panneau de la réclamation.')
                withdraw_claim(claim,request.user,delete=action=='delete')
                messages.success(request, 'Brouillon supprimé. Les écarts sont de nouveau à traiter.' if action=='delete' else 'Réclamation annulée. Les écarts sont de nouveau à traiter.')
                return redirect(claim_return_url(request,None if action=='delete' else pk))
            if action in ('replied','credit_due','credit_received','closed'):
                transitions={'sent':{'replied','credit_due'}, 'replied':{'credit_due','credit_received','closed'}, 'credit_due':{'replied','credit_received','closed'}, 'credit_received':{'closed'}}
                if action not in transitions.get(claim.state,set()):
                    raise ValidationError('Cette étape ne suit pas l’état actuel de la réclamation.')
                form=ClaimFollowupForm(request.POST)
                form.fields['credit_invoice'].queryset = claim.fournisseur.factures.filter(kind='credit',devise=claim.currency)
                if not form.is_valid():
                    raise ValidationError([str(e) for errors in form.errors.values() for e in errors])
                data=form.cleaned_data
                proof=data['credit_invoice']; amount=data['recovered_amount']
                if amount > claim_total(claim):
                    raise ValidationError('Le montant récupéré ne peut pas dépasser le montant réclamé.')
                if action=='credit_received' and (amount<=0 or proof is None):
                    raise ValidationError('Pour un avoir reçu, indiquez le montant récupéré et un avoir justificatif de ce fournisseur et de cette devise.')
                if amount>0:
                    from .services.quality import document_is_verified
                    from django.db.models import Sum
                    if proof is None or not document_is_verified(proof) or proof.montant_ht is None:
                        raise ValidationError('Rattachez un avoir vérifié avec son montant HT avant de confirmer une récupération.')
                    used=SupplierClaim.objects.filter(credit_invoice=proof).exclude(pk=claim.pk).aggregate(total=Sum('recovered_amount'))['total'] or Decimal('0')
                    if used+amount>abs(proof.montant_ht):
                        raise ValidationError('Le montant récupéré dépasse le disponible de cet avoir, qui peut déjà justifier une autre réclamation.')
                if action=='closed' and not data['followup_note'].strip():
                    raise ValidationError('Précisez le motif de clôture, notamment si aucun avoir n’a été reçu.')
                claim.state=action
                for field in ('recovered_amount','due_date','followup_note','credit_invoice'):
                    setattr(claim,field,data[field])
            elif action == 'prepare' and claim.state == 'draft' and claim.lines.exists():
                from .services.reconciliation import check_quantity_claims
                check_quantity_claims(claim)
                claim.state = 'prepared'
            elif action == 'sent' and claim.state == 'prepared':
                claim.state = 'sent'
            else:
                raise ValidationError('Cette action n’est plus disponible pour cette réclamation.')
            claim.save()
            PriceReviewEvent.objects.create(actor=request.user, action='claim_' + action,
                before={'claim_id': claim.pk,'state':old_state}, after={'claim_state': claim.state, 'recovered_amount':str(claim.recovered_amount)})
        messages.success(request, 'Document préparé.' if action == 'prepare' else 'Suivi enregistré. Aucun email n’a été envoyé par l’application.')
    except ValidationError as exc:
        messages.warning(request, ' '.join(exc.messages))
    return redirect(claim_return_url(request,pk))


@manage
@require_http_methods(['GET'])
@xframe_options_sameorigin
def claim_document(request, pk):
    claim = get_object_or_404(SupplierClaim.objects.select_related('fournisseur__restaurant').prefetch_related('lines'), pk=pk)
    if claim.state == 'cancelled':
        messages.warning(request, 'Cette réclamation est annulée ; son historique reste consultable.')
        return redirect('supplier_claims')
    if claim.state == 'draft':
        messages.warning(request, 'Préparez la réclamation avant de télécharger son document.')
        return redirect('supplier_claims')
    response = render(request, 'core/claim_document.html', document_context(claim))
    disposition = 'inline' if request.GET.get('preview') == '1' else 'attachment'
    response['Content-Disposition'] = f'{disposition}; filename="reclamation-{claim.pk}.html"'
    response['Cache-Control'] = 'private, no-store'
    return response


@review
@require_http_methods(['GET'])
@xframe_options_sameorigin
def review_invoice_file(request, pk):
    """Document source consultable dans la revue, sans donner accès aux autres écrans."""
    from .views import secure_document_response
    invoice = get_object_or_404(Facture, pk=pk, kind='invoice', statut__in=('a_valider', 'validee', 'litige'))
    return secure_document_response(invoice.fichier, download=request.GET.get('download') == '1')
