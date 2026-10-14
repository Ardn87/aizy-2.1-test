import logging
from pathlib import Path
from uuid import uuid4
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction, connection
from django.db.models import Q, Max, Count, Prefetch
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render, redirect
from django.template.loader import render_to_string
from django.urls import reverse
from django.views.decorators.http import require_http_methods
from django.utils import timezone
from .access import role_required, ROLE_ADMIN, ROLE_GESTIONNAIRE, ROLE_RECEPTION, user_can_access_admin
from .models import (Facture, BonLivraison, CatalogueFournisseur, Fournisseur, Restaurant,
    AnalysisVersion, AnalysisJob, BusinessAudit, UnitConversion, LigneBonLivraison, DeliveryIssue)
from .operations_forms import DocumentDepositForm, UnitConversionForm, CatalogueProductPriceForm, CataloguePeriodForm, CatalogueProductCreateForm
from .services.uploads import content_hash
from .services.operations import catalogue_health
from .services.extraction import apply_version, document_type

logger = logging.getLogger(__name__)

manage = role_required(ROLE_ADMIN, ROLE_GESTIONNAIRE)
MODELS = {'facture':Facture, 'bon_livraison':BonLivraison, 'catalogue':CatalogueFournisseur}
DETAILS = {'facture':'facture_detail','bon_livraison':'bon_livraison_detail','catalogue':'catalogue_detail'}

def document_for(kind, pk, archived=False):
    model = MODELS.get(kind)
    if model is None:
        from django.http import Http404
        raise Http404
    return get_object_or_404(model.all_objects if archived else model.objects, pk=pk)

@role_required(ROLE_ADMIN, ROLE_GESTIONNAIRE, ROLE_RECEPTION)
@require_http_methods(['GET','POST'])
def deposit(request):
    wants_json='application/json' in request.headers.get('Accept','')
    form = DocumentDepositForm(request.POST or None, request.FILES or None, initial=request.session.get('last_deposit',{}))
    if request.method=='POST' and form.is_valid():
        from .services.jobs import enqueue
        supplier = form.cleaned_data['fournisseur']; selected_kind = form.cleaned_data['kind']
        kind = 'facture' if selected_kind == 'combined' else selected_kind
        with transaction.atomic():
            documents=[]
            for file in form.cleaned_data['files']:
                doc = MODELS[kind](fournisseur=supplier, fichier=file, file_sha256=content_hash(file), note=form.cleaned_data['duplicate_reason'])
                if kind=='facture':
                    doc.devise='UNK'
                    if selected_kind=='combined': doc.delivery_mode='combined'
                elif form.cleaned_data['delivery_comments']:
                    doc.delivery_feedback_requested_at=timezone.now()
                doc.save(); enqueue(kind,doc.pk)
                documents.append({'id':doc.pk,'kind':kind,
                    'feedback_url':reverse('delivery_feedback',args=[doc.pk]) if kind=='bon_livraison' and form.cleaned_data['delivery_comments'] else ''})
        request.session['last_deposit']={'restaurant':supplier.restaurant_id,'fournisseur':supplier.pk,'kind':selected_kind}
        if wants_json:
            return JsonResponse({'received':len(documents),'supplier':supplier.nom,'documents':documents,
                'message':f'{len(documents)} document(s) reçu(s). Leur analyse est programmée.'},status=201)
        messages.success(request,f'{len(documents)} document(s) reçu(s). Le traitement est programmé et les fichiers sont conservés.')
        if form.cleaned_data['delivery_comments']:
            return redirect(reverse('document_deposit')+f'?feedback={documents[0]["id"]}')
        return redirect('document_deposit')
    if request.method=='POST' and wants_json:
        return JsonResponse({'errors':form.errors.get_json_data()},status=400)
    pending_feedback_query=BonLivraison.objects.filter(
        delivery_feedback_requested_at__isnull=False,
        delivery_feedback_completed_at__isnull=True,
    ).select_related('fournisseur').order_by('-delivery_feedback_requested_at','-pk')
    pending_feedback_page=Paginator(pending_feedback_query,10).get_page(request.GET.get('feedback_page'))
    resume_feedback_id=request.GET.get('feedback','')
    if not resume_feedback_id.isdigit() or not pending_feedback_query.filter(pk=resume_feedback_id).exists():
        resume_feedback_id=None
    else:
        resume_feedback_id=int(resume_feedback_id)
    return render(request,'core/operations/deposit.html',{'form':form,
        'pending_feedback_deliveries':pending_feedback_page.object_list,
        'pending_feedback_page':pending_feedback_page,
        'resume_feedback_id':resume_feedback_id,
        'resume_feedback_url':reverse('delivery_feedback',args=[resume_feedback_id]) if resume_feedback_id else ''})


