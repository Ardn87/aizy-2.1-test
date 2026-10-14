from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from core import test_price_review as fixtures
from core.models import LigneFacture, SupplierClaim, SupplierClaimLine, PriceReviewEvent
from core.services.price_review import current_detail


class PriceClaimPreparationTests(TestCase):
    action = fixtures.PriceReviewTests.action

    def setUp(self):
        fixtures.PriceReviewTests.setUp(self)
        self.invoice.statut = 'a_valider'
        self.invoice.verified_at = None
        self.invoice.verified_by = None
        self.invoice.validation_exception = ''
        self.invoice.save()

    def test_positive_gap_exposes_button_and_local_confirmation(self):
        detail = current_detail(self.line)
        self.assertFalse(detail['can_claim'])
        self.assertTrue(detail['can_prepare_claim'])
        self.assertTrue(detail['requires_claim_confirmation'])
        response = self.client.get(reverse('price_review'))
        self.assertContains(response, 'Préparer une réclamation')
        self.assertContains(response, 'class="claim-panel claim-inline"')
        self.assertNotContains(response, 'Confirmer cette ligne et l’ajouter au brouillon')
        self.assertContains(response, 'name="confirm_line" value="1"')
        self.assertNotContains(response, 'Vérifiez les données de cette facture avant de préparer une réclamation')

    def test_explicit_confirmation_creates_draft_without_validating_invoice(self):
        # An unrelated incomplete product must not block this complete price claim.
        LigneFacture.objects.create(facture=self.invoice, reference_produit='OTHER', designation='Autre produit')
        response = self.action('claim', confirm_line='1')
        self.assertEqual(response.status_code, 302)
        claim = SupplierClaim.objects.get(); item = SupplierClaimLine.objects.get()
        self.assertEqual(claim.state, 'draft')
        self.assertEqual(item.snapshot['impact'], '16.80')
        self.assertEqual(item.snapshot['line_confirmation']['actor'], self.user.pk)
        self.assertIn('line_confirmation', PriceReviewEvent.objects.get(action='claim').after)
        self.invoice.refresh_from_db()
        self.assertIsNone(self.invoice.verified_at)
        self.assertEqual(self.invoice.statut, 'a_valider')
        self.assertEqual(self.invoice.validation_exception, '')
        self.line.refresh_from_db(); self.assertEqual(self.line.prix_unitaire_ht, Decimal('8.84'))

    def test_unconfirmed_post_cannot_bypass_local_confirmation(self):
        for value in ('', '0', 'yes'):
            self.action('claim', confirm_line=value)
        self.assertFalse(SupplierClaim.objects.exists())

    def test_missing_quantity_keeps_button_with_explanation_without_claim_submission(self):
        self.line.quantite = None; self.line.save()
        response = self.client.get(reverse('price_review'))
        self.assertContains(response, 'Préparer une réclamation')
        self.assertContains(response, 'la quantité facturée positive')
        self.assertNotContains(response, 'name="confirm_line"')
        self.assertNotContains(response, 'name="action" value="claim"')
        self.action('claim', confirm_line='1')
        self.assertFalse(SupplierClaimLine.objects.exists())

    def test_confirmation_does_not_bypass_missing_invoice_identifiers(self):
        self.invoice.numero_facture = ''; self.invoice.save()
        self.action('claim', confirm_line='1')
        self.assertFalse(SupplierClaimLine.objects.exists())
        self.assertContains(self.client.get(reverse('price_review')), 'le numéro de facture')

    def test_lower_price_has_no_claim_button(self):
        self.line.prix_unitaire_ht = 7; self.line.save()
        response = self.client.get(reverse('price_review'))
        self.assertNotContains(response, 'Préparer une réclamation')
        self.action('claim', confirm_line='1')
        self.assertFalse(SupplierClaimLine.objects.exists())

    def test_source_changes_invalidate_confirmation(self):
        fingerprint = current_detail(self.line)['fingerprint']
        self.line.prix_unitaire_ht = 9; self.line.save()
        response = self.client.post(reverse('review_action', args=[self.line.pk]),
            {'action': 'claim', 'confirm_line': '1', 'fingerprint': fingerprint}, follow=True)
        self.assertContains(response, 'ont changé')
        self.assertFalse(SupplierClaimLine.objects.exists())

    def test_repeat_confirmation_does_not_duplicate_claim(self):
        self.action('claim', confirm_line='1')
        self.action('claim', confirm_line='1')
        self.assertEqual(SupplierClaim.objects.count(), 1)
        self.assertEqual(SupplierClaimLine.objects.count(), 1)

    def test_same_claim_does_not_return_after_catalogue_restore(self):
        from core.models import CatalogueFournisseur, LigneCatalogue
        from core.services.price_review import review_queue
        self.action('claim', confirm_line='1')
        item = SupplierClaimLine.objects.get()
        item.claim.state = 'sent'; item.claim.save()
        original_snapshot = item.snapshot.copy()
        self.catalogue.statut = 'a_valider'; self.catalogue.save()
        restored = CatalogueFournisseur.objects.create(fournisseur=self.supplier, nom='Restauré',
            date_debut=self.catalogue.date_debut, date_fin=self.catalogue.date_fin, statut='valide', devise='CHF')
        LigneCatalogue.objects.create(catalogue=restored, reference_produit='P1', designation='Poulet',
                                      unite='KG', prix_unitaire_ht='8.00')
        self.assertNotEqual(current_detail(self.line)['fingerprint'], item.fingerprint)
        for state in ('pending', 'all', 'deferred'):
            self.assertEqual(review_queue(state)[0], [])
        item.refresh_from_db(); self.assertEqual(item.snapshot, original_snapshot)
        self.assertEqual(SupplierClaimLine.objects.count(), 1)

    def test_changed_claim_amount_stays_visible_without_duplicate_action(self):
        from core.services.price_review import review_queue
        self.action('claim', confirm_line='1')
        self.line.prix_unitaire_ht = Decimal('9.84'); self.line.save()
        tasks, _ = review_queue()
        self.assertEqual(len(tasks), 1)
        self.assertIn('existing_claim', tasks[0])
        self.assertFalse(tasks[0]['show_claim_action'])
        response = self.client.get(reverse('price_review'))
        self.assertContains(response, 'Déjà dans la réclamation')
        self.assertNotContains(response, 'name="action" value="claim"')
        self.action('claim', confirm_line='1')
        self.assertEqual(SupplierClaimLine.objects.count(), 1)

    def test_changed_quantity_is_not_hidden_by_existing_claim(self):
        from core.services.price_review import review_queue
        self.action('claim', confirm_line='1')
        self.line.quantite = 3; self.line.save()
        self.assertEqual(len(review_queue()[0]), 1)

    def test_quantity_claim_does_not_hide_price_difference(self):
        from core.services.price_review import review_queue
        self.action('claim', confirm_line='1')
        SupplierClaimLine.objects.update(category='quantity')
        from core.models import PriceReviewDecision
        PriceReviewDecision.objects.all().delete()
        tasks, _ = review_queue()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]['existing_claim_category'], 'quantity')
        self.assertFalse(tasks[0]['show_claim_action'])
