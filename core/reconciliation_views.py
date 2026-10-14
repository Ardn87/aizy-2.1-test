from datetime import date
from urllib.parse import urlencode
from django.urls import reverse

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from .access import role_required, ROLE_ADMIN, ROLE_GESTIONNAIRE
from .models import (DeliveryMode, Fournisseur, Facture, BonLivraison, LigneFacture, LigneBonLivraison,
    InvoiceDeliveryLink, DeliveryAllocation, DeliveryReceipt, InvoiceReceipt, QuantityCredit,
    DeliveryProductMapping, SupplierClaimLine, BusinessAudit)
from .supplier_logos import supplier_logo
from .services import reconciliation as service
from .services import automatic_reconciliation as automatic
from .services.catalogue_control import control_invoice_against_catalogue

manage = role_required(ROLE_ADMIN, ROLE_GESTIONNAIRE)


def selection(qs, value):
    return get_object_or_404(qs, pk=value if str(value).isdigit() else 0)


def _perform(invoice, data, user):
    action=data.get('action')
    if action in ('approve_auto', 'reject_auto', 'restore_auto'):
        automatic.decide(invoice, data.get('proposal'), action, user)
    elif action=='policy':
        mode=data.get('mode')
        if mode not in DeliveryMode.values:
            raise ValidationError('Choisissez le fonctionnement du fournisseur.')
        days=data.get('wait_days','')
        if not days.isdigit() or not 0<=int(days)<=365:
            raise ValidationError('Indiquez un délai entre 0 et 365 jours.')
        supplier=invoice.fournisseur
        supplier.delivery_mode=mode;supplier.delivery_wait_days=int(days);supplier.save(update_fields=['delivery_mode','delivery_wait_days'])
    elif action=='mode':
        mode=data.get('mode','')
        if mode and mode not in DeliveryMode.values: raise ValidationError('Choisissez un fonctionnement valide.')
        target=mode or invoice.fournisseur.delivery_mode
        if target!=service.mode_for(invoice) and (DeliveryAllocation.objects.filter(invoice_line__facture=invoice).exists() or InvoiceReceipt.objects.filter(line__facture=invoice).exists() or invoice.delivery_links.exists()):
            raise ValidationError('Retirez les associations et confirmations existantes avant de changer le fonctionnement de cette facture.')
        invoice.delivery_mode=mode;invoice.reconciliation_note=service.reason_text(data.get('reason'));invoice.save(update_fields=['delivery_mode','reconciliation_note'])
    elif action=='wait':
        try: until=date.fromisoformat(data.get('until',''))
        except ValueError: raise ValidationError('Choisissez une date de rappel.')
        invoice.delivery_wait_until=until;invoice.reconciliation_note=service.reason_text(data.get('reason'));invoice.save(update_fields=['delivery_wait_until','reconciliation_note'])
    elif action=='distinct':
        other=selection(Facture.objects.filter(fournisseur=invoice.fournisseur,kind=invoice.kind,document_role='active').exclude(pk=invoice.pk),data.get('other'))
        if invoice.file_sha256 and invoice.file_sha256==other.file_sha256:
            raise ValidationError('Ces documents utilisent exactement le même fichier. Vérifiez le duplicata ou la double fonction BL et facture.')
        invoice.duplicate_dismissals[str(other.pk)]={'signature':service.duplicate_pair_signature(invoice,other),
            'reason':service.reason_text(data.get('reason')),'actor':user.pk}
        invoice.save(update_fields=['duplicate_dismissals'])
    elif action=='reset_distinct':
        invoice.duplicate_dismissals={};invoice.save(update_fields=['duplicate_dismissals'])
    elif action=='role':
        role=data.get('role')
        if role not in ('active','duplicate','replaced','statement'): raise ValidationError('Choisissez la nature du document.')
        if role!='active' and (DeliveryAllocation.objects.filter(invoice_line__facture=invoice).exists() or QuantityCredit.objects.filter(Q(invoice_line__facture=invoice)|Q(credit_line__facture=invoice)).exists() or InvoiceReceipt.objects.filter(line__facture=invoice).exists() or SupplierClaimLine.objects.exclude(claim__state='cancelled').filter(invoice_line__facture=invoice).exists() or invoice.credit_claims.exists()):
            raise ValidationError('Ce document est utilisé dans un rapprochement ou une réclamation. Retirez ses affectations et vérifiez les réclamations avant de l’exclure.')
        replacement=None
        if role in ('duplicate','replaced'):
            replacement=selection(Facture.objects.filter(fournisseur=invoice.fournisseur,kind=invoice.kind,document_role='active').exclude(pk=invoice.pk),data.get('replacement'))
        invoice.document_role=role;invoice.replaces_with=replacement;invoice.reconciliation_note=service.reason_text(data.get('reason'))
        invoice.save(update_fields=['document_role','replaces_with','reconciliation_note'])
    else:
        service.ensure_invoice(invoice)
        if action=='link':
            service.confirm_link(invoice,selection(BonLivraison.objects.filter(fournisseur=invoice.fournisseur),data.get('delivery')),user)
        elif action=='unlink':
            link=selection(invoice.delivery_links.all(),data.get('link'))
            if DeliveryAllocation.objects.filter(invoice_line__facture=invoice,delivery_line__bon_livraison=link.delivery).exists():
                raise ValidationError('Retirez les affectations de ce BL avant de le délier.')
            link.delete()
        elif action=='allocate':
            line=selection(invoice.lignes.filter(kind='product'),data.get('invoice_line'))
            delivery=selection(LigneBonLivraison.objects.filter(bon_livraison__fournisseur=invoice.fournisseur),data.get('delivery_line'))
            service.allocate_delivery(invoice,line.pk,delivery.pk,data.get('quantity'),user,data.get('factor','1'),data.get('reason',''),data.get('remember')=='on')
        elif action=='remove_allocation':
            selection(DeliveryAllocation.objects.filter(invoice_line__facture=invoice),data.get('allocation')).delete()
        elif action=='receipt':
            line=selection(invoice.lignes.filter(kind='product'),data.get('invoice_line'))
            service.save_invoice_receipt(invoice,line,data.get('quantity'),data.get('reason'),user)
        elif action=='remove_receipt':
            selection(InvoiceReceipt.objects.filter(line__facture=invoice),data.get('receipt')).delete()
        elif action=='delivery_receipt':
            dl=selection(LigneBonLivraison.objects.filter(bon_livraison__fournisseur=invoice.fournisseur),data.get('delivery_line'))
            service.save_delivery_receipt(invoice,dl,data.get('received'),data.get('returned','0'),data.get('reason'),user)
        elif action=='remove_delivery_receipt':
            selection(DeliveryReceipt.objects.filter(line__bon_livraison__fournisseur=invoice.fournisseur),data.get('receipt')).delete()
        elif action=='credit':
            line=selection(invoice.lignes.filter(kind='product'),data.get('invoice_line'))
            cl=selection(LigneFacture.objects.filter(facture__fournisseur=invoice.fournisseur,facture__kind='credit',facture__document_role='active'),data.get('credit_line'))
            service.save_credit(invoice,line,cl,data.get('quantity'),data.get('factor','1'),data.get('reason'),user)
        elif action=='remove_credit':
            selection(QuantityCredit.objects.filter(invoice_line__facture=invoice),data.get('credit')).delete()
        elif action=='claim':
            service.quantity_claim(invoice,selection(invoice.lignes.all(),data.get('invoice_line')),user)
        elif action=='remove_quantity_claim':
            item=selection(SupplierClaimLine.objects.exclude(claim__state='cancelled').filter(invoice_line__facture=invoice,category='quantity',claim__state='draft'),data.get('claim_line'))
            BusinessAudit.objects.create(restaurant=invoice.fournisseur.restaurant,actor=user,model_label='core.supplierclaimline',object_id=item.pk,action='remove_quantity_claim',before=item.snapshot)
            item.delete()
        elif action=='line_kind':
            line=selection(invoice.lignes.all(),data.get('invoice_line'))
            kind=data.get('kind')
            if kind not in LigneFacture.Kind.values: raise ValidationError('Choisissez le type de ligne.')
            service.reason_text(data.get('reason'))
            if DeliveryAllocation.objects.filter(invoice_line=line).exists() or QuantityCredit.objects.filter(invoice_line=line).exists() or InvoiceReceipt.objects.filter(line=line).exists() or SupplierClaimLine.objects.exclude(claim__state='cancelled').filter(invoice_line=line).exists():
                raise ValidationError('Retirez les rapprochements de cette ligne avant de changer son type.')
            line.kind=kind;line.corrigee_manuellement=True;line.save(update_fields=['kind','corrigee_manuellement'])
        elif action=='forget_mapping':
            selection(DeliveryProductMapping.objects.filter(fournisseur=invoice.fournisseur),data.get('mapping')).delete()
        else: raise ValidationError('Action inconnue.')


