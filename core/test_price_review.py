from .test_helpers import restaurant_fixture, invoice_fixture
from datetime import date
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase, Client
from django.urls import reverse
from .models import (Restaurant, Fournisseur, Facture, LigneFacture, CatalogueFournisseur,
    LigneCatalogue, PriceReviewDecision, PriceReviewEvent, SupplierClaim, SupplierClaimLine,
    CataloguePriceHistory, ProductAssociation)
from .services.price_review import current_detail, review_queue


class PriceReviewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('manager', password='test-123-pass')
        self.user.groups.add(Group.objects.create(name='Gestionnaire'))
        self.client.force_login(self.user)
        self.restaurant = restaurant_fixture(nom='Les Halles')
        self.supplier = Fournisseur.objects.create(nom='Bianchi', restaurant=self.restaurant)
        self.catalogue = CatalogueFournisseur.objects.create(fournisseur=self.supplier, nom='Automne',
            date_debut=date(2026, 9, 1), date_fin=date(2026, 12, 31), statut='valide', devise='CHF')
        self.product = LigneCatalogue.objects.create(catalogue=self.catalogue, reference_produit='P1',
            designation='Poulet', unite='kg', prix_unitaire_ht=Decimal('8'))
        self.invoice = invoice_fixture(fournisseur=self.supplier, fichier='example.pdf', numero_facture='F1',
            date_facture=date(2026, 10, 1), devise='CHF', statut='a_valider')
        self.line = LigneFacture.objects.create(facture=self.invoice, reference_produit='P1', designation='Poulet',
            unite='kg', quantite=Decimal('20'), prix_unitaire_ht=Decimal('8.84'))
        self.invoice.statut='validee';self.invoice.validation_exception='Fixture contrôlée : prix et quantités confirmés.';self.invoice.save()

    def action(self, action, line=None, **extra):
        line = line or self.line
        return self.client.post(reverse('review_action', args=[line.pk]), {
            'action': action, 'fingerprint': current_detail(line)['fingerprint'], **extra})

    def test_real_data_render_and_inline_panels(self):
        response = self.client.get(reverse('price_review'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Bianchi')
        self.assertContains(response, '16,80')
        self.assertContains(response, 'CHF')
        self.assertContains(response, 'review/logos/bianchi')
        self.assertContains(response, 'Corriger les données extraites')
        self.assertEqual(response.context['total'], 1)

    def test_roles_all_endpoints(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse('price_review')).status_code, 302)
        reception = get_user_model().objects.create_user('reception')
        reception.groups.add(Group.objects.create(name='Reception'))
        self.client.force_login(reception)
        for name in ['price_review', 'supplier_claims']:
            self.assertEqual(self.client.get(reverse(name)).status_code, 403)
        self.assertEqual(self.action('claim').status_code, 403)
        self.assertEqual(self.client.post(reverse('review_undo')).status_code, 403)
        claim = SupplierClaim.objects.create(fournisseur=self.supplier, currency='CHF')
        self.assertEqual(self.client.post(reverse('claim_state', args=[claim.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse('claim_document', args=[claim.pk])).status_code, 403)

    def test_csrf_required(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(client.post(reverse('review_action', args=[self.line.pk]), {'action': 'claim'}).status_code, 403)

    def test_group_claim_by_supplier_across_invoices(self):
        self.action('claim')
        second = invoice_fixture(fournisseur=self.supplier, fichier='example.pdf', numero_facture='F2',
            date_facture=date(2026, 10, 2), devise='CHF', statut='validee')
        line = LigneFacture.objects.create(facture=second, reference_produit='P1', designation='Poulet',
            unite='kg', quantite=10, prix_unitaire_ht=9)
        self.action('claim', line=line)
        claim = SupplierClaim.objects.get()
        self.assertEqual(claim.lines.count(), 2)
        self.assertEqual(review_queue()[0], [])
        self.client.post(reverse('claim_state', args=[claim.pk]), {'action': 'prepare'})
        response = self.client.get(reverse('claim_document', args=[claim.pk]))
        self.assertContains(response, 'Facture F1')
        self.assertContains(response, 'Facture F2')
        self.assertContains(response, '26,80')
        self.assertIn('attachment', response['Content-Disposition'])
        self.product.prix_unitaire_ht = 15
        self.product.save()
        self.assertContains(self.client.get(reverse('claim_document', args=[claim.pk])), '26,80')

    def test_double_claim_not_duplicated(self):
        self.action('claim')
        self.action('claim')
        self.assertEqual(SupplierClaimLine.objects.count(), 1)

    def test_stale_price_rejected(self):
        fingerprint = current_detail(self.line)['fingerprint']
        self.product.prix_unitaire_ht = 7
        self.product.save()
        self.client.post(reverse('review_action', args=[self.line.pk]), {'action':'claim','fingerprint':fingerprint})
        self.assertFalse(SupplierClaim.objects.exists())

    def test_price_update_history_and_recalculate_other_invoice(self):
        other = LigneFacture.objects.create(facture=self.invoice, reference_produit='P1', designation='Poulet',
            unite='kg', quantite=2, prix_unitaire_ht=Decimal('8.84'))
        self.action('price', price='8.84')
        self.product.refresh_from_db()
        self.assertEqual(self.product.prix_unitaire_ht, Decimal('8.84'))
        self.assertEqual(CataloguePriceHistory.objects.get().old_price, Decimal('8'))
        self.assertEqual(review_queue()[0], [])
        self.assertEqual(current_detail(other)['status'], 'conforme')

    def test_create_missing_product_with_explicit_price(self):
        self.line.reference_produit = 'NEW'
        self.line.save()
        self.action('price', price='8.84')
        self.assertEqual(LigneCatalogue.objects.get(reference_produit='NEW').prix_unitaire_ht, Decimal('8.84'))
        self.assertFalse(review_queue()[0])

    def test_invalid_prices_leave_catalogue_unchanged(self):
        for price in ['-1', 'NaN', 'Infinity', '1.234', '10000000000', 'hello']:
            self.action('price', price=price)
        self.product.refresh_from_db()
        self.assertEqual(self.product.prix_unitaire_ht, Decimal('8'))
        self.assertFalse(CataloguePriceHistory.objects.exists())

    def test_association_reused_and_remaining_gap_shown(self):
        self.line.reference_produit = 'ALIAS'
        self.line.save()
        self.action('associate', target=str(self.product.pk))
        self.assertEqual(ProductAssociation.objects.get().target_reference, 'P1')
        self.assertEqual(current_detail(self.line)['status'], 'ecart_prix')
        self.assertEqual(len(review_queue()[0]), 1)

    def test_association_cannot_cross_supplier(self):
        other = Fournisseur.objects.create(nom='Other', restaurant=self.restaurant)
        cat = CatalogueFournisseur.objects.create(fournisseur=other, nom='other', date_debut=date(2026,9,1), statut='valide')
        target = LigneCatalogue.objects.create(catalogue=cat, reference_produit='X', prix_unitaire_ht=8)
        self.assertEqual(self.action('associate', target=str(target.pk)).status_code, 404)
        self.assertFalse(ProductAssociation.objects.exists())

    def test_defer_persists_and_reappears_after_change(self):
        self.action('defer', reason='Attente confirmation')
        self.assertFalse(review_queue()[0])
        self.assertEqual(len(review_queue('deferred')[0]), 1)
        self.product.prix_unitaire_ht = 7
        self.product.save()
        self.assertEqual(len(review_queue()[0]), 1)
        self.assertFalse(review_queue('deferred')[0])

    def test_empty_defer_rejected(self):
        self.action('defer', reason='')
        self.assertFalse(PriceReviewDecision.objects.exists())

    def test_undo_defer_and_price(self):
        self.action('defer', reason='Attente')
        event = PriceReviewEvent.objects.get()
        self.client.post(reverse('review_undo'), {'event': event.pk})
        self.assertFalse(PriceReviewDecision.objects.exists())
        self.action('price', price='8.84')
        event = PriceReviewEvent.objects.filter(undone=False).first()
        self.client.post(reverse('review_undo'), {'event': event.pk})
        self.product.refresh_from_db()
        self.assertEqual(self.product.prix_unitaire_ht, Decimal('8'))
        self.assertEqual(len(review_queue()[0]), 1)

    def test_undo_claim_only_while_draft(self):
        self.action('claim')
        event = PriceReviewEvent.objects.get()
        claim = SupplierClaim.objects.get()
        self.client.post(reverse('claim_state', args=[claim.pk]), {'action': 'prepare'})
        self.client.post(reverse('review_undo'), {'event':event.pk})
        self.assertEqual(SupplierClaimLine.objects.count(), 1)
        event.refresh_from_db()
        self.assertFalse(event.undone)

    def test_undo_stale_change_rejected(self):
        self.action('price', price='8.84')
        event = PriceReviewEvent.objects.get()
        self.product.prix_unitaire_ht = 9
        self.product.save()
        self.client.post(reverse('review_undo'), {'event':event.pk})
        self.product.refresh_from_db()
        self.assertEqual(self.product.prix_unitaire_ht, Decimal('9'))

    def test_quantity_and_unit_uncertainty_block_claims(self):
        for quantity, unit in [(None,'kg'), (Decimal('-1'),'kg'), (Decimal('20'),'box'), (Decimal('20'),'')]:
            self.line.quantite, self.line.unite = quantity, unit
            self.line.save()
            self.action('claim')
        self.assertFalse(SupplierClaim.objects.exists())

    def test_currency_mismatch_and_no_date_are_alerts(self):
        self.invoice.devise='EUR'
        self.invoice.save()
        tasks, alerts = review_queue()
        self.assertFalse(tasks)
        self.assertEqual(len(alerts), 1)
        self.invoice.date_facture=None
        self.invoice.save()
        self.assertContains(self.client.get(reverse('price_review')), 'aucune date')

    def test_unprocessed_documents_excluded(self):
        self.invoice.statut='analyse_en_cours'
        self.invoice.save()
        self.assertFalse(review_queue()[0])
        self.action('claim')
        self.assertFalse(SupplierClaim.objects.exists())

    def test_filters_and_large_queue(self):
        LigneFacture.objects.bulk_create([LigneFacture(facture=self.invoice, reference_produit='P1',
            designation='Poulet', unite='kg', quantite=1, prix_unitaire_ht=9) for _ in range(84)])
        response = self.client.get(reverse('price_review'), {'position': 43})
        self.assertEqual(response.context['total'], 85)
        self.assertEqual(len(response.context['page'].object_list), 40)
        self.assertEqual(response.context['page'].number, 2)
        self.assertLessEqual(len(response.context['numbers']), 9)
        self.assertEqual(self.client.get(reverse('price_review'), {'supplier':999}).context['total'], 0)

    def test_correction_and_undo(self):
        self.action('correct', designation='Poulet corrigé',reference_produit='P1',ean='',quantite='20',unite='kg',prix_unitaire_ht='8')
        self.line.refresh_from_db()
        self.assertEqual(self.line.prix_unitaire_ht, Decimal('8'))
        self.assertFalse(review_queue()[0])
        event=PriceReviewEvent.objects.get()
        self.client.post(reverse('review_undo'), {'event': event.pk})
        self.line.refresh_from_db()
        self.assertEqual(self.line.prix_unitaire_ht, Decimal('8.84'))

    def test_xss_autoescaped(self):
        self.line.designation='<script>alert(1)</script>'
        self.line.save()
        response=self.client.get(reverse('price_review'))
        self.assertNotContains(response, '<script>alert(1)</script>')
        self.assertContains(response, '&lt;script&gt;alert(1)&lt;/script&gt;')

    def test_duplicate_catalogue_reference_not_claimable(self):
        LigneCatalogue.objects.create(catalogue=self.catalogue, reference_produit='P1', designation='Autre poulet', unite='kg', prix_unitaire_ht=7)
        self.assertEqual(current_detail(self.line)['status'], 'produit_ambigu')
        self.action('claim')
        self.assertFalse(SupplierClaim.objects.exists())
        self.action('price',price='8.84')
        self.assertEqual(LigneCatalogue.objects.filter(prix_unitaire_ht=Decimal('8.84')).count(),0)

    def test_undo_new_product_ignores_line_without_deleting(self):
        self.line.reference_produit='NEW'
        self.line.save()
        self.action('price',price='8.84')
        event=PriceReviewEvent.objects.get()
        self.client.post(reverse('review_undo'),{'event':event.pk})
        self.assertEqual(LigneCatalogue.objects.get(reference_produit='NEW').statut_ligne,'ignoree')
        self.assertEqual(current_detail(self.line)['status'],'produit_non_trouve')

    def test_draft_claim_undo_removes_empty_draft(self):
        self.action('claim')
        event=PriceReviewEvent.objects.get()
        self.client.post(reverse('review_undo'),{'event':event.pk})
        self.assertFalse(SupplierClaim.objects.exists())
        self.assertEqual(len(review_queue()[0]),1)

    def test_undo_association_restores_original_mapping(self):
        self.line.reference_produit='ALIAS'
        self.line.save()
        self.action('associate',target=self.product.pk)
        event=PriceReviewEvent.objects.get()
        self.client.post(reverse('review_undo'),{'event':event.pk})
        self.assertFalse(ProductAssociation.objects.exists())
        self.assertEqual(current_detail(self.line)['status'],'produit_non_trouve')

    def test_price_history_from_existing_catalogue_editor(self):
        self.client.post(reverse('ligne_catalogue_update',args=[self.catalogue.pk,self.product.pk]),{
            'reference_produit':'P1','ean':'','designation':'Poulet','marque':'','unite':'kg',
            'conditionnement':'','prix_unitaire_ht':'9','statut_ligne':'ok','ordre':0})
        self.assertEqual(CataloguePriceHistory.objects.get().old_price,Decimal('8'))
        self.assertEqual(CataloguePriceHistory.objects.get().new_price,Decimal('9'))

    def test_claims_separated_by_supplier_and_currency(self):
        self.action('claim')
        other=Fournisseur.objects.create(nom='Other',restaurant=self.restaurant)
        cat=CatalogueFournisseur.objects.create(fournisseur=other,nom='EUR',date_debut=date(2026,9,1),statut='valide',devise='EUR')
        LigneCatalogue.objects.create(catalogue=cat,reference_produit='P1',unite='kg',prix_unitaire_ht=8)
        inv=invoice_fixture(fournisseur=other,fichier='x.pdf',numero_facture='F3',date_facture=date(2026,10,3),statut='validee',devise='EUR')
        line=LigneFacture.objects.create(facture=inv,reference_produit='P1',unite='kg',quantite=20,prix_unitaire_ht=9)
        self.action('claim',line=line)
        self.assertEqual(SupplierClaim.objects.count(),2)
        self.assertEqual(set(SupplierClaim.objects.values_list('currency',flat=True)),{'EUR','CHF'})

    def test_queue_query_count_does_not_grow_per_invoice(self):
        from django.test.utils import CaptureQueriesContext
        from django.db import connection
        with CaptureQueriesContext(connection) as initial_queries:
            review_queue()
        for n in range(12):
            inv=invoice_fixture(fournisseur=self.supplier,fichier='x.pdf',numero_facture=f'F{n+10}',date_facture=date(2026,10,3),statut='validee',devise='CHF')
            LigneFacture.objects.create(facture=inv,reference_produit='P1',unite='kg',quantite=1,prix_unitaire_ht=9)
        with CaptureQueriesContext(connection) as queries:
            tasks,_=review_queue()
        self.assertEqual(len(tasks),13)
        # One fixed query fetches existing claims for every invoice in the queue.
        self.assertLessEqual(len(queries),9)
        self.assertLessEqual(len(queries),len(initial_queries))


from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless
from django.db import connection, close_old_connections
from django.test import TransactionTestCase


@skipUnless(connection.vendor == 'postgresql', 'Row-lock concurrency requires PostgreSQL')
class PriceReviewConcurrencyTests(TransactionTestCase):
    def test_two_users_cannot_claim_the_same_revision(self):
        group=Group.objects.create(name='Gestionnaire')
        users=[get_user_model().objects.create_user(f'parallel-{n}') for n in range(2)]
        for user in users:user.groups.add(group)
        restaurant=restaurant_fixture(nom='Parallel')
        supplier=Fournisseur.objects.create(nom='Parallel',restaurant=restaurant)
        catalogue=CatalogueFournisseur.objects.create(fournisseur=supplier,date_debut=date(2026,1,1),statut='valide')
        LigneCatalogue.objects.create(catalogue=catalogue,reference_produit='P',unite='kg',prix_unitaire_ht=8)
        invoice=invoice_fixture(fournisseur=supplier,fichier='x.pdf',numero_facture='F',date_facture=date(2026,10,1),statut='a_valider')
        line=LigneFacture.objects.create(facture=invoice,reference_produit='P',unite='kg',quantite=20,prix_unitaire_ht=9)
        invoice.statut='validee';invoice.validation_exception='Fixture contrôlée : prix et quantités confirmés.';invoice.save()
        version=current_detail(line)['fingerprint']
        barrier=Barrier(2)
        def claim(user):
            close_old_connections()
            try:
                client=Client()
                client.force_login(user)
                barrier.wait(timeout=10)
                return client.post(reverse('review_action',args=[line.pk]),{'action':'claim','fingerprint':version}).status_code
            finally:close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            codes=list(pool.map(claim,users))
        self.assertEqual(codes,[302,302])
        self.assertEqual(SupplierClaimLine.objects.count(),1)
        self.assertEqual(PriceReviewEvent.objects.filter(action='claim').count(),1)