def _delivery_feedback_payload(request, delivery):
    if delivery.delivery_feedback_completed_at:
        return {'state':'completed','message':'Les commentaires de ce bon de livraison sont enregistrés.'}
    job=AnalysisJob.objects.filter(document_type='bon_livraison',document_id=delivery.pk).order_by('-pk').first()
    if delivery.statut==BonLivraison.Statut.ANALYSE_ECHOUEE or (job and job.state=='failed'):
        return {'state':'failed','message':'L’analyse n’a pas abouti. Le BL est conservé et le commentaire pourra être repris après une nouvelle analyse.'}
    if delivery.statut==BonLivraison.Statut.ANALYSE_EN_COURS or (job and job.state in ('queued','running')):
        return {'state':'processing','message':'Analyse du bon de livraison en cours…'}
    lines=list(delivery.lignes.filter(archived_at__isnull=True).exclude(
        statut_ligne=LigneBonLivraison.StatutLigne.IGNOREE).order_by('ordre','pk'))
    if lines:
        return {'state':'ready','html':render_to_string(
            'core/operations/delivery_feedback_form.html',
            {'delivery':delivery,'lines':lines},request=request)}
    if job and job.state=='done':
        return {'state':'empty','message':'Aucun produit n’a été reconnu. Le BL est conservé et doit être complété avant de saisir un commentaire.'}
    return {'state':'processing','message':'Analyse du bon de livraison en cours…'}