@manage
@require_http_methods(['GET','POST'])
def invoice_reconcile(request, pk, queue_context=None):
    invoice=get_object_or_404(Facture.objects.select_related('fournisseur__restaurant'),pk=pk)
    if request.method=='POST':
        try:
            with transaction.atomic():
                Fournisseur.objects.select_for_update().get(pk=invoice.fournisseur_id)
                invoice=Facture.objects.select_for_update().select_related('fournisseur__restaurant').get(pk=pk)
                if request.POST.get('token')!=service.review_token(invoice):
                    raise ValidationError('Cette fiche a changé. Vérifiez les données actualisées avant de confirmer.')
                _perform(invoice,request.POST,request.user)
            messages.success(request,'Décision enregistrée. Le rapprochement a été recalculé.')
        except ValidationError as exc:
            messages.warning(request,' '.join(exc.messages))
        if request.POST.get('from_queue') == '1':
            params = {'state':request.POST.get('queue_state','attention'),
                      'supplier':request.POST.get('queue_supplier',''),
                      'q':request.POST.get('queue_q',''),
                      'position':request.POST.get('queue_position','1'), 'invoice':pk}
            return redirect(reverse('reconciliation_queue')+'?'+urlencode(params))
        response = redirect('invoice_reconcile',pk=pk)
        if request.GET.get('advanced') == '1':
            response['Location'] += '?advanced=1'
        return response
    state=service.reconciliation_state(invoice)
    control=control_invoice_against_catalogue(invoice)
    if invoice.document_role!='active' or invoice.kind!='invoice': price_label='Prix · document exclu'
    elif not control['control_available']: price_label='Prix · contrôle indisponible'
    elif control['problem_details']: price_label='Prix · points à traiter'
    else: price_label='Prix · aucun écart détecté'
    qs=Facture.objects.filter(kind='invoice',document_role='active').order_by('date_facture','pk')
    ids=list(qs.values_list('pk',flat=True));index=ids.index(pk) if pk in ids else None
    neighbors={'previous':qs.filter(pk=ids[index-1]).first() if index is not None and index>0 else None,
               'next':qs.filter(pk=ids[index+1]).first() if index is not None and index+1<len(ids) else None}
    issue_page=Paginator(state['issues'],8).get_page(request.GET.get('line_page'))
    visible_state={**state,'issues':list(issue_page)}
    proposals=service.suggestions(invoice,visible_state)
    context={'invoice':invoice,'state':state,'token':service.review_token(invoice),'mode_choices':DeliveryMode.choices,
        'position':index+1 if index is not None else None,'count':len(ids), 'neighbors':neighbors,'price_label':price_label,
        'proposals':proposals,'issue_page':issue_page,
        'proposal_line_ids':{p['line'].pk for p in proposals},
        'links':invoice.delivery_links.select_related('delivery'),
        'delivery_lines':LigneBonLivraison.objects.filter(bon_livraison__fournisseur=invoice.fournisseur).exclude(statut_ligne='ignoree').select_related('bon_livraison').order_by('-bon_livraison__date_livraison','ordre'),
        'deliveries':BonLivraison.objects.filter(fournisseur=invoice.fournisseur),
        'allocations':DeliveryAllocation.objects.filter(invoice_line__facture=invoice).select_related('invoice_line','delivery_line__bon_livraison'),
        'receipts':InvoiceReceipt.objects.filter(line__facture=invoice).select_related('line'),
        'delivery_receipts':DeliveryReceipt.objects.filter(line__bon_livraison__fournisseur=invoice.fournisseur).select_related('line__bon_livraison'),
        'credit_lines':LigneFacture.objects.filter(facture__fournisseur=invoice.fournisseur,facture__kind='credit',facture__document_role='active',kind='product').select_related('facture'),
        'credits':QuantityCredit.objects.filter(invoice_line__facture=invoice).select_related('invoice_line','credit_line__facture'),
        'replacement_candidates':Facture.objects.filter(fournisseur=invoice.fournisseur,kind=invoice.kind,document_role='active').exclude(pk=pk),
        'quantity_claims':SupplierClaimLine.objects.exclude(claim__state='cancelled').filter(invoice_line__facture=invoice,category='quantity').select_related('claim'),
        'mappings':DeliveryProductMapping.objects.filter(fournisseur=invoice.fournisseur),
        'auto_plan':automatic.build_plan(invoice, state),
    }
    if queue_context:
        context.update(queue_context)
    else:
        context['previous_url'] = reverse('invoice_reconcile',args=[neighbors['previous'].pk]) if neighbors['previous'] else ''
        context['next_url'] = reverse('invoice_reconcile',args=[neighbors['next'].pk]) if neighbors['next'] else ''
    context['logo'] = supplier_logo(invoice.fournisseur.nom)
    total=context['count']; position=context['position']
    progress=[];last=0
    for n in sorted({1,total,*range(max(1,position-2),min(total,position+2)+1)}) if position else []:
        if last and n-last>1: progress.append(None)
        url = '?'+context['params']+'&position='+str(n) if queue_context else reverse('invoice_reconcile',args=[ids[n-1]])
        progress.append({'number':n,'url':url});last=n
    context['progress']=progress
    template = 'core/operations/reconcile.html' if not queue_context and request.GET.get('advanced') == '1' else 'core/operations/reconcile_simple.html'
    return render(request,template,context)


