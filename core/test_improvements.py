import hashlib
import io
import json
import tarfile
import tempfile
from pathlib import Path
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import connection
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from .models import (Restaurant, RestaurantAccess, Fournisseur, Facture, BonLivraison,
    LigneFacture,LigneBonLivraison,CatalogueFournisseur,LigneCatalogue,AnalysisVersion,
    AnalysisJob, BusinessAudit, SupplierClaim, SupplierClaimLine, UnitConversion,
    InvoiceDeliveryLink, DeliveryAllocation, InvoiceControlCache)
from .forms import FactureForm, BonLivraisonForm, UserPasswordForm, UserCreateForm, validate_document_file
from .services.price_review import current_detail, review_queue
from .services.extraction import apply_version
from .services.jobs import enqueue, process_next
from .services.operations import allocate, reconciliation, catalogue_health, archive_document
from .services.quality import invoice_checks
from .views import sync_invoice_lines_from_ai, apply_invoice_ai_analysis, cascade_restaurant_currency


class ImprovementsTests(TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.settings_override=override_settings(MEDIA_ROOT=self.temp.name);self.settings_override.enable();self.addCleanup(self.settings_override.disable)
        self.restaurant=Restaurant.objects.create(nom='Restaurant QA',devise_defaut='CHF')
        self.supplier=Fournisseur.objects.create(restaurant=self.restaurant,nom='Fournisseur QA',devise_defaut='CHF')
        self.user=get_user_model().objects.create_user('controle_qa',password='a-secure-test-pass')
        self.user.groups.add(Group.objects.create(name='Gestionnaire'))
        RestaurantAccess.objects.create(user=self.user,restaurant=self.restaurant)
        self.client.force_login(self.user)
        self.cat=CatalogueFournisseur.objects.create(fournisseur=self.supplier,date_debut=date(2026,1,1),statut='valide',devise='CHF')
        self.product=LigneCatalogue.objects.create(catalogue=self.cat,reference_produit='P',designation='Produit',unite='kg',prix_unitaire_ht=8)
        self.invoice=Facture.objects.create(fournisseur=self.supplier,fichier=SimpleUploadedFile('qa.pdf',b'%PDF-1.7\nsource'),numero_facture='F-QA',date_facture=date(2026,10,1),devise='CHF',statut='a_valider',montant_ht=20,montant_tva=0,montant_ttc=20)
        self.line=LigneFacture.objects.create(facture=self.invoice,reference_produit='P',designation='Produit',unite='kg',quantite=2,prix_unitaire_ht=10,montant_ht=20,montant_ttc=20,ordre=0)

    def verify(self):
        self.invoice.refresh_from_db();self.invoice.statut='validee';self.invoice.save();self.line.facture=self.invoice

    def claim(self):
        self.line.refresh_from_db();self.line.facture.refresh_from_db()
        detail=current_detail(self.line)
        return self.client.post(reverse('review_action',args=[self.line.pk]),{'action':'claim','fingerprint':detail['fingerprint']})

    def payload(self,price='10'):
        return {'numero_facture':'F-QA','date_facture':'2026-10-01','devise':'CHF','montant_ht':'20','montant_tva':'0','montant_ttc':'20','confiance_ia':.9,
            'lignes':[{'reference_produit':'P','designation':'Produit','quantite':'2','unite':'kg','prix_unitaire_ht':price,'montant_ht':'20','montant_ttc':'20'}]}

    def test_currency_preference_preserves_all_historical_amounts(self):
        self.restaurant.devise_defaut='EUR';self.restaurant.save();cascade_restaurant_currency(self.restaurant,'CHF')
        self.invoice.refresh_from_db();self.cat.refresh_from_db();self.supplier.refresh_from_db()
        self.assertEqual(self.invoice.devise,'CHF');self.assertEqual(self.cat.devise,'CHF');self.assertEqual(self.invoice.montant_ht,20)
        self.assertEqual(self.supplier.devise_defaut,'CHF')

    def test_empty_invoice_and_delivery_cannot_be_validated(self):
        form=FactureForm({'statut':'validee','devise':'CHF'},instance=Facture(fournisseur=self.supplier))
        self.assertFalse(form.is_valid());self.assertIn('validation_exception',form.errors)
        form=BonLivraisonForm({'statut':'valide'},instance=BonLivraison(fournisseur=self.supplier))
        self.assertFalse(form.is_valid())

    def test_explicit_reason_allows_validation_exception_and_is_audited(self):
        invoice=Facture.objects.create(fournisseur=self.supplier,fichier='missing.pdf')
        response=self.client.post(reverse('facture_detail',args=[invoice.pk]),{'statut':'validee','devise':'CHF','validation_exception':'Facture de frais sans ligne : justificatif vérifié.'})
        self.assertEqual(response.status_code,302);invoice.refresh_from_db()
        self.assertIsNotNone(invoice.verified_at);self.assertEqual(invoice.verified_by_id,self.user.pk)
        self.assertTrue(BusinessAudit.objects.filter(object_id=invoice.pk,actor=self.user,model_label='core.facture',reason__contains='justificatif').exists())

    def test_unverified_invoice_cannot_create_claim(self):
        self.assertFalse(current_detail(self.line)['can_claim']);self.claim();self.assertEqual(SupplierClaim.objects.count(),0)
        self.verify();self.assertTrue(current_detail(self.line)['can_claim']);self.claim();self.assertEqual(SupplierClaimLine.objects.count(),1)

    def test_legacy_validation_stamp_cannot_hide_missing_totals(self):
        Facture.all_objects.filter(pk=self.invoice.pk).update(statut='validee',verified_at=timezone.now(),montant_ht=None)
        self.invoice.refresh_from_db()
        self.line.facture=self.invoice
        self.assertFalse(current_detail(self.line)['can_claim'])

    def test_manual_correction_requires_revalidation(self):
        self.verify();self.line.corrigee_manuellement=True;self.line.quantite=3;self.line.save();self.invoice.refresh_from_db()
        self.assertIsNone(self.invoice.verified_at);self.assertEqual(self.invoice.statut,'a_valider')

    def test_missing_units_never_conform_even_at_equal_prices(self):
        self.line.unite='';self.line.prix_unitaire_ht=8;self.line.save()
        self.assertEqual(current_detail(self.line)['status'],'unite_a_verifier')

    def test_confirmed_packaging_conversion_uses_catalogue_basis(self):
        self.line.unite='carton';self.line.prix_unitaire_ht=50;self.line.save()
        self.assertEqual(current_detail(self.line)['status'],'unite_a_verifier')
        UnitConversion.objects.create(fournisseur=self.supplier,reference='P',invoice_unit='carton',catalogue_unit='kg',factor=6,reason='Carton 6 × 1 kg confirmé',confirmed_by=self.user)
        detail=current_detail(self.line);self.assertEqual(detail['prix_catalogue'],48);self.assertEqual(detail['impact'],4)
        self.assertFalse(detail['can_price'])

    def test_weak_password_is_rejected(self):
        self.assertFalse(UserPasswordForm({'password1':'a','password2':'a'},user=self.user).is_valid())
        Group.objects.get_or_create(name='Reception')
        form=UserCreateForm({'username':'nouveau','password1':'a','password2':'a','role':'Reception','is_active':True})
        self.assertFalse(form.is_valid());self.assertIn('password1',form.errors)

    def test_login_is_rate_limited_without_exposing_account_existence(self):
        self.client.logout()
        for _ in range(8):self.client.post(reverse('login'),{'username':'absent','password':'wrong'})
        response=self.client.post(reverse('login'),{'username':'absent','password':'wrong'})
        self.assertEqual(response.status_code,429);self.assertContains(response,'Trop de tentatives',status_code=429)

    def test_own_password_change_keeps_session(self):
        response=self.client.post(reverse('own_password'),{'old_password':'a-secure-test-pass','new_password1':'a-new-secure-test-pass','new_password2':'a-new-secure-test-pass'})
        self.assertEqual(response.status_code,302);self.assertEqual(self.client.get(reverse('dashboard')).status_code,200)

    def test_ocr_is_idempotent_and_preserves_manual_line_identity(self):
        self.line.corrigee_manuellement=True;self.line.prix_unitaire_ht=11;self.line.save()
        for _ in range(2):
            with patch('core.views.analyze_invoice',return_value=self.payload()):apply_invoice_ai_analysis(self.invoice)
        self.line.refresh_from_db();self.assertEqual(self.invoice.lignes.count(),1);self.assertEqual(self.line.prix_unitaire_ht,11)
        self.assertEqual(AnalysisVersion.objects.count(),2);self.assertFalse(AnalysisVersion.objects.first().applied)

    def test_ocr_same_document_does_not_duplicate_claim_after_revalidation(self):
        self.verify();self.claim()
        original=self.line.pk
        for _ in range(2):
            with patch('core.views.analyze_invoice',return_value=self.payload()):apply_invoice_ai_analysis(self.invoice)
        self.verify();self.claim()
        self.assertEqual(self.invoice.lignes.get().pk,original);self.assertEqual(SupplierClaimLine.objects.count(),1)

    def test_extraction_differences_are_explicit_and_detect_stale_edits(self):
        with patch('core.views.analyze_invoice',return_value=self.payload('11')):apply_invoice_ai_analysis(self.invoice)
        version=AnalysisVersion.objects.first();self.assertEqual(self.invoice.lignes.get().prix_unitaire_ht,10)
        self.line.prix_unitaire_ht=12;self.line.save()
        with self.assertRaises(ValidationError):apply_version(self.invoice,version.pk)
        self.assertEqual(self.invoice.lignes.get().prix_unitaire_ht,12)

    def test_extraction_confirm_preserves_ids_and_history(self):
        with patch('core.views.analyze_invoice',return_value=self.payload('11')):apply_invoice_ai_analysis(self.invoice)
        version=AnalysisVersion.objects.first();apply_version(self.invoice,version.pk)
        self.assertEqual(self.invoice.lignes.get().pk,self.line.pk);self.assertEqual(self.invoice.lignes.get().prix_unitaire_ht,11)
        version.refresh_from_db();self.assertTrue(version.applied)

    def test_extraction_header_changes_require_confirmation_and_preserve_currency(self):
        payload = self.payload()
        payload.update(numero_facture='F-CORRECTED', devise='EUR')
        with patch('core.views.analyze_invoice', return_value=payload):
            apply_invoice_ai_analysis(self.invoice)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.numero_facture, 'F-QA')
        self.assertEqual(self.invoice.devise, 'CHF')
        version = AnalysisVersion.objects.first()
        self.assertTrue(any(c['operation'] == 'header' for c in version.changes))
        apply_version(self.invoice, version.pk)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.numero_facture, 'F-CORRECTED')
        self.assertEqual(self.invoice.devise, 'EUR')

    def test_header_proposals_refuse_stale_manual_edits(self):
        payload = self.payload()
        payload['numero_facture'] = 'F-NEW'
        with patch('core.views.analyze_invoice', return_value=payload):
            apply_invoice_ai_analysis(self.invoice)
        version = AnalysisVersion.objects.first()
        self.invoice.numero_facture = 'F-MANUAL'
        self.invoice.save()
        with self.assertRaises(ValidationError):
            apply_version(self.invoice, version.pk)

    def test_catalogue_explicit_unknown_currency_is_not_inherited(self):
        cat = CatalogueFournisseur.objects.create(fournisseur=self.supplier, devise='UNK')
        cat.refresh_from_db()
        self.assertEqual(cat.devise, 'UNK')

    def test_repeated_product_reference_stays_two_source_rows(self):
        fresh=Facture.objects.create(fournisseur=self.supplier,fichier='two.pdf')
        data=[{'reference_produit':'P','designation':'Produit','quantite':'1','unite':'kg','prix_unitaire_ht':'8'}, {'reference_produit':'P','designation':'Produit','quantite':'2','unite':'kg','prix_unitaire_ht':'8'}]
        sync_invoice_lines_from_ai(fresh,data);ids=list(fresh.lignes.values_list('pk',flat=True));sync_invoice_lines_from_ai(fresh,data)
        self.assertEqual(list(fresh.lignes.values_list('pk',flat=True)),ids);self.assertEqual(fresh.lignes.count(),2)

    def test_source_page_is_kept_without_inventing_a_page(self):
        fresh=Facture.objects.create(fournisseur=self.supplier,fichier='pages.pdf')
        sync_invoice_lines_from_ai(fresh,[{'designation':'Source lisible','source_page':3},{'designation':'Page inconnue','source_page':None}])
        self.assertEqual(list(fresh.lignes.order_by('ordre').values_list('source_page',flat=True)),[3,None])
        from .services.document_ai import _clean_delivery_line
        from .services.catalogue_ai import _clean_catalogue_line
        self.assertEqual(_clean_delivery_line({'source_page':2})['source_page'],2)
        self.assertEqual(_clean_catalogue_line({'source_page':4})['source_page'],4)
        self.assertIsNone(_clean_catalogue_line({'source_page':0})['source_page'])

    def test_durable_jobs_are_unique_and_survive_queue_retries(self):
        first=enqueue('facture',self.invoice.pk);second=enqueue('facture',self.invoice.pk)
        self.assertEqual(first.pk,second.pk)
        with patch('core.views.analyze_invoice',return_value=self.payload()):self.assertTrue(process_next())
        first.refresh_from_db();self.assertEqual(first.state,'done');self.assertEqual(first.attempts,1)
        self.assertFalse(process_next())

    def test_interrupted_job_is_reclaimed(self):
        job=enqueue('facture',self.invoice.pk);job.state='running';job.started_at=timezone.now()-timedelta(hours=1);job.save()
        with patch('core.views.analyze_invoice',return_value=self.payload()):process_next()
        job.refresh_from_db();self.assertEqual(job.state,'done')

    def test_job_failure_keeps_source_and_can_be_retried(self):
        job=enqueue('facture',self.invoice.pk)
        with patch('core.views.analyze_invoice',side_effect=RuntimeError('OCR unavailable')):process_next()
        job.refresh_from_db();self.invoice.refresh_from_db()
        self.assertEqual(job.state,'failed');self.assertTrue(Path(self.invoice.fichier.path).exists());self.assertEqual(self.invoice.lignes.count(),1)
        self.assertNotEqual(enqueue('facture',self.invoice.pk).pk,job.pk)

    def test_catalogue_csv_import_needs_no_openai_and_preserves_zeroes(self):
        from .services.catalogue_ai import analyze_supplier_catalog
        p=Path(self.temp.name)/'prices.csv';p.write_text('Référence;Désignation;Unité;Prix HT\n00001;Lait;litre;1,25\n')
        with patch('core.services.catalogue_ai._analyze_with_openai',side_effect=AssertionError('No API')):
            data=analyze_supplier_catalog(p)
        self.assertEqual(data['lignes'][0]['reference_produit'],'00001');self.assertEqual(data['devise'],'')

    def test_upload_signature_and_size_rejected_before_analysis(self):
        with self.assertRaises(ValidationError):validate_document_file(SimpleUploadedFile('fake.pdf',b'plain text'))
        with override_settings(DOCUMENT_MAX_UPLOAD_BYTES=4):
            with self.assertRaises(ValidationError):validate_document_file(SimpleUploadedFile('huge.pdf',b'%PDF-1.7\n'))

    def test_reception_can_deposit_invoices_but_not_edit_prices(self):
        self.user.groups.clear();self.user.groups.add(Group.objects.create(name='Reception'))
        response=self.client.post(reverse('document_deposit'),{'kind':'facture','restaurant':self.restaurant.pk,'fournisseur':self.supplier.pk,'files':SimpleUploadedFile('receipt.pdf',b'%PDF-1.7\ninvoice')})
        self.assertEqual(response.status_code,302);self.assertEqual(Facture.objects.count(),2);self.assertEqual(AnalysisJob.objects.count(),1)
        self.assertEqual(self.client.get(reverse('price_review')).status_code,403)

    def test_duplicate_deposit_warns_and_requires_reason(self):
        data={'kind':'facture','restaurant':self.restaurant.pk,'fournisseur':self.supplier.pk}
        for expected in (302,200):
            response=self.client.post(reverse('document_deposit'),{**data,'files':SimpleUploadedFile('same.pdf',b'%PDF-1.7\nsame')})
            self.assertEqual(response.status_code,expected)
        self.assertEqual(Facture.objects.count(),2)
        response=self.client.post(reverse('document_deposit'),{**data,'duplicate_reason':'Copie fournisseur à conserver explicitement','files':SimpleUploadedFile('same.pdf',b'%PDF-1.7\nsame')})
        self.assertEqual(response.status_code,302);self.assertEqual(Facture.objects.count(),3)

    def test_scope_covers_direct_documents_files_review_and_posted_foreign_supplier(self):
        other=Restaurant.objects.create(nom='Autre restaurant');supplier=Fournisseur.objects.create(restaurant=other,nom='Invisible')
        inv=Facture.objects.create(fournisseur=supplier,fichier='other.pdf')
        for name,args in [('facture_detail',[inv.pk]),('facture_file',[inv.pk]),('facture_ai_reanalyze',[inv.pk]),('fournisseur_detail',[supplier.pk])]:
            response=self.client.post(reverse(name,args=args)) if 'reanalyze' in name else self.client.get(reverse(name,args=args))
            self.assertEqual(response.status_code,404)
        self.assertNotContains(self.client.get(reverse('dashboard')),'Invisible')
        response=self.client.post(reverse('document_deposit'),{'kind':'facture','restaurant':other.pk,'fournisseur':supplier.pk,'files':SimpleUploadedFile('cross.pdf',b'%PDF-1.7\n')})
        self.assertEqual(response.status_code,200);self.assertEqual(Facture.objects.filter(fournisseur=supplier).count(),1)

    def test_archive_and_restore_keep_file_lines_and_journal(self):
        self.user.groups.clear();self.user.groups.add(Group.objects.create(name='Admin'))
        response=self.client.post(reverse('facture_delete',args=[self.invoice.pk]));self.assertEqual(response.status_code,302)
        self.assertFalse(Facture.objects.filter(pk=self.invoice.pk).exists());self.assertTrue(Facture.all_objects.filter(pk=self.invoice.pk).exists())
        self.assertTrue(Path(self.invoice.fichier.path).exists());self.assertTrue(LigneFacture.all_objects.filter(pk=self.line.pk).exists())
        self.client.post(reverse('document_archives'),{'kind':'facture','document':self.invoice.pk})
        self.assertTrue(Facture.objects.filter(pk=self.invoice.pk).exists())

    def test_credit_and_discount_totals_are_not_catalogue_prices(self):
        self.invoice.kind='credit';self.invoice.montant_ht=-20;self.invoice.montant_ttc=-20;self.invoice.save()
        self.line.prix_unitaire_ht=-10;self.line.montant_ht=-20;self.line.montant_ttc=-20;self.line.save()
        self.assertEqual(invoice_checks(self.invoice),[]);self.assertEqual(review_queue()[0],[])

    def test_inconsistent_totals_are_explained(self):
        self.line.montant_ht=18;self.line.save();problems=invoice_checks(self.invoice)
        self.assertTrue(any('quantité × prix' in p for p in problems));self.assertTrue(any('Somme' in p for p in problems))

    def test_many_delivery_notes_and_partial_allocation_no_double_count(self):
        self.supplier.delivery_mode='separate';self.supplier.save()
        d=BonLivraison.objects.create(fournisseur=self.supplier,date_livraison=date(2026,10,1),fichier='bl.pdf')
        l=LigneBonLivraison.objects.create(bon_livraison=d,designation='Produit',unite='kg',quantite_livree=3)
        InvoiceDeliveryLink.objects.create(invoice=self.invoice,delivery=d,confirmed_by=self.user)
        allocate(self.invoice,self.line.pk,l.pk,Decimal('2'),self.user)
        self.assertEqual(reconciliation(self.invoice),[])
        other=Facture.objects.create(fournisseur=self.supplier,fichier='f2.pdf')
        other_line=LigneFacture.objects.create(facture=other,designation='Produit',unite='kg',quantite=2)
        InvoiceDeliveryLink.objects.create(invoice=other,delivery=d)
        with self.assertRaises(ValidationError):allocate(other,other_line.pk,l.pk,Decimal('2'),self.user)
        allocate(other,other_line.pk,l.pk,Decimal('1'),self.user)
        self.assertEqual(reconciliation(other)[0]['gap'],1)

    def test_claim_follows_credit_to_close_with_supplier_currency_proof(self):
        self.verify();self.claim();claim=SupplierClaim.objects.get()
        for action in ('prepare','sent'):
            self.client.post(reverse('claim_state',args=[claim.pk]),{'action':action})
        credit=Facture.objects.create(fournisseur=self.supplier,fichier='credit.pdf',kind='credit',devise='CHF',numero_facture='AV-QA',date_facture=date(2026,10,2),montant_ht=-4,montant_tva=0,montant_ttc=-4)
        LigneFacture.objects.create(facture=credit,designation='Avoir',unite='kg',quantite=1,prix_unitaire_ht=-4,montant_ht=-4,montant_ttc=-4)
        credit.statut='validee';credit.save()
        follow={'recovered_amount':'4','credit_invoice':credit.pk,'followup_note':'Avoir contrôlé'}
        for action in ('credit_due','credit_received','closed'):
            self.client.post(reverse('claim_state',args=[claim.pk]),{'action':action,**follow})
        claim.refresh_from_db();self.assertEqual(claim.state,'closed');self.assertEqual(claim.recovered_amount,4)

    def test_claim_cannot_skip_preparation_or_accept_foreign_credit(self):
        self.verify();self.claim();claim=SupplierClaim.objects.get()
        self.client.post(reverse('claim_state',args=[claim.pk]),{'action':'closed','recovered_amount':0,'followup_note':'Clôture'})
        claim.refresh_from_db();self.assertEqual(claim.state,'draft')

    def test_control_cache_reuses_calculations_and_invalidates_on_tariff_change(self):
        review_queue();self.assertTrue(InvoiceControlCache.objects.exists())
        with patch('core.services.price_review.control_invoice_against_catalogue',side_effect=AssertionError('Recalculation')):review_queue()
        self.product.prix_unitaire_ht=10;self.product.save();self.assertFalse(InvoiceControlCache.objects.exists())
        self.assertEqual(review_queue()[0],[])

    def test_document_work_pages_render_and_supplier_lists_are_paginated(self):
        for name,args in [('document_deposit',[]),('work_documents',[]),('catalogue_coverage',[]),('audit_log',[]),('supplier_units',[self.supplier.pk]),('invoice_reconcile',[self.invoice.pk]),('extraction_changes',['facture',self.invoice.pk])]:
            self.assertEqual(self.client.get(reverse(name,args=args)).status_code,200)
        for n in range(30):Facture.objects.create(fournisseur=self.supplier,fichier='page.pdf',numero_facture=str(n))
        response=self.client.get(reverse('fournisseur_detail',args=[self.supplier.pk]));self.assertEqual(len(response.context['factures']),25)
        response=self.client.get(reverse('fournisseur_detail',args=[self.supplier.pk])+'?facture_page=2');self.assertEqual(len(response.context['factures']),6)

    def test_health_checks_database_storage_and_migrations(self):
        self.assertEqual(self.client.get(reverse('health')).status_code,200)
        with override_settings(MEDIA_ROOT='/nonexistent/health/path'):
            self.assertEqual(self.client.get(reverse('health')).status_code,503)

    def test_dashboard_counts_ignore_archived_documents(self):
        from .views import fournisseurs_with_document_counts
        self.invoice.archived_at = timezone.now()
        self.invoice.save()
        supplier = fournisseurs_with_document_counts().get(pk=self.supplier.pk)
        self.assertEqual(supplier.factures_count, 0)
        self.assertEqual(supplier.documents_a_valider_count, 0)

    def test_empty_catalogue_and_unknown_currency_are_incomplete(self):
        self.product.archived_at = timezone.now()
        self.product.save()
        self.assertEqual(catalogue_health(self.supplier)['state'], 'incomplete')
        self.product.archived_at = None
        self.product.save()
        self.cat.devise = 'UNK'
        self.cat.save()
        self.assertEqual(catalogue_health(self.supplier)['state'], 'incomplete')

    def test_restore_archive_rejects_traversal_and_symlinks(self):
        from scripts.media_archive import extract
        for name,type_ in [('../escape',tarfile.REGTYPE),('media/link',tarfile.SYMTYPE)]:
            p=Path(self.temp.name)/'bad.tar.gz'
            with tarfile.open(p,'w:gz') as tar:
                member=tarfile.TarInfo(name);member.type=type_;member.linkname='/etc/passwd';tar.addfile(member)
            with self.assertRaises(ValueError):extract(p,Path(self.temp.name)/'stage')


    def test_archived_source_row_can_be_restored_without_new_identity(self):
        # Populate source identity, then explicitly archive the source row.
        with patch('core.views.analyze_invoice',return_value=self.payload()):apply_invoice_ai_analysis(self.invoice)
        self.line.refresh_from_db();self.line.archived_at=timezone.now();self.line.save()
        with patch('core.views.analyze_invoice',return_value=self.payload()):apply_invoice_ai_analysis(self.invoice)
        version=AnalysisVersion.objects.first();self.assertFalse(version.applied)
        self.assertEqual(LigneFacture.all_objects.filter(facture=self.invoice).count(),1)
        apply_version(self.invoice,version.pk)
        self.assertEqual(self.invoice.lignes.get().pk,self.line.pk)

    def test_column_mapping_can_import_unrecognized_headers_locally(self):
        cat=CatalogueFournisseur.objects.create(fournisseur=self.supplier,fichier_original=SimpleUploadedFile('map.csv',b'A;B;C\n0001;Lait;2.50\n'),devise='UNK')
        response=self.client.post(reverse('catalogue_columns',args=[cat.pk]),{'header_row':'0','column_0':'reference_produit','column_1':'designation','column_2':'prix_unitaire_ht'})
        self.assertEqual(response.status_code,302);self.assertEqual(cat.lignes.get().reference_produit,'0001')
        self.assertEqual(cat.lignes.get().prix_unitaire_ht,Decimal('2.50'))

    def test_disabled_unit_rule_does_not_authorize_comparison(self):
        self.line.unite='carton';self.line.save()
        UnitConversion.objects.create(fournisseur=self.supplier,invoice_unit='carton',catalogue_unit='kg',factor=6,reason='Ancien conditionnement',active=False)
        self.assertEqual(current_detail(self.line)['status'],'unite_a_verifier')


    def test_admin_login_uses_same_attempt_limit(self):
        self.client.logout()
        for _ in range(8):self.client.post(reverse('admin:login'),{'username':'absent-admin','password':'wrong'})
        self.assertEqual(self.client.post(reverse('admin:login'),{'username':'absent-admin','password':'wrong'}).status_code,429)