@role_required(ROLE_ADMIN, ROLE_GESTIONNAIRE, ROLE_RECEPTION)
@require_http_methods(['GET','POST'])
def delivery_feedback(request, pk):
    wants_json='application/json' in request.headers.get('Accept','')
    delivery=get_object_or_404(BonLivraison.objects.select_related('fournisseur__restaurant'),pk=pk,
        delivery_feedback_requested_at__isnull=False)
    if request.method=='GET':
        response=JsonResponse(_delivery_feedback_payload(request,delivery))
        response['Cache-Control']='no-store'
        return response
    if delivery.delivery_feedback_completed_at:
        return JsonResponse({'state':'completed','message':'Les commentaires de ce bon de livraison sont déjà enregistrés.'})
    action=request.POST.get('action')
    try:
        with transaction.atomic():
            delivery=BonLivraison.objects.select_for_update().get(pk=delivery.pk)
            if delivery.delivery_feedback_completed_at:
                return JsonResponse({'state':'completed','message':'Les commentaires de ce bon de livraison sont déjà enregistrés.'})
            job=AnalysisJob.objects.filter(
                document_type='bon_livraison',document_id=delivery.pk).order_by('-pk').first()
            available_lines=LigneBonLivraison.objects.filter(
                bon_livraison=delivery,archived_at__isnull=True).exclude(
                statut_ligne=LigneBonLivraison.StatutLigne.IGNOREE)
            if (not job or job.state!='done'
                    or delivery.statut in (BonLivraison.Statut.ANALYSE_EN_COURS,BonLivraison.Statut.ANALYSE_ECHOUEE)
                    or not available_lines.exists()):
                raise ValidationError(
                    'Attendez la fin de l’analyse et l’affichage des produits avant de terminer ce commentaire.')
            if action=='none':
                selected=[]
            elif action=='save':
                raw_ids=request.POST.getlist('line')
                if not raw_ids or any(not value.isdigit() for value in raw_ids):
                    raise ValidationError('Sélectionnez au moins un produit en défaut.')
                selected=list(LigneBonLivraison.objects.select_for_update().filter(
                    pk__in={int(value) for value in raw_ids},bon_livraison=delivery,
                    archived_at__isnull=True).exclude(statut_ligne=LigneBonLivraison.StatutLigne.IGNOREE))
                if len(selected)!=len(set(raw_ids)):
                    raise ValidationError('La liste des produits a changé. Vérifiez le bon de livraison actualisé.')
                if DeliveryIssue.objects.filter(line_id__in=[line.pk for line in selected]).exists():
                    raise ValidationError('Un problème est déjà enregistré pour l’un des produits sélectionnés.')
                from .services.reconciliation import line_fingerprint
                for line in selected:
                    comment=request.POST.get(f'comment_{line.pk}','').strip()
                    issue=DeliveryIssue(
                        delivery=delivery,line=line,comment=comment,
                        source_fingerprint=line_fingerprint(line),reported_by=request.user,
                        snapshot={
                            'supplier':delivery.fournisseur.nom,
                            'restaurant':delivery.fournisseur.restaurant.nom,
                            'delivery_number':delivery.numero_bon_livraison,
                            'delivery_date':delivery.date_livraison.isoformat() if delivery.date_livraison else None,
                            'reference':line.reference_produit,
                            'designation':line.designation,
                            'quantity':str(line.quantite_livree) if line.quantite_livree is not None else None,
                            'unit':line.unite,
                        })
                    issue.save()
            else:
                raise ValidationError('Action inconnue.')
            delivery.delivery_feedback_completed_at=timezone.now()
            delivery.delivery_feedback_completed_by=request.user
            delivery.save(update_fields=['delivery_feedback_completed_at','delivery_feedback_completed_by','date_modification'])
    except ValidationError as exc:
        message=' '.join(exc.messages)
        if wants_json:
            return JsonResponse({'error':message},status=400)
        messages.error(request,message)
        return redirect(reverse('document_deposit')+f'?feedback={delivery.pk}')
    except Exception:
        reference=uuid4().hex[:8].upper()
        logger.exception(
            'Echec inattendu de l’enregistrement des commentaires BL: reference=%s delivery=%s user=%s action=%s',
            reference,delivery.pk,request.user.pk,action)
        if wants_json:
            return JsonResponse({'error':(
                'L’enregistrement n’a pas abouti. Vos commentaires restent saisis. '
                f'Référence technique : {reference}.')},status=500)
        raise
    message=(f'{len(selected)} problème(s) de livraison transmis pour revue.' if selected
        else 'Le bon de livraison est enregistré sans problème signalé.')
    logger.info('Commentaires BL enregistrés: delivery=%s user=%s issues=%s',delivery.pk,request.user.pk,len(selected))
    if wants_json:
        return JsonResponse({'state':'completed','message':message})
    messages.success(request,message)
    return redirect('document_deposit')

@manage
@require_http_methods(['GET'])
def work_documents(request):
    kind=request.GET.get('kind','facture')
    if kind not in MODELS: kind='facture'
    qs=MODELS[kind].objects.select_related('fournisseur__restaurant')
    state=request.GET.get('state','attention'); query=request.GET.get('q','').strip()
    if state=='attention': qs=qs.exclude(statut__in=['validee','valide','litige'])
    elif state=='failed': qs=qs.filter(statut='analyse_echouee')
    elif state=='running': qs=qs.filter(statut='analyse_en_cours')
    if request.GET.get('supplier','').isdigit(): qs=qs.filter(fournisseur_id=request.GET['supplier'])
    if query:
        number={'facture':'numero_facture','bon_livraison':'numero_bon_livraison','catalogue':'nom'}[kind]
        qs=qs.filter(Q(**{number+'__icontains':query})|Q(fournisseur__nom__icontains=query))
    page=Paginator(qs.order_by('-date_modification','-pk'),25).get_page(request.GET.get('page'))
    for d in page:
        d.detail_url=reverse(DETAILS[kind],args=[d.pk])
    return render(request,'core/operations/documents.html',{'page':page,'kind':kind,'state':state,'query':query,'preserved_query':request.GET.urlencode(),'suppliers':Fournisseur.objects.all()})

