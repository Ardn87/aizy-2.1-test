from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core import test_price_review as fixtures
from core.models import CatalogueFournisseur, LigneCatalogue, InvoiceControlCache, SupplierClaim
from core.services.catalogue_control import control_invoice_against_catalogue, CONTROL_VERSION
from core.services.price_review import current_detail, review_queue, snapshot
from core.services.operations import catalogue_health


class InitialPriceReferenceTests(TestCase):
    action = fixtures.PriceReviewTests.action

    def setUp(self):
        fixtures.PriceReviewTests.setUp(self)
        self.catalogue.origine = 'documents'
        self.catalogue.statut = 'a_valider'
        self.catalogue.date_debut = None
        self.catalogue.date_fin = None
        self.catalogue.save()
        self.product.price_source = {'initialised_from_document': True, 'kind': 'facture',
            'document_id': self.invoice.pk, 'line_id': self.line.pk, 'number': 'F-SOURCE',
            'price': '8.00', 'currency': 'CHF'}
        self.product.save()

    def test_initial_reference_enables_control_without_changing_status_date_or_price(self):
        result = control_invoice_against_catalogue(self.invoice)
        self.assertTrue(result['control_available'])
        self.assertTrue(result['initial_reference'])
        self.assertEqual(result['details'][0]['status'], 'ecart_prix')
        self.assertEqual(result['details'][0]['ecart'], Decimal('.84'))
        self.catalogue.refresh_from_db(); self.product.refresh_from_db()
        self.assertEqual(self.catalogue.statut, 'a_valider')
        self.assertIsNone(self.catalogue.date_debut)
        self.assertEqual(self.product.prix_unitaire_ht, 8)
        self.assertEqual(self.product.price_source['number'], 'F-SOURCE')
        self.assertEqual(catalogue_health(self.supplier)['blocked'], 0)

    def test_queue_and_cached_queue_match_direct_control(self):
        tasks, alerts = review_queue()
        self.assertFalse(alerts); self.assertEqual(len(tasks), 1)
        self.assertTrue(tasks[0]['initial_reference'])
        with patch('core.services.price_review.control_invoice_against_catalogue', side_effect=AssertionError('Cache expected')):
            cached, alerts = review_queue()
        self.assertEqual(cached[0]['fingerprint'], current_detail(self.line)['fingerprint'])
        self.assertTrue(cached[0]['initial_reference'])

    def test_old_blocking_cache_is_recomputed_automatically(self):
        self.supplier.refresh_from_db()
        InvoiceControlCache.objects.create(invoice=self.invoice, revision=self.supplier.control_revision,
            result={'available': False, 'message': 'Aucun catalogue valide trouvé.', 'details': []})
        tasks, alerts = review_queue()
        self.assertEqual(len(tasks), 1); self.assertFalse(alerts)
        self.assertEqual(InvoiceControlCache.objects.get().result['version'], CONTROL_VERSION)

    def test_applicable_confirmed_catalogue_has_priority(self):
        confirmed = CatalogueFournisseur.objects.create(fournisseur=self.supplier, statut='valide',
            origine='fournisseur', date_debut=date(2026, 9, 1), date_fin=date(2026, 12, 31), devise='CHF')
        LigneCatalogue.objects.create(catalogue=confirmed, reference_produit='P1', unite='kg', prix_unitaire_ht='8.84')
        result = control_invoice_against_catalogue(self.invoice)
        self.assertEqual(result['catalogue'], confirmed)
        self.assertFalse(result['initial_reference'])
        self.assertEqual(result['details'][0]['status'], 'conforme')
        self.assertFalse(review_queue()[0])

    def test_initial_reference_is_not_used_to_hide_overlapping_confirmed_catalogues(self):
        for i in range(2):
            c = CatalogueFournisseur.objects.create(fournisseur=self.supplier, statut='a_valider',
                date_debut=date(2026, 9, 1), devise='CHF')
            CatalogueFournisseur.objects.filter(pk=c.pk).update(statut='valide')
        result = control_invoice_against_catalogue(self.invoice)
        self.assertFalse(result['control_available'])
        self.assertEqual(result['status'], 'catalogues_multiples')

    def test_dated_unconfirmed_or_supplier_import_does_not_become_initial_reference(self):
        self.catalogue.origine = 'fournisseur'; self.catalogue.save()
        self.assertFalse(control_invoice_against_catalogue(self.invoice)['control_available'])
        self.catalogue.origine = 'documents'; self.catalogue.date_debut = date(2027, 1, 1); self.catalogue.save()
        self.assertFalse(control_invoice_against_catalogue(self.invoice)['control_available'])

    def test_archived_and_failed_catalogues_do_not_enable_control(self):
        self.catalogue.statut = 'analyse_echouee'; self.catalogue.save()
        self.assertFalse(control_invoice_against_catalogue(self.invoice)['control_available'])
        self.catalogue.statut = 'a_valider'; self.catalogue.archived_at = timezone.now(); self.catalogue.save()
        self.assertFalse(control_invoice_against_catalogue(self.invoice)['control_available'])

    def test_missing_date_blocks_control_and_empty_screen_explains_why(self):
        self.invoice.date_facture = None; self.invoice.date_livraison_detectee = None; self.invoice.save()
        tasks, alerts = review_queue()
        self.assertFalse(tasks); self.assertEqual(len(alerts), 1)
        response = self.client.get(reverse('price_review'))
        self.assertContains(response, '1 facture en attente de contrôle')
        self.assertContains(response, 'aucune date de contrôle disponible')
        self.assertNotContains(response, 'Votre revue est à jour')
        self.assertContains(response, 'class="review-alerts" open')

    def test_catalogue_currency_and_source_currency_are_not_guessed(self):
        self.catalogue.devise = 'UNK'; self.catalogue.save()
        self.assertEqual(control_invoice_against_catalogue(self.invoice)['status'], 'devise_differente')
        self.catalogue.devise = 'CHF'; self.catalogue.save()
        for currency in ('EUR', 'UNK', None):
            self.product.price_source['currency'] = currency; self.product.save()
            detail = current_detail(self.line)
            self.assertEqual(detail['status'], 'devise_differente')
            self.assertIsNone(detail['impact']); self.assertFalse(detail['can_claim'])

    def test_missing_price_and_unit_remain_review_items(self):
        self.product.prix_unitaire_ht = None; self.product.save()
        self.assertEqual(current_detail(self.line)['status'], 'prix_manquant')
        self.product.prix_unitaire_ht = 8; self.product.save()
        self.line.unite = ''; self.line.save()
        self.assertEqual(current_detail(self.line)['status'], 'unite_a_verifier')

    def test_initial_origin_is_visible_in_review_invoice_and_claim(self):
        response = self.client.get(reverse('price_review'))
        self.assertContains(response, 'Prix de référence initial issu des documents')
        self.assertContains(response, 'Référence initiale sans période contractuelle')
        response = self.client.get(reverse('facture_detail', args=[self.invoice.pk]))
        self.assertContains(response, 'prix de référence initiaux')
        self.assertTrue(snapshot(current_detail(self.line))['initial_reference'])
        self.action('claim')
        claim = SupplierClaim.objects.get(); claim.state = 'prepared'; claim.save()
        response = self.client.get(reverse('claim_document', args=[claim.pk]))
        self.assertContains(response, 'Prix de référence initial issu des documents')
        self.assertContains(response, 'F-SOURCE')

    def test_deferred_items_do_not_display_all_clear(self):
        self.action('defer', reason='Vérification auprès du fournisseur')
        response = self.client.get(reverse('price_review'))
        self.assertContains(response, '1 fiche en attente')
        self.assertNotContains(response, 'Votre revue est à jour')

    def test_price_changes_reopen_comparison_and_preserve_source_invoice(self):
        self.action('price', price='8.84')
        self.assertFalse(review_queue()[0])
        self.line.refresh_from_db(); self.assertEqual(self.line.prix_unitaire_ht, Decimal('8.84'))
        self.product.refresh_from_db(); self.assertEqual(self.product.prix_unitaire_ht, Decimal('8.84'))
        self.product.prix_unitaire_ht = 7; self.product.save()
        self.assertEqual(len(review_queue()[0]), 1)
