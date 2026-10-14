import tempfile
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import (
    AnalysisJob,
    BonLivraison,
    DeliveryIssue,
    Fournisseur,
    LigneBonLivraison,
    Restaurant,
    RestaurantAccess,
)
from .services.reconciliation import line_fingerprint


class DeliveryFeedbackTests(TestCase):
    def setUp(self):
        self.media = tempfile.TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        override = override_settings(MEDIA_ROOT=self.media.name)
        override.enable()
        self.addCleanup(override.disable)
        self.restaurant = Restaurant.objects.create(nom="Le Dahu")
        self.supplier = Fournisseur.objects.create(restaurant=self.restaurant, nom="Bianchi")
        self.reception = get_user_model().objects.create_user("reception_delivery", password="test-only")
        self.reception.groups.add(Group.objects.get_or_create(name="Reception")[0])
        RestaurantAccess.objects.create(user=self.reception, restaurant=self.restaurant)
        self.client.force_login(self.reception)

    def photo(self, name="bl.jpg", content=b"\xff\xd8\xff\xe0delivery"):
        return SimpleUploadedFile(name, content, content_type="image/jpeg")

    def upload_with_feedback(self):
        return self.client.post(
            reverse("document_deposit"),
            {
                "kind": "bon_livraison",
                "fournisseur": self.supplier.pk,
                "delivery_comments": "on",
                "files": self.photo(),
            },
            HTTP_ACCEPT="application/json",
        )

    def make_ready(self):
        response = self.upload_with_feedback()
        delivery = BonLivraison.objects.get()
        delivery.statut = BonLivraison.Statut.IMPORTE
        delivery.save(update_fields=["statut", "date_modification"])
        AnalysisJob.objects.filter(document_type="bon_livraison", document_id=delivery.pk).update(
            state="done", finished_at=timezone.now()
        )
        line = LigneBonLivraison.objects.create(
            bon_livraison=delivery,
            reference_produit="P-001",
            designation="Tomates",
            quantite_livree=4,
            unite="kg",
        )
        return response, delivery, line

    def test_upload_starts_analysis_and_returns_feedback_url(self):
        response = self.upload_with_feedback()
        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertEqual(payload["received"], 1)
        self.assertEqual(len(payload["documents"]), 1)
        delivery = BonLivraison.objects.get()
        self.assertIsNotNone(delivery.delivery_feedback_requested_at)
        self.assertIsNone(delivery.delivery_feedback_completed_at)
        self.assertEqual(payload["documents"][0]["feedback_url"], reverse("delivery_feedback", args=[delivery.pk]))
        self.assertEqual(AnalysisJob.objects.get(document_id=delivery.pk).state, "queued")
        self.assertEqual(Path(delivery.fichier.path).read_bytes(), b"\xff\xd8\xff\xe0delivery")

    def test_feedback_requires_one_real_delivery_note(self):
        response = self.client.post(
            reverse("document_deposit"),
            {
                "kind": "combined",
                "fournisseur": self.supplier.pk,
                "delivery_comments": "on",
                "files": self.photo(),
            },
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("delivery_comments", response.json()["errors"])
        self.assertEqual(BonLivraison.objects.count(), 0)
        response = self.client.post(
            reverse("document_deposit"),
            {
                "kind": "bon_livraison",
                "fournisseur": self.supplier.pk,
                "delivery_comments": "on",
                "files": [self.photo("one.jpg"), self.photo("two.jpg", b"\xff\xd8\xff\xe0two")],
            },
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("files", response.json()["errors"])
        self.assertEqual(BonLivraison.objects.count(), 0)

    def test_feedback_status_moves_from_processing_to_product_list(self):
        response = self.upload_with_feedback()
        delivery = BonLivraison.objects.get()
        url = response.json()["documents"][0]["feedback_url"]
        pending = self.client.get(url)
        self.assertEqual(pending.json()["state"], "processing")
        delivery.statut = BonLivraison.Statut.IMPORTE
        delivery.save(update_fields=["statut", "date_modification"])
        AnalysisJob.objects.filter(document_id=delivery.pk).update(state="done", finished_at=timezone.now())
        LigneBonLivraison.objects.create(
            bon_livraison=delivery, designation="Filet de poulet", quantite_livree=5, unite="kg"
        )
        ready = self.client.get(url)
        self.assertEqual(ready.json()["state"], "ready")
        self.assertIn("Filet de poulet", ready.json()["html"])
        self.assertIn("data-delivery-feedback-form", ready.json()["html"])

    def test_failed_or_empty_ocr_keeps_feedback_open_without_inventing_products(self):
        response = self.upload_with_feedback()
        delivery = BonLivraison.objects.get()
        url = response.json()["documents"][0]["feedback_url"]
        delivery.statut = BonLivraison.Statut.ANALYSE_ECHOUEE
        delivery.save(update_fields=["statut", "date_modification"])
        AnalysisJob.objects.filter(document_id=delivery.pk).update(state="failed", finished_at=timezone.now())
        failed = self.client.get(url).json()
        self.assertEqual(failed["state"], "failed")
        self.assertIn("conservé", failed["message"])
        delivery.statut = BonLivraison.Statut.A_VERIFIER
        delivery.save(update_fields=["statut", "date_modification"])
        AnalysisJob.objects.filter(document_id=delivery.pk).update(state="done")
        empty = self.client.get(url).json()
        self.assertEqual(empty["state"], "empty")
        self.assertFalse(DeliveryIssue.objects.exists())
        delivery.refresh_from_db()
        self.assertIsNone(delivery.delivery_feedback_completed_at)

    def test_multiple_product_problems_are_saved_atomically(self):
        _, delivery, first = self.make_ready()
        second = LigneBonLivraison.objects.create(
            bon_livraison=delivery, reference_produit="P-002", designation="Pommes", quantite_livree=3, unite="kg"
        )
        response = self.client.post(
            reverse("delivery_feedback", args=[delivery.pk]),
            {
                "action": "save",
                "line": [str(first.pk), str(second.pk)],
                f"comment_{first.pk}": "Deux colis sont abîmés.",
                f"comment_{second.pk}": "Produit manquant à la livraison.",
            },
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["state"], "completed")
        self.assertEqual(DeliveryIssue.objects.count(), 2)
        issue = DeliveryIssue.objects.get(line=first)
        self.assertEqual(issue.snapshot["designation"], "Tomates")
        self.assertEqual(issue.source_fingerprint, line_fingerprint(first))
        self.assertEqual(issue.reported_by, self.reception)
        delivery.refresh_from_db()
        self.assertEqual(delivery.delivery_feedback_completed_by, self.reception)
        self.assertIsNotNone(delivery.delivery_feedback_completed_at)
        repeated = self.client.post(
            reverse("delivery_feedback", args=[delivery.pk]),
            {"action": "save", "line": str(first.pk), f"comment_{first.pk}": "Nouvelle saisie"},
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(DeliveryIssue.objects.count(), 2)

    def test_invalid_comment_or_foreign_line_rolls_everything_back(self):
        _, delivery, first = self.make_ready()
        second = LigneBonLivraison.objects.create(
            bon_livraison=delivery, designation="Pommes", quantite_livree=3, unite="kg"
        )
        response = self.client.post(
            reverse("delivery_feedback", args=[delivery.pk]),
            {
                "action": "save",
                "line": [str(first.pk), str(second.pk)],
                f"comment_{first.pk}": "Emballage ouvert à la réception.",
                f"comment_{second.pk}": "non",
            },
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(DeliveryIssue.objects.count(), 0)
        delivery.refresh_from_db()
        self.assertIsNone(delivery.delivery_feedback_completed_at)
        other = BonLivraison.objects.create(fournisseur=self.supplier, fichier="other.pdf")
        foreign = LigneBonLivraison.objects.create(bon_livraison=other, designation="Étranger", quantite_livree=1)
        response = self.client.post(
            reverse("delivery_feedback", args=[delivery.pk]),
            {"action": "save", "line": str(foreign.pk), f"comment_{foreign.pk}": "Mauvais produit reçu."},
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(DeliveryIssue.objects.count(), 0)

    def test_unexpected_save_error_returns_json_and_rolls_back(self):
        _, delivery, line = self.make_ready()
        with patch("core.operations_views.DeliveryIssue.save", side_effect=RuntimeError("test failure")):
            with self.assertLogs("core.operations_views", level="ERROR"):
                response = self.client.post(
                    reverse("delivery_feedback", args=[delivery.pk]),
                    {
                        "action": "save",
                        "line": str(line.pk),
                        f"comment_{line.pk}": "Produit abîmé à la réception.",
                    },
                    HTTP_ACCEPT="application/json",
                )
        self.assertEqual(response.status_code, 500)
        self.assertIn("Référence technique", response.json()["error"])
        self.assertFalse(DeliveryIssue.objects.exists())
        delivery.refresh_from_db()
        self.assertIsNone(delivery.delivery_feedback_completed_at)

    def test_feedback_accepts_csrf_protected_browser_post(self):
        _, delivery, line = self.make_ready()
        browser = Client(enforce_csrf_checks=True)
        browser.force_login(self.reception)
        browser.get(reverse("document_deposit"))
        csrf_token = browser.cookies[settings.CSRF_COOKIE_NAME].value
        response = browser.post(
            reverse("delivery_feedback", args=[delivery.pk]),
            {
                "action": "save",
                "line": str(line.pk),
                f"comment_{line.pk}": "Produit abîmé à la réception.",
            },
            HTTP_ACCEPT="application/json",
            HTTP_X_CSRFTOKEN=csrf_token,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["state"], "completed")
        self.assertTrue(DeliveryIssue.objects.filter(line=line).exists())
        delivery.refresh_from_db()
        self.assertIsNotNone(delivery.delivery_feedback_completed_at)

    def test_no_problem_finishes_the_feedback_without_creating_issue(self):
        _, delivery, _ = self.make_ready()
        response = self.client.post(
            reverse("delivery_feedback", args=[delivery.pk]), {"action": "none"}, HTTP_ACCEPT="application/json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(DeliveryIssue.objects.exists())
        delivery.refresh_from_db()
        self.assertIsNotNone(delivery.delivery_feedback_completed_at)

    def test_feedback_cannot_be_completed_before_ocr_products_are_ready(self):
        self.upload_with_feedback()
        delivery = BonLivraison.objects.get()
        response = self.client.post(
            reverse("delivery_feedback", args=[delivery.pk]),
            {"action": "none"},
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("fin de l’analyse", response.json()["error"])
        delivery.refresh_from_db()
        self.assertIsNone(delivery.delivery_feedback_completed_at)
        self.assertFalse(DeliveryIssue.objects.exists())

    def test_reception_cannot_access_another_restaurants_delivery(self):
        other_restaurant = Restaurant.objects.create(nom="Autre établissement")
        other_supplier = Fournisseur.objects.create(restaurant=other_restaurant, nom="Invisible")
        delivery = BonLivraison.objects.create(
            fournisseur=other_supplier, fichier="private.pdf", delivery_feedback_requested_at=timezone.now()
        )
        self.assertEqual(self.client.get(reverse("delivery_feedback", args=[delivery.pk])).status_code, 404)

    def test_all_pending_comments_remain_reachable_by_pages_and_direct_resume(self):
        deliveries = [
            BonLivraison.objects.create(
                fournisseur=self.supplier,
                fichier=f"pending-{index}.pdf",
                delivery_feedback_requested_at=timezone.now(),
            )
            for index in range(11)
        ]
        first_page = self.client.get(reverse("document_deposit"))
        self.assertEqual(first_page.context["pending_feedback_page"].paginator.count, 11)
        self.assertEqual(len(first_page.context["pending_feedback_deliveries"]), 10)
        second_page = self.client.get(reverse("document_deposit"), {"feedback_page": 2})
        self.assertEqual(len(second_page.context["pending_feedback_deliveries"]), 1)
        direct = self.client.get(reverse("document_deposit"), {"feedback": deliveries[0].pk})
        self.assertEqual(
            direct.context["resume_feedback_url"],
            reverse("delivery_feedback", args=[deliveries[0].pk]),
        )


class DeliveryIssueReviewTests(TestCase):
    def setUp(self):
        self.restaurant = Restaurant.objects.create(nom="Le Dahu")
        self.supplier = Fournisseur.objects.create(restaurant=self.restaurant, nom="Bianchi")
        self.delivery = BonLivraison.objects.create(
            fournisseur=self.supplier,
            fichier="bl.pdf",
            numero_bon_livraison="BL-2026-01",
            delivery_feedback_requested_at=timezone.now(),
            delivery_feedback_completed_at=timezone.now(),
        )
        self.line = LigneBonLivraison.objects.create(
            bon_livraison=self.delivery,
            reference_produit="P-001",
            designation="Tomates",
            quantite_livree=4,
            unite="kg",
        )
        self.reporter = get_user_model().objects.create_user("reporter", password="test-only")
        self.issue = DeliveryIssue.objects.create(
            delivery=self.delivery,
            line=self.line,
            comment="Deux caisses sont arrivées abîmées.",
            source_fingerprint=line_fingerprint(self.line),
            snapshot={
                "supplier": "Bianchi",
                "restaurant": "Le Dahu",
                "delivery_number": "BL-2026-01",
                "delivery_date": None,
                "reference": "P-001",
                "designation": "Tomates",
                "quantity": "4",
                "unit": "kg",
            },
            reported_by=self.reporter,
        )
        self.manager = get_user_model().objects.create_user("delivery_manager", password="test-only")
        self.manager.groups.add(Group.objects.get_or_create(name="Gestionnaire")[0])
        RestaurantAccess.objects.create(user=self.manager, restaurant=self.restaurant)
        self.client.force_login(self.manager)

    def test_review_shows_horizontal_card_and_dashboard_counter(self):
        response = self.client.get(reverse("delivery_issue_review"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Deux caisses sont arrivées abîmées.")
        self.assertContains(response, "BL-2026-01")
        self.assertContains(response, 'class="review-card')
        self.assertEqual(response.context["pending_count"], 1)
        dashboard = self.client.get(reverse("dashboard"))
        self.assertEqual(dashboard.context["home_counts"]["delivery_issues"], 1)
        self.assertContains(dashboard, "Problèmes de livraison")

    def test_manager_can_confirm_and_history_keeps_snapshot(self):
        response = self.client.post(
            reverse("delivery_issue_action", args=[self.issue.pk]),
            {"action": "confirm", "review_note": "Défaut confirmé avec la réception."},
        )
        self.assertEqual(response.status_code, 302)
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.state, DeliveryIssue.State.CONFIRMED)
        self.assertEqual(self.issue.reviewed_by, self.manager)
        self.assertIsNotNone(self.issue.reviewed_at)
        history = self.client.get(reverse("delivery_issue_review"), {"state": "history"})
        self.assertContains(history, "Tomates")
        self.assertContains(history, "Défaut confirmé avec la réception.")

    def test_source_change_is_visible_and_does_not_rewrite_snapshot(self):
        self.line.designation = "Tomates corrigées"
        self.line.save()
        response = self.client.get(reverse("delivery_issue_review"))
        self.assertContains(response, "modifi")
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.snapshot["designation"], "Tomates")

    def test_history_keeps_snapshot_when_delivery_is_archived(self):
        self.client.post(
            reverse("delivery_issue_action", args=[self.issue.pk]),
            {"action": "confirm"},
        )
        self.delivery.archived_at = timezone.now()
        self.delivery.numero_bon_livraison = "BL-CORRIGE"
        self.delivery.save(update_fields=["archived_at", "numero_bon_livraison", "date_modification"])
        self.supplier.nom = "Fournisseur renommé"
        self.supplier.save(update_fields=["nom"])
        history = self.client.get(reverse("delivery_issue_review"), {"state": "history"})
        self.assertContains(history, "Bianchi")
        self.assertContains(history, "BL-2026-01")
        self.assertContains(history, "BL original archivé")
        self.assertNotContains(history, "BL-CORRIGE")

    def test_reception_and_price_review_roles_cannot_open_delivery_review(self):
        for role in ("Reception", "Revue"):
            user = get_user_model().objects.create_user(f"restricted_{role}", password="test-only")
            user.groups.add(Group.objects.get_or_create(name=role)[0])
            RestaurantAccess.objects.create(user=user, restaurant=self.restaurant)
            self.client.force_login(user)
            self.assertEqual(self.client.get(reverse("delivery_issue_review")).status_code, 403)
            self.assertEqual(
                self.client.post(reverse("delivery_issue_action", args=[self.issue.pk]), {"action": "dismiss"}).status_code,
                403,
            )
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.state, DeliveryIssue.State.PENDING)

    def test_manager_scope_hides_other_restaurant_issues(self):
        other = Restaurant.objects.create(nom="Restaurant non autorisé")
        supplier = Fournisseur.objects.create(restaurant=other, nom="Autre fournisseur")
        delivery = BonLivraison.objects.create(fournisseur=supplier, fichier="other.pdf")
        line = LigneBonLivraison.objects.create(bon_livraison=delivery, designation="Invisible", quantite_livree=1)
        DeliveryIssue.objects.create(
            delivery=delivery,
            line=line,
            comment="Ce problème ne doit pas apparaître.",
            source_fingerprint=line_fingerprint(line),
        )
        response = self.client.get(reverse("delivery_issue_review"))
        self.assertEqual(response.context["pending_count"], 1)
        self.assertNotContains(response, "Ce problème ne doit pas apparaître.")