@manage
@require_http_methods(['GET'])
def catalogue_coverage(request):
    catalogues=CatalogueFournisseur.objects.filter(origine='documents').annotate(
        products=Count('lignes',filter=Q(lignes__archived_at__isnull=True),distinct=True),
        priced=Count('lignes',filter=Q(lignes__archived_at__isnull=True,lignes__prix_unitaire_ht__isnull=False),distinct=True))
    suppliers=Fournisseur.objects.filter(actif=True).select_related('restaurant').prefetch_related(Prefetch('catalogues',queryset=catalogues,to_attr='product_catalogues'))
    query=request.GET.get('q','').strip()
    if query: suppliers=suppliers.filter(Q(nom__icontains=query)|Q(restaurant__nom__icontains=query))
    rows=[]
    for supplier in suppliers.order_by('nom'):
        catalogue=supplier.product_catalogues[0] if supplier.product_catalogues else None
        rows.append({'supplier':supplier,'catalogue':catalogue,'products':catalogue.products if catalogue else 0,'priced':catalogue.priced if catalogue else 0})
    return render(request,'core/operations/coverage.html',{'rows':rows,'query':query,'totals':{'suppliers':len(rows),'products':sum(r['products'] for r in rows),'priced':sum(r['priced'] for r in rows)}})


@manage
@require_http_methods(['GET'])
def catalogue_coverage_legacy(request):
    return redirect(reverse('catalogue_coverage') + ('?' + request.GET.urlencode() if request.GET else ''))


@manage
@require_http_methods(['GET','POST'])
def supplier_catalogue(request, pk):
    supplier=get_object_or_404(Fournisseur.objects.select_related('restaurant'),pk=pk)
    catalogue=CatalogueFournisseur.objects.filter(fournisseur=supplier,origine='documents').first()
    period_form=CataloguePeriodForm(instance=catalogue) if catalogue else None
    product_form=CatalogueProductCreateForm(request.POST if request.method=='POST' and request.POST.get('action')=='create' else None)
    if request.method=='POST':
        if request.POST.get('action')=='refresh':
            from .services.catalogue_builder import build_supplier_catalogue, fill_catalogue_prices
            catalogue,result=build_supplier_catalogue(supplier)
            prices=fill_catalogue_prices(catalogue,request.user)
            messages.success(request,f'{result["added"]} nouveau(x) produit(s) récupéré(s) depuis les documents, {prices["filled"]} prix renseigné(s). Les prix présents sont conservés.')
            return redirect('supplier_catalogue',pk=pk)
        if request.POST.get('action') == 'create':
            if product_form.is_valid():
                from .models import LigneCatalogue
                from .services.catalogue_builder import build_supplier_catalogue, product_key
                from .services.price_review import record_price
                with transaction.atomic():
                    Fournisseur.objects.select_for_update().get(pk=pk)
                    if catalogue is None:
                        catalogue, _ = build_supplier_catalogue(supplier)
                    product = product_form.save(commit=False)
                    key = product_key(product.reference_produit, product.ean, product.designation, product.unite)
                    if any(product_key(l.reference_produit,l.ean,l.designation,l.unite) == key for l in catalogue.lignes.all()):
                        product_form.add_error(None, 'Ce produit et cette unité existent déjà dans le catalogue.')
                    else:
                        product.catalogue=catalogue
                        product.ordre=(catalogue.lignes.aggregate(last=Max('ordre'))['last'] or 0)+1
                        product.corrigee_manuellement=True
                        product.save()
                        if product.prix_unitaire_ht is not None:
                            record_price(product,request.user,None)
                        messages.success(request, 'Produit ajouté au catalogue.')
                        return redirect('supplier_catalogue',pk=pk)
        elif request.POST.get('action') in ('settings','activate') and catalogue:
            with transaction.atomic():
                Fournisseur.objects.select_for_update().get(pk=pk)
                catalogue=CatalogueFournisseur.objects.select_for_update().get(pk=catalogue.pk)
                activating=request.POST['action']=='activate'
                period_form=CataloguePeriodForm(request.POST,instance=catalogue,require_date=activating)
                if activating and not catalogue.lignes.filter(prix_unitaire_ht__isnull=False).exists():
                    period_form.add_error(None,'Renseignez au moins un prix catalogue avant d’activer le contrôle.')
                if period_form.is_valid():
                    catalogue=period_form.save(commit=False)
                    if activating: catalogue.statut='valide'
                    try: catalogue.save()
                    except ValidationError as exc: period_form.add_error(None,exc)
                    else:
                        messages.success(request,'Les prix catalogue sont applicables à la période confirmée.' if activating else 'Devise et période enregistrées.')
                        return redirect('supplier_catalogue',pk=pk)
        else:
            messages.warning(request,'Cette action n’est pas disponible.')
    query=request.GET.get('q','').strip();state=request.GET.get('state','all')
    lines=catalogue.lignes.all() if catalogue else LigneFacture.objects.none()
    counts={'products':lines.count(),'priced':lines.filter(prix_unitaire_ht__isnull=False).count()}
    if query: lines=lines.filter(Q(designation__icontains=query)|Q(reference_produit__icontains=query)|Q(ean__icontains=query))
    if state=='missing': lines=lines.filter(prix_unitaire_ht__isnull=True)
    elif state=='priced': lines=lines.filter(prix_unitaire_ht__isnull=False)
    page=Paginator(lines.order_by('designation','pk'),40).get_page(request.GET.get('page'))
    for line in page:
        line.price_form=CatalogueProductPriceForm(initial={'price':line.prix_unitaire_ht,'previous_price':str(line.prix_unitaire_ht) if line.prix_unitaire_ht is not None else ''})
        observations=[dict(o) for o in line.observations]
        if line.price_source.get('read_from_pdf') or line.price_source.get('read_from_image'):
            for observation in observations:
                if observation['kind'] == line.price_source['kind'] and observation['line_id'] == line.price_source['line_id']:
                    observation.update(price=line.price_source['price'],basis=line.price_source['basis'],read_from_pdf=bool(line.price_source.get('read_from_pdf')),read_from_image=bool(line.price_source.get('read_from_image')),unit=line.price_source.get('unit',observation.get('unit')))
        line.observations=observations
        for observation in observations:
            observation['url']=reverse('facture_detail' if observation['kind']=='facture' else 'bon_livraison_detail',args=[observation['document_id']])
            observation['label']='Facture' if observation['kind']=='facture' else 'BL'
        line.observed=next((o for o in observations if o['price'] is not None),None)
        line.price_variants=len({(o['price'],o['currency']) for o in observations if o['price'] is not None})
        line.source_count=len({(o['kind'],o['document_id']) for o in observations})
    return render(request,'core/operations/supplier_catalogue.html',{'supplier':supplier,'catalogue':catalogue,'page':page,'counts':counts,'period_form':period_form,'product_form':product_form,'query':query,'state':state})


