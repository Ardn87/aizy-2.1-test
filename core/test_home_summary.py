from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from . import test_price_review as fixtures
from .models import BonLivraison, SupplierClaim, Restaurant, Fournisseur, Facture, RestaurantAccess


class HomeSummaryTests(TestCase):
    setUp = fixtures.PriceReviewTests.setUp

    def summary(self):
        return self.client.get(reverse('dashboard')).context['home_counts']

    def test_counters_match_actual_queues_and_hide_empty_warning(self):
        summary=self.summary()
        self.assertEqual(summary['price'],self.client.get(reverse('price_review')).context['pending_count'])
        self.assertEqual(summary['reconcile'],self.client.get(reverse('reconciliation_queue')).context['totals']['attention'])
        self.assertEqual(summary['claims'],0)
        self.assertEqual(summary['attention'],0)
        self.assertNotContains(self.client.get(reverse('dashboard')),'class="home-admin-warning"')

    def test_deferred_or_equal_prices_are_not_pending(self):
        fixtures.PriceReviewTests.action(self,'defer',reason='Vérification en attente du fournisseur')
        self.assertEqual(self.summary()['price'],0)
        self.line.prix_unitaire_ht=8;self.line.save()
        self.assertEqual(self.summary()['price'],0)

    def test_claims_ignore_cancelled_closed_and_empty_dossiers(self):
        SupplierClaim.objects.create(fournisseur=self.supplier,currency='CHF',state='prepared',created_by=self.user)
        self.assertEqual(self.summary()['claims'],0)
        fixtures.PriceReviewTests.action(self,'claim')
        claim=SupplierClaim.objects.get(state='draft')
        self.assertEqual(self.summary()['claims'],1)
        for state in ('closed','cancelled'):
            claim.state=state;claim.save()
            self.assertEqual(self.summary()['claims'],0)

    def test_failed_documents_are_distinct_and_archives_are_excluded(self):
        self.invoice.statut='analyse_echouee';self.invoice.save()
        delivery=BonLivraison.objects.create(fournisseur=self.supplier,fichier='failed-bl.pdf',statut='analyse_echouee')
        summary=self.summary()
        self.assertEqual(summary['failed'],2)
        self.assertEqual(summary['blocked'],0)
        self.assertEqual(summary['attention'],2)
        delivery.archived_at=timezone.now();delivery.save()
        self.assertEqual(self.summary()['attention'],1)
        self.invoice.archived_at=timezone.now();self.invoice.save()
        self.assertEqual(self.summary()['attention'],0)
        self.assertEqual(self.summary()['reconcile'],0)

    def test_two_controls_on_same_invoice_produce_one_administrative_warning(self):
        self.line.unite='';self.line.save()
        self.invoice.devise='UNK';self.invoice.save()
        summary=self.summary()
        self.assertEqual(summary['blocked'],1)
        self.assertEqual(summary['attention'],1)
        self.assertContains(self.client.get(reverse('dashboard')),'class="home-admin-warning"')

    def test_other_restaurant_does_not_leak_into_counters(self):
        other=Restaurant.objects.create(nom='Établissement inaccessible')
        supplier=Fournisseur.objects.create(restaurant=other,nom='Invisible')
        Facture.objects.create(fournisseur=supplier,fichier='other.pdf',statut='analyse_echouee')
        # Explicit access is used by the tenant-scoped model managers.
        RestaurantAccess.objects.get_or_create(user=self.user,restaurant=self.restaurant)
        self.assertEqual(self.summary()['failed'],0)