@manage
@require_http_methods(['GET'])
def reconciliation_queue(request):
    query=request.GET.get('q','').strip();supplier=request.GET.get('supplier','');status=request.GET.get('state','attention')
    qs=Facture.objects.filter(kind='invoice').select_related('fournisseur__restaurant').prefetch_related('lignes').order_by('date_facture','pk')
    if status!='all': qs=qs.filter(document_role='active')
    if supplier.isdigit(): qs=qs.filter(fournisseur_id=supplier)
    if query: qs=qs.filter(Q(numero_facture__icontains=query)|Q(fournisseur__nom__icontains=query))
    rows=[];totals={'attention':0,'waiting':0,'matched':0,'unverified':0}
    for invoice in qs:
        entry=automatic.queue_entry(invoice)
        state=entry['state'];key=entry['bucket']
        totals[key]+=1
        if status=='attention' and key!='attention': continue
        if status in ('waiting','matched','unverified') and key!=status: continue
        rows.append({'invoice':invoice,'state':state})
    scope={'state':status,'supplier':supplier,'q':query}
    params=urlencode(scope)
    context={'query':query,'supplier':supplier,'suppliers':Fournisseur.objects.all(),
             'status':status,'totals':totals,'params':params,'queue_scope':scope}
    if not rows:
        return render(request,'core/operations/reconciliation_queue.html',context)
    try:
        position=max(1,min(int(request.GET.get('position',1)),len(rows)))
    except ValueError:
        position=1
    requested=request.GET.get('invoice','')
    if requested.isdigit():
        position=next((i+1 for i,row in enumerate(rows) if row['invoice'].pk==int(requested)),position)
    context.update({'position':position,'count':len(rows),
        'neighbors':{'previous':rows[position-2]['invoice'] if position>1 else None,
                     'next':rows[position]['invoice'] if position<len(rows) else None},
        'previous_url':'?'+params+'&position='+str(position-1) if position>1 else '',
        'next_url':'?'+params+'&position='+str(position+1) if position<len(rows) else ''})
    return invoice_reconcile(request,rows[position-1]['invoice'].pk,queue_context=context)


