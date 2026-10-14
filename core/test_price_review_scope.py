from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from core import test_price_review as fixtures
from core.models import LigneCatalogue, BonLivraison, LigneBonLivraison
from core.services.price_review import review_queue, current_detail


class PriceReviewScopeTests(TestCase):
    setUp = fixtures.PriceReviewTests.setUp
    action = fixtures.PriceReviewTests.action

    def test_equal_prices_with_missing_unit_do_not_become_price_cards(self):
        self.line.unite = ''; self.line.prix_unitaire_ht = 8; self.line.save()
        self.assertEqual(current_detail(self.line)['status'], 'unite_a_verifier')
        tasks, alerts = review_queue()
        self.assertFalse(tasks)
        self.assertEqual(len(alerts), 1)
        self.assertIn('unité ou conditionnement', alerts[0]['message'])
        response = self.client.get(reverse('price_review'))
        self.assertIsNone(response.context['current'])
        self.assertNotContains(response, 'Votre revue est à jour')

    def test_equal_prices_with_known_units_are_hidden_even_without_quantity_or_bl(self):
        self.line.quantite = None; self.line.prix_unitaire_ht = 8; self.line.save()
        self.assertFalse(BonLivraison.objects.exists())
        tasks, alerts = review_queue()
        self.assertFalse(tasks); self.assertFalse(alerts)

    def test_different_quantities_do_not_create_price_discrepancy(self):
        self.line.prix_unitaire_ht = 8; self.line.quantite = 99; self.line.save()
        bl = BonLivraison.objects.create(fournisseur=self.supplier, fichier='bl.pdf')
        LigneBonLivraison.objects.create(bon_livraison=bl, reference_produit='P1',
            unite='kg', quantite_livree=1, prix_unitaire=8)
        self.assertFalse(review_queue()[0])

    def test_actual_price_difference_is_visible_without_received_or_invoiced_quantity(self):
        self.line.quantite = None; self.line.save()
        tasks, alerts = review_queue()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]['status'], 'ecart_prix')
        self.assertFalse(tasks[0]['can_claim'])
        self.assertIsNone(tasks[0]['impact'])
        self.assertFalse(alerts)

    def test_different_unconverted_units_are_data_alerts_not_price_differences(self):
        self.line.unite = 'carton'; self.line.save()
        tasks, alerts = review_queue()
        self.assertFalse(tasks); self.assertEqual(len(alerts), 1)

    def test_ambiguous_catalogue_product_is_a_data_alert(self):
        LigneCatalogue.objects.create(catalogue=self.catalogue, reference_produit='P1', unite='kg', prix_unitaire_ht=7)
        tasks, alerts = review_queue()
        self.assertFalse(tasks)
        self.assertIn('correspondance produit ambiguë', alerts[0]['message'])

    def test_missing_document_price_is_a_data_alert(self):
        self.line.prix_unitaire_ht = None; self.line.save()
        tasks, alerts = review_queue()
        self.assertFalse(tasks)
        self.assertIn('prix du document absent', alerts[0]['message'])

    def test_missing_catalogue_price_stays_actionable(self):
        self.product.prix_unitaire_ht = None; self.product.save()
        tasks, alerts = review_queue()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]['status'], 'prix_manquant')
        self.assertTrue(tasks[0]['can_price'])
        self.assertFalse(alerts)

    def test_missing_catalogue_product_stays_actionable(self):
        self.line.reference_produit = 'NEW'; self.line.save()
        tasks, alerts = review_queue()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]['status'], 'produit_non_trouve')
        self.assertTrue(tasks[0]['can_price'])
        self.assertFalse(alerts)

    def test_lower_price_is_still_a_price_difference(self):
        self.line.prix_unitaire_ht = Decimal('7'); self.line.save()
        tasks, alerts = review_queue()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]['ecart'], -1)
        self.assertFalse(tasks[0]['can_claim'])

    def test_cached_results_are_filtered_in_all_queue_states(self):
        self.line.unite = ''; self.line.prix_unitaire_ht = 8; self.line.save()
        self.action('defer', reason='Ancien report lié à une unité')
        review_queue()
        with patch('core.services.price_review.control_invoice_against_catalogue', side_effect=AssertionError('Expected cache')):
            for state in ('pending', 'deferred', 'all'):
                tasks, alerts = review_queue(state)
                self.assertFalse(tasks); self.assertEqual(len(alerts), 1)

    def test_source_correction_automatically_exposes_real_price_difference(self):
        self.line.unite = ''; self.line.save()
        self.assertFalse(review_queue()[0])
        self.line.unite = 'kg'; self.line.save()
        tasks, alerts = review_queue()
        self.assertEqual(len(tasks), 1); self.assertFalse(alerts)
