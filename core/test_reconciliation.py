from datetime import date, timedelta
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from core.models import (Restaurant, RestaurantAccess, Fournisseur, Facture, BonLivraison,
    LigneFacture, LigneBonLivraison, DeliveryAllocation, DeliveryReceipt, InvoiceReceipt,
    QuantityCredit, DeliveryProductMapping, InvoiceDeliveryLink, SupplierClaimLine, SupplierClaim,
    BusinessAudit)
from core.services import reconciliation as service
from core.services.price_review import current_detail, review_queue
from core.services.extraction import sync_lines

D=Decimal


class ReconciliationTests(TestCase):
    def setUp(self):
        self.restaurant=Restaurant.objects.create(nom='Restaurant test',devise_defaut='CHF')
        self.supplier=Fournisseur.objects.create(restaurant=self.restaurant,nom='Fournisseur test',delivery_mode='separate')
        self.user=get_user_model().objects.create_user('gestionnaire_test',password='test-only-strong-password')
        self.user.groups.add(Group.objects.get_or_create(name='Gestionnaire')[0])
        RestaurantAccess.objects.create(user=self.user,restaurant=self.restaurant)
        self.client.force_login(self.user)
        self.invoice=self.make_invoice('F-1')
        self.line=self.make_line(self.invoice,10)
        self.delivery=BonLivraison.objects.create(fournisseur=self.supplier,numero_bon_livraison='BL-1',date_livraison=date(2026,10,1),fichier='bl.pdf')
        self.dl=LigneBonLivraison.objects.create(bon_livraison=self.delivery,reference_produit='P1',designation='Pommes',quantite_livree=D('10'),unite='kg')

    def make_invoice(self,number,**kwargs):
        return Facture.objects.create(fournisseur=self.supplier,numero_facture=number,date_facture=date(2026,10,3),fichier=number+'.pdf',devise='CHF',statut='a_valider',**kwargs)

    def make_line(self,invoice,amount,**kwargs):
        return LigneFacture.objects.create(facture=invoice,reference_produit='P1',designation='Pommes',quantite=D(str(amount)),prix_unitaire_ht=D('2'),unite='kg',**kwargs)

    def post(self,action,**data):
        self.invoice.refresh_from_db()
        data.update(action=action,token=service.review_token(self.invoice))
        return self.client.post(reverse('invoice_reconcile',args=[self.invoice.pk]),data,follow=True)

    def allocate(self,amount='10',**kwargs):
        return service.allocate_delivery(self.invoice,self.line.pk,self.dl.pk,amount,self.user,**kwargs)

    def test_one_invoice_and_delivery_confirmed(self):
        self.allocate()
        state=service.reconciliation_state(self.invoice)
        self.assertEqual(state['state'],'matched')
        self.assertEqual(state['issues'],[])
        self.assertEqual(state['matched'],1)

    def test_many_deliveries_same_product(self):
        self.dl.quantite_livree=4;self.dl.save()
        self.allocate('4')
        b=BonLivraison.objects.create(fournisseur=self.supplier,numero_bon_livraison='BL-2',fichier='bl2.pdf')
        l=LigneBonLivraison.objects.create(bon_livraison=b,reference_produit='P1',designation='Pommes',quantite_livree=6,unite='kg')
        service.allocate_delivery(self.invoice,self.line.pk,l.pk,'6',self.user)
        self.assertEqual(service.reconciliation_state(self.invoice)['state'],'matched')
        self.assertEqual(self.invoice.delivery_links.count(),2)

    def test_delivery_shared_between_invoices_no_double_count(self):
        self.line.quantite=6;self.line.save();self.allocate('6')
        other=self.make_invoice('F-2');line=self.make_line(other,4)
        with self.assertRaises(ValidationError):service.allocate_delivery(other,line.pk,self.dl.pk,'5',self.user)
        service.allocate_delivery(other,line.pk,self.dl.pk,'4',self.user)
        self.assertEqual(service.reconciliation_state(other)['state'],'matched')

    def test_overallocation_invoice_is_rejected(self):
        with self.assertRaises(ValidationError):self.allocate('11')
        self.assertFalse(DeliveryAllocation.objects.exists())

    def test_suggestions_are_read_only_and_explain_matching(self):
        proposals=service.suggestions(self.invoice)
        self.assertEqual(proposals[0]['choices'][0]['confidence'],'Référence identique')
        self.assertEqual(proposals[0]['choices'][0]['quantity'],10)
        self.assertFalse(InvoiceDeliveryLink.objects.exists())
        self.assertFalse(DeliveryAllocation.objects.exists())

    def test_ambiguous_products_stay_candidates(self):
        LigneBonLivraison.objects.create(bon_livraison=self.delivery,reference_produit='P1',designation='Pommes',quantite_livree=10,unite='kg')
        self.assertEqual(len(service.suggestions(self.invoice)[0]['choices']),2)
        self.assertFalse(DeliveryAllocation.objects.exists())

    def test_same_name_different_reference_is_only_a_proposal(self):
        self.dl.reference_produit='OTHER';self.dl.save()
        self.assertEqual(service.suggestions(self.invoice)[0]['choices'][0]['confidence'],'Désignation identique')

    def test_unit_conversion_requires_confirmation_and_remembered_mapping(self):
        self.dl.unite='carton';self.dl.quantite_livree=2;self.dl.save()
        self.assertIsNone(service.suggestions(self.invoice)[0]['choices'][0]['factor'])
        with self.assertRaises(ValidationError):self.allocate('2',factor='5')
        self.allocate('2',factor='5',reason='Un carton contient 5 kg',remember=True)
        self.assertEqual(service.reconciliation_state(self.invoice)['state'],'matched')
        self.assertEqual(DeliveryProductMapping.objects.get().factor,5)

    def test_equal_units_cannot_apply_arbitrary_factor(self):
        with self.assertRaises(ValidationError):self.allocate('2',factor='5',reason='Mauvais facteur')

    def test_missing_unit_or_fractional_excess_is_rejected(self):
        self.dl.unite='';self.dl.save()
        with self.assertRaises(ValidationError):self.allocate()
        for value in ('NaN','Infinity','-1','0','1.0001'):
            with self.assertRaises(ValidationError):service.quantity(value)

    def test_waiting_and_overdue_are_separate(self):
        self.assertEqual(service.reconciliation_state(self.invoice)['state'],'pending')
        self.invoice.delivery_wait_until=timezone.localdate()-timedelta(days=1);self.invoice.save()
        state=service.reconciliation_state(self.invoice)
        self.assertEqual(state['state'],'difference')
        self.assertFalse(state['rows'][0]['can_claim'])

    def test_invoice_only_does_not_imply_receipt(self):
        self.invoice.delivery_mode='invoice_only';self.invoice.save()
        state=service.reconciliation_state(self.invoice)
        self.assertEqual(state['state'],'unverified')
        self.assertIsNone(state['rows'][0]['gap'])
        service.save_invoice_receipt(self.invoice,self.line,'10','Réception vérifiée par comptage',self.user)
        self.assertEqual(service.reconciliation_state(self.invoice)['state'],'matched')

    def test_combined_document_requires_independent_receipt(self):
        self.invoice.delivery_mode='combined';self.invoice.save()
        self.assertEqual(service.reconciliation_state(self.invoice)['state'],'unverified')
        with self.assertRaises(ValidationError):self.allocate()

    def test_receipt_and_allocation_cannot_be_cumulated(self):
        self.allocate()
        with self.assertRaises(ValidationError):service.save_invoice_receipt(self.invoice,self.line,'10','Réception vérifiée',self.user)

    def test_return_changes_available_quantity(self):
        service.save_delivery_receipt(self.invoice,self.dl,'10','2','Deux kg retournés au livreur',self.user)
        self.allocate('8')
        state=service.reconciliation_state(self.invoice)
        self.assertEqual(state['rows'][0]['gap'],2)
        self.assertEqual(self.dl.quantite_livree,10)

    def test_return_after_allocation_requires_correction(self):
        self.allocate()
        service.save_delivery_receipt(self.invoice,self.dl,'10','2','Deux kg retournés au livreur',self.user)
        self.assertEqual(service.reconciliation_state(self.invoice)['state'],'review')

    def test_invalid_return_rejected(self):
        with self.assertRaises(ValidationError):service.save_delivery_receipt(self.invoice,self.dl,'10','11','Retour impossible',self.user)

    def test_credit_regularizes_quantity_and_is_not_used_twice(self):
        self.allocate('8')
        credit=self.make_invoice('A-1',kind='credit');cl=self.make_line(credit,-2)
        service.save_credit(self.invoice,self.line,cl,'2','1','Avoir pour deux kg non livrés',self.user)
        self.assertEqual(service.reconciliation_state(self.invoice)['state'],'matched')
        other=self.make_invoice('F-2');line=self.make_line(other,3)
        with self.assertRaises(ValidationError):service.save_credit(other,line,cl,'1','1','Avoir déjà utilisé',self.user)

    def test_credit_currency_or_supplier_mismatch_rejected(self):
        credit=self.make_invoice('A-1',kind='credit');credit.devise='EUR';credit.save();cl=self.make_line(credit,-2)
        with self.assertRaises(ValidationError):service.save_credit(self.invoice,self.line,cl,'2','1','Mauvaise devise',self.user)

    def test_edited_source_invalidates_confirmation(self):
        self.allocate()
        self.dl.quantite_livree=20;self.dl.save()
        self.assertEqual(service.reconciliation_state(self.invoice)['state'],'review')

    def test_archived_source_does_not_validate_invoice(self):
        self.allocate()
        self.delivery.archived_at=timezone.now();self.delivery.save()
        self.assertEqual(service.reconciliation_state(self.invoice)['state'],'review')

    def test_invoice_receipt_becomes_stale_after_edit(self):
        self.invoice.delivery_mode='invoice_only';self.invoice.save()
        service.save_invoice_receipt(self.invoice,self.line,'10','Réception vérifiée',self.user)
        self.line.quantite=11;self.line.save()
        self.assertEqual(service.reconciliation_state(self.invoice)['state'],'review')

    def test_policy_change_cannot_silently_validate_old_allocations(self):
        self.allocate();self.supplier.delivery_mode='combined';self.supplier.save()
        self.invoice.refresh_from_db()
        self.assertEqual(service.reconciliation_state(self.invoice)['state'],'review')

    def test_fees_do_not_create_quantity_discrepancies(self):
        self.allocate();LigneFacture.objects.create(facture=self.invoice,kind='fee',designation='Transport',montant_ht=5)
        state=service.reconciliation_state(self.invoice)
        self.assertEqual(state['state'],'matched');self.assertEqual(state['total'],1)
        self.assertEqual(state['excluded_lines'].count(),1)

    def test_empty_invoice_never_matches(self):
        self.assertEqual(service.reconciliation_state(self.make_invoice('EMPTY'))['state'],'review')

    def test_duplicates_block_green_state_and_claims(self):
        self.allocate('8');other=self.make_invoice('F-1')
        self.assertEqual(service.reconciliation_state(self.invoice)['state'],'review')
        with self.assertRaises(ValidationError):service.quantity_claim(self.invoice,self.line,self.user)
        other.document_role='duplicate';other.replaces_with=self.invoice;other.save()
        self.assertEqual(service.reconciliation_state(self.invoice)['state'],'difference')

    def test_exclusion_is_reversible_and_removes_price_tasks(self):
        self.post('role',role='statement',reason='Relevé récapitulatif fournisseur')
        self.invoice.refresh_from_db()
        self.assertEqual(service.reconciliation_state(self.invoice)['state'],'excluded')
        self.assertIsNone(current_detail(self.line))
        self.assertFalse(review_queue()[0])
        self.post('role',role='active',reason='Il s’agit bien de la facture')
        self.invoice.refresh_from_db();self.assertEqual(self.invoice.document_role,'active')

    def test_used_document_cannot_be_excluded(self):
        self.allocate();self.post('role',role='statement',reason='Relevé récapitulatif')
        self.invoice.refresh_from_db();self.assertEqual(self.invoice.document_role,'active')

    def test_claims_group_by_supplier_and_preserve_numbers(self):
        self.allocate('8');first=service.quantity_claim(self.invoice,self.line,self.user)
        other=self.make_invoice('F-2');line=self.make_line(other,5)
        dl=LigneBonLivraison.objects.create(bon_livraison=self.delivery,reference_produit='P2',designation='Poires',unite='kg',quantite_livree=3)
        service.allocate_delivery(other,line.pk,dl.pk,'3',self.user)
        second=service.quantity_claim(other,line,self.user)
        self.assertEqual(first.pk,second.pk)
        self.assertEqual(set(first.lines.values_list('snapshot__invoice_number',flat=True)),{'F-1','F-2'})
        self.assertEqual(first.lines.first().snapshot['delivery_numbers'],['BL-1'])
        with self.assertRaises(ValidationError):service.quantity_claim(self.invoice,self.line,self.user)
        first.state='prepared';first.save()
        response=self.client.get(reverse('claim_document',args=[first.pk]))
        self.assertContains(response,'F-1');self.assertContains(response,'F-2');self.assertContains(response,'BL-1')
        self.assertContains(response,'Différence')

    def test_cancelled_quantity_claim_releases_line_and_preserves_history(self):
        self.allocate('8')
        claim = service.quantity_claim(self.invoice,self.line,self.user)
        snapshot = claim.lines.get().snapshot
        response = self.client.post(reverse('claim_state',args=[claim.pk]),{'action':'cancel','confirmed':'1'})
        self.assertEqual(response.status_code,302)
        claim.refresh_from_db()
        self.assertEqual(claim.state,'cancelled')
        self.assertEqual(claim.lines.get().snapshot,snapshot)
        other = service.quantity_claim(self.invoice,self.line,self.user)
        self.assertNotEqual(claim.pk,other.pk)
        self.assertEqual(SupplierClaimLine.objects.filter(invoice_line=self.line).count(),2)
        with self.assertRaises(ValidationError):
            service.quantity_claim(self.invoice,self.line,self.user)

    def test_stale_post_cannot_overwrite_source_changes(self):
        token=service.review_token(self.invoice)
        self.dl.quantite_livree=9;self.dl.save()
        response=self.client.post(reverse('invoice_reconcile',args=[self.invoice.pk]),{'action':'allocate','token':token,'invoice_line':self.line.pk,'delivery_line':self.dl.pk,'quantity':'9','factor':'1'},follow=True)
        self.assertContains(response,'Cette fiche a changé')
        self.assertFalse(DeliveryAllocation.objects.exists())

    def test_routes_render_and_log_decisions(self):
        self.assertEqual(self.client.get(reverse('reconciliation_queue')).status_code,200)
        response=self.post('allocate',invoice_line=self.line.pk,delivery_line=self.dl.pk,quantity='10',factor='1')
        self.assertContains(response,'Livraisons rapprochées')
        self.assertTrue(BusinessAudit.objects.filter(model_label='core.deliveryallocation',actor=self.user).exists())
        self.assertContains(response,'Prix · contrôle indisponible')

    def test_other_restaurant_is_inaccessible(self):
        r=Restaurant.objects.create(nom='Autre');s=Fournisseur.objects.create(restaurant=r,nom='Autre')
        inv=Facture.objects.create(fournisseur=s,fichier='private.pdf')
        self.assertEqual(self.client.get(reverse('invoice_reconcile',args=[inv.pk])).status_code,404)
        dl=BonLivraison.objects.create(fournisseur=s,fichier='private.pdf')
        self.assertEqual(self.client.get(reverse('delivery_as_invoice',args=[dl.pk])).status_code,404)

    def test_reception_and_review_roles_remain_limited(self):
        for role in ('Reception','Revue'):
            user=get_user_model().objects.create_user('role_'+role)
            user.groups.add(Group.objects.get_or_create(name=role)[0]);RestaurantAccess.objects.create(user=user,restaurant=self.restaurant)
            self.client.force_login(user)
            for url in (reverse('reconciliation_queue'),reverse('invoice_reconcile',args=[self.invoice.pk]),reverse('delivery_as_invoice',args=[self.delivery.pk])):
                self.assertEqual(self.client.get(url).status_code,403)
                self.assertEqual(self.client.post(url,{}).status_code,403)

    def test_delivery_can_become_combined_without_duplicating_file(self):
        url=reverse('delivery_as_invoice',args=[self.delivery.pk])
        response=self.client.post(url,{'number':'FC-1','date':'2026-10-03','currency':'CHF','reason':'Document explicitement BL et facture'},follow=True)
        self.assertEqual(response.status_code,200)
        inv=Facture.objects.get(source_delivery=self.delivery)
        self.assertEqual(inv.fichier.name,self.delivery.fichier.name)
        self.assertEqual(inv.delivery_mode,'combined')
        self.assertEqual(service.reconciliation_state(inv)['state'],'unverified')
        self.assertEqual(inv.lignes.count(),1)
        self.client.post(url,{'number':'FC-1','date':'2026-10-03','currency':'CHF','reason':'Confirmation répétée'})
        self.assertEqual(Facture.objects.filter(source_delivery=self.delivery).count(),1)
        with self.assertRaises(ValidationError):self.allocate()

    def test_reanalysis_does_not_erase_receipt_metadata(self):
        self.invoice.delivery_mode='invoice_only';self.invoice.save()
        service.save_invoice_receipt(self.invoice,self.line,'10','Réception vérifiée',self.user)
        fresh=LigneFacture(facture=self.invoice,reference_produit='P1',designation='Pommes',quantite=D('10'),prix_unitaire_ht=D('2'),unite='kg')
        sync_lines(self.invoice,[fresh])
        self.assertEqual(InvoiceReceipt.objects.count(),1)

    def test_credit_received_after_draft_blocks_obsolete_claim(self):
        self.allocate('8');claim=service.quantity_claim(self.invoice,self.line,self.user)
        credit=self.make_invoice('A-1',kind='credit');cl=self.make_line(credit,-2)
        service.save_credit(self.invoice,self.line,cl,'2','1','Avoir régularisant cet écart',self.user)
        response=self.client.post(reverse('claim_state',args=[claim.pk]),{'action':'prepare'},follow=True)
        claim.refresh_from_db();self.assertEqual(claim.state,'draft')
        self.assertContains(response,'ont changé')
        self.post('remove_quantity_claim',claim_line=claim.lines.get().pk)
        self.assertFalse(claim.lines.exists())

    def test_explicit_wait_holds_partial_delivery_until_deadline(self):
        self.allocate('8')
        self.assertEqual(service.reconciliation_state(self.invoice)['state'],'difference')
        self.post('wait',until=str(timezone.localdate()+timedelta(days=3)),reason='Deuxième BL annoncé par le fournisseur')
        self.invoice.refresh_from_db()
        self.assertEqual(service.reconciliation_state(self.invoice)['state'],'waiting')

    def test_unfingerprinted_legacy_allocations_require_review(self):
        InvoiceDeliveryLink.objects.create(invoice=self.invoice,delivery=self.delivery)
        DeliveryAllocation.objects.create(invoice_line=self.line,delivery_line=self.dl,quantity=10)
        self.assertEqual(service.reconciliation_state(self.invoice)['state'],'review')

    def test_combined_deposit_creates_only_one_invoice(self):
        import tempfile
        from django.test import override_settings
        from django.core.files.uploadedfile import SimpleUploadedFile
        with tempfile.TemporaryDirectory() as media, override_settings(MEDIA_ROOT=media):
            response=self.client.post(reverse('document_deposit'),{'kind':'combined','restaurant':self.restaurant.pk,'fournisseur':self.supplier.pk,
                'files':SimpleUploadedFile('bl-facture.pdf',b'%PDF-1.4\n%%EOF',content_type='application/pdf')})
            self.assertEqual(response.status_code,302)
            self.assertEqual(Facture.objects.filter(delivery_mode='combined').count(),1)
            self.assertEqual(BonLivraison.objects.count(),1)

    def test_policy_and_invoice_exception_are_independent(self):
        self.post('policy',mode='invoice_only',wait_days='7')
        self.invoice.refresh_from_db()
        self.assertEqual(service.mode_for(self.invoice),'invoice_only')
        self.post('mode',mode='combined',reason='Ce document est un BL facture')
        self.invoice.refresh_from_db();self.supplier.refresh_from_db()
        self.assertEqual(self.invoice.delivery_mode,'combined')
        self.assertEqual(self.supplier.delivery_mode,'invoice_only')

    def test_cross_supplier_line_forgery_is_rejected(self):
        supplier=Fournisseur.objects.create(restaurant=self.restaurant,nom='Autre fournisseur')
        delivery=BonLivraison.objects.create(fournisseur=supplier,fichier='bl2.pdf')
        line=LigneBonLivraison.objects.create(bon_livraison=delivery,designation='Pommes',quantite_livree=10,unite='kg')
        response=self.post('allocate',invoice_line=self.line.pk,delivery_line=line.pk,quantity='10',factor='1')
        self.assertEqual(response.status_code,404)
        self.assertFalse(DeliveryAllocation.objects.exists())

    def test_negative_gap_is_not_silently_accepted(self):
        self.invoice.delivery_mode='invoice_only';self.invoice.save()
        service.save_invoice_receipt(self.invoice,self.line,'12','Deux kilos supplémentaires reçus',self.user)
        state=service.reconciliation_state(self.invoice)
        self.assertEqual(state['state'],'difference')
        self.assertEqual(state['rows'][0]['gap'],-2)
        self.assertFalse(state['rows'][0]['can_claim'])

    def test_removed_allocation_keeps_audit(self):
        allocation=self.allocate()
        response=self.post('remove_allocation',allocation=allocation.pk)
        self.assertEqual(response.status_code,200)
        self.assertFalse(DeliveryAllocation.objects.exists())
        self.assertTrue(BusinessAudit.objects.filter(model_label='core.deliveryallocation',object_id=allocation.pk,action='delete',actor=self.user).exists())

    def test_reused_number_can_be_confirmed_distinct_and_reopens_after_change(self):
        other=self.make_invoice('F-1');other.date_facture=date(2025,10,3);other.save()
        self.post('distinct',other=other.pk,reason='Numérotation annuelle : années différentes')
        self.invoice.refresh_from_db()
        self.assertFalse(service.duplicate_candidates(self.invoice).exists())
        self.assertFalse(service.duplicate_candidates(other).exists())
        other.date_facture=date(2026,10,3);other.save()
        self.assertTrue(service.duplicate_candidates(self.invoice).exists())

    def test_identical_file_cannot_be_dismissed_as_distinct(self):
        other=self.make_invoice('F-2');other.file_sha256='a'*64;other.save()
        self.invoice.file_sha256='a'*64;self.invoice.save()
        self.post('distinct',other=other.pk,reason='Mauvaise décision de distinction')
        self.invoice.refresh_from_db()
        self.assertTrue(service.duplicate_candidates(self.invoice).exists())

    def test_mixed_policy_still_suggests_and_confirmation_sets_invoice_exception(self):
        self.supplier.delivery_mode='mixed';self.supplier.save();self.invoice.refresh_from_db()
        self.assertTrue(service.suggestions(self.invoice))
        response=self.client.get(reverse('invoice_reconcile',args=[self.invoice.pk]))
        self.assertContains(response,'BL proposé')
        self.assertNotContains(response,'Différence à justifier : 10')
        self.assertContains(response,'Valider le rapprochement')
        self.allocate();self.invoice.refresh_from_db();self.supplier.refresh_from_db()
        self.assertEqual(self.invoice.delivery_mode,'separate')
        self.assertEqual(self.supplier.delivery_mode,'mixed')

    def test_missing_units_show_candidate_without_inventing_unit_or_zero_delivery(self):
        self.line.unite='';self.line.save();self.dl.unite='';self.dl.save()
        proposals=service.suggestions(self.invoice)
        self.assertEqual(proposals[0]['choices'][0]['delivery'].pk,self.dl.pk)
        self.assertFalse(proposals[0]['choices'][0]['can_confirm'])
        self.assertTrue(proposals[0]['choices'][0]['data_warning'])
        response=self.client.get(reverse('invoice_reconcile',args=[self.invoice.pk]))
        self.assertContains(response,'Une quantité ou une unité manque')
        self.assertNotContains(response,'Confirmer cette association')

    def test_suspect_date_surfaces_reference_match_for_verification(self):
        self.delivery.date_livraison=date(2004,10,1);self.delivery.save()
        proposal=service.suggestions(self.invoice)[0]['choices'][0]
        self.assertTrue(proposal['date_warning'])
        self.assertFalse(proposal['can_confirm'])
        self.assertEqual(self.delivery.date_livraison.year,2004)

    def test_absent_delivery_is_stated_as_unknown_not_zero_received(self):
        self.dl.delete();self.delivery.delete()
        response=self.client.get(reverse('invoice_reconcile',args=[self.invoice.pk]))
        self.assertContains(response,'Aucun BL importé pour ce fournisseur')
        self.assertContains(response,'Quantité reçue inconnue')
        self.assertNotContains(response,'Différence à justifier : 10')


