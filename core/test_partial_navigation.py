"""Optional end-to-end checks against an isolated Django test database.

AIZY_BROWSER_TEST=1 AIZY_PLAYWRIGHT_MODULE=... AIZY_CHROMIUM=... python manage.py test core.test_partial_navigation
"""
import json
import os
import base64
from tempfile import TemporaryDirectory
from pathlib import Path
import subprocess
from unittest import skipUnless

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.contrib.auth.models import Group
from django.conf import settings
from django.test import override_settings
from django.utils import timezone

from . import test_price_review as fixtures
from .models import AnalysisJob, BonLivraison, DeliveryIssue, LigneBonLivraison, LigneFacture, SupplierClaimLine


@skipUnless(os.environ.get('AIZY_BROWSER_TEST') == '1', 'Opt-in Chromium integration checks')
@override_settings(ALLOWED_HOSTS=['localhost', '127.0.0.1', 'testserver'],
                   STORAGES={'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
                             'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class PartialNavigationBrowserTests(StaticLiveServerTestCase):
    def test_navigation_and_mutations_on_desktop_and_phone(self):
        fixtures.PriceReviewTests.setUp(self)
        media = TemporaryDirectory()
        self.addCleanup(media.cleanup)
        media_settings = override_settings(MEDIA_ROOT=media.name)
        media_settings.enable()
        self.addCleanup(media_settings.disable)
        Path(media.name, "browser-feedback.png").write_bytes(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aY1cAAAAASUVORK5CYII="))
        # A real, self-contained PDF for testing the protected inline response.
        stream = b"BT /F1 18 Tf 40 760 Td (Facture originale Aizy) Tj ET"
        objects = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        ]
        pdf, offsets = bytearray(b"%PDF-1.4\n"), [0]
        for index, obj in enumerate(objects, 1):
            offsets.append(len(pdf))
            pdf.extend(str(index).encode() + b" 0 obj\n" + obj + b"\nendobj\n")
        xref = len(pdf)
        pdf.extend(b"xref\n0 6\n0000000000 65535 f \n")
        for offset in offsets[1:]:
            pdf.extend(f"{offset:010d} 00000 n \n".encode())
        pdf.extend(f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode())
        Path(media.name, "example.pdf").write_bytes(pdf)
        self.supplier.email = 'reclamations.bianchi@example.invalid'
        self.supplier.save(update_fields=['email'])
        self.user.groups.add(Group.objects.get_or_create(name='Admin')[0])
        LigneFacture.objects.create(facture=self.invoice, reference_produit='P1', designation='Poulet deuxième lot',
                                   unite='kg', quantite=3, prix_unitaire_ht='9')
        LigneFacture.objects.create(facture=self.invoice, reference_produit='NEW', designation='Nouveau produit',
                                   unite='kg', quantite=2, prix_unitaire_ht='6')
        delivery = BonLivraison.objects.create(
            fournisseur=self.supplier, fichier='browser-feedback.png',
            numero_bon_livraison='BL-BROWSER', statut=BonLivraison.Statut.A_VERIFIER,
            delivery_feedback_requested_at=timezone.now())
        delivery_line = LigneBonLivraison.objects.create(
            bon_livraison=delivery, reference_produit='P-BL', designation='Produit livré',
            quantite_livree=2, unite='kg')
        AnalysisJob.objects.create(
            document_type='bon_livraison', document_id=delivery.pk, state='done',
            finished_at=timezone.now(), actor=self.user)
        env = dict(os.environ, AIZY_TEST_URL=self.live_server_url,
                   AIZY_TEST_SESSION_COOKIE=settings.SESSION_COOKIE_NAME,
                   AIZY_TEST_SESSION=self.client.cookies[settings.SESSION_COOKIE_NAME].value,
                   AIZY_TEST_LINE=str(self.line.pk),
                   AIZY_TEST_DELIVERY=str(delivery.pk))
        result = subprocess.run(['node', str(Path(__file__).resolve().parent.parent / 'scripts/test_partial_navigation.cjs')],
                                env=env, text=True, capture_output=True, timeout=150)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['errors'], [])
        self.assertEqual(report['viewports'], [1440, 390])
        self.assertEqual(DeliveryIssue.objects.filter(line=delivery_line).count(), 1)
        issue = DeliveryIssue.objects.get(line=delivery_line)
        self.assertEqual(issue.state, DeliveryIssue.State.CONFIRMED)
        self.assertEqual(SupplierClaimLine.objects.filter(delivery_issue=issue).count(), 1)
        delivery.refresh_from_db()
        self.assertIsNotNone(delivery.delivery_feedback_completed_at)
