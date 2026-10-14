from datetime import date
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import BonLivraison, DeliveryIssue, Facture, Fournisseur, LigneBonLivraison, LigneFacture, Restaurant, SupplierClaim, SupplierClaimLine
from .services.delivery_claims import add_delivery_issue_to_claim
from .services.price_review import claim_total
from .services.reconciliation import line_fingerprint
from .test_delivery_issues import DeliveryIssueReviewTests


class DeliveryClaimTests(TestCase):
    setUp = DeliveryIssueReviewTests.setUp

    def confirm(self, issue=None, **data):
        return self.client.post(reverse("delivery_issue_action", args=[(issue or self.issue).pk]),
                                {"action":"confirm", **data})

    def another_issue(self, delivery=None):
        delivery = delivery or self.delivery
        line = LigneBonLivraison.objects.create(bon_livraison=delivery, designation="Fromage", quantite_livree=2, unite="kg")
        return DeliveryIssue.objects.create(delivery=delivery, line=line, comment="Emballage ouvert à la réception.",
                                           source_fingerprint=line_fingerprint(line))

    def price_draft(self, currency="EUR"):
        claim = SupplierClaim.objects.create(fournisseur=self.supplier, currency=currency, created_by=self.manager)
        invoice = Facture.objects.create(fournisseur=self.supplier, fichier="invoice.pdf", numero_facture="F-01",
                                         date_facture=date(2026,10,1), devise=currency)
        line = LigneFacture.objects.create(facture=invoice, designation="Poulet", quantite=1, unite="kg", prix_unitaire_ht=12)
        SupplierClaimLine.objects.create(claim=claim, invoice_line=line, fingerprint="price-snapshot", snapshot={
            "invoice_id":invoice.pk, "invoice_number":"F-01", "invoice_date":"2026-10-01",
            "product":"Poulet", "quantity":"1", "unit":"kg", "invoice_price":"12", "catalogue_price":"7",
            "reference":"P-002", "impact":"5.00",
        })
        return claim

    def test_confirmation_adds_bl_comment_and_review_note_without_inventing_amount(self):
        self.confirm(review_note="Défaut confirmé avec la réception.")
        item = SupplierClaimLine.objects.get(delivery_issue=self.issue)
        self.assertEqual(item.category, "delivery")
        self.assertIsNone(item.invoice_line_id)
        self.assertIsNone(item.snapshot["impact"])
        self.assertEqual(item.snapshot["delivery_number"], "BL-2026-01")
        self.assertEqual(item.snapshot["comment"], self.issue.comment)
        self.assertEqual(item.snapshot["review_note"], "Défaut confirmé avec la réception.")
        self.assertEqual(claim_total(item.claim), Decimal("0.00"))
        page = self.client.get(reverse("supplier_claims"), {"claim":item.claim_id})
        self.assertContains(page, self.issue.comment)
        self.assertContains(page, "À déterminer")
        self.assertNotContains(page, "Facturé  / Catalogue")
        self.assertContains(self.client.get(reverse("delivery_issue_review"), {"state":"history"}), "Voir la réclamation")
        self.assertEqual(self.client.get(reverse("dashboard")).context["home_counts"]["claims"], 1)

    def test_merge_with_existing_price_draft_and_preserve_its_currency_and_total(self):
        claim = self.price_draft(currency="CHF")
        self.confirm()
        self.assertEqual(SupplierClaim.objects.count(), 1)
        self.assertEqual(SupplierClaimLine.objects.get(delivery_issue=self.issue).claim_id, claim.pk)
        self.assertEqual(claim_total(claim), Decimal("5.00"))
        claim.refresh_from_db()
        self.assertEqual(claim.currency, "CHF")

    def test_repeated_confirmation_and_service_do_not_duplicate(self):
        self.confirm()
        self.confirm()
        claim, created = add_delivery_issue_to_claim(self.issue.pk, self.manager)
        self.assertFalse(created)
        self.assertEqual(claim.lines.count(), 1)

    def test_sent_claim_is_not_modified_and_new_draft_is_created(self):
        old = self.price_draft()
        old.state = "sent"
        old.save()
        saved = old.lines.get().snapshot.copy()
        self.confirm()
        item = SupplierClaimLine.objects.get(delivery_issue=self.issue)
        self.assertNotEqual(item.claim_id, old.pk)
        self.assertEqual(item.claim.state, "draft")
        self.assertEqual(old.lines.count(), 1)
        self.assertEqual(old.lines.get().snapshot, saved)

    def test_two_confirmed_products_are_grouped_in_one_supplier_draft(self):
        other = self.another_issue()
        self.confirm()
        self.confirm(other)
        self.assertEqual(SupplierClaim.objects.count(), 1)
        self.assertEqual(SupplierClaimLine.objects.filter(category="delivery").count(), 2)

    def test_delivery_only_document_contains_bl_and_comments_not_fake_prices(self):
        self.confirm(review_note="Le fournisseur doit remplacer les caisses.")
        claim = SupplierClaim.objects.get()
        self.client.post(reverse("claim_state", args=[claim.pk]), {"action":"prepare"})
        claim.refresh_from_db()
        self.assertEqual(claim.state, "prepared")
        response = self.client.get(reverse("claim_document", args=[claim.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "BL-2026-01")
        self.assertContains(response, self.issue.comment)
        self.assertContains(response, "Le fournisseur doit remplacer les caisses.")
        self.assertContains(response, "Montant à déterminer")
        self.assertNotContains(response, "Total demandé HT : 0")

    def test_mixed_document_preserves_invoice_numbers_and_known_total(self):
        claim = self.price_draft()
        self.confirm()
        self.client.post(reverse("claim_state", args=[claim.pk]), {"action":"prepare"})
        response = self.client.get(reverse("claim_document", args=[claim.pk]))
        self.assertContains(response, "Facture F-01")
        self.assertContains(response, "BL-2026-01")
        self.assertContains(response, "5,00")
        self.assertContains(response, "écarts chiffrés")

    def test_cancel_returns_delivery_to_review_and_reconfirm_keeps_cancelled_snapshot(self):
        self.confirm()
        claim = SupplierClaim.objects.get()
        saved = claim.lines.get().snapshot.copy()
        self.client.post(reverse("claim_state", args=[claim.pk]), {"action":"cancel", "confirmed":"1"})
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.state, "pending")
        self.confirm()
        self.assertEqual(SupplierClaimLine.objects.filter(delivery_issue=self.issue).count(), 2)
        self.assertEqual(claim.lines.get().snapshot, saved)
        self.assertEqual(SupplierClaimLine.objects.filter(delivery_issue=self.issue).exclude(claim__state="cancelled").count(), 1)

    def test_save_failure_rolls_back_confirmation_and_claim(self):
        with patch("core.services.delivery_claims.SupplierClaimLine.objects.create", side_effect=RuntimeError("test-only")):
            with self.assertRaises(RuntimeError):
                self.confirm()
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.state, "pending")
        self.assertFalse(SupplierClaim.objects.exists())

    def test_dismissed_signal_does_not_create_claim(self):
        self.client.post(reverse("delivery_issue_action", args=[self.issue.pk]), {"action":"dismiss"})
        self.assertFalse(SupplierClaim.objects.exists())

    def test_command_imports_previous_confirmations_once(self):
        self.issue.state = "confirmed"
        self.issue.reviewed_by = self.manager
        self.issue.reviewed_at = timezone.now()
        self.issue.save()
        for _ in range(2):
            call_command("attach_delivery_claims", stdout=StringIO())
        self.assertEqual(SupplierClaimLine.objects.filter(delivery_issue=self.issue).count(), 1)
        self.assertEqual(SupplierClaim.objects.get().created_by, self.manager)

    def test_confirmation_snapshot_survives_later_product_and_comment_changes(self):
        self.confirm()
        item = SupplierClaimLine.objects.get(delivery_issue=self.issue)
        self.issue.refresh_from_db()
        self.issue.comment = "Commentaire corrigé après confirmation"
        self.issue.save()
        self.line.designation = "Tomates modifiées"
        self.line.save()
        add_delivery_issue_to_claim(self.issue.pk)
        item.refresh_from_db()
        self.assertEqual(item.snapshot["product"], "Tomates")
        self.assertEqual(item.snapshot["comment"], "Deux caisses sont arrivées abîmées.")

    def test_another_supplier_is_kept_in_a_separate_draft(self):
        self.confirm()
        supplier = Fournisseur.objects.create(restaurant=self.restaurant, nom="Culturefood")
        delivery = BonLivraison.objects.create(fournisseur=supplier, fichier="other-bl.pdf")
        issue = self.another_issue(delivery)
        self.confirm(issue)
        self.assertEqual(SupplierClaim.objects.count(), 2)
        self.assertEqual(SupplierClaimLine.objects.get(delivery_issue=issue).claim.fournisseur, supplier)

    def test_out_of_scope_post_cannot_add_other_restaurants_delivery(self):
        restaurant = Restaurant.objects.create(nom="Restaurant inaccessible")
        supplier = Fournisseur.objects.create(restaurant=restaurant, nom="Fournisseur inaccessible")
        delivery = BonLivraison.objects.create(fournisseur=supplier, fichier="private-bl.pdf")
        issue = self.another_issue(delivery)
        self.assertEqual(self.confirm(issue).status_code, 404)
        self.assertFalse(SupplierClaim.objects.exists())

    def test_admin_deleting_draft_reopens_issue_without_deleting_source(self):
        from django.contrib.auth.models import Group
        self.manager.groups.add(Group.objects.get_or_create(name="Admin")[0])
        self.confirm()
        claim = SupplierClaim.objects.get()
        self.client.post(reverse("claim_state", args=[claim.pk]), {"action":"delete", "confirmed":"1"})
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.state, "pending")
        self.assertFalse(SupplierClaim.objects.exists())
        self.assertTrue(LigneBonLivraison.objects.filter(pk=self.line.pk).exists())

    def test_supplier_document_escapes_user_comment(self):
        self.issue.comment = "Défaut signalé : <script>alert(1)</script>"
        self.issue.save()
        self.confirm()
        claim = SupplierClaim.objects.get()
        self.client.post(reverse("claim_state", args=[claim.pk]), {"action":"prepare"})
        response = self.client.get(reverse("claim_document", args=[claim.pk]))
        self.assertNotContains(response, "<script>alert(1)</script>")
        self.assertContains(response, "&lt;script&gt;")