@manage
@require_http_methods(['POST'])
def catalogue_product_price(request, pk):
    from .models import LigneCatalogue
    from .services.price_review import record_price
    line=get_object_or_404(LigneCatalogue.objects.select_related('catalogue'),pk=pk,catalogue__origine='documents')
    form=CatalogueProductPriceForm(request.POST)
    if form.is_valid():
        with transaction.atomic():
            Fournisseur.objects.select_for_update().get(pk=line.catalogue.fournisseur_id)
            line=LigneCatalogue.objects.select_for_update().select_related('catalogue').get(pk=pk)
            previous=form.cleaned_data['previous_price']
            if previous != (str(line.prix_unitaire_ht) if line.prix_unitaire_ht is not None else ''):
                messages.warning(request,'Le prix a changé depuis l’ouverture de cette fiche. Actualisez avant de confirmer.')
            elif line.catalogue.devise=='UNK' or not line.unite.strip():
                messages.warning(request,'Confirmez d’abord la devise du catalogue et l’unité du produit depuis ses informations.')
            else:
                old=line.prix_unitaire_ht;line.prix_unitaire_ht=form.cleaned_data['price'];line.corrigee_manuellement=True
                line.statut_ligne='ok';line.save();record_price(line,request.user,old)
                messages.success(request,'Prix catalogue HT enregistré. Le prix relevé reste visible dans les sources.')
    else: messages.warning(request,'Indiquez un prix HT positif ou nul, avec deux décimales maximum.')
    return redirect('supplier_catalogue',pk=line.catalogue.fournisseur_id)

@manage
@require_http_methods(['GET','POST'])
def units(request, pk):
    supplier=get_object_or_404(Fournisseur,pk=pk)
    edit_id=request.GET.get('edit','')
    rule=get_object_or_404(UnitConversion,pk=edit_id,fournisseur=supplier) if edit_id.isdigit() else UnitConversion(fournisseur=supplier)
    rule.confirmed_by=request.user
    form=UnitConversionForm(request.POST or None,instance=rule)
    if request.method=='POST' and form.is_valid():
        form.save();messages.success(request,'Conversion confirmée. Le contrôle de prix sera recalculé.');return redirect('supplier_units',pk=pk)
    return render(request,'core/operations/units.html',{'supplier':supplier,'form':form,'rules':supplier.unit_conversions.all()})