@manage
@require_http_methods(['GET','POST'])
def delivery_as_invoice(request, pk):
    delivery=get_object_or_404(BonLivraison.objects.select_related('fournisseur'),pk=pk)
    existing=Facture.objects.filter(source_delivery=delivery).first()
    if existing: return redirect('invoice_reconcile',pk=existing.pk)
    if request.method=='POST':
        try:
            with transaction.atomic():
                Fournisseur.objects.select_for_update().get(pk=delivery.fournisseur_id)
                delivery=BonLivraison.objects.select_for_update().get(pk=pk)
                existing=Facture.all_objects.filter(source_delivery=delivery).first()
                if existing: raise ValidationError('Une facture existe déjà pour ce BL, éventuellement dans les archives.')
                if delivery.invoice_links.exists(): raise ValidationError('Ce BL est déjà rattaché à une facture. Vérifiez ce lien avant de le convertir.')
                if delivery.statut=='analyse_en_cours': raise ValidationError('Attendez la fin de l’analyse du BL.')
                number=request.POST.get('number','').strip();currency=request.POST.get('currency')
                if not number or len(number)>100 or currency not in ('CHF','EUR'): raise ValidationError('Confirmez le numéro de facture et sa devise.')
                try: invoice_date=date.fromisoformat(request.POST.get('date',''))
                except ValueError: raise ValidationError('Confirmez la date de facture.')
                reason=service.reason_text(request.POST.get('reason'))
                if Facture.objects.filter(fournisseur=delivery.fournisseur,numero_facture__iexact=number,document_role='active').exists():
                    raise ValidationError('Ce numéro de facture existe déjà chez ce fournisseur.')
                invoice=Facture.objects.create(fournisseur=delivery.fournisseur,source_delivery=delivery,fichier=delivery.fichier.name,
                    file_sha256=delivery.file_sha256,numero_facture=number,date_facture=invoice_date,date_livraison_detectee=delivery.date_livraison,
                    devise=currency,delivery_mode='combined',statut='a_valider',reconciliation_note=reason)
                for dl in delivery.lignes.exclude(statut_ligne='ignoree'):
                    LigneFacture.objects.create(facture=invoice,reference_produit=dl.reference_produit,designation=dl.designation,
                        quantite=dl.quantite_livree,unite=dl.unite,prix_unitaire_ht=dl.prix_unitaire,montant_ht=dl.total_ligne,ordre=dl.ordre,
                        source_data={'source_delivery_id':delivery.pk,'source_line_id':dl.pk,'basis':'BL faisant fonction de facture · données à vérifier'})
            messages.success(request,'Le même fichier sert désormais de BL et de facture. Vérifiez les données financières extraites avant le contrôle des prix.')
            return redirect('invoice_reconcile',pk=invoice.pk)
        except ValidationError as exc: messages.warning(request,' '.join(exc.messages))
    return render(request,'core/operations/delivery_as_invoice.html',{'delivery':delivery})