from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from django.db import connections
from django.test import TransactionTestCase


class ConcurrentReconciliationTests(TransactionTestCase):
    def test_two_simultaneous_invoices_cannot_consume_the_same_delivery(self):
        r=Restaurant.objects.create(nom='Concurrent')
        s=Fournisseur.objects.create(restaurant=r,nom='Concurrent',delivery_mode='separate')
        user=get_user_model().objects.create_user('concurrent')
        d=BonLivraison.objects.create(fournisseur=s,fichier='bl.pdf')
        dl=LigneBonLivraison.objects.create(bon_livraison=d,reference_produit='P',quantite_livree=10,unite='kg')
        pairs=[]
        for number in ('A','B'):
            invoice=Facture.objects.create(fournisseur=s,numero_facture=number,fichier=number+'.pdf')
            line=LigneFacture.objects.create(facture=invoice,reference_produit='P',quantite=6,unite='kg')
            pairs.append((invoice.pk,line.pk))
        barrier=Barrier(2)
        def run(pair):
            connections.close_all()
            try:
                invoice=Facture.objects.select_related('fournisseur').get(pk=pair[0])
                barrier.wait(timeout=10)
                try:
                    service.allocate_delivery(invoice,pair[1],dl.pk,'6',user)
                    return 'ok'
                except ValidationError:
                    return 'blocked'
            finally: connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(run,pairs))
        self.assertCountEqual(results,['ok','blocked'])
        self.assertEqual(sum(a.quantity for a in DeliveryAllocation.objects.all()),6)