@manage
@require_http_methods(['GET','POST'])
def extraction_changes(request, kind, pk):
    document=document_for(kind,pk)
    versions=AnalysisVersion.objects.filter(document_type=kind,document_id=pk).order_by('-pk')
    latest=versions.first()
    if request.method=='POST':
        try:
            with transaction.atomic():
                # Supplier is the common lock for pricing/reconciliation/extraction.
                Fournisseur.objects.select_for_update().get(pk=document.fournisseur_id)
                version=apply_version(document,request.POST.get('version','0') if request.POST.get('version','').isdigit() else 0)
            messages.success(request,'Différences appliquées. Vérifiez les données avant de valider le document.')
        except (ValidationError,AnalysisVersion.DoesNotExist) as exc:
            messages.warning(request,' '.join(exc.messages) if isinstance(exc,ValidationError) else 'Version introuvable.')
        return redirect(DETAILS[kind],pk=pk)
    changes=[]
    for c in latest.changes if latest else []:
        keys=set(c['before'])|set(c['after'])
        pairs=[{'label':(type(document) if c['operation']=='header' else document.lignes.model)._meta.get_field(k).verbose_name, 'before':c['before'].get(k),'after':c['after'].get(k)} for k in sorted(keys) if c['before'].get(k)!=c['after'].get(k)]
        changes.append({'operation':{'update':'Modifier','create':'Ajouter','archive':'Archiver','header':'Modifier les données du document'}[c['operation']], 'line': c['line_id'], 'pairs':pairs})
    return render(request,'core/operations/extraction.html',{'document':document,'kind':kind,'latest':latest,'changes':changes,'versions':versions[:10],'back':reverse(DETAILS[kind],args=[pk])})


@manage
@require_http_methods(['GET'])
def audit_log(request):
    qs=BusinessAudit.objects.select_related('actor','restaurant')
    if not user_can_access_admin(request.user):
        qs=qs.filter(restaurant__in=Restaurant.objects.all())
    if request.GET.get('object','').isdigit(): qs=qs.filter(object_id=request.GET['object'],model_label=request.GET.get('model',''))
    page=Paginator(qs,30).get_page(request.GET.get('page'))
    from django.apps import apps
    for event in page:
        event.changes=[]
        for key in sorted(set(event.before)|set(event.after)):
            if event.before.get(key)==event.after.get(key) or key in ('date_modification','source_data','file_sha256','control_revision','source_key'):
                continue
            try:
                label=apps.get_model(event.model_label)._meta.get_field(key.removesuffix('_id')).verbose_name
            except Exception:
                label=key.replace('_',' ')
            event.changes.append({'label':label,'before':event.before.get(key),'after':event.after.get(key)})
    return render(request,'core/operations/audit.html',{'page':page, 'preserved_query':request.GET.urlencode()})

@role_required(ROLE_ADMIN)
@require_http_methods(['GET','POST'])
def archives(request):
    if request.method=='POST':
        identifier=request.POST.get('document','')
        doc=document_for(request.POST.get('kind'),identifier if identifier.isdigit() else 0,archived=True)
        doc.archived_at=None; doc.save(update_fields=['archived_at','date_modification'])
        messages.success(request,'Document restauré.');return redirect('document_archives')
    rows=[]
    for kind,model in MODELS.items():
        for doc in model.all_objects.filter(archived_at__isnull=False).select_related('fournisseur').order_by('-archived_at')[:100]:
            rows.append({'kind':kind,'document':doc})
    return render(request,'core/operations/archives.html',{'rows':rows})

@manage
@require_http_methods(['GET'])
def job_status(request, kind, pk):
    doc=document_for(kind,pk)
    job=AnalysisJob.objects.filter(document_type=kind,document_id=pk).order_by('-pk').first()
    return JsonResponse({'status':doc.statut, 'label':doc.get_statut_display(), 'job':job.state if job else None})

