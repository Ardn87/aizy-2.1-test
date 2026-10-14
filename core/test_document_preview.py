import base64
from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import TestCase, override_settings
from django.urls import reverse

from . import test_delivery_issues as fixtures
from .models import Facture


class DocumentPreviewTests(TestCase):
    def setUp(self):
        self.media = TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        settings = override_settings(MEDIA_ROOT=self.media.name)
        settings.enable()
        self.addCleanup(settings.disable)
        fixtures.DeliveryIssueReviewTests.setUp(self)

    def write_delivery(self, filename, content):
        Path(self.media.name, filename).write_bytes(content)
        self.delivery.fichier = filename
        self.delivery.save(update_fields=["fichier", "date_modification"])

    def test_delivery_photo_opens_inside_card_and_file_remains_protected(self):
        self.write_delivery("original.png", base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aY1cAAAAASUVORK5CYII="))
        page = self.client.get(reverse("delivery_issue_review"))
        self.assertEqual(page.context["current"].preview_type, "image")
        self.assertContains(page, 'class="action-panel proof-panel delivery-issue-proof"')
        self.assertContains(page, '<img class="review-document"')
        self.assertNotContains(page, 'target="_blank"')
        response = self.client.get(reverse("bon_livraison_file", args=[self.delivery.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Content-Type"], "image/png")
        self.assertEqual(response.headers["X-Frame-Options"], "SAMEORIGIN")
        b"".join(response.streaming_content)
        self.client.logout()
        self.assertEqual(self.client.get(reverse("bon_livraison_file", args=[self.delivery.pk])).status_code, 302)

    def test_delivery_pdf_uses_inline_frame_and_still_downloads(self):
        self.write_delivery("original.pdf", b"%PDF-1.4\nfixture")
        page = self.client.get(reverse("delivery_issue_review"))
        self.assertEqual(page.context["current"].preview_type, "pdf")
        self.assertContains(page, 'title="Bon de livraison original"')
        url = reverse("bon_livraison_file", args=[self.delivery.pk])
        response = self.client.get(url)
        self.assertEqual(response.headers["X-Frame-Options"], "SAMEORIGIN")
        self.assertTrue(response.headers["Content-Disposition"].startswith("inline"))
        b"".join(response.streaming_content)
        response = self.client.get(url, {"download":"1"})
        self.assertTrue(response.headers["Content-Disposition"].startswith("attachment"))
        b"".join(response.streaming_content)

    def test_missing_delivery_file_shows_message_without_broken_frame(self):
        page = self.client.get(reverse("delivery_issue_review"))
        self.assertContains(page, "L’aperçu de ce fichier n’est pas disponible.")
        self.assertNotContains(page, 'title="Bon de livraison original"')

    def test_review_invoice_permits_same_origin_frame_without_relaxing_other_pages(self):
        Path(self.media.name, "invoice.pdf").write_bytes(b"%PDF-1.4\nfixture")
        invoice = Facture.objects.create(
            fournisseur=self.supplier, fichier="invoice.pdf", statut="a_valider")
        url = reverse("review_invoice_file", args=[invoice.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Frame-Options"], "SAMEORIGIN")
        self.assertTrue(response.headers["Content-Disposition"].startswith("inline"))
        b"".join(response.streaming_content)
        download = self.client.get(url, {"download":"1"})
        self.assertTrue(download.headers["Content-Disposition"].startswith("attachment"))
        b"".join(download.streaming_content)
        self.assertEqual(self.client.get(reverse("dashboard")).headers["X-Frame-Options"], "DENY")