@require_http_methods(['GET'])
def health(request):
    from django.conf import settings
    import os, tempfile
    try:
        with connection.cursor() as cursor: cursor.execute('SELECT 1'); cursor.fetchone()
        with tempfile.TemporaryFile(dir=settings.MEDIA_ROOT) as f: f.write(b'health');f.flush()
        from django.db.migrations.executor import MigrationExecutor
        executor=MigrationExecutor(connection)
        if executor.migration_plan(executor.loader.graph.leaf_nodes()): raise RuntimeError
    except Exception:
        return JsonResponse({'status':'unavailable'},status=503)
    return JsonResponse({'status':'ok'})

@manage
@require_http_methods(['GET','POST'])
def catalogue_columns(request, pk):
    catalogue=get_object_or_404(CatalogueFournisseur,pk=pk)
    if catalogue.origine == 'documents':
        messages.info(request,'Ce catalogue est constitué de fiches produits. La correspondance de colonnes concerne les fichiers CSV ou XLSX importés.')
        return redirect('supplier_catalogue',pk=catalogue.fournisseur_id)
    if not catalogue.fichier_original:
        messages.warning(request,'Ce catalogue ne contient aucun fichier à lire.')
        return redirect('catalogue_detail',pk=pk)
    from .services.catalogue_ai import (_read_tabular_rows,_detect_header_mapping,_catalogue_line_from_row,
        _tabular_line_has_product_content,CATALOGUE_LINE_FIELDS,TABULAR_CATALOGUE_EXTENSIONS)
    from .views import sync_catalogue_lines_from_ai
    from .services.extraction import record_version
    path=Path(catalogue.fichier_original.path)
    if path.suffix.lower() not in TABULAR_CATALOGUE_EXTENSIONS:
        messages.warning(request,'Cet écran est réservé aux CSV et XLSX.');return redirect('catalogue_detail',pk=pk)
    try:
        rows=_read_tabular_rows(path)
    except Exception:
        messages.warning(request,'Ce tableau ne peut pas être lu. Vérifiez le fichier et réimportez-le.');return redirect('catalogue_detail',pk=pk)
    header,mapping=_detect_header_mapping(rows)
    if request.method=='POST':
        try:
            header=int(request.POST.get('header_row','0'))
            if header<0 or header>=len(rows): raise ValueError
            mapping={i:request.POST.get('column_'+str(i),'') for i in range(len(rows[header])) if request.POST.get('column_'+str(i),'') in CATALOGUE_LINE_FIELDS}
            if len(mapping.values())!=len(set(mapping.values())) or 'prix_unitaire_ht' not in mapping.values() or not {'reference_produit','ean','designation'} & set(mapping.values()):
                raise ValidationError('Choisissez un prix HT et un identifiant ou une désignation, sans affecter deux colonnes au même champ.')
            lines=[_catalogue_line_from_row(row,mapping) for row in rows[header+1:]]
            lines=[line for line in lines if _tabular_line_has_product_content(line)]
            if not lines: raise ValidationError('Aucune ligne produit exploitable dans ces colonnes.')
            with transaction.atomic():
                Fournisseur.objects.select_for_update().get(pk=catalogue.fournisseur_id)
                sync_catalogue_lines_from_ai(catalogue,lines)
                payload={'lignes':lines,'mapping':{str(k):v for k,v in mapping.items()},'header_row':header,'source':'local_mapping'}
                record_version(catalogue,payload)
                catalogue.donnees_ia=payload;catalogue.statut='a_valider';catalogue.save()
            messages.success(request,'Colonnes confirmées. Les éventuelles différences de lignes restent à approuver.');return redirect('catalogue_detail',pk=pk)
        except (ValueError,ValidationError) as exc:
            messages.warning(request,' '.join(exc.messages) if isinstance(exc,ValidationError) else 'Choisissez une ligne d’en-tête existante.')
    header=header if 0<=header<len(rows) else 0
    columns=[{'index':i,'label':value,'selected':mapping.get(i,'')} for i,value in enumerate(rows[header] if rows else [])]
    return render(request,'core/operations/columns.html',{'catalogue':catalogue,'columns':columns,'fields':[{'key':f,'label':{'reference_produit':'Référence produit','ean':'Code EAN','designation':'Nom du produit','marque':'Marque','unite':'Unité','conditionnement':'Conditionnement','prix_unitaire_ht':'Prix unitaire HT'}[f]} for f in sorted(CATALOGUE_LINE_FIELDS)],'header':header,'sample':rows[header+1:header+4],'row_count':len(rows)})
