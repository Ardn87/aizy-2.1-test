from .test_helpers import restaurant_fixture, invoice_fixture, upload_fixture
from datetime import date
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import CommandError
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from .access import ROLE_ADMIN, ROLE_GESTIONNAIRE, ROLE_RECEPTION
from .models import (
    AnalysisVersion,
    AIDiagnosticEvent,
    BonLivraison,
    CatalogueFournisseur,
    Devise,
    Facture,
    Fournisseur,
    LigneCatalogue,
    LigneBonLivraison,
    LigneFacture,
    Restaurant,
)
from .services import catalogue_ai, document_ai
from .services.catalogue_control import control_invoice_against_catalogue
from .services.document_ai import DocumentAIConfigurationError, DocumentAIError
from .services.openai_config import OpenAIConfigurationError as OpenAIConfigError
from .services.openai_config import get_openai_config
from . import views


def sample_invoice_ai_data(**overrides):
    data = {
        "type_document": "facture",
        "numero_facture": "FAC-IA-001",
        "date_facture": "2026-06-12",
        "date_livraison_detectee": "2026-06-11",
        "montant_ht": "100.00",
        "montant_tva": "20.00",
        "montant_ttc": "120.00",
        "devise": "EUR",
        "fournisseur_detecte": "Fournisseur IA",
        "lignes": [
            {
                "reference_produit": "REF-IA-001",
                "ean": "7612345678999",
                "designation": "Produit IA",
                "quantite": "1",
                "unite": "",
                "prix_unitaire_ht": "100.00",
                "montant_ht": "100.00",
                "taux_tva": "20.00",
                "montant_ttc": "120.00",
            }
        ],
        "confiance_ia": 0.82,
        "champs_incertains": [],
        "commentaire_analyse": "Analyse OpenAI de test.",
    }
    data.update(overrides)
    return data


def sample_delivery_ai_data(**overrides):
    numero_bl = overrides.pop("numero_bl", "BL-IA-001")
    date_livraison = overrides.pop("date_livraison", "2026-06-12")
    fournisseur_detecte = overrides.pop("fournisseur_detecte", "Fournisseur IA")
    restaurant_detecte = overrides.pop("restaurant_detecte", "Restaurant IA")
    adresse_livraison = overrides.pop("adresse_livraison", "1 rue de test")
    devise = overrides.pop("devise", None)
    confiance_ia = overrides.pop("confiance_ia", 0.78)
    champs_incertains = overrides.pop("champs_incertains", [])
    commentaire_analyse = overrides.pop("commentaire_analyse", "Analyse OpenAI de test.")
    lines = overrides.pop("lines", overrides.pop("lignes", [
        {
            "product_reference": "REF-BL-IA",
            "product_name": "Produit IA",
            "quantity": "1",
            "unit": "kg",
            "packaging": None,
            "unit_price": None,
            "line_total": None,
            "confidence": 0.76,
            "raw_text": "Produit IA 1 kg",
        }
    ]))
    data = {
        "type_document": "bon_livraison",
        "delivery_note": {
            "number": numero_bl,
            "date": date_livraison,
            "supplier_name": fournisseur_detecte,
            "restaurant_name": restaurant_detecte,
            "delivery_address": adresse_livraison,
            "currency": devise,
            "confidence": confiance_ia,
            "warnings": champs_incertains,
        },
        "lines": lines,
        "numero_bl": numero_bl,
        "date_livraison": date_livraison,
        "fournisseur_detecte": fournisseur_detecte,
        "restaurant_detecte": restaurant_detecte,
        "adresse_livraison": adresse_livraison,
        "devise": devise or "",
        "lignes": lines,
        "confiance_ia": confiance_ia,
        "champs_incertains": champs_incertains,
        "commentaire_analyse": commentaire_analyse,
    }
    data.update(overrides)
    return data


def sample_catalogue_ai_data(**overrides):
    data = {
        "nom_catalogue": "Catalogue IA 2026",
        "fournisseur_detecte": "Fournisseur Catalogue IA",
        "date_debut": "2026-01-01",
        "date_fin": "2026-12-31",
        "devise": "CHF",
        "commentaire_analyse": "Catalogue lisible.",
        "confiance_ia": 0.84,
        "champs_incertains": [],
        "lignes": [
            {
                "reference_produit": "CAT-001",
                "ean": "7612345678901",
                "designation": "Produit catalogue",
                "marque": "Marque Test",
                "unite": "kg",
                "conditionnement": "Carton 6 kg",
                "prix_unitaire_ht": "10,50",
            },
            {
                "reference_produit": "",
                "ean": "",
                "designation": "Produit incomplet",
                "marque": "",
                "unite": "",
                "conditionnement": "",
                "prix_unitaire_ht": "",
            },
        ],
    }
    data.update(overrides)
    return data


class CatalogueModelTests(TestCase):
    def setUp(self):
        self.restaurant = restaurant_fixture(nom="Restaurant Catalogue", devise_defaut=Devise.CHF)
        self.fournisseur = Fournisseur.objects.create(
            restaurant=self.restaurant,
            nom="Fournisseur Catalogue",
            devise_defaut=Devise.CHF,
        )

    def test_catalogue_inherits_supplier_currency_and_upload_path(self):
        upload = upload_fixture("catalogue.pdf", b"%PDF-1.7\npdf-content", content_type="application/pdf")

        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            catalogue = CatalogueFournisseur.objects.create(
                fournisseur=self.fournisseur,
                nom="Tarifs ete",
                fichier_original=upload,
            )

        self.assertEqual(catalogue.devise, Devise.CHF)
        self.assertTrue(
            catalogue.fichier_original.name.startswith(f"catalogues/{self.fournisseur.pk}/")
        )

    def test_valid_catalogues_for_same_supplier_cannot_overlap(self):
        CatalogueFournisseur.objects.create(
            fournisseur=self.fournisseur,
            nom="Catalogue S1",
            date_debut=date(2026, 1, 1),
            date_fin=date(2026, 6, 30),
            statut=CatalogueFournisseur.Statut.VALIDE,
        )

        with self.assertRaisesMessage(
            ValidationError,
            CatalogueFournisseur.OVERLAP_ERROR_MESSAGE,
        ):
            CatalogueFournisseur.objects.create(
                fournisseur=self.fournisseur,
                nom="Catalogue chevauchant",
                date_debut=date(2026, 6, 30),
                date_fin=date(2026, 12, 31),
                statut=CatalogueFournisseur.Statut.VALIDE,
            )

    def test_non_valid_catalogues_can_overlap(self):
        CatalogueFournisseur.objects.create(
            fournisseur=self.fournisseur,
            nom="Catalogue valide",
            date_debut=date(2026, 1, 1),
            date_fin=None,
            statut=CatalogueFournisseur.Statut.VALIDE,
        )

        catalogue = CatalogueFournisseur.objects.create(
            fournisseur=self.fournisseur,
            nom="Catalogue importe",
            date_debut=date(2026, 2, 1),
            date_fin=date(2026, 3, 1),
            statut=CatalogueFournisseur.Statut.IMPORTE,
        )

        self.assertEqual(catalogue.statut, CatalogueFournisseur.Statut.IMPORTE)

    def test_valid_catalogue_can_start_after_previous_period(self):
        CatalogueFournisseur.objects.create(
            fournisseur=self.fournisseur,
            nom="Catalogue janvier",
            date_debut=date(2026, 1, 1),
            date_fin=date(2026, 1, 31),
            statut=CatalogueFournisseur.Statut.VALIDE,
        )

        catalogue = CatalogueFournisseur.objects.create(
            fournisseur=self.fournisseur,
            nom="Catalogue fevrier",
            date_debut=date(2026, 2, 1),
            date_fin=None,
            statut=CatalogueFournisseur.Statut.VALIDE,
        )

        self.assertEqual(catalogue.statut, CatalogueFournisseur.Statut.VALIDE)

    def test_catalogue_line_status_is_marked_to_verify_when_data_is_not_usable(self):
        catalogue = CatalogueFournisseur.objects.create(
            fournisseur=self.fournisseur,
            nom="Catalogue lignes",
        )

        missing_identifier = LigneCatalogue.objects.create(
            catalogue=catalogue,
            designation="Produit sans identifiant",
            prix_unitaire_ht="10.00",
        )
        missing_price = LigneCatalogue.objects.create(
            catalogue=catalogue,
            reference_produit="REF-SANS-PRIX",
            designation="Produit sans prix",
        )
        inconsistent_price = LigneCatalogue.objects.create(
            catalogue=catalogue,
            reference_produit="REF-INCOHERENT",
            prix_unitaire_ht="-12.00",
        )
        usable_line = LigneCatalogue.objects.create(
            catalogue=catalogue,
            reference_produit="REF-OK",
            designation="Produit OK",
            prix_unitaire_ht="10.00",
        )
        ignored_line = LigneCatalogue.objects.create(
            catalogue=catalogue,
            designation="Ligne ignoree",
            statut_ligne=LigneCatalogue.StatutLigne.IGNOREE,
        )

        self.assertEqual(missing_identifier.statut_ligne, LigneCatalogue.StatutLigne.A_VERIFIER)
        self.assertEqual(missing_price.statut_ligne, LigneCatalogue.StatutLigne.A_VERIFIER)
        self.assertEqual(inconsistent_price.statut_ligne, LigneCatalogue.StatutLigne.A_VERIFIER)
        self.assertEqual(usable_line.statut_ligne, LigneCatalogue.StatutLigne.OK)
        self.assertEqual(ignored_line.statut_ligne, LigneCatalogue.StatutLigne.IGNOREE)


class CatalogueControlServiceTests(TestCase):
    def setUp(self):
        self.restaurant = restaurant_fixture(nom="Restaurant Controle Catalogue")
        self.fournisseur = Fournisseur.objects.create(
            restaurant=self.restaurant,
            nom="Fournisseur Controle Catalogue",
        )

    def test_control_matches_by_reference_then_ean_and_reports_line_statuses(self):
        catalogue = CatalogueFournisseur.objects.create(
            fournisseur=self.fournisseur,
            nom="Catalogue Controle",
            date_debut=date(2026, 1, 1),
            date_fin=date(2026, 12, 31),
            devise=Devise.EUR,
            statut=CatalogueFournisseur.Statut.VALIDE,
        )
        LigneCatalogue.objects.create(
            catalogue=catalogue,
            reference_produit="REF-OK",
            designation="Produit conforme",
            unite="kg",
            prix_unitaire_ht="10.00",
        )
        LigneCatalogue.objects.create(
            catalogue=catalogue,
            ean="7612345678001",
            designation="Produit EAN",
            unite="piece",
            prix_unitaire_ht="4.50",
        )
        LigneCatalogue.objects.create(
            catalogue=catalogue,
            reference_produit="REF-GAP",
            unite="kg",
            designation="Produit trop cher",
            prix_unitaire_ht="3.00",
        )
        LigneCatalogue.objects.create(
            catalogue=catalogue,
            reference_produit="REF-NOPRICE",
            designation="Produit sans prix",
        )
        LigneCatalogue.objects.create(
            catalogue=catalogue,
            reference_produit="REF-UNIT",
            designation="Produit unité",
            unite="carton",
            prix_unitaire_ht="2.00",
        )
        LigneCatalogue.objects.create(
            catalogue=catalogue,
            reference_produit="IGNORED",
            designation="Produit ignoré",
            prix_unitaire_ht="1.00",
            statut_ligne=LigneCatalogue.StatutLigne.IGNOREE,
        )
        facture = invoice_fixture(
            fournisseur=self.fournisseur,
            fichier="factures/1/controle.pdf",
            date_facture=date(2026, 6, 16),
            date_livraison_detectee=date(2026, 6, 15),
            devise=Devise.EUR,
        )
        LigneFacture.objects.create(
            facture=facture,
            reference_produit="REF-OK",
            designation="Produit conforme",
            unite="kg",
            prix_unitaire_ht="10.01",
            ordre=0,
        )
        LigneFacture.objects.create(
            facture=facture,
            ean="7612345678001",
            designation="Produit EAN facture",
            unite="piece",
            prix_unitaire_ht="4.50",
            ordre=1,
        )
        LigneFacture.objects.create(
            facture=facture,
            reference_produit="REF-GAP",
            unite="kg",
            designation="Produit trop cher",
            prix_unitaire_ht="3.05",
            ordre=2,
        )
        LigneFacture.objects.create(
            facture=facture,
            reference_produit="REF-NOPRICE",
            designation="Produit sans prix",
            prix_unitaire_ht="1.00",
            ordre=3,
        )
        LigneFacture.objects.create(
            facture=facture,
            reference_produit="REF-UNIT",
            designation="Produit unité",
            unite="kg",
            prix_unitaire_ht="2.00",
            ordre=4,
        )
        LigneFacture.objects.create(
            facture=facture,
            reference_produit="IGNORED",
            designation="Produit ignoré facture",
            prix_unitaire_ht="1.00",
            ordre=5,
        )

        result = control_invoice_against_catalogue(facture)

        self.assertTrue(result["control_available"])
        self.assertEqual(result["catalogue"], catalogue)
        self.assertEqual(result["date_controle"], date(2026, 6, 15))
        self.assertEqual(result["counts"]["conforme"], 2)
        self.assertEqual(result["counts"]["ecart_prix"], 1)
        self.assertEqual(result["counts"]["prix_manquant"], 1)
        self.assertEqual(result["counts"]["produit_non_trouve"], 1)
        self.assertEqual(result["counts"]["unite_a_verifier"], 1)

        details_by_reference = {
            detail["reference_produit"]: detail
            for detail in result["details"]
            if detail["reference_produit"]
        }
        details_by_ean = {detail["ean"]: detail for detail in result["details"] if detail["ean"]}
        self.assertEqual(details_by_reference["REF-OK"]["status"], "conforme")
        self.assertEqual(details_by_ean["7612345678001"]["status"], "conforme")
        self.assertEqual(details_by_reference["REF-GAP"]["status"], "ecart_prix")
        self.assertEqual(str(details_by_reference["REF-GAP"]["ecart"]), "0.05")
        self.assertEqual(details_by_reference["REF-NOPRICE"]["status"], "prix_manquant")
        self.assertEqual(details_by_reference["REF-UNIT"]["status"], "unite_a_verifier")
        self.assertEqual(details_by_reference["IGNORED"]["status"], "produit_non_trouve")

    def test_control_requires_a_control_date(self):
        facture = invoice_fixture(
            fournisseur=self.fournisseur,
            fichier="factures/1/sans-date.pdf",
        )

        result = control_invoice_against_catalogue(facture)

        self.assertEqual(result["status"], "date_absente")
        self.assertEqual(
            result["message"],
            "Contrôle catalogue impossible : aucune date de contrôle disponible.",
        )
        self.assertFalse(result["control_available"])

    def test_control_reports_missing_catalogue_for_date(self):
        facture = invoice_fixture(
            fournisseur=self.fournisseur,
            fichier="factures/1/sans-catalogue.pdf",
            date_facture=date(2026, 6, 15),
        )

        result = control_invoice_against_catalogue(facture)

        self.assertEqual(result["status"], "catalogue_absent")
        self.assertEqual(result["message"], "Aucun catalogue valide trouvé.")

    def test_control_reports_currency_mismatch_without_comparing_lines(self):
        CatalogueFournisseur.objects.create(
            fournisseur=self.fournisseur,
            nom="Catalogue CHF",
            date_debut=date(2026, 1, 1),
            devise=Devise.CHF,
            statut=CatalogueFournisseur.Statut.VALIDE,
        )
        facture = invoice_fixture(
            fournisseur=self.fournisseur,
            fichier="factures/1/devise.pdf",
            date_facture=date(2026, 6, 15),
            devise=Devise.EUR,
        )
        LigneFacture.objects.create(
            facture=facture,
            reference_produit="REF",
            prix_unitaire_ht="1.00",
        )

        result = control_invoice_against_catalogue(facture)

        self.assertEqual(result["status"], "devise_differente")
        self.assertEqual(result["message"], "Devise différente, contrôle impossible.")
        self.assertEqual(result["details"], [])

    def test_control_reports_multiple_applicable_catalogues(self):
        CatalogueFournisseur.objects.create(
            fournisseur=self.fournisseur,
            nom="Catalogue 1",
            date_debut=date(2026, 1, 1),
            date_fin=date(2026, 12, 31),
            statut=CatalogueFournisseur.Statut.VALIDE,
        )
        second_catalogue = CatalogueFournisseur.objects.create(
            fournisseur=self.fournisseur,
            nom="Catalogue 2",
            date_debut=date(2026, 1, 1),
            date_fin=date(2026, 12, 31),
            statut=CatalogueFournisseur.Statut.IMPORTE,
        )
        CatalogueFournisseur.objects.filter(pk=second_catalogue.pk).update(
            statut=CatalogueFournisseur.Statut.VALIDE
        )
        facture = invoice_fixture(
            fournisseur=self.fournisseur,
            fichier="factures/1/multiples.pdf",
            date_facture=date(2026, 6, 15),
        )

        result = control_invoice_against_catalogue(facture)

        self.assertEqual(result["status"], "catalogues_multiples")
        self.assertEqual(
            result["message"],
            "Plusieurs catalogues correspondent à cette date. Vérifiez les catalogues du fournisseur.",
        )


class DeliveryNoteImportPipelineTests(TestCase):
    def setUp(self):
        self.restaurant = restaurant_fixture(nom="Restaurant BL Pipeline")
        self.fournisseur = Fournisseur.objects.create(
            restaurant=self.restaurant,
            nom="Fournisseur BL Pipeline",
        )

    def create_delivery_note(self, filename="bon.pdf"):
        return BonLivraison.objects.create(
            fournisseur=self.fournisseur,
            fichier=upload_fixture(filename, b"document-content", content_type="application/pdf"),
            statut=BonLivraison.Statut.UPLOADE,
        )

    def test_delivery_note_ai_syncs_multiple_lines_with_normalized_values(self):
        ai_data = sample_delivery_ai_data(
            confiance_ia=0.91,
            lines=[
                {
                    "product_reference": "TOM-001",
                    "product_name": "Tomates rondes",
                    "quantity": "2,5",
                    "unit": "Kilos",
                    "packaging": "Caisse",
                    "unit_price": None,
                    "line_total": None,
                    "confidence": 0.89,
                    "raw_text": "TOM-001 Tomates rondes 2,5 Kilos",
                },
                {
                    "product_reference": None,
                    "product_name": "Basilic",
                    "quantity": 3,
                    "unit": "barquettes",
                    "packaging": None,
                    "unit_price": None,
                    "line_total": None,
                    "confidence": 0.82,
                    "raw_text": "Basilic 3 barquettes",
                },
            ],
        )

        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            bon = self.create_delivery_note()
            with patch("core.views.analyze_delivery_note", return_value=ai_data):
                views.apply_delivery_note_ai_analysis(bon)

        bon.refresh_from_db()
        self.assertEqual(bon.statut, BonLivraison.Statut.IMPORTE)
        self.assertEqual(bon.lignes.count(), 2)
        first_line = bon.lignes.order_by("ordre").first()
        self.assertEqual(first_line.reference_produit, "TOM-001")
        self.assertEqual(str(first_line.quantite_livree), "2.500")
        self.assertEqual(first_line.quantite_originale, "2,5")
        self.assertEqual(first_line.unite, "kg")
        self.assertIsNone(first_line.prix_unitaire)
        self.assertEqual(first_line.statut_ligne, LigneBonLivraison.StatutLigne.OK)

    def test_delivery_note_without_number_is_kept_to_verify(self):
        ai_data = sample_delivery_ai_data(numero_bl=None, confiance_ia=0.86)

        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            bon = self.create_delivery_note()
            with patch("core.views.analyze_delivery_note", return_value=ai_data):
                views.apply_delivery_note_ai_analysis(bon)

        bon.refresh_from_db()
        self.assertEqual(bon.numero_bon_livraison, "")
        self.assertEqual(bon.statut, BonLivraison.Statut.A_VERIFIER)
        self.assertEqual(bon.lignes.count(), 1)

    def test_delivery_note_unknown_unit_keeps_raw_value_and_marks_line_to_verify(self):
        ai_data = sample_delivery_ai_data(
            confiance_ia=0.93,
            lines=[
                {
                    "product_reference": "SP-001",
                    "product_name": "Sachets epices",
                    "quantity": "4",
                    "unit": "sachet pro",
                    "packaging": None,
                    "unit_price": None,
                    "line_total": None,
                    "confidence": 0.88,
                    "raw_text": "SP-001 Sachets epices 4 sachet pro",
                }
            ],
        )

        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            bon = self.create_delivery_note()
            with patch("core.views.analyze_delivery_note", return_value=ai_data):
                views.apply_delivery_note_ai_analysis(bon)

        bon.refresh_from_db()
        line = bon.lignes.get()
        self.assertEqual(line.unite, "sachet pro")
        self.assertEqual(line.statut_ligne, LigneBonLivraison.StatutLigne.A_VERIFIER)
        self.assertEqual(bon.statut, BonLivraison.Statut.A_VERIFIER)

    def test_delivery_note_reanalysis_preserves_manual_lines(self):
        ai_data = sample_delivery_ai_data(
            lines=[
                {
                    "product_reference": "AI-001",
                    "product_name": "Produit relu",
                    "quantity": "1",
                    "unit": "kg",
                    "packaging": None,
                    "unit_price": None,
                    "line_total": None,
                    "confidence": 0.8,
                    "raw_text": "AI-001 Produit relu 1 kg",
                }
            ],
        )

        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            bon = self.create_delivery_note()
            LigneBonLivraison.objects.create(
                bon_livraison=bon,
                designation="Produit corrige",
                quantite_livree="2",
                unite="kg",
                corrigee_manuellement=True,
            )
            with patch("core.views.analyze_delivery_note", return_value=ai_data):
                views.apply_delivery_note_ai_analysis(bon)

        bon.refresh_from_db()
        self.assertEqual(bon.lignes.count(), 1)
        self.assertTrue(bon.lignes.filter(designation="Produit corrige", corrigee_manuellement=True).exists())
        self.assertFalse(bon.lignes.filter(designation="Produit relu").exists())
        self.assertTrue(AnalysisVersion.objects.filter(document_type="bon_livraison",document_id=bon.pk,applied=False).exists())


class AuthAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("init_roles")
        user_model = get_user_model()

        cls.reception_user = user_model.objects.create_user(
            username="reception",
            password="password",
        )
        cls.reception_user.groups.add(Group.objects.get(name=ROLE_RECEPTION))

        cls.gestionnaire_user = user_model.objects.create_user(
            username="gestionnaire",
            password="password",
        )
        cls.gestionnaire_user.groups.add(Group.objects.get(name=ROLE_GESTIONNAIRE))

        cls.admin_user = user_model.objects.create_user(
            username="role_admin",
            password="password",
        )
        cls.admin_user.groups.add(Group.objects.get(name=ROLE_ADMIN))

    def test_home_requires_authentication(self):
        response = self.client.get(reverse("home"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_reception_can_only_access_deposit_page(self):
        self.client.force_login(self.reception_user)
        response = self.client.get(reverse("document_deposit"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Prendre une photo")
        self.assertContains(response, "Déposer un document")
        self.assertNotContains(response, reverse("dashboard"))
        self.assertNotContains(response, reverse("restaurant_list"))
        self.assertNotContains(response, reverse("fournisseur_list"))
        self.assertNotContains(response, reverse("user_list"))
        for name in ["bon_livraison_add", "price_review", "dashboard", "restaurant_list", "restaurant_create", "fournisseur_list", "fournisseur_create", "facture_add", "user_list", "ai_diagnostic", "admin:index"]:
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 403)

    def test_reception_cannot_access_invoice_pages_with_direct_urls(self):
        restaurant = restaurant_fixture(nom="Restaurant Acces Direct")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Fournisseur Acces Direct")
        facture = invoice_fixture(
            fournisseur=fournisseur,
            fichier="factures/1/direct.pdf",
            statut=Facture.Statut.A_VALIDER,
        )
        catalogue = CatalogueFournisseur.objects.create(
            fournisseur=fournisseur,
            nom="Catalogue Direct",
            fichier_original="catalogues/1/direct.pdf",
            statut=CatalogueFournisseur.Statut.A_VALIDER,
        )
        ligne = LigneCatalogue.objects.create(
            catalogue=catalogue,
            reference_produit="CAT-DIRECT",
            designation="Produit direct",
            prix_unitaire_ht="10.00",
        )

        self.client.force_login(self.reception_user)
        urls = [
            reverse("facture_detail", args=[facture.pk]),
            reverse("facture_file", args=[facture.pk]),
            reverse("facture_lignes_update", args=[facture.pk]),
            reverse("fournisseur_facture_add", args=[fournisseur.pk]),
            reverse("fournisseur_catalogue_list", args=[fournisseur.pk]),
            reverse("fournisseur_catalogue_import", args=[fournisseur.pk]),
            reverse("catalogue_detail", args=[catalogue.pk]),
            reverse("catalogue_update", args=[catalogue.pk]),
            reverse("catalogue_file", args=[catalogue.pk]),
            reverse("catalogue_validate", args=[catalogue.pk]),
            reverse("catalogue_ai_reanalyze", args=[catalogue.pk]),
            reverse("ligne_catalogue_update", args=[catalogue.pk, ligne.pk]),
        ]
        for url in urls:
            with self.subTest(url=url):
                if url in {
                    reverse("catalogue_validate", args=[catalogue.pk]),
                    reverse("catalogue_ai_reanalyze", args=[catalogue.pk]),
                }:
                    response = self.client.post(url)
                else:
                    response = self.client.get(url)
                self.assertEqual(response.status_code, 403)

    def test_gestionnaire_can_access_dashboard_and_business_pages(self):
        self.client.force_login(self.gestionnaire_user)
        allowed_urls = [
            reverse("dashboard"),
            reverse("restaurant_list"),
            reverse("restaurant_create"),
            reverse("fournisseur_list"),
            reverse("facture_add"),
            reverse("bon_livraison_add"),
        ]
        for url in allowed_urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)

        dashboard_response = self.client.get(reverse("dashboard"))
        self.assertContains(dashboard_response, reverse("dashboard"))
        self.assertContains(dashboard_response, reverse("restaurant_list"))
        self.assertContains(dashboard_response, reverse("fournisseur_list"))
        self.assertNotContains(dashboard_response, reverse("user_list"))

        response = self.client.get(reverse("admin:index"))
        self.assertEqual(response.status_code, 403)

    def test_admin_role_can_access_django_admin(self):
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse("admin:index"))
        self.assertEqual(response.status_code, 200)

    def test_admin_navigation_includes_user_management(self):
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("dashboard"))
        self.assertContains(response, reverse("restaurant_list"))
        self.assertContains(response, reverse("fournisseur_list"))
        self.assertContains(response, reverse("user_list"))
        self.assertContains(response, reverse("ai_diagnostic"))

    def test_delete_buttons_and_routes_are_admin_only(self):
        restaurant = restaurant_fixture(nom="Restaurant Suppression Droits")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Fournisseur Suppression Droits")
        facture = invoice_fixture(
            fournisseur=fournisseur,
            fichier="factures/1/droits.pdf",
        )
        bon = BonLivraison.objects.create(
            fournisseur=fournisseur,
            fichier="bons_livraison/1/droits.pdf",
        )
        catalogue = CatalogueFournisseur.objects.create(
            fournisseur=fournisseur,
            nom="Catalogue droits",
            fichier_original="catalogues/1/droits.pdf",
        )
        delete_urls = [
            reverse("facture_delete", args=[facture.pk]),
            reverse("bon_livraison_delete", args=[bon.pk]),
            reverse("catalogue_delete", args=[catalogue.pk]),
        ]

        self.client.force_login(self.gestionnaire_user)
        response = self.client.get(reverse("facture_detail", args=[facture.pk]))
        self.assertNotContains(response, "Archiver la facture")
        self.assertNotContains(response, reverse("facture_delete", args=[facture.pk]))
        response = self.client.get(reverse("bon_livraison_detail", args=[bon.pk]))
        self.assertNotContains(response, "Archiver le bon de livraison")
        self.assertNotContains(response, reverse("bon_livraison_delete", args=[bon.pk]))
        response = self.client.get(reverse("catalogue_detail", args=[catalogue.pk]))
        self.assertNotContains(response, "Archiver le catalogue")
        self.assertNotContains(response, reverse("catalogue_delete", args=[catalogue.pk]))
        for url in delete_urls:
            with self.subTest(role="gestionnaire", url=url):
                self.assertEqual(self.client.get(url).status_code, 403)
                self.assertEqual(self.client.post(url).status_code, 403)

        self.client.force_login(self.reception_user)
        for url in delete_urls:
            with self.subTest(role="reception", url=url):
                self.assertEqual(self.client.get(url).status_code, 403)
                self.assertEqual(self.client.post(url).status_code, 403)

        self.client.logout()
        response = self.client.get(delete_urls[0])
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

        self.client.force_login(self.admin_user)
        response = self.client.get(reverse("facture_detail", args=[facture.pk]))
        self.assertContains(response, "Archiver la facture")
        response = self.client.get(reverse("bon_livraison_detail", args=[bon.pk]))
        self.assertContains(response, "Archiver le bon de livraison")
        response = self.client.get(reverse("catalogue_detail", args=[catalogue.pk]))
        self.assertContains(response, "Archiver le catalogue")

    def test_admin_can_delete_facture_with_lines_and_file(self):
        restaurant = restaurant_fixture(nom="Restaurant Suppression Facture")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Fournisseur Suppression Facture")

        self.client.force_login(self.admin_user)
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            facture = invoice_fixture(
                fournisseur=fournisseur,
                fichier=upload_fixture("facture.pdf", b"%PDF-1.7\npdf-content"),
                numero_facture="FAC-DEL-001",
                date_facture=date(2026, 6, 23),
                montant_ttc="120.00",
            )
            ligne = LigneFacture.objects.create(
                facture=facture,
                designation="Produit supprimé",
                montant_ttc="120.00",
            )
            file_path = facture.fichier.path
            url = reverse("facture_delete", args=[facture.pk])

            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "Ce document sera archivé.")
            self.assertContains(response, "Fournisseur Suppression Facture")
            self.assertContains(response, "FAC-DEL-001")
            self.assertContains(response, "23/06/2026")
            self.assertContains(response, "120.00 EUR")
            self.assertTrue(Facture.objects.filter(pk=facture.pk).exists())
            self.assertTrue(os.path.exists(file_path))

            with self.assertLogs("core.views", level="INFO") as logs:
                response = self.client.post(url, follow=True)

            self.assertRedirects(response, reverse("fournisseur_detail", args=[fournisseur.pk]))
            self.assertContains(response, "Facture archivée.")
            self.assertFalse(Facture.objects.filter(pk=facture.pk).exists())
            self.assertFalse(LigneFacture.objects.filter(pk=ligne.pk).exists())
            self.assertTrue(os.path.exists(file_path))
            self.assertIn("Suppression facture", "\n".join(logs.output))
            self.assertIn("role_admin", "\n".join(logs.output))

    def test_admin_delete_continues_when_physical_file_is_missing(self):
        restaurant = restaurant_fixture(nom="Restaurant Suppression Fichier Manquant")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Fournisseur Suppression Fichier Manquant")
        facture = invoice_fixture(
            fournisseur=fournisseur,
            fichier="factures/1/fichier-manquant.pdf",
            numero_facture="FAC-MISSING",
        )

        self.client.force_login(self.admin_user)
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            with self.assertLogs("core.views", level="INFO") as logs:
                response = self.client.post(reverse("facture_delete", args=[facture.pk]), follow=True)

        self.assertRedirects(response, reverse("fournisseur_detail", args=[fournisseur.pk]))
        self.assertContains(response, "Facture archivée.")
        self.assertFalse(Facture.objects.filter(pk=facture.pk).exists())
        self.assertIn("Suppression facture", "\n".join(logs.output))

    def test_admin_can_delete_bon_livraison_with_file(self):
        restaurant = restaurant_fixture(nom="Restaurant Suppression BL")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Fournisseur Suppression BL")

        self.client.force_login(self.admin_user)
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            bon = BonLivraison.objects.create(
                fournisseur=fournisseur,
                fichier=upload_fixture("bon.pdf", b"%PDF-1.7\npdf-content"),
                numero_bon_livraison="BL-DEL-001",
                date_livraison=date(2026, 6, 22),
            )
            file_path = bon.fichier.path
            url = reverse("bon_livraison_delete", args=[bon.pk])

            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "Ce document sera archivé.")
            self.assertContains(response, "Fournisseur Suppression BL")
            self.assertContains(response, "BL-DEL-001")
            self.assertContains(response, "22/06/2026")
            self.assertTrue(BonLivraison.objects.filter(pk=bon.pk).exists())
            self.assertTrue(os.path.exists(file_path))

            with self.assertLogs("core.views", level="INFO") as logs:
                response = self.client.post(url, follow=True)

            self.assertRedirects(response, reverse("fournisseur_detail", args=[fournisseur.pk]))
            self.assertContains(response, "Bon de livraison archivé.")
            self.assertFalse(BonLivraison.objects.filter(pk=bon.pk).exists())
            self.assertTrue(os.path.exists(file_path))
            self.assertIn("Suppression bon_livraison", "\n".join(logs.output))
            self.assertIn("role_admin", "\n".join(logs.output))

    def test_admin_can_delete_catalogue_with_lines_and_file(self):
        restaurant = restaurant_fixture(nom="Restaurant Suppression Catalogue")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Fournisseur Suppression Catalogue")

        self.client.force_login(self.admin_user)
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            catalogue = CatalogueFournisseur.objects.create(
                fournisseur=fournisseur,
                nom="Catalogue à supprimer",
                fichier_original=upload_fixture("catalogue.pdf", b"%PDF-1.7\npdf-content"),
                date_debut=date(2026, 1, 1),
            )
            ligne = LigneCatalogue.objects.create(
                catalogue=catalogue,
                designation="Produit catalogue supprimé",
            )
            file_path = catalogue.fichier_original.path
            url = reverse("catalogue_delete", args=[catalogue.pk])

            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "Ce document sera archivé.")
            self.assertContains(response, "Fournisseur Suppression Catalogue")
            self.assertContains(response, "Catalogue à supprimer")
            self.assertTrue(CatalogueFournisseur.objects.filter(pk=catalogue.pk).exists())
            self.assertTrue(os.path.exists(file_path))

            with self.assertLogs("core.views", level="INFO") as logs:
                response = self.client.post(url, follow=True)

            self.assertRedirects(response, reverse("fournisseur_detail", args=[fournisseur.pk]))
            self.assertContains(response, "Catalogue archivé.")
            self.assertFalse(CatalogueFournisseur.objects.filter(pk=catalogue.pk).exists())
            self.assertFalse(LigneCatalogue.objects.filter(pk=ligne.pk).exists())
            self.assertTrue(os.path.exists(file_path))
            self.assertIn("Suppression catalogue", "\n".join(logs.output))
            self.assertIn("role_admin", "\n".join(logs.output))

    def test_user_management_requires_authentication(self):
        response = self.client.get(reverse("user_list"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_only_admin_can_manage_users(self):
        target_user = self.admin_user
        urls = [
            reverse("user_list"),
            reverse("user_create"),
            reverse("user_update", args=[target_user.pk]),
            reverse("user_password_update", args=[target_user.pk]),
            reverse("ai_diagnostic"),
        ]

        for user in [self.reception_user, self.gestionnaire_user]:
            self.client.force_login(user)
            for url in urls:
                with self.subTest(username=user.username, url=url):
                    response = self.client.get(url)
                    self.assertEqual(response.status_code, 403)

    def test_dashboard_shows_user_management_only_to_admin(self):
        self.client.force_login(self.gestionnaire_user)
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, reverse("user_list"))
        self.assertNotContains(response, reverse("ai_diagnostic"))

        self.client.force_login(self.admin_user)
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("user_list"))
        self.assertContains(response, reverse("ai_diagnostic"))

    def test_ai_diagnostic_requires_admin_and_hides_api_key(self):
        AIDiagnosticEvent.objects.create(
            event_type=AIDiagnosticEvent.EventType.SUCCESS,
            source="analyse_facture",
            document_type="facture",
            document_id=123,
            message="Analyse réussie.",
        )
        AIDiagnosticEvent.objects.create(
            event_type=AIDiagnosticEvent.EventType.ERROR,
            source="upload_facture",
            document_type="facture",
            document_id=124,
            message="Erreur OpenAI lisible.",
        )

        response = self.client.get(reverse("ai_diagnostic"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

        for user in [self.reception_user, self.gestionnaire_user]:
            self.client.force_login(user)
            response = self.client.get(reverse("ai_diagnostic"))
            self.assertEqual(response.status_code, 403)

        self.client.force_login(self.admin_user)
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": ("sk" + "-secret-value"),
                "OPENAI_MODEL": "gpt-4.1-mini",
            },
        ):
            response = self.client.get(reverse("ai_diagnostic"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Diagnostic IA")
        self.assertContains(response, "Clé OpenAI configurée")
        self.assertContains(response, "Source de configuration")
        self.assertContains(response, "Variable d’environnement")
        self.assertContains(response, "Oui")
        self.assertContains(response, "gpt-4.1-mini")
        self.assertNotContains(response, "État du service IA")
        self.assertNotContains(response, "Dernière analyse réussie")
        self.assertNotContains(response, "Dernière erreur IA")
        self.assertNotContains(response, "Erreur OpenAI lisible.")
        self.assertNotContains(response, ("sk" + "-secret-value"))

    def test_ai_diagnostic_shows_development_keys_file_source(self):
        self.client.force_login(self.admin_user)

        with tempfile.NamedTemporaryFile("w", suffix=".json") as keys_file:
            json.dump(
                {
                    "OPENAI_API_KEY": ("sk" + "-file-secret-value"),
                    "OPENAI_MODEL": "gpt-file-test",
                },
                keys_file,
            )
            keys_file.flush()

            with override_settings(DEBUG=True), patch.dict(
                os.environ,
                {"OPENAI_KEYS_FILE": keys_file.name},
                clear=True,
            ):
                response = self.client.get(reverse("ai_diagnostic"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Clé OpenAI configurée")
        self.assertContains(response, "Oui")
        self.assertContains(response, "Fichier local de développement")
        self.assertContains(response, "gpt-file-test")
        self.assertNotContains(response, ("sk" + "-file-secret-value"))

    def test_ai_diagnostic_logs_do_not_expose_keys_file_secret(self):
        self.client.force_login(self.admin_user)
        secret_key = ("sk" + "-secret-that-must-not-appear")

        with tempfile.NamedTemporaryFile("w", suffix=".json") as keys_file:
            json.dump({"OPENAI_API_KEY": secret_key}, keys_file)
            keys_file.flush()

            with override_settings(DEBUG=True), patch.dict(
                os.environ,
                {"OPENAI_KEYS_FILE": keys_file.name},
                clear=True,
            ):
                with self.assertLogs("core.views", level="ERROR") as captured_logs:
                    response = self.client.post(reverse("ai_diagnostic"), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Connexion OpenAI échouée")
        self.assertNotIn(secret_key, "\n".join(captured_logs.output))

    def test_ai_diagnostic_test_openai_success_and_failure_messages(self):
        self.client.force_login(self.admin_user)

        with patch("core.views.test_openai_connection", return_value="OK"):
            response = self.client.post(reverse("ai_diagnostic"), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Connexion OpenAI OK")
        self.assertTrue(
            AIDiagnosticEvent.objects.filter(
                event_type=AIDiagnosticEvent.EventType.SUCCESS,
                source="test_openai",
            ).exists()
        )

        with patch("core.views.test_openai_connection", side_effect=DocumentAIError("erreur reseau")):
            with self.assertLogs("core.views", level="ERROR"):
                response = self.client.post(reverse("ai_diagnostic"), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Connexion OpenAI échouée")
        self.assertTrue(
            AIDiagnosticEvent.objects.filter(
                event_type=AIDiagnosticEvent.EventType.ERROR,
                source="test_openai",
                message="erreur reseau",
            ).exists()
        )
        self.assertContains(response, reverse("dashboard"))

    def test_admin_can_view_user_list(self):
        self.gestionnaire_user.email = "gestionnaire@example.test"
        self.gestionnaire_user.save(update_fields=["email"])

        self.client.force_login(self.admin_user)
        response = self.client.get(reverse("user_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "gestionnaire")
        self.assertContains(response, "gestionnaire@example.test")
        self.assertContains(response, ROLE_GESTIONNAIRE)
        self.assertContains(response, reverse("user_update", args=[self.gestionnaire_user.pk]))
        self.assertContains(response, reverse("user_password_update", args=[self.gestionnaire_user.pk]))

    def test_admin_can_create_user_with_role(self):
        user_model = get_user_model()

        self.client.force_login(self.admin_user)
        response = self.client.post(
            reverse("user_create"),
            {
                "username": "nouveau",
                "email": "nouveau@example.test",
                "password1": "mot-de-passe-solide",
                "password2": "mot-de-passe-solide",
                "role": ROLE_GESTIONNAIRE,
                "is_active": "on",
            },
            follow=True,
        )

        created_user = user_model.objects.get(username="nouveau")
        self.assertRedirects(response, reverse("user_list"))
        self.assertContains(response, "Utilisateur créé avec succès.")
        self.assertEqual(created_user.email, "nouveau@example.test")
        self.assertTrue(created_user.check_password("mot-de-passe-solide"))
        self.assertTrue(created_user.is_active)
        self.assertTrue(created_user.groups.filter(name=ROLE_GESTIONNAIRE).exists())

    def test_user_creation_requires_matching_passwords_and_role(self):
        user_model = get_user_model()

        self.client.force_login(self.admin_user)
        response = self.client.post(
            reverse("user_create"),
            {
                "username": "sansrole",
                "email": "",
                "password1": "secret-1",
                "password2": "secret-2",
                "role": "",
                "is_active": "on",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Les deux mots de passe doivent correspondre.")
        self.assertContains(response, "Choisissez un rôle.")
        self.assertFalse(user_model.objects.filter(username="sansrole").exists())

    def test_admin_can_update_user_and_role(self):
        user_model = get_user_model()
        user = user_model.objects.create_user(
            username="a_modifier",
            email="old@example.test",
            password="ancien-secret",
        )
        user.groups.add(Group.objects.get(name=ROLE_RECEPTION))

        self.client.force_login(self.admin_user)
        response = self.client.post(
            reverse("user_update", args=[user.pk]),
            {
                "username": "modifie",
                "email": "new@example.test",
                "role": ROLE_ADMIN,
            },
            follow=True,
        )

        user.refresh_from_db()
        self.assertRedirects(response, reverse("user_list"))
        self.assertContains(response, "Utilisateur modifié avec succès.")
        self.assertEqual(user.username, "modifie")
        self.assertEqual(user.email, "new@example.test")
        self.assertFalse(user.is_active)
        self.assertTrue(user.check_password("ancien-secret"))
        self.assertTrue(user.groups.filter(name=ROLE_ADMIN).exists())
        self.assertFalse(user.groups.filter(name=ROLE_RECEPTION).exists())

    def test_admin_can_update_user_password(self):
        user_model = get_user_model()
        user = user_model.objects.create_user(
            username="motdepasse",
            password="ancien-secret",
        )
        user.groups.add(Group.objects.get(name=ROLE_RECEPTION))

        self.client.force_login(self.admin_user)
        response = self.client.post(
            reverse("user_password_update", args=[user.pk]),
            {
                "password1": "nouveau-secret",
                "password2": "nouveau-secret",
            },
            follow=True,
        )

        user.refresh_from_db()
        self.assertRedirects(response, reverse("user_list"))
        self.assertContains(response, "Mot de passe mis à jour.")
        self.assertTrue(user.check_password("nouveau-secret"))
        self.assertNotEqual(user.password, "nouveau-secret")

    def test_login_page_status_code(self):
        response = self.client.get(reverse("login"))
        self.assertEqual(response.status_code, 200)

    def test_status_labels_are_simple_and_french(self):
        self.assertEqual(Facture.Statut.IMPORTEE.label, "Importée")
        self.assertEqual(Facture.Statut.ANALYSE_EN_COURS.label, "Analyse en cours")
        self.assertEqual(Facture.Statut.A_VALIDER.label, "À valider")
        self.assertEqual(Facture.Statut.ANALYSE_ECHOUEE.label, "Analyse échouée")
        self.assertEqual(Facture.Statut.VALIDEE.label, "Validée")
        self.assertEqual(Facture.Statut.LITIGE.label, "Litige")
        self.assertEqual(CatalogueFournisseur.Statut.ANALYSE_EN_COURS.label, "Analyse en cours")
        self.assertEqual(CatalogueFournisseur.Statut.ANALYSE_ECHOUEE.label, "Analyse échouée")
        self.assertEqual(BonLivraison.Statut.IMPORTE.label, "Importé")
        self.assertEqual(BonLivraison.Statut.A_VERIFIER.label, "À vérifier")
        self.assertEqual(BonLivraison.Statut.VALIDE.label, "Validé")
        self.assertEqual(BonLivraison.Statut.LITIGE.label, "Litige")

    def test_home_redirects_gestionnaire_to_dashboard(self):
        self.client.force_login(self.gestionnaire_user)
        response = self.client.get(reverse("home"))
        self.assertRedirects(response, reverse("dashboard"))

    def test_dashboard_displays_supplier_document_counts(self):
        restaurant = restaurant_fixture(nom="Restaurant Central")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Primeur Test")
        invoice_fixture(
            fournisseur=fournisseur,
            fichier="factures/1/facture-1.pdf",
            statut=Facture.Statut.A_VALIDER,
        )
        invoice_fixture(
            fournisseur=fournisseur,
            fichier="factures/1/facture-2.pdf",
            statut=Facture.Statut.LITIGE,
        )
        BonLivraison.objects.create(
            fournisseur=fournisseur,
            fichier="bons_livraison/1/bl-1.pdf",
            statut=BonLivraison.Statut.A_VERIFIER,
        )

        self.client.force_login(self.gestionnaire_user)
        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Que souhaitez-vous faire ?")
        self.assertContains(response, reverse("fournisseur_list"))
        self.assertNotContains(response, 'class="project-list"')
        self.assertEqual(response.context["fournisseurs"][0].factures_count, 2)
        self.assertEqual(response.context["fournisseurs"][0].bons_livraison_count, 1)
        self.assertEqual(response.context["fournisseurs"][0].documents_a_valider_count, 2)
        self.assertEqual(response.context["fournisseurs"][0].litiges_count, 1)

    def test_dashboard_is_home_menu_even_with_legacy_supplier_search(self):
        restaurant = restaurant_fixture(nom="Restaurant Recherche")
        Fournisseur.objects.create(restaurant=restaurant, nom="Metro Frais")
        Fournisseur.objects.create(restaurant=restaurant, nom="Boulanger Local")

        self.client.force_login(self.gestionnaire_user)
        response = self.client.get(f"{reverse('dashboard')}?q=metro")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="home-menu-grid"')
        self.assertContains(response, reverse("fournisseur_list"))
        self.assertNotContains(response, "Metro Frais")
        self.assertNotContains(response, "Boulanger Local")

    def test_supplier_detail_is_restricted_to_gestionnaire_and_admin(self):
        restaurant = restaurant_fixture(nom="Restaurant Nord")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Boulanger Test")
        url = reverse("fournisseur_detail", args=[fournisseur.pk])

        self.client.force_login(self.reception_user)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 403)

        self.client.force_login(self.gestionnaire_user)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Boulanger Test")

    def test_supplier_management_requires_authentication(self):
        response = self.client.get(reverse("fournisseur_create"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_reception_cannot_manage_suppliers(self):
        restaurant = restaurant_fixture(nom="Restaurant Acces")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Fournisseur Acces")

        self.client.force_login(self.reception_user)
        urls = [
            reverse("fournisseur_create"),
            reverse("fournisseur_update", args=[fournisseur.pk]),
        ]
        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 403)

    def test_gestionnaire_can_create_supplier(self):
        restaurant = restaurant_fixture(nom="Restaurant Creation", devise_defaut=Devise.CHF)

        self.client.force_login(self.gestionnaire_user)
        response = self.client.post(
            reverse("fournisseur_create"),
            {
                "restaurant": restaurant.pk,
                "nom": "Nouveau Fournisseur",
                "email": "contact@example.test",
                "telephone": "0102030405",
                "actif": "on",
            },
            follow=True,
        )

        fournisseur = Fournisseur.objects.get(nom="Nouveau Fournisseur")
        self.assertRedirects(response, reverse("fournisseur_detail", args=[fournisseur.pk]))
        self.assertContains(response, "Fournisseur créé avec succès.")
        self.assertEqual(fournisseur.restaurant, restaurant)
        self.assertEqual(fournisseur.email, "contact@example.test")
        self.assertEqual(fournisseur.telephone, "0102030405")
        self.assertEqual(fournisseur.devise_defaut, Devise.CHF)
        self.assertTrue(fournisseur.actif)

    def test_admin_can_access_supplier_form(self):
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse("fournisseur_create"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ajouter un fournisseur")

    def test_restaurant_management_requires_authentication(self):
        response = self.client.get(reverse("restaurant_create"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_reception_cannot_manage_restaurants(self):
        restaurant = restaurant_fixture(nom="Restaurant Interdit")

        self.client.force_login(self.reception_user)
        urls = [
            reverse("restaurant_list"),
            reverse("restaurant_create"),
            reverse("restaurant_detail", args=[restaurant.pk]),
            reverse("restaurant_update", args=[restaurant.pk]),
        ]
        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 403)

    def test_restaurant_list_displays_active_restaurants(self):
        active_restaurant = restaurant_fixture(nom="Restaurant Actif", adresse="1 rue A")
        inactive_restaurant = restaurant_fixture(nom="Restaurant Inactif", actif=False)
        Fournisseur.objects.create(restaurant=active_restaurant, nom="Fournisseur actif")
        Fournisseur.objects.create(
            restaurant=active_restaurant,
            nom="Fournisseur inactif",
            actif=False,
        )

        self.client.force_login(self.gestionnaire_user)
        response = self.client.get(reverse("restaurant_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Restaurant Actif")
        self.assertContains(response, "1 rue A")
        self.assertContains(response, reverse("restaurant_detail", args=[active_restaurant.pk]))
        self.assertContains(response, reverse("restaurant_update", args=[active_restaurant.pk]))
        self.assertEqual(response.context["restaurants"][0].fournisseurs_actifs_count, 1)
        self.assertNotContains(response, inactive_restaurant.nom)

    def test_gestionnaire_can_create_restaurant(self):
        self.client.force_login(self.gestionnaire_user)
        response = self.client.post(
            reverse("restaurant_create"),
            {
                "nom": "Restaurant Nouveau",
                "adresse": "10 rue Test",
                "devise_defaut": Devise.CHF,
                "actif": "on",
            },
            follow=True,
        )

        restaurant = Restaurant.objects.get(nom="Restaurant Nouveau")
        self.assertRedirects(response, reverse("restaurant_detail", args=[restaurant.pk]))
        self.assertContains(response, "Restaurant créé avec succès.")
        self.assertEqual(restaurant.adresse, "10 rue Test")
        self.assertEqual(restaurant.devise_defaut, Devise.CHF)
        self.assertTrue(restaurant.actif)

    def test_admin_can_access_restaurant_form(self):
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse("restaurant_create"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ajouter un restaurant")

    def test_restaurant_duplicate_name_is_rejected(self):
        restaurant_fixture(nom="Restaurant Doublon Nom")

        self.client.force_login(self.gestionnaire_user)
        response = self.client.post(
            reverse("restaurant_create"),
            {
                "nom": "Restaurant Doublon Nom",
                "adresse": "",
                "actif": "on",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ce restaurant existe déjà.")
        self.assertEqual(Restaurant.objects.filter(nom="Restaurant Doublon Nom").count(), 1)

    def test_gestionnaire_can_update_and_deactivate_restaurant(self):
        restaurant = restaurant_fixture(nom="Restaurant Avant", adresse="Ancienne adresse")

        self.client.force_login(self.gestionnaire_user)
        response = self.client.post(
            reverse("restaurant_update", args=[restaurant.pk]),
            {
                "nom": "Restaurant Apres",
                "adresse": "Nouvelle adresse",
            },
            follow=True,
        )

        restaurant.refresh_from_db()
        self.assertRedirects(response, reverse("restaurant_detail", args=[restaurant.pk]))
        self.assertContains(response, "Restaurant modifié avec succès.")
        self.assertContains(response, "Inactif")
        self.assertEqual(restaurant.nom, "Restaurant Apres")
        self.assertEqual(restaurant.adresse, "Nouvelle adresse")
        self.assertFalse(restaurant.actif)

        list_response = self.client.get(reverse("restaurant_list"))
        self.assertNotContains(list_response, "Restaurant Apres")

    def test_restaurant_currency_update_preserves_historical_documents(self):
        restaurant = restaurant_fixture(
            nom="Restaurant Devise Cascade",
            devise_defaut=Devise.EUR,
        )
        fournisseur = Fournisseur.objects.create(
            restaurant=restaurant,
            nom="Fournisseur Devise Cascade",
            devise_defaut=Devise.EUR,
        )
        facture = invoice_fixture(
            fournisseur=fournisseur,
            fichier="factures/1/facture-devise.pdf",
            devise=Devise.EUR,
            montant_ttc="120.00",
        )
        catalogue = CatalogueFournisseur.objects.create(
            fournisseur=fournisseur,
            nom="Catalogue Devise Cascade",
            devise=Devise.EUR,
        )
        other_restaurant = restaurant_fixture(nom="Restaurant Autre", devise_defaut=Devise.EUR)
        other_fournisseur = Fournisseur.objects.create(
            restaurant=other_restaurant,
            nom="Fournisseur Autre",
            devise_defaut=Devise.EUR,
        )
        other_facture = invoice_fixture(
            fournisseur=other_fournisseur,
            fichier="factures/1/facture-autre.pdf",
            devise=Devise.EUR,
        )

        self.client.force_login(self.gestionnaire_user)
        response = self.client.post(
            reverse("restaurant_update", args=[restaurant.pk]),
            {
                "nom": restaurant.nom,
                "adresse": "",
                "devise_defaut": Devise.CHF,
                "actif": "on",
            },
            follow=True,
        )

        self.assertRedirects(response, reverse("restaurant_detail", args=[restaurant.pk]))
        self.assertContains(
            response,
            "La devise des documents existants est conservée.",
        )
        restaurant.refresh_from_db()
        fournisseur.refresh_from_db()
        facture.refresh_from_db()
        catalogue.refresh_from_db()
        other_facture.refresh_from_db()
        self.assertEqual(restaurant.devise_defaut, Devise.CHF)
        self.assertEqual(fournisseur.devise_defaut, Devise.EUR)
        self.assertEqual(facture.devise, Devise.EUR)
        self.assertEqual(str(facture.montant_ttc), "120.00")
        self.assertEqual(catalogue.devise, Devise.EUR)
        self.assertEqual(other_facture.devise, Devise.EUR)

    def test_restaurant_detail_displays_active_suppliers_and_prefilled_add_link(self):
        restaurant = restaurant_fixture(nom="Restaurant Detail", adresse="Adresse Detail")
        active_supplier = Fournisseur.objects.create(restaurant=restaurant, nom="Fournisseur Visible")
        Fournisseur.objects.create(restaurant=restaurant, nom="Fournisseur Cache", actif=False)

        self.client.force_login(self.gestionnaire_user)
        response = self.client.get(reverse("restaurant_detail", args=[restaurant.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Restaurant Detail")
        self.assertContains(response, "Adresse Detail")
        self.assertContains(response, "Fournisseur Visible")
        self.assertContains(response, reverse("fournisseur_detail", args=[active_supplier.pk]))
        self.assertContains(response, f"{reverse('fournisseur_create')}?restaurant={restaurant.pk}")
        self.assertNotContains(response, "Fournisseur Cache")

    def test_supplier_create_can_be_prefilled_from_restaurant(self):
        restaurant = restaurant_fixture(nom="Restaurant Prefill", devise_defaut=Devise.CHF)

        self.client.force_login(self.gestionnaire_user)
        response = self.client.get(f"{reverse('fournisseur_create')}?restaurant={restaurant.pk}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"].initial["restaurant"], restaurant)
        self.assertEqual(response.context["form"].fields["devise_defaut"].initial, Devise.CHF)
        self.assertContains(response, 'data-devise="CHF"')

    def test_supplier_duplicate_name_for_same_restaurant_is_rejected(self):
        restaurant = restaurant_fixture(nom="Restaurant Doublon")
        Fournisseur.objects.create(restaurant=restaurant, nom="Fournisseur Double")

        self.client.force_login(self.gestionnaire_user)
        response = self.client.post(
            reverse("fournisseur_create"),
            {
                "restaurant": restaurant.pk,
                "nom": "Fournisseur Double",
                "email": "",
                "telephone": "",
                "actif": "on",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ce fournisseur existe déjà pour ce restaurant.")
        self.assertEqual(Fournisseur.objects.filter(nom="Fournisseur Double").count(), 1)

    def test_gestionnaire_can_update_and_deactivate_supplier(self):
        restaurant = restaurant_fixture(nom="Restaurant Modification")
        fournisseur = Fournisseur.objects.create(
            restaurant=restaurant,
            nom="Ancien Fournisseur",
            email="old@example.test",
            telephone="0000",
        )

        self.client.force_login(self.gestionnaire_user)
        response = self.client.post(
            reverse("fournisseur_update", args=[fournisseur.pk]),
            {
                "restaurant": restaurant.pk,
                "nom": "Fournisseur Modifie",
                "email": "new@example.test",
                "telephone": "1111",
            },
            follow=True,
        )

        fournisseur.refresh_from_db()
        self.assertRedirects(response, reverse("fournisseur_detail", args=[fournisseur.pk]))
        self.assertContains(response, "Fournisseur modifié avec succès.")
        self.assertContains(response, "Inactif")
        self.assertEqual(fournisseur.nom, "Fournisseur Modifie")
        self.assertEqual(fournisseur.email, "new@example.test")
        self.assertEqual(fournisseur.telephone, "1111")
        self.assertFalse(fournisseur.actif)

        dashboard_response = self.client.get(reverse("dashboard"))
        self.assertNotContains(dashboard_response, "Fournisseur Modifie")

    def test_supplier_detail_displays_short_document_lists(self):
        restaurant = restaurant_fixture(nom="Restaurant Sud")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Epicerie Test")
        facture = invoice_fixture(
            fournisseur=fournisseur,
            fichier="factures/1/facture-3.pdf",
            numero_facture="FAC-003",
            date_facture=date(2026, 6, 1),
            montant_ttc="42.50",
            statut=Facture.Statut.A_VALIDER,
            note="Cette note de facture ne doit pas etre affichee dans la liste.",
        )
        bon = BonLivraison.objects.create(
            fournisseur=fournisseur,
            fichier="bons_livraison/1/bl-2.pdf",
            numero_bon_livraison="BL-002",
            date_livraison=date(2026, 6, 2),
            statut=BonLivraison.Statut.LITIGE,
            note="Palette incomplete",
        )

        self.client.force_login(self.gestionnaire_user)
        response = self.client.get(reverse("fournisseur_detail", args=[fournisseur.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Factures")
        self.assertContains(response, "Bons de livraison")
        self.assertContains(response, "FAC-003")
        self.assertContains(response, "01/06/2026")
        self.assertContains(response, "42,50 EUR")
        self.assertContains(response, reverse("facture_detail", args=[facture.pk]))
        self.assertContains(response, reverse("fournisseur_facture_add", args=[fournisseur.pk]))
        self.assertNotContains(response, "Cette note de facture")
        self.assertContains(response, "BL-002")
        self.assertContains(response, "02/06/2026")
        self.assertContains(response, "Palette incomplete")
        self.assertContains(response, reverse("bon_livraison_detail", args=[bon.pk]))
        self.assertContains(response, reverse("fournisseur_bon_livraison_add", args=[fournisseur.pk]))

    def test_supplier_detail_blocks_pending_document_links(self):
        restaurant = restaurant_fixture(nom="Restaurant Async Liste")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Fournisseur Async Liste")
        facture = invoice_fixture(
            fournisseur=fournisseur,
            fichier="factures/1/facture-pending.pdf",
            statut=Facture.Statut.ANALYSE_EN_COURS,
        )
        bon = BonLivraison.objects.create(
            fournisseur=fournisseur,
            fichier="bons_livraison/1/bl-pending.pdf",
            statut=BonLivraison.Statut.ANALYSE_EN_COURS,
        )
        catalogue = CatalogueFournisseur.objects.create(
            fournisseur=fournisseur,
            fichier_original="catalogues/1/catalogue-pending.pdf",
            statut=CatalogueFournisseur.Statut.ANALYSE_EN_COURS,
        )

        self.client.force_login(self.gestionnaire_user)
        response = self.client.get(reverse("fournisseur_detail", args=[fournisseur.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Analyse IA en cours...", count=3)
        self.assertContains(response, "disabled>Analyse en cours</button>", count=3)
        self.assertNotContains(response, reverse("facture_detail", args=[facture.pk]))
        self.assertNotContains(response, reverse("bon_livraison_detail", args=[bon.pk]))
        self.assertNotContains(response, reverse("catalogue_detail", args=[catalogue.pk]))

    def test_pending_documents_remain_consultable(self):
        restaurant = restaurant_fixture(nom="Restaurant Async Detail")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Fournisseur Async Detail")
        facture = invoice_fixture(
            fournisseur=fournisseur,
            fichier="factures/1/facture-pending.pdf",
            statut=Facture.Statut.ANALYSE_EN_COURS,
        )
        bon = BonLivraison.objects.create(
            fournisseur=fournisseur,
            fichier="bons_livraison/1/bl-pending.pdf",
            statut=BonLivraison.Statut.ANALYSE_EN_COURS,
        )
        catalogue = CatalogueFournisseur.objects.create(
            fournisseur=fournisseur,
            fichier_original="catalogues/1/catalogue-pending.pdf",
            statut=CatalogueFournisseur.Statut.ANALYSE_EN_COURS,
        )

        self.client.force_login(self.gestionnaire_user)
        for url in [
            reverse("facture_detail", args=[facture.pk]),
            reverse("bon_livraison_detail", args=[bon.pk]),
            reverse("catalogue_detail", args=[catalogue.pk]),
        ]:
            with self.subTest(url=url):
                response = self.client.get(url, follow=True)
                self.assertEqual(response.status_code, 200)
                self.assertContains(
                    response,
                    "Le fichier est conservé.",
                )

    def test_supplier_detail_filters_factures(self):
        restaurant = restaurant_fixture(nom="Restaurant Filtre Facture")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Fournisseur Filtre Facture")
        matching_facture = invoice_fixture(
            fournisseur=fournisseur,
            fichier="factures/1/facture-ok.pdf",
            numero_facture="FAC-OK-100",
            date_facture=date(2026, 6, 5),
            statut=Facture.Statut.LITIGE,
            note="Ecart de prix.",
        )
        invoice_fixture(
            fournisseur=fournisseur,
            fichier="factures/1/facture-numero.pdf",
            numero_facture="FAC-AUTRE-200",
            date_facture=date(2026, 6, 5),
            statut=Facture.Statut.LITIGE,
            note="Autre litige.",
        )
        invoice_fixture(
            fournisseur=fournisseur,
            fichier="factures/1/facture-statut.pdf",
            numero_facture="FAC-OK-300",
            date_facture=date(2026, 6, 5),
            statut=Facture.Statut.A_VALIDER,
        )
        invoice_fixture(
            fournisseur=fournisseur,
            fichier="factures/1/facture-date.pdf",
            numero_facture="FAC-OK-400",
            date_facture=date(2026, 6, 6),
            statut=Facture.Statut.LITIGE,
            note="Autre date.",
        )

        self.client.force_login(self.gestionnaire_user)
        response = self.client.get(
            (
                f"{reverse('fournisseur_detail', args=[fournisseur.pk])}"
                f"?facture_q=FAC-OK&facture_statut={Facture.Statut.LITIGE}&facture_date=2026-06-05"
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["factures_count"], 1)
        self.assertContains(response, "FAC-OK-100")
        self.assertContains(response, reverse("facture_detail", args=[matching_facture.pk]))
        self.assertContains(response, 'value="FAC-OK"')
        self.assertNotContains(response, "FAC-AUTRE-200")
        self.assertNotContains(response, "FAC-OK-300")
        self.assertNotContains(response, "FAC-OK-400")

    def test_supplier_detail_filters_bons_livraison(self):
        restaurant = restaurant_fixture(nom="Restaurant Filtre BL")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Fournisseur Filtre BL")
        matching_bon = BonLivraison.objects.create(
            fournisseur=fournisseur,
            fichier="bons_livraison/1/bon-ok.pdf",
            numero_bon_livraison="BL-OK-100",
            date_livraison=date(2026, 6, 7),
            statut=BonLivraison.Statut.A_VERIFIER,
        )
        BonLivraison.objects.create(
            fournisseur=fournisseur,
            fichier="bons_livraison/1/bon-numero.pdf",
            numero_bon_livraison="BL-AUTRE-200",
            date_livraison=date(2026, 6, 7),
            statut=BonLivraison.Statut.A_VERIFIER,
        )
        BonLivraison.objects.create(
            fournisseur=fournisseur,
            fichier="bons_livraison/1/bon-statut.pdf",
            numero_bon_livraison="BL-OK-300",
            date_livraison=date(2026, 6, 7),
            statut=BonLivraison.Statut.VALIDE,
            validation_exception="Fixture : données contrôlées pour ce scénario.",
        )
        BonLivraison.objects.create(
            fournisseur=fournisseur,
            fichier="bons_livraison/1/bon-date.pdf",
            numero_bon_livraison="BL-OK-400",
            date_livraison=date(2026, 6, 8),
            statut=BonLivraison.Statut.A_VERIFIER,
        )

        self.client.force_login(self.gestionnaire_user)
        response = self.client.get(
            (
                f"{reverse('fournisseur_detail', args=[fournisseur.pk])}"
                f"?bon_q=BL-OK&bon_statut={BonLivraison.Statut.A_VERIFIER}&bon_date=2026-06-07"
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["bons_livraison_count"], 1)
        self.assertContains(response, "BL-OK-100")
        self.assertContains(response, reverse("bon_livraison_detail", args=[matching_bon.pk]))
        self.assertContains(response, 'value="BL-OK"')
        self.assertNotContains(response, "BL-AUTRE-200")
        self.assertNotContains(response, "BL-OK-300")
        self.assertNotContains(response, "BL-OK-400")

    def test_facture_detail_can_update_main_fields(self):
        restaurant = restaurant_fixture(nom="Restaurant Est")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Poissonnerie Test")
        facture = invoice_fixture(
            fournisseur=fournisseur,
            fichier="factures/1/facture-4.pdf",
            statut=Facture.Statut.IMPORTEE,
        )

        self.client.force_login(self.gestionnaire_user)
        detail_response = self.client.get(reverse("facture_detail", args=[facture.pk]))
        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(detail_response, 'name="statut"')
        self.assertContains(detail_response, "Importée")
        self.assertContains(detail_response, "À valider")
        self.assertContains(detail_response, "Validée")
        self.assertContains(detail_response, "Litige")
        self.assertContains(detail_response, "Statut : Importée")
        self.assertContains(detail_response, "text-bg-secondary")
        self.assertContains(detail_response, "Marquer à valider")
        self.assertContains(detail_response, "Valider")
        self.assertContains(detail_response, "Mettre en litige")
        self.assertContains(detail_response, 'data-ai-loading-label="Analyse..."')
        self.assertContains(detail_response, "Relance IA en cours...")

        LigneFacture.objects.create(
            facture=facture,
            reference_produit="MER-001",
            designation="Filet de merlu",
            quantite="2.000",
            unite="kg",
            prix_unitaire_ht="50.00",
            montant_ht="100.00",
            montant_ttc="110.00",
            ordre=0,
        )
        facture.montant_ttc = "120.00"
        facture.save(update_fields=["montant_ttc"])
        detail_response = self.client.get(reverse("facture_detail", args=[facture.pk]))
        self.assertContains(detail_response, "Produits détectés")
        self.assertContains(detail_response, "MER-001")
        self.assertContains(detail_response, "Filet de merlu")
        self.assertContains(detail_response, "2 kg")
        self.assertContains(detail_response, "50,00 EUR")
        self.assertContains(detail_response, "Total détecté")
        self.assertContains(detail_response, "Le total des lignes diffère du total de la facture.")
        self.assertContains(detail_response, reverse("facture_lignes_update", args=[facture.pk]))
        self.assertContains(detail_response, 'name="devise"')

        response = self.client.post(
            reverse("facture_detail", args=[facture.pk]),
            {
                "numero_facture": "FAC-004",
                "date_facture": "2026-06-03",
                "date_livraison_detectee": "2026-06-02",
                "devise": Devise.CHF,
                "montant_ht": "100.00",
                "montant_tva": "20.00",
                "montant_ttc": "120.00",
                "statut": Facture.Statut.VALIDEE,
                "validation_exception": "Fixture : validation des champs hors de ce scénario.",
                "note": "Controlee manuellement.",
            },
        )

        self.assertRedirects(response, reverse("facture_detail", args=[facture.pk]))
        facture.refresh_from_db()
        self.assertEqual(facture.numero_facture, "FAC-004")
        self.assertEqual(facture.date_facture, date(2026, 6, 3))
        self.assertEqual(facture.date_livraison_detectee, date(2026, 6, 2))
        self.assertEqual(facture.devise, Devise.CHF)
        self.assertEqual(str(facture.montant_ttc), "120.00")
        self.assertEqual(facture.statut, Facture.Statut.VALIDEE)
        self.assertEqual(facture.note, "Controlee manuellement.")

        supplier_response = self.client.get(reverse("fournisseur_detail", args=[fournisseur.pk]))
        self.assertContains(supplier_response, "Validée")

    def test_facture_detail_shows_catalogue_control_summary_and_filters_details(self):
        restaurant = restaurant_fixture(nom="Restaurant Controle Vue")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Fournisseur Controle Vue")
        catalogue = CatalogueFournisseur.objects.create(
            fournisseur=fournisseur,
            nom="Catalogue facture",
            date_debut=date(2026, 1, 1),
            date_fin=date(2026, 12, 31),
            devise=Devise.EUR,
            statut=CatalogueFournisseur.Statut.VALIDE,
        )
        LigneCatalogue.objects.create(
            catalogue=catalogue,
            reference_produit="REF-OK",
            unite="kg",
            designation="Produit conforme",
            prix_unitaire_ht="10.00",
        )
        LigneCatalogue.objects.create(
            catalogue=catalogue,
            reference_produit="REF-GAP",
            unite="kg",
            designation="Produit avec écart",
            prix_unitaire_ht="8.00",
        )
        facture = invoice_fixture(
            fournisseur=fournisseur,
            fichier="factures/1/controle-vue.pdf",
            numero_facture="FAC-CONTROLE",
            date_facture=date(2026, 6, 15),
            devise=Devise.EUR,
            statut=Facture.Statut.A_VALIDER,
        )
        LigneFacture.objects.create(
            facture=facture,
            reference_produit="REF-OK",
            unite="kg",
            designation="Produit conforme facture",
            prix_unitaire_ht="10.00",
            ordre=0,
        )
        LigneFacture.objects.create(
            facture=facture,
            reference_produit="REF-GAP",
            unite="kg",
            designation="Produit écart facture",
            prix_unitaire_ht="8.50",
            ordre=1,
        )

        self.client.force_login(self.gestionnaire_user)
        response = self.client.get(reverse("facture_detail", args=[facture.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Contrôle catalogue")
        self.assertContains(response, "Catalogue facture")
        self.assertContains(response, "Voir les écarts")
        self.assertContains(response, "Afficher aussi les lignes conformes")
        self.assertContains(response, "Écart de prix")
        self.assertContains(response, "8,50 EUR")
        self.assertContains(response, "8,00 EUR")
        self.assertEqual(response.context["catalogue_control"]["counts"]["conforme"], 1)
        self.assertEqual(response.context["catalogue_control"]["counts"]["ecart_prix"], 1)
        self.assertEqual(
            [detail["status"] for detail in response.context["catalogue_control_details"]],
            ["ecart_prix"],
        )

        response = self.client.get(f"{reverse('facture_detail', args=[facture.pk])}?catalogue_all=1")

        self.assertContains(response, "Toutes les lignes contrôlées")
        self.assertTrue(response.context["catalogue_control_show_all"])
        self.assertEqual(len(response.context["catalogue_control_details"]), 2)

    def test_invoice_unknown_product_can_be_added_to_valid_catalogue(self):
        restaurant = restaurant_fixture(nom="Restaurant Ajout Catalogue")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Fournisseur Ajout Catalogue")
        catalogue = CatalogueFournisseur.objects.create(
            fournisseur=fournisseur,
            nom="Catalogue courant",
            date_debut=date(2026, 1, 1),
            date_fin=date(2026, 12, 31),
            devise=Devise.EUR,
            statut=CatalogueFournisseur.Statut.VALIDE,
        )
        facture = invoice_fixture(
            fournisseur=fournisseur,
            fichier="factures/1/ajout-catalogue.pdf",
            date_facture=date(2026, 6, 15),
            devise=Devise.EUR,
            statut=Facture.Statut.A_VALIDER,
        )
        invoice_line = LigneFacture.objects.create(
            facture=facture,
            reference_produit="NEW-CAT-001",
            ean="7612345678111",
            designation="Produit absent",
            unite="kg",
            prix_unitaire_ht="7.25",
            ordre=0,
        )

        self.client.force_login(self.gestionnaire_user)
        detail_response = self.client.get(reverse("facture_detail", args=[facture.pk]))

        self.assertContains(detail_response, "Produit non trouvé dans le catalogue")
        self.assertContains(detail_response, "Ajouter au catalogue")
        self.assertContains(
            detail_response,
            reverse("facture_line_add_to_catalogue", args=[facture.pk, invoice_line.pk]),
        )

        response = self.client.post(
            reverse("facture_line_add_to_catalogue", args=[facture.pk, invoice_line.pk]),
            follow=True,
        )

        self.assertRedirects(response, f"{reverse('facture_detail', args=[facture.pk])}#controle-catalogue")
        self.assertContains(response, "Produit ajouté au catalogue")
        catalogue_line = catalogue.lignes.get(reference_produit="NEW-CAT-001")
        self.assertEqual(catalogue_line.ean, "7612345678111")
        self.assertEqual(catalogue_line.designation, "Produit absent")
        self.assertEqual(catalogue_line.unite, "kg")
        self.assertEqual(str(catalogue_line.prix_unitaire_ht), "7.25")
        self.assertTrue(catalogue_line.corrigee_manuellement)

    def test_invoice_unknown_product_add_button_is_hidden_without_valid_catalogue(self):
        restaurant = restaurant_fixture(nom="Restaurant Sans Catalogue")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Fournisseur Sans Catalogue")
        facture = invoice_fixture(
            fournisseur=fournisseur,
            fichier="factures/1/sans-catalogue.pdf",
            date_facture=date(2026, 6, 15),
            devise=Devise.EUR,
            statut=Facture.Statut.A_VALIDER,
        )
        invoice_line = LigneFacture.objects.create(
            facture=facture,
            reference_produit="NO-CAT-001",
            designation="Produit sans catalogue",
            prix_unitaire_ht="9.00",
            ordre=0,
        )

        self.client.force_login(self.gestionnaire_user)
        detail_response = self.client.get(reverse("facture_detail", args=[facture.pk]))

        self.assertContains(detail_response, "Aucun catalogue valide trouvé.")
        self.assertNotContains(detail_response, "Ajouter au catalogue")

        response = self.client.post(
            reverse("facture_line_add_to_catalogue", args=[facture.pk, invoice_line.pk]),
            follow=True,
        )

        self.assertRedirects(response, f"{reverse('facture_detail', args=[facture.pk])}#controle-catalogue")
        self.assertContains(response, "Aucun catalogue valide disponible pour ajouter ce produit.")
        self.assertEqual(LigneCatalogue.objects.count(), 0)

    def test_facture_quick_status_buttons_update_status_and_require_note_for_litige(self):
        restaurant = restaurant_fixture(nom="Restaurant Boutons Facture")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Fournisseur Boutons Facture")
        facture = invoice_fixture(
            fournisseur=fournisseur,
            fichier="factures/1/facture-bouton.pdf",
            statut=Facture.Statut.IMPORTEE,
        )

        self.client.force_login(self.gestionnaire_user)
        response = self.client.post(
            reverse("facture_detail", args=[facture.pk]),
            {
                "numero_facture": "",
                "date_facture": "",
                "montant_ht": "",
                "montant_tva": "",
                "montant_ttc": "",
                "statut": Facture.Statut.IMPORTEE,
                "note": "",
                "quick_status": Facture.Statut.VALIDEE,
                "validation_exception": "Fixture : validation des champs hors de ce scénario.",
            },
        )
        self.assertRedirects(response, reverse("facture_detail", args=[facture.pk]))
        facture.refresh_from_db()
        self.assertEqual(facture.statut, Facture.Statut.VALIDEE)

        supplier_response = self.client.get(reverse("fournisseur_detail", args=[fournisseur.pk]))
        self.assertContains(supplier_response, "Validée")

        response = self.client.post(
            reverse("facture_detail", args=[facture.pk]),
            {
                "numero_facture": "",
                "date_facture": "",
                "montant_ht": "",
                "montant_tva": "",
                "montant_ttc": "",
                "statut": Facture.Statut.VALIDEE,
                "validation_exception": "Fixture : validation des champs hors de ce scénario.",
                "note": "",
                "quick_status": Facture.Statut.LITIGE,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Une note est obligatoire pour passer cette facture en litige.")
        facture.refresh_from_db()
        self.assertEqual(facture.statut, Facture.Statut.VALIDEE)

        response = self.client.post(
            reverse("facture_detail", args=[facture.pk]),
            {
                "numero_facture": "",
                "date_facture": "",
                "montant_ht": "",
                "montant_tva": "",
                "montant_ttc": "",
                "statut": Facture.Statut.VALIDEE,
                "validation_exception": "Fixture : validation des champs hors de ce scénario.",
                "note": "Prix à vérifier.",
                "quick_status": Facture.Statut.LITIGE,
            },
        )
        self.assertRedirects(response, reverse("facture_detail", args=[facture.pk]))
        facture.refresh_from_db()
        self.assertEqual(facture.statut, Facture.Statut.LITIGE)
        self.assertEqual(facture.note, "Prix à vérifier.")

    def test_facture_litige_requires_note(self):
        restaurant = restaurant_fixture(nom="Restaurant Litige Facture")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Fournisseur Litige Facture")
        facture = invoice_fixture(
            fournisseur=fournisseur,
            fichier="factures/1/facture-litige.pdf",
            statut=Facture.Statut.A_VALIDER,
        )

        self.client.force_login(self.gestionnaire_user)
        response = self.client.post(
            reverse("facture_detail", args=[facture.pk]),
            {"validation_exception": "Fixture : document contrôlé pour ce scénario.",
                "numero_facture": "",
                "date_facture": "",
                "montant_ht": "",
                "montant_tva": "",
                "montant_ttc": "",
                "statut": Facture.Statut.LITIGE,
                "note": "   ",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Obligatoire en cas de litige.")
        self.assertContains(response, "Une note est obligatoire pour passer cette facture en litige.")
        facture.refresh_from_db()
        self.assertEqual(facture.statut, Facture.Statut.A_VALIDER)
        self.assertEqual(facture.note, "")

    def test_invoice_lines_can_be_corrected_manually(self):
        restaurant = restaurant_fixture(nom="Restaurant Lignes")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Fournisseur Lignes")
        facture = invoice_fixture(
            fournisseur=fournisseur,
            fichier="factures/1/facture-lignes.pdf",
        )
        line_to_update = LigneFacture.objects.create(
            facture=facture,
            reference_produit="OLD",
            ean="7612345000001",
            designation="Ancien produit",
            quantite="1.000",
            unite="kg",
            prix_unitaire_ht="10.00",
            montant_ht="10.00",
            montant_ttc="12.00",
            ordre=0,
        )
        line_to_delete = LigneFacture.objects.create(
            facture=facture,
            reference_produit="DEL",
            designation="Produit à supprimer",
            montant_ht="1.00",
            montant_ttc="1.20",
            ordre=1,
        )
        url = reverse("facture_lignes_update", args=[facture.pk])

        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

        self.client.force_login(self.reception_user)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 403)

        self.client.force_login(self.gestionnaire_user)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Vérifier les lignes")
        self.assertContains(response, "Ancien produit")

        response = self.client.post(
            url,
            {
                "lignes-TOTAL_FORMS": "3",
                "lignes-INITIAL_FORMS": "2",
                "lignes-MIN_NUM_FORMS": "0",
                "lignes-MAX_NUM_FORMS": "1000",
                "lignes-0-id": line_to_update.pk,
                "lignes-0-reference_produit": "NEW",
                "lignes-0-ean": "7612345000002",
                "lignes-0-designation": "Produit corrigé",
                "lignes-0-quantite": "2.500",
                "lignes-0-unite": "kg",
                "lignes-0-prix_unitaire_ht": "11.00",
                "lignes-0-montant_ht": "27.50",
                "lignes-0-montant_ttc": "33.00",
                "lignes-1-id": line_to_delete.pk,
                "lignes-1-reference_produit": "DEL",
                "lignes-1-ean": "",
                "lignes-1-designation": "Produit à supprimer",
                "lignes-1-quantite": "",
                "lignes-1-unite": "",
                "lignes-1-prix_unitaire_ht": "",
                "lignes-1-montant_ht": "1.00",
                "lignes-1-montant_ttc": "1.20",
                "lignes-1-DELETE": "on",
                "lignes-2-id": "",
                "lignes-2-reference_produit": "ADD",
                "lignes-2-ean": "7612345000003",
                "lignes-2-designation": "Produit ajouté",
                "lignes-2-quantite": "1",
                "lignes-2-unite": "piece",
                "lignes-2-prix_unitaire_ht": "5.00",
                "lignes-2-montant_ht": "5.00",
                "lignes-2-montant_ttc": "6.00",
            },
            follow=True,
        )

        self.assertRedirects(response, reverse("facture_detail", args=[facture.pk]))
        self.assertContains(response, "Lignes de facture sauvegardées.")
        line_to_update.refresh_from_db()
        self.assertEqual(line_to_update.reference_produit, "NEW")
        self.assertEqual(line_to_update.ean, "7612345000002")
        self.assertEqual(line_to_update.designation, "Produit corrigé")
        self.assertEqual(str(line_to_update.quantite), "2.500")
        self.assertEqual(line_to_update.unite, "kg")
        self.assertTrue(line_to_update.corrigee_manuellement)
        self.assertFalse(LigneFacture.objects.filter(pk=line_to_delete.pk).exists())

        added_line = LigneFacture.objects.get(facture=facture, reference_produit="ADD")
        self.assertEqual(added_line.ean, "7612345000003")
        self.assertEqual(added_line.designation, "Produit ajouté")
        self.assertEqual(added_line.unite, "piece")
        self.assertTrue(added_line.corrigee_manuellement)

    def test_bon_livraison_detail_displays_lines_like_invoice_table(self):
        restaurant = restaurant_fixture(nom="Restaurant Tableau BL")
        fournisseur = Fournisseur.objects.create(
            restaurant=restaurant,
            nom="Fournisseur Tableau BL",
            devise_defaut=Devise.EUR,
        )
        bon = BonLivraison.objects.create(
            fournisseur=fournisseur,
            fichier="bons_livraison/1/bon-tableau.pdf",
            numero_bon_livraison="BL-TABLE-001",
            statut=BonLivraison.Statut.A_VERIFIER,
        )
        LigneBonLivraison.objects.create(
            bon_livraison=bon,
            reference_produit="TOM-001",
            designation="Tomates rondes",
            quantite_livree="2.500",
            unite="kg",
            conditionnement="Caisse",
            prix_unitaire="3.20",
            total_ligne="8.00",
            confiance_ia="0.82",
            texte_brut="TOM-001 Tomates rondes 2,5 kg",
            statut_ligne=LigneBonLivraison.StatutLigne.A_VERIFIER,
        )

        self.client.force_login(self.gestionnaire_user)
        response = self.client.get(reverse("bon_livraison_detail", args=[bon.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Produits détectés")
        self.assertContains(response, "Référence / Produit")
        self.assertContains(response, "Quantité livrée")
        self.assertContains(response, "Prix unitaire")
        self.assertContains(response, "Total détecté")
        self.assertContains(response, "TOM-001")
        self.assertContains(response, "Tomates rondes")
        self.assertContains(response, "2,5 kg")
        self.assertContains(response, "8,00 EUR")
        self.assertContains(response, 'data-ai-loading-label="Analyse..."')
        self.assertContains(response, "Relance IA en cours...")
        self.assertContains(response, reverse("bon_livraison_lignes_update", args=[bon.pk]))
        self.assertNotContains(response, 'name="lignes-0-designation"')

    def test_bon_livraison_lines_can_be_corrected_manually(self):
        restaurant = restaurant_fixture(nom="Restaurant Correction BL")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Fournisseur Correction BL")
        bon = BonLivraison.objects.create(
            fournisseur=fournisseur,
            fichier="bons_livraison/1/bon-lignes.pdf",
        )
        line_to_update = LigneBonLivraison.objects.create(
            bon_livraison=bon,
            reference_produit="OLD",
            designation="Ancien produit BL",
            quantite_livree="1.000",
            unite="kg",
            prix_unitaire="10.00",
            total_ligne="10.00",
            ordre=0,
        )
        line_to_delete = LigneBonLivraison.objects.create(
            bon_livraison=bon,
            reference_produit="DEL",
            designation="Produit BL à supprimer",
            total_ligne="1.20",
            ordre=1,
        )
        url = reverse("bon_livraison_lignes_update", args=[bon.pk])

        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

        self.client.force_login(self.reception_user)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 403)

        self.client.force_login(self.gestionnaire_user)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Vérifier les lignes")
        self.assertContains(response, "Ancien produit BL")

        response = self.client.post(
            url,
            {
                "lignes-TOTAL_FORMS": "3",
                "lignes-INITIAL_FORMS": "2",
                "lignes-MIN_NUM_FORMS": "0",
                "lignes-MAX_NUM_FORMS": "1000",
                "lignes-0-id": line_to_update.pk,
                "lignes-0-bon_livraison": bon.pk,
                "lignes-0-reference_produit": "NEW",
                "lignes-0-designation": "Produit BL corrigé",
                "lignes-0-quantite_livree": "2.500",
                "lignes-0-unite": "kg",
                "lignes-0-conditionnement": "Caisse",
                "lignes-0-prix_unitaire": "11.00",
                "lignes-0-total_ligne": "27.50",
                "lignes-0-statut_ligne": LigneBonLivraison.StatutLigne.OK,
                "lignes-1-id": line_to_delete.pk,
                "lignes-1-bon_livraison": bon.pk,
                "lignes-1-reference_produit": "DEL",
                "lignes-1-designation": "Produit BL à supprimer",
                "lignes-1-quantite_livree": "",
                "lignes-1-unite": "",
                "lignes-1-conditionnement": "",
                "lignes-1-prix_unitaire": "",
                "lignes-1-total_ligne": "1.20",
                "lignes-1-statut_ligne": LigneBonLivraison.StatutLigne.OK,
                "lignes-1-DELETE": "on",
                "lignes-2-id": "",
                "lignes-2-bon_livraison": bon.pk,
                "lignes-2-reference_produit": "ADD",
                "lignes-2-designation": "Produit BL ajouté",
                "lignes-2-quantite_livree": "1",
                "lignes-2-unite": "piece",
                "lignes-2-conditionnement": "Carton",
                "lignes-2-prix_unitaire": "5.00",
                "lignes-2-total_ligne": "5.00",
                "lignes-2-statut_ligne": LigneBonLivraison.StatutLigne.OK,
            },
            follow=True,
        )

        self.assertRedirects(response, reverse("bon_livraison_detail", args=[bon.pk]))
        self.assertContains(response, "Lignes du bon de livraison sauvegardées.")
        line_to_update.refresh_from_db()
        self.assertEqual(line_to_update.reference_produit, "NEW")
        self.assertEqual(line_to_update.designation, "Produit BL corrigé")
        self.assertEqual(str(line_to_update.quantite_livree), "2.500")
        self.assertEqual(line_to_update.conditionnement, "Caisse")
        self.assertTrue(line_to_update.corrigee_manuellement)
        self.assertFalse(LigneBonLivraison.objects.filter(pk=line_to_delete.pk).exists())

        added_line = LigneBonLivraison.objects.get(bon_livraison=bon, reference_produit="ADD")
        self.assertEqual(added_line.designation, "Produit BL ajouté")
        self.assertEqual(added_line.unite, "piece")
        self.assertTrue(added_line.corrigee_manuellement)

    def test_bon_livraison_detail_can_update_main_fields(self):
        restaurant = restaurant_fixture(nom="Restaurant Ouest")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Cave Test")
        bon = BonLivraison.objects.create(
            fournisseur=fournisseur,
            fichier="bons_livraison/1/bl-3.pdf",
            statut=BonLivraison.Statut.IMPORTE,
        )

        self.client.force_login(self.gestionnaire_user)
        detail_response = self.client.get(reverse("bon_livraison_detail", args=[bon.pk]))
        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(detail_response, 'name="statut"')
        self.assertContains(detail_response, "Importé")
        self.assertContains(detail_response, "À vérifier")
        self.assertContains(detail_response, "Validé")
        self.assertContains(detail_response, "Litige")
        self.assertContains(detail_response, "Statut : Importé")
        self.assertContains(detail_response, "text-bg-secondary")
        self.assertContains(detail_response, "Corriger plus tard")
        self.assertContains(detail_response, "Valider l’import")
        self.assertContains(detail_response, "Mettre en litige")

        response = self.client.post(
            reverse("bon_livraison_detail", args=[bon.pk]),
            {
                "numero_bon_livraison": "BL-003",
                "date_livraison": "2026-06-04",
                "statut": BonLivraison.Statut.VALIDE,
                "validation_exception": "Fixture : validation des champs hors de ce scénario.",
                "note": "Livraison complete.",
            },
        )

        self.assertRedirects(response, reverse("bon_livraison_detail", args=[bon.pk]))
        bon.refresh_from_db()
        self.assertEqual(bon.numero_bon_livraison, "BL-003")
        self.assertEqual(bon.date_livraison, date(2026, 6, 4))
        self.assertEqual(bon.statut, BonLivraison.Statut.VALIDE)
        self.assertEqual(bon.note, "Livraison complete.")

        supplier_response = self.client.get(reverse("fournisseur_detail", args=[fournisseur.pk]))
        self.assertContains(supplier_response, "Validé")

    def test_bon_livraison_quick_status_buttons_update_status_and_require_note_for_litige(self):
        restaurant = restaurant_fixture(nom="Restaurant Boutons BL")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Fournisseur Boutons BL")
        bon = BonLivraison.objects.create(
            fournisseur=fournisseur,
            fichier="bons_livraison/1/bon-bouton.pdf",
            statut=BonLivraison.Statut.IMPORTE,
        )

        self.client.force_login(self.gestionnaire_user)
        response = self.client.post(
            reverse("bon_livraison_detail", args=[bon.pk]),
            {
                "numero_bon_livraison": "",
                "date_livraison": "",
                "statut": BonLivraison.Statut.IMPORTE,
                "note": "",
                "quick_status": BonLivraison.Statut.VALIDE,
                "validation_exception": "Fixture : validation des champs hors de ce scénario.",
            },
        )
        self.assertRedirects(response, reverse("bon_livraison_detail", args=[bon.pk]))
        bon.refresh_from_db()
        self.assertEqual(bon.statut, BonLivraison.Statut.VALIDE)

        supplier_response = self.client.get(reverse("fournisseur_detail", args=[fournisseur.pk]))
        self.assertContains(supplier_response, "Validé")

        response = self.client.post(
            reverse("bon_livraison_detail", args=[bon.pk]),
            {
                "numero_bon_livraison": "",
                "date_livraison": "",
                "statut": BonLivraison.Statut.VALIDE,
                "validation_exception": "Fixture : validation des champs hors de ce scénario.",
                "note": "",
                "quick_status": BonLivraison.Statut.LITIGE,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Une note est obligatoire pour passer ce bon de livraison en litige.")
        bon.refresh_from_db()
        self.assertEqual(bon.statut, BonLivraison.Statut.VALIDE)

        response = self.client.post(
            reverse("bon_livraison_detail", args=[bon.pk]),
            {
                "numero_bon_livraison": "",
                "date_livraison": "",
                "statut": BonLivraison.Statut.VALIDE,
                "validation_exception": "Fixture : validation des champs hors de ce scénario.",
                "note": "Produit manquant.",
                "quick_status": BonLivraison.Statut.LITIGE,
            },
        )
        self.assertRedirects(response, reverse("bon_livraison_detail", args=[bon.pk]))
        bon.refresh_from_db()
        self.assertEqual(bon.statut, BonLivraison.Statut.LITIGE)
        self.assertEqual(bon.note, "Produit manquant.")

    def test_bon_livraison_litige_requires_note(self):
        restaurant = restaurant_fixture(nom="Restaurant Litige BL")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Fournisseur Litige BL")
        bon = BonLivraison.objects.create(
            fournisseur=fournisseur,
            fichier="bons_livraison/1/bon-litige.pdf",
            statut=BonLivraison.Statut.A_VERIFIER,
        )

        self.client.force_login(self.gestionnaire_user)
        response = self.client.post(
            reverse("bon_livraison_detail", args=[bon.pk]),
            {"validation_exception": "Fixture : document contrôlé pour ce scénario.",
                "numero_bon_livraison": "",
                "date_livraison": "",
                "statut": BonLivraison.Statut.LITIGE,
                "note": "",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Obligatoire en cas de litige.")
        self.assertContains(response, "Une note est obligatoire pour passer ce bon de livraison en litige.")
        bon.refresh_from_db()
        self.assertEqual(bon.statut, BonLivraison.Statut.A_VERIFIER)
        self.assertEqual(bon.note, "")

    def test_note_remains_optional_for_non_litige_statuses(self):
        restaurant = restaurant_fixture(nom="Restaurant Note Optionnelle")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Fournisseur Note Optionnelle")
        facture = invoice_fixture(
            fournisseur=fournisseur,
            fichier="factures/1/facture-note-optionnelle.pdf",
            statut=Facture.Statut.A_VALIDER,
            note="Ancienne note",
        )
        bon = BonLivraison.objects.create(
            fournisseur=fournisseur,
            fichier="bons_livraison/1/bon-note-optionnelle.pdf",
            statut=BonLivraison.Statut.A_VERIFIER,
            note="Ancienne note",
        )

        self.client.force_login(self.gestionnaire_user)
        facture_response = self.client.post(
            reverse("facture_detail", args=[facture.pk]),
            {
                "numero_facture": "",
                "date_facture": "",
                "montant_ht": "",
                "montant_tva": "",
                "montant_ttc": "",
                "statut": Facture.Statut.VALIDEE,
                "validation_exception": "Fixture : validation des champs hors de ce scénario.",
                "note": "",
            },
        )
        bon_response = self.client.post(
            reverse("bon_livraison_detail", args=[bon.pk]),
            {
                "numero_bon_livraison": "",
                "date_livraison": "",
                "statut": BonLivraison.Statut.VALIDE,
                "validation_exception": "Fixture : validation des champs hors de ce scénario.",
                "note": "",
            },
        )

        self.assertRedirects(facture_response, reverse("facture_detail", args=[facture.pk]))
        self.assertRedirects(bon_response, reverse("bon_livraison_detail", args=[bon.pk]))
        facture.refresh_from_db()
        bon.refresh_from_db()
        self.assertEqual(facture.statut, Facture.Statut.VALIDEE)
        self.assertEqual(facture.note, "")
        self.assertEqual(bon.statut, BonLivraison.Statut.VALIDE)
        self.assertEqual(bon.note, "")

    def test_gestionnaire_legacy_can_upload_bon_livraison(self):
        restaurant = restaurant_fixture(nom="Restaurant Reception")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Livreur Reception")

        upload = upload_fixture(
            "bon.jpg",
            b"image-content",
            content_type="image/jpeg",
        )

        self.client.force_login(self.gestionnaire_user)
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            with patch("core.views.enqueue_ai_analysis") as enqueue_ai_analysis:
                response = self.client.post(
                    reverse("bon_livraison_add"),
                    {
                        "restaurant": restaurant.pk,
                        "fournisseur": fournisseur.pk,
                        "fichier": upload,
                        "note": "Depose ce matin.",
                    },
                    follow=True,
                )
                bon = BonLivraison.objects.get(fournisseur=fournisseur)
                enqueue_ai_analysis.assert_called_once_with("reception_bon_livraison", bon.pk)

        self.assertEqual(response.status_code, 200)
        self.assertRedirects(response, reverse("fournisseur_detail", args=[fournisseur.pk]))
        self.assertContains(response, "Document importé. Analyse IA en cours...")

        bon.refresh_from_db()
        self.assertEqual(bon.statut, BonLivraison.Statut.ANALYSE_EN_COURS)
        self.assertEqual(bon.note, "Depose ce matin.")
        self.assertIsNone(bon.date_livraison)
        self.assertEqual(bon.numero_bon_livraison, "")
        self.assertIsNone(bon.donnees_ia)
        self.assertIsNone(bon.confiance_ia)
        self.assertEqual(bon.lignes.count(), 0)
        self.assertTrue(bon.fichier.name.startswith(f"bons_livraison/{fournisseur.pk}/"))

    def test_gestionnaire_legacy_cannot_use_supplier_from_another_restaurant(self):
        restaurant = restaurant_fixture(nom="Restaurant A")
        other_restaurant = restaurant_fixture(nom="Restaurant B")
        fournisseur = Fournisseur.objects.create(restaurant=other_restaurant, nom="Mauvais fournisseur")
        upload = upload_fixture("bon.pdf", b"%PDF-1.7\npdf-content", content_type="application/pdf")

        self.client.force_login(self.gestionnaire_user)
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            response = self.client.post(
                reverse("bon_livraison_add"),
                {
                    "restaurant": restaurant.pk,
                    "fournisseur": fournisseur.pk,
                    "fichier": upload,
                    "note": "",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ce fournisseur n&#x27;appartient pas au restaurant choisi.")
        self.assertEqual(BonLivraison.objects.count(), 0)

    def test_gestionnaire_can_upload_facture_from_supplier(self):
        restaurant = restaurant_fixture(nom="Restaurant Facture", devise_defaut=Devise.CHF)
        fournisseur = Fournisseur.objects.create(
            restaurant=restaurant,
            nom="Fournisseur Facture",
            devise_defaut=Devise.CHF,
        )
        upload = upload_fixture(
            "facture.pdf",
            b"%PDF-1.7\npdf-content",
            content_type="application/pdf",
        )

        self.client.force_login(self.gestionnaire_user)
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            response = self.client.get(reverse("fournisseur_facture_add", args=[fournisseur.pk]))
            self.assertContains(response, "Déposez le document. Les informations seront lues automatiquement.")
            self.assertContains(response, 'name="fichier"')
            self.assertContains(response, 'name="note"')
            self.assertNotContains(response, 'name="numero_facture"')
            self.assertNotContains(response, 'name="date_facture"')
            self.assertNotContains(response, 'name="devise"')
            self.assertNotContains(response, 'name="montant_ttc"')

            with patch("core.views.enqueue_ai_analysis") as enqueue_ai_analysis:
                response = self.client.post(
                    reverse("fournisseur_facture_add", args=[fournisseur.pk]),
                    {
                        "fichier": upload,
                        "note": "",
                    },
                )
                facture = Facture.objects.get(fournisseur=fournisseur)
                enqueue_ai_analysis.assert_called_once_with("facture", facture.pk)

        self.assertRedirects(response, reverse("fournisseur_detail", args=[fournisseur.pk]))
        facture.refresh_from_db()
        self.assertEqual(facture.numero_facture, "")
        self.assertIsNone(facture.date_facture)
        self.assertIsNone(facture.date_livraison_detectee)
        self.assertEqual(facture.devise, Devise.UNKNOWN)
        self.assertIsNone(facture.montant_ht)
        self.assertIsNone(facture.montant_tva)
        self.assertIsNone(facture.montant_ttc)
        self.assertEqual(facture.statut, Facture.Statut.ANALYSE_EN_COURS)
        self.assertIsNone(facture.donnees_ia)
        self.assertIsNone(facture.confiance_ia)
        self.assertTrue(facture.fichier.name.startswith(f"factures/{fournisseur.pk}/"))
        self.assertEqual(facture.lignes.count(), 0)

    def test_background_invoice_analysis_updates_imported_document(self):
        restaurant = restaurant_fixture(nom="Restaurant Facture Async", devise_defaut=Devise.CHF)
        fournisseur = Fournisseur.objects.create(
            restaurant=restaurant,
            nom="Fournisseur Facture Async",
            devise_defaut=Devise.CHF,
        )

        self.client.force_login(self.gestionnaire_user)
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            facture = invoice_fixture(
                fournisseur=fournisseur,
                fichier=upload_fixture("facture.pdf", b"%PDF-1.7\npdf-content", content_type="application/pdf"),
                devise=Devise.CHF,
                statut=Facture.Statut.ANALYSE_EN_COURS,
            )

            with patch("core.views.analyze_invoice", return_value=sample_invoice_ai_data()):
                views.run_background_ai_analysis("facture", facture.pk)

            facture.refresh_from_db()
            self.assertEqual(facture.numero_facture, "FAC-IA-001")
            self.assertEqual(facture.date_facture, date(2026, 6, 12))
            self.assertEqual(facture.date_livraison_detectee, date(2026, 6, 11))
            self.assertEqual(facture.devise, Devise.CHF)
            self.assertEqual(str(facture.montant_ht), "100.00")
            self.assertEqual(str(facture.montant_tva), "20.00")
            self.assertEqual(str(facture.montant_ttc), "120.00")
            self.assertEqual(facture.statut, Facture.Statut.A_VALIDER)
            self.assertIn("lignes", facture.donnees_ia)
            self.assertEqual(str(facture.confiance_ia), "0.82")

            ligne = facture.lignes.get()
            self.assertEqual(ligne.reference_produit, "REF-IA-001")
            self.assertEqual(ligne.ean, "7612345678999")
            self.assertEqual(ligne.designation, "Produit IA")
            self.assertEqual(str(ligne.quantite), "1.000")
            self.assertEqual(str(ligne.prix_unitaire_ht), "100.00")
            self.assertEqual(str(ligne.montant_ht), "100.00")
            self.assertEqual(str(ligne.montant_ttc), "120.00")
            self.assertFalse(ligne.corrigee_manuellement)

            detail_response = self.client.get(reverse("facture_detail", args=[facture.pk]))
            self.assertContains(
                detail_response,
                "Devise détectée par l’IA différente de la devise du fournisseur.",
            )
            self.assertContains(detail_response, "Devise fournisseur : CHF")
            self.assertContains(detail_response, "Devise détectée : EUR")
            self.assertContains(
                detail_response,
                "Date utilisée pour le contrôle catalogue : date de livraison détectée",
            )

    def test_gestionnaire_can_import_supplier_catalogue(self):
        restaurant = restaurant_fixture(nom="Restaurant Catalogue Upload", devise_defaut=Devise.CHF)
        fournisseur = Fournisseur.objects.create(
            restaurant=restaurant,
            nom="Fournisseur Catalogue Upload",
            devise_defaut=Devise.CHF,
        )
        upload = upload_fixture(
            "catalogue.csv",
            b"reference,prix\nCAT-001,10.50",
            content_type="text/csv",
        )

        self.client.force_login(self.gestionnaire_user)
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            response = self.client.get(reverse("fournisseur_catalogue_import", args=[fournisseur.pk]))
            self.assertContains(response, "Importer un catalogue")
            self.assertContains(response, 'name="fichier_original"')
            self.assertContains(response, 'name="nom"')
            self.assertContains(response, 'name="date_debut"')
            self.assertContains(response, 'name="date_fin"')
            self.assertContains(response, 'name="note"')
            self.assertNotContains(response, 'name="devise"')

            with patch("core.views.enqueue_ai_analysis") as enqueue_ai_analysis:
                response = self.client.post(
                    reverse("fournisseur_catalogue_import", args=[fournisseur.pk]),
                    {
                        "fichier_original": upload,
                        "nom": "",
                        "date_debut": "",
                        "date_fin": "",
                        "note": "Catalogue reçu par email.",
                    },
                    follow=True,
                )
                catalogue = CatalogueFournisseur.objects.get(fournisseur=fournisseur)
                enqueue_ai_analysis.assert_called_once_with("catalogue", catalogue.pk)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Catalogue importé. Analyse IA en cours...")
        self.assertContains(response, "Analyse IA en cours...")
        catalogue.refresh_from_db()
        self.assertEqual(catalogue.nom, "")
        self.assertIsNone(catalogue.date_debut)
        self.assertIsNone(catalogue.date_fin)
        self.assertEqual(catalogue.devise, Devise.UNKNOWN)
        self.assertEqual(catalogue.type_prix, CatalogueFournisseur.TypePrix.HT)
        self.assertEqual(catalogue.statut, CatalogueFournisseur.Statut.ANALYSE_EN_COURS)
        self.assertEqual(catalogue.note, "Catalogue reçu par email.")
        self.assertIsNone(catalogue.confiance_ia)
        self.assertIsNone(catalogue.donnees_ia)
        self.assertTrue(catalogue.fichier_original.name.startswith(f"catalogues/{fournisseur.pk}/"))
        self.assertFalse(catalogue.lignes.exists())
        self.assertNotContains(response, reverse("catalogue_detail", args=[catalogue.pk]))

        list_response = self.client.get(reverse("fournisseur_catalogue_list", args=[fournisseur.pk]))
        self.assertContains(list_response, "Analyse IA en cours...")
        self.assertNotContains(list_response, reverse("catalogue_detail", args=[catalogue.pk]))

    def test_background_catalogue_analysis_updates_imported_document(self):
        restaurant = restaurant_fixture(nom="Restaurant Catalogue Async", devise_defaut=Devise.CHF)
        fournisseur = Fournisseur.objects.create(
            restaurant=restaurant,
            nom="Fournisseur Catalogue Async",
            devise_defaut=Devise.CHF,
        )

        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            catalogue = CatalogueFournisseur.objects.create(
                fournisseur=fournisseur,
                fichier_original=upload_fixture("catalogue.csv", b"reference,prix\nCAT-001,10.50"),
                note="Catalogue reçu par email.",
                statut=CatalogueFournisseur.Statut.ANALYSE_EN_COURS,
            )

            with patch("core.views.analyze_supplier_catalog", return_value=sample_catalogue_ai_data()):
                views.run_background_ai_analysis("catalogue", catalogue.pk)

        catalogue.refresh_from_db()
        self.assertEqual(catalogue.nom, "Catalogue IA 2026")
        self.assertEqual(catalogue.date_debut, date(2026, 1, 1))
        self.assertEqual(catalogue.date_fin, date(2026, 12, 31))
        self.assertEqual(catalogue.devise, Devise.CHF)
        self.assertEqual(catalogue.type_prix, CatalogueFournisseur.TypePrix.HT)
        self.assertEqual(catalogue.statut, CatalogueFournisseur.Statut.A_VALIDER)
        self.assertNotEqual(catalogue.statut, CatalogueFournisseur.Statut.VALIDE)
        self.assertEqual(catalogue.note, "Catalogue reçu par email.")
        self.assertEqual(str(catalogue.confiance_ia), "0.84")
        self.assertIn("lignes", catalogue.donnees_ia)

        lignes = list(catalogue.lignes.order_by("ordre"))
        self.assertEqual(len(lignes), 2)
        self.assertEqual(lignes[0].reference_produit, "CAT-001")
        self.assertEqual(lignes[0].ean, "7612345678901")
        self.assertEqual(str(lignes[0].prix_unitaire_ht), "10.50")
        self.assertEqual(lignes[0].statut_ligne, LigneCatalogue.StatutLigne.OK)
        self.assertEqual(lignes[1].statut_ligne, LigneCatalogue.StatutLigne.A_VERIFIER)

        self.client.force_login(self.gestionnaire_user)
        supplier_response = self.client.get(reverse("fournisseur_detail", args=[fournisseur.pk]))
        self.assertContains(supplier_response, "Catalogues")
        self.assertContains(supplier_response, "Aucun catalogue valide actuellement.")
        self.assertContains(supplier_response, "Anciens catalogues")
        list_response = self.client.get(reverse("fournisseur_catalogue_list", args=[fournisseur.pk]))
        self.assertContains(list_response, "Catalogue IA 2026")
        self.assertContains(list_response, reverse("catalogue_detail", args=[catalogue.pk]))

    def test_catalogue_import_keeps_file_when_ai_analysis_fails(self):
        restaurant = restaurant_fixture(nom="Restaurant Catalogue Erreur")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Fournisseur Catalogue Erreur")
        upload = upload_fixture(
            "catalogue.pdf",
            b"%PDF-1.7\ncatalogue",
            content_type="application/pdf",
        )

        self.client.force_login(self.gestionnaire_user)
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            with patch("core.views.enqueue_ai_analysis"):
                response = self.client.post(
                    reverse("fournisseur_catalogue_import", args=[fournisseur.pk]),
                    {
                        "fichier_original": upload,
                        "nom": "Catalogue à reprendre",
                        "date_debut": "",
                        "date_fin": "",
                        "note": "",
                    },
                    follow=True,
                )
            catalogue = CatalogueFournisseur.objects.get(fournisseur=fournisseur)
            with patch(
                "core.views.apply_catalogue_ai_analysis",
                side_effect=DocumentAIError("boom"),
            ):
                with self.assertLogs("core.views", level="ERROR"):
                    views.run_background_ai_analysis("catalogue", catalogue.pk)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Catalogue importé. Analyse IA en cours...")
        catalogue.refresh_from_db()
        self.assertEqual(catalogue.statut, CatalogueFournisseur.Statut.ANALYSE_ECHOUEE)
        self.assertEqual(catalogue.erreur_analyse, "boom")
        self.assertEqual(catalogue.nom, "Catalogue à reprendre")
        self.assertIsNone(catalogue.donnees_ia)
        self.assertIsNone(catalogue.confiance_ia)
        self.assertFalse(catalogue.lignes.exists())
        self.assertTrue(catalogue.fichier_original.name.startswith(f"catalogues/{fournisseur.pk}/"))

    def test_catalogue_detail_prevents_validated_period_overlap(self):
        restaurant = restaurant_fixture(nom="Restaurant Catalogue Chevauchement")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Fournisseur Catalogue Chevauchement")
        CatalogueFournisseur.objects.create(
            fournisseur=fournisseur,
            nom="Catalogue validé",
            date_debut=date(2026, 1, 1),
            date_fin=date(2026, 6, 30),
            statut=CatalogueFournisseur.Statut.VALIDE,
        )
        catalogue = CatalogueFournisseur.objects.create(
            fournisseur=fournisseur,
            nom="Catalogue à valider",
            date_debut=date(2026, 6, 1),
            date_fin=date(2026, 12, 31),
            statut=CatalogueFournisseur.Statut.A_VALIDER,
        )

        self.client.force_login(self.gestionnaire_user)
        response = self.client.post(
            reverse("catalogue_validate", args=[catalogue.pk]),
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "Ce catalogue chevauche une période existante.",
        )
        catalogue.refresh_from_db()
        self.assertEqual(catalogue.statut, CatalogueFournisseur.Statut.A_VALIDER)

    def test_catalogue_can_be_validated_and_shown_as_current_supplier_catalogue(self):
        restaurant = restaurant_fixture(nom="Restaurant Catalogue Valide")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Fournisseur Catalogue Valide")
        catalogue = CatalogueFournisseur.objects.create(
            fournisseur=fournisseur,
            nom="Catalogue courant",
            date_debut=date(2026, 1, 1),
            date_fin=None,
            statut=CatalogueFournisseur.Statut.A_VALIDER,
        )
        LigneCatalogue.objects.create(
            catalogue=catalogue,
            reference_produit="CUR-001",
            designation="Produit courant",
            prix_unitaire_ht="9.50",
        )
        LigneCatalogue.objects.create(
            catalogue=catalogue,
            designation="Produit à vérifier",
            prix_unitaire_ht="2.00",
        )

        self.client.force_login(self.gestionnaire_user)
        response = self.client.post(reverse("catalogue_validate", args=[catalogue.pk]), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Catalogue validé.")
        catalogue.refresh_from_db()
        self.assertEqual(catalogue.statut, CatalogueFournisseur.Statut.VALIDE)

        supplier_response = self.client.get(reverse("fournisseur_detail", args=[fournisseur.pk]))
        self.assertContains(supplier_response, "Catalogue courant")
        self.assertContains(supplier_response, "2 · 1 à vérifier")

    def test_catalogue_validation_requires_start_date(self):
        restaurant = restaurant_fixture(nom="Restaurant Catalogue Date")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Fournisseur Catalogue Date")
        catalogue = CatalogueFournisseur.objects.create(
            fournisseur=fournisseur,
            nom="Catalogue sans date",
            statut=CatalogueFournisseur.Statut.A_VALIDER,
        )

        self.client.force_login(self.gestionnaire_user)
        response = self.client.post(reverse("catalogue_validate", args=[catalogue.pk]), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "La date de début est obligatoire pour valider un catalogue.")
        catalogue.refresh_from_db()
        self.assertEqual(catalogue.statut, CatalogueFournisseur.Statut.A_VALIDER)

    def test_catalogue_detail_filters_lines_to_verify_by_default(self):
        restaurant = restaurant_fixture(nom="Restaurant Catalogue Filtres")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Fournisseur Catalogue Filtres")
        catalogue = CatalogueFournisseur.objects.create(
            fournisseur=fournisseur,
            nom="Catalogue filtres",
            date_debut=date(2026, 1, 1),
            statut=CatalogueFournisseur.Statut.A_VALIDER,
        )
        LigneCatalogue.objects.create(
            catalogue=catalogue,
            reference_produit="OK-001",
            designation="Produit OK visible seulement sans filtre",
            prix_unitaire_ht="10.00",
        )
        LigneCatalogue.objects.create(
            catalogue=catalogue,
            designation="Produit à vérifier par défaut",
            prix_unitaire_ht="8.00",
        )

        self.client.force_login(self.gestionnaire_user)
        response = self.client.get(reverse("catalogue_detail", args=[catalogue.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Produit à vérifier par défaut")
        self.assertNotContains(response, "Produit OK visible seulement sans filtre")
        self.assertContains(response, 'name="q"')
        self.assertContains(response, 'name="statut"')
        self.assertContains(response, 'data-ai-loading-label="Analyse..."')
        self.assertContains(response, "Relance IA en cours...")

        response = self.client.get(f"{reverse('catalogue_detail', args=[catalogue.pk])}?statut=")
        self.assertContains(response, "Produit OK visible seulement sans filtre")

    def test_gestionnaire_can_update_catalogue_info_and_line(self):
        restaurant = restaurant_fixture(nom="Restaurant Catalogue Edition")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Fournisseur Catalogue Edition")
        catalogue = CatalogueFournisseur.objects.create(
            fournisseur=fournisseur,
            nom="Catalogue avant",
            date_debut=date(2026, 1, 1),
            statut=CatalogueFournisseur.Statut.A_VALIDER,
        )
        ligne = LigneCatalogue.objects.create(
            catalogue=catalogue,
            designation="Produit à corriger",
        )

        self.client.force_login(self.gestionnaire_user)
        response = self.client.post(
            reverse("catalogue_update", args=[catalogue.pk]),
            {
                "nom": "Catalogue après",
                "date_debut": "2026-02-01",
                "date_fin": "",
                "devise": Devise.CHF,
                "note": "Informations corrigées.",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Informations du catalogue sauvegardées.")
        catalogue.refresh_from_db()
        self.assertEqual(catalogue.nom, "Catalogue après")
        self.assertEqual(catalogue.devise, Devise.CHF)
        self.assertEqual(catalogue.type_prix, CatalogueFournisseur.TypePrix.HT)

        response = self.client.post(
            reverse("ligne_catalogue_update", args=[catalogue.pk, ligne.pk]),
            {
                "reference_produit": "EDIT-001",
                "ean": "7612345678000",
                "designation": "Produit corrigé",
                "marque": "Marque corrigée",
                "unite": "kg",
                "conditionnement": "Carton",
                "prix_unitaire_ht": "11.00",
                "statut_ligne": LigneCatalogue.StatutLigne.OK,
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ligne catalogue sauvegardée.")
        ligne.refresh_from_db()
        self.assertTrue(ligne.corrigee_manuellement)
        self.assertEqual(ligne.reference_produit, "EDIT-001")
        self.assertEqual(ligne.statut_ligne, LigneCatalogue.StatutLigne.OK)
        self.assertEqual(str(ligne.prix_unitaire_ht), "11.00")

    def test_gestionnaire_can_upload_bon_livraison_from_supplier(self):
        restaurant = restaurant_fixture(nom="Restaurant BL")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Fournisseur BL")
        upload = upload_fixture(
            "bon.png",
            b"\x89PNG\r\n\x1a\npng-content",
            content_type="image/png",
        )

        self.client.force_login(self.gestionnaire_user)
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            response = self.client.get(reverse("fournisseur_bon_livraison_add", args=[fournisseur.pk]))
            self.assertContains(response, "Déposez le document. Les informations seront lues automatiquement.")
            self.assertContains(response, 'name="fichier"')
            self.assertContains(response, 'name="note"')
            self.assertNotContains(response, 'name="numero_bon_livraison"')
            self.assertNotContains(response, 'name="date_livraison"')

            with patch("core.views.enqueue_ai_analysis") as enqueue_ai_analysis:
                response = self.client.post(
                    reverse("fournisseur_bon_livraison_add", args=[fournisseur.pk]),
                    {
                        "fichier": upload,
                        "note": "Reception rapide.",
                    },
                )
                bon = BonLivraison.objects.get(fournisseur=fournisseur)
                enqueue_ai_analysis.assert_called_once_with("bon_livraison", bon.pk)

        self.assertRedirects(response, reverse("fournisseur_detail", args=[fournisseur.pk]))
        bon.refresh_from_db()
        self.assertEqual(bon.numero_bon_livraison, "")
        self.assertIsNone(bon.date_livraison)
        self.assertEqual(bon.note, "Reception rapide.")
        self.assertEqual(bon.statut, BonLivraison.Statut.ANALYSE_EN_COURS)
        self.assertIsNone(bon.donnees_ia)
        self.assertIsNone(bon.confiance_ia)
        self.assertEqual(bon.lignes.count(), 0)
        self.assertTrue(bon.fichier.name.startswith(f"bons_livraison/{fournisseur.pk}/"))

    def test_background_delivery_note_analysis_updates_imported_document(self):
        restaurant = restaurant_fixture(nom="Restaurant BL Async")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Fournisseur BL Async")

        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            bon = BonLivraison.objects.create(
                fournisseur=fournisseur,
                fichier=upload_fixture("bon.png", b"\x89PNG\r\n\x1a\npng-content", content_type="image/png"),
                note="Reception rapide.",
                statut=BonLivraison.Statut.ANALYSE_EN_COURS,
            )

            with patch("core.views.analyze_delivery_note", return_value=sample_delivery_ai_data()):
                views.run_background_ai_analysis("bon_livraison", bon.pk)

        bon.refresh_from_db()
        self.assertEqual(bon.numero_bon_livraison, "BL-IA-001")
        self.assertEqual(bon.date_livraison, date(2026, 6, 12))
        self.assertEqual(bon.note, "Reception rapide.")
        self.assertEqual(bon.statut, BonLivraison.Statut.IMPORTE)
        self.assertIn("lignes", bon.donnees_ia)
        self.assertEqual(str(bon.confiance_ia), "0.78")
        self.assertEqual(bon.lignes.count(), 1)

    def test_invoice_upload_keeps_document_when_ai_analysis_fails(self):
        restaurant = restaurant_fixture(nom="Restaurant Erreur IA")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Fournisseur Erreur IA")
        upload = upload_fixture(
            "facture.pdf",
            b"%PDF-1.7\npdf-content",
            content_type="application/pdf",
        )

        self.client.force_login(self.gestionnaire_user)
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            with patch("core.views.enqueue_ai_analysis"):
                response = self.client.post(
                    reverse("fournisseur_facture_add", args=[fournisseur.pk]),
                    {
                        "fichier": upload,
                        "note": "",
                    },
                    follow=True,
                )
            facture = Facture.objects.get(fournisseur=fournisseur)
            with patch(
                "core.views.apply_invoice_ai_analysis",
                side_effect=DocumentAIError("boom"),
            ):
                with self.assertLogs("core.views", level="ERROR"):
                    views.run_background_ai_analysis("facture", facture.pk)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Document importé. Analyse IA en cours...")
        facture.refresh_from_db()
        self.assertEqual(facture.statut, Facture.Statut.ANALYSE_ECHOUEE)
        self.assertEqual(facture.erreur_analyse, "boom")
        self.assertIsNone(facture.donnees_ia)
        self.assertIsNone(facture.confiance_ia)
        self.assertTrue(facture.fichier.name.startswith(f"factures/{fournisseur.pk}/"))

    def test_invoice_upload_keeps_document_when_openai_is_not_configured(self):
        restaurant = restaurant_fixture(nom="Restaurant Config IA")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Fournisseur Config IA")
        upload = upload_fixture(
            "facture.pdf",
            b"%PDF-1.7\npdf-content",
            content_type="application/pdf",
        )

        self.client.force_login(self.gestionnaire_user)
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            with patch("core.views.enqueue_ai_analysis"):
                response = self.client.post(
                    reverse("fournisseur_facture_add", args=[fournisseur.pk]),
                    {
                        "fichier": upload,
                        "note": "",
                    },
                    follow=True,
                )
            facture = Facture.objects.get(fournisseur=fournisseur)
            with patch(
                "core.views.apply_invoice_ai_analysis",
                side_effect=DocumentAIConfigurationError("missing key"),
            ):
                views.run_background_ai_analysis("facture", facture.pk)

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "Document importé. Analyse IA en cours...",
        )
        facture.refresh_from_db()
        self.assertEqual(facture.statut, Facture.Statut.ANALYSE_ECHOUEE)
        self.assertEqual(facture.erreur_analyse, "missing key")
        self.assertIsNone(facture.donnees_ia)
        self.assertIsNone(facture.confiance_ia)

    def test_gestionnaire_legacy_upload_keeps_delivery_note_when_ai_analysis_fails(self):
        restaurant = restaurant_fixture(nom="Restaurant Reception Erreur IA")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Livreur Erreur IA")
        upload = upload_fixture(
            "bon.pdf",
            b"%PDF-1.7\npdf-content",
            content_type="application/pdf",
        )

        self.client.force_login(self.gestionnaire_user)
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            with patch("core.views.enqueue_ai_analysis"):
                response = self.client.post(
                    reverse("bon_livraison_add"),
                    {
                        "restaurant": restaurant.pk,
                        "fournisseur": fournisseur.pk,
                        "fichier": upload,
                        "note": "",
                    },
                    follow=True,
                )
            bon = BonLivraison.objects.get(fournisseur=fournisseur)
            with patch(
                "core.views.apply_delivery_note_ai_analysis",
                side_effect=DocumentAIError("boom"),
            ):
                with self.assertLogs("core.views", level="ERROR"):
                    views.run_background_ai_analysis("reception_bon_livraison", bon.pk)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Document importé.")
        self.assertContains(response, "Document importé. Analyse IA en cours...")
        bon.refresh_from_db()
        self.assertEqual(bon.statut, BonLivraison.Statut.ANALYSE_ECHOUEE)
        self.assertEqual(bon.erreur_analyse, "boom")
        self.assertIsNone(bon.donnees_ia)
        self.assertIsNone(bon.confiance_ia)
        self.assertTrue(bon.fichier.name.startswith(f"bons_livraison/{fournisseur.pk}/"))

    def test_delivery_note_upload_marks_failed_when_openai_is_not_configured(self):
        restaurant = restaurant_fixture(nom="Restaurant Config BL")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Livreur Config BL")
        upload = upload_fixture(
            "bon.pdf",
            b"%PDF-1.7\npdf-content",
            content_type="application/pdf",
        )

        self.client.force_login(self.gestionnaire_user)
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            with patch("core.views.enqueue_ai_analysis"):
                response = self.client.post(
                    reverse("fournisseur_bon_livraison_add", args=[fournisseur.pk]),
                    {
                        "fichier": upload,
                        "note": "",
                    },
                    follow=True,
                )
            bon = BonLivraison.objects.get(fournisseur=fournisseur)
            with patch(
                "core.views.apply_delivery_note_ai_analysis",
                side_effect=DocumentAIConfigurationError("missing key"),
            ):
                views.run_background_ai_analysis("bon_livraison", bon.pk)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Document importé. Analyse IA en cours...")
        bon.refresh_from_db()
        self.assertEqual(bon.statut, BonLivraison.Statut.ANALYSE_ECHOUEE)
        self.assertEqual(bon.erreur_analyse, "missing key")
        self.assertTrue(bon.fichier.name.startswith(f"bons_livraison/{fournisseur.pk}/"))

    def test_upload_rejects_unsupported_file_type(self):
        restaurant = restaurant_fixture(nom="Restaurant Type")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Fournisseur Type")
        upload = upload_fixture(
            "facture.txt",
            b"text-content",
            content_type="text/plain",
        )

        self.client.force_login(self.gestionnaire_user)
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            response = self.client.post(
                reverse("fournisseur_facture_add", args=[fournisseur.pk]),
                {
                    "fichier": upload,
                    "note": "",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Seuls les fichiers PDF, JPG, JPEG ou PNG sont acceptés.")
        self.assertEqual(Facture.objects.count(), 0)

    def test_gestionnaire_legacy_rejects_unsupported_file_type(self):
        restaurant = restaurant_fixture(nom="Restaurant Reception Type")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Livreur Type")
        upload = upload_fixture(
            "bon.txt",
            b"text-content",
            content_type="text/plain",
        )

        self.client.force_login(self.gestionnaire_user)
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            response = self.client.post(
                reverse("bon_livraison_add"),
                {
                    "restaurant": restaurant.pk,
                    "fournisseur": fournisseur.pk,
                    "fichier": upload,
                    "note": "",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Seuls les fichiers PDF, JPG, JPEG ou PNG sont acceptés.")
        self.assertEqual(BonLivraison.objects.count(), 0)

    def test_gestionnaire_can_view_and_download_invoice_file(self):
        restaurant = restaurant_fixture(nom="Restaurant Fichier Facture")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Fournisseur Fichier")

        self.client.force_login(self.gestionnaire_user)
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            facture = invoice_fixture(
                fournisseur=fournisseur,
                fichier=upload_fixture(
                    "facture.pdf",
                    b"%PDF-1.7\npdf-content",
                    content_type="application/pdf",
                ),
            )
            url = reverse("facture_file", args=[facture.pk])

            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response["Content-Type"], "application/pdf")
            self.assertIn("inline", response["Content-Disposition"])
            self.assertEqual(response["X-Frame-Options"], "SAMEORIGIN")
            self.assertEqual(b"".join(response.streaming_content), b"%PDF-1.7\npdf-content")

            response = self.client.get(f"{url}?download=1")
            self.assertEqual(response.status_code, 200)
            self.assertIn("attachment", response["Content-Disposition"])

            response = self.client.get(reverse("facture_detail", args=[facture.pk]))
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, url)
            self.assertContains(response, f'<iframe\n      src="{url}"')
            self.assertContains(response, "Ouvrir dans un nouvel onglet")
            self.assertContains(response, f"{url}?download=1")
            self.assertNotContains(response, f"/media/{facture.fichier.name}")

    def test_admin_can_view_delivery_note_file(self):
        restaurant = restaurant_fixture(nom="Restaurant Fichier BL")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Livreur Fichier")

        self.client.force_login(self.admin_user)
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            bon = BonLivraison.objects.create(
                fournisseur=fournisseur,
                fichier=upload_fixture(
                    "bon.png",
                    b"\x89PNG\r\n\x1a\npng-content",
                    content_type="image/png",
                ),
            )

            response = self.client.get(reverse("bon_livraison_file", args=[bon.pk]))
            detail_response = self.client.get(reverse("bon_livraison_detail", args=[bon.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/png")
        self.assertEqual(response["X-Frame-Options"], "SAMEORIGIN")
        self.assertEqual(b"".join(response.streaming_content), b"\x89PNG\r\n\x1a\npng-content")
        self.assertEqual(detail_response.status_code, 200)
        file_url = reverse("bon_livraison_file", args=[bon.pk])
        self.assertContains(detail_response, file_url)
        self.assertContains(detail_response, f'<img src="{file_url}"')
        self.assertContains(detail_response, "Ouvrir dans un nouvel onglet")
        self.assertNotContains(detail_response, f"/media/{bon.fichier.name}")

    def test_unsupported_document_preview_keeps_download_only(self):
        restaurant = restaurant_fixture(nom="Restaurant Apercu")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Fournisseur Apercu")

        self.client.force_login(self.gestionnaire_user)
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            facture = invoice_fixture(
                fournisseur=fournisseur,
                fichier=upload_fixture(
                    "facture.txt",
                    b"text-content",
                    content_type="text/plain",
                ),
            )
            file_url = reverse("facture_file", args=[facture.pk])

            response = self.client.get(reverse("facture_detail", args=[facture.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Aperçu non disponible pour ce type de fichier.")
        self.assertContains(response, f"{file_url}?download=1")
        self.assertNotContains(response, "Ouvrir dans un nouvel onglet")
        self.assertNotContains(response, "<iframe")
        self.assertNotContains(response, 'class="document-preview-image"')

    def test_document_detail_shows_ai_summary_without_raw_json_to_gestionnaire(self):
        restaurant = restaurant_fixture(nom="Restaurant Resume IA")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Fournisseur Resume IA")
        facture = invoice_fixture(
            fournisseur=fournisseur,
            fichier="factures/1/facture-ia.pdf",
            donnees_ia={
                "champs_incertains": ["montant_tva"],
                "commentaire_analyse": "TVA peu lisible.",
            },
            confiance_ia="0.72",
        )

        self.client.force_login(self.gestionnaire_user)
        response = self.client.get(reverse("facture_detail", args=[facture.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Analyse automatique")
        self.assertContains(response, "Confiance IA")
        self.assertContains(response, "0,72")
        self.assertContains(response, "montant_tva")
        self.assertContains(response, "TVA peu lisible.")
        self.assertNotContains(response, "Voir les données IA brutes")

    def test_invoice_detail_can_relaunch_ai_analysis_without_overwriting_manual_fields(self):
        restaurant = restaurant_fixture(nom="Restaurant Relance Facture")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Fournisseur Relance Facture")
        ai_data = sample_invoice_ai_data(
            numero_facture="FAC-IA-RELANCE",
            date_facture="2026-06-10",
            lignes=[],
            confiance_ia=0.93,
            champs_incertains=["montant_tva"],
            commentaire_analyse="Relance lisible.",
        )

        self.client.force_login(self.gestionnaire_user)
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            facture = invoice_fixture(
                fournisseur=fournisseur,
                fichier=upload_fixture(
                    "facture.pdf",
                    b"%PDF-1.7\npdf-content",
                    content_type="application/pdf",
                ),
                numero_facture="FAC-MANUELLE",
                date_facture=date(2026, 1, 5),
                montant_ttc="99.00",
                statut=Facture.Statut.IMPORTEE,
            )

            response = self.client.get(reverse("facture_detail", args=[facture.pk]))
            self.assertContains(response, "Relancer l’analyse IA")
            self.assertContains(response, reverse("facture_ai_reanalyze", args=[facture.pk]))
            self.assertContains(response, "Relancer l’analyse IA de ce document ?")

            with patch("core.views.analyze_invoice", return_value=ai_data):
                response = self.client.post(
                    reverse("facture_ai_reanalyze", args=[facture.pk]),
                    follow=True,
                )
                from .services.jobs import process_next
                process_next()
                response = self.client.get(response.request['PATH_INFO'])

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Terminée")
        facture.refresh_from_db()
        self.assertEqual(facture.numero_facture, "FAC-MANUELLE")
        self.assertEqual(facture.date_facture, date(2026, 1, 5))
        self.assertEqual(facture.date_livraison_detectee, date(2026, 6, 11))
        self.assertEqual(str(facture.montant_ttc), "99.00")
        self.assertEqual(str(facture.montant_ht), "100.00")
        self.assertEqual(str(facture.montant_tva), "20.00")
        self.assertEqual(facture.statut, Facture.Statut.A_VALIDER)
        self.assertEqual(facture.donnees_ia["numero_facture"], "FAC-IA-RELANCE")
        self.assertEqual(str(facture.confiance_ia), "0.93")

    def test_invoice_reanalysis_proposes_changes_without_replacing_lines(self):
        restaurant = restaurant_fixture(nom="Restaurant Relance Lignes")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Fournisseur Relance Lignes")
        ai_data = sample_invoice_ai_data(
            lignes=[
                {
                    "reference_produit": "NEW-AI",
                    "designation": "Produit relu par IA",
                    "quantite": "3",
                    "unite": "kg",
                    "prix_unitaire_ht": "4.00",
                    "montant_ht": "12.00",
                    "taux_tva": "5.50",
                    "montant_ttc": "12.66",
                }
            ],
            confiance_ia=0.9,
        )

        self.client.force_login(self.gestionnaire_user)
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            facture = invoice_fixture(
                fournisseur=fournisseur,
                fichier=upload_fixture("facture.pdf", b"%PDF-1.7\npdf-content"),
                statut=Facture.Statut.IMPORTEE,
            )
            manual_line = LigneFacture.objects.create(
                facture=facture,
                reference_produit="MANUAL",
                designation="Produit corrigé",
                montant_ht="20.00",
                corrigee_manuellement=True,
                ordre=0,
            )
            old_ai_line = LigneFacture.objects.create(
                facture=facture,
                reference_produit="OLD-AI",
                designation="Ancienne ligne IA",
                montant_ht="10.00",
                corrigee_manuellement=False,
                ordre=1,
            )

            with patch("core.views.analyze_invoice", return_value=ai_data):
                response = self.client.post(
                    reverse("facture_ai_reanalyze", args=[facture.pk]),
                    follow=True,
                )
                from .services.jobs import process_next
                process_next()
                response = self.client.get(response.request['PATH_INFO'])

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Terminée")
        self.assertTrue(LigneFacture.objects.filter(pk=manual_line.pk).exists())
        self.assertTrue(LigneFacture.objects.filter(pk=old_ai_line.pk).exists())

        self.assertFalse(LigneFacture.objects.filter(facture=facture, reference_produit="NEW-AI").exists())
        self.assertTrue(AnalysisVersion.objects.filter(document_type="facture",document_id=facture.pk,applied=False).exists())

    def test_delivery_note_detail_can_relaunch_ai_analysis_without_overwriting_manual_fields(self):
        restaurant = restaurant_fixture(nom="Restaurant Relance BL")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Fournisseur Relance BL")
        ai_data = sample_delivery_ai_data(
            numero_bl="BL-IA-RELANCE",
            date_livraison="2026-06-11",
            lignes=[],
            confiance_ia=0.87,
            commentaire_analyse="Bon lisible.",
        )

        self.client.force_login(self.admin_user)
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            bon = BonLivraison.objects.create(
                fournisseur=fournisseur,
                fichier=upload_fixture(
                    "bon.png",
                    b"\x89PNG\r\n\x1a\npng-content",
                    content_type="image/png",
                ),
                numero_bon_livraison="BL-MANUEL",
                statut=BonLivraison.Statut.IMPORTE,
            )

            response = self.client.get(reverse("bon_livraison_detail", args=[bon.pk]))
            self.assertContains(response, "Relancer l’analyse IA")
            self.assertContains(response, reverse("bon_livraison_ai_reanalyze", args=[bon.pk]))

            with patch("core.views.analyze_delivery_note", return_value=ai_data):
                response = self.client.post(
                    reverse("bon_livraison_ai_reanalyze", args=[bon.pk]),
                    follow=True,
                )
                from .services.jobs import process_next
                process_next()
                response = self.client.get(response.request['PATH_INFO'])

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Terminée")
        bon.refresh_from_db()
        self.assertEqual(bon.numero_bon_livraison, "BL-MANUEL")
        self.assertEqual(bon.date_livraison, date(2026, 6, 11))
        self.assertEqual(bon.statut, BonLivraison.Statut.A_VERIFIER)
        self.assertEqual(bon.donnees_ia["numero_bl"], "BL-IA-RELANCE")
        self.assertEqual(str(bon.confiance_ia), "0.87")

    def test_ai_relaunch_keeps_litige_status(self):
        restaurant = restaurant_fixture(nom="Restaurant Litige Relance")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Fournisseur Litige Relance")
        ai_data = sample_invoice_ai_data(
            numero_facture="FAC-LITIGE-IA",
            date_facture="2026-06-10",
            lignes=[],
            confiance_ia=0.91,
            commentaire_analyse="Document relu.",
        )

        self.client.force_login(self.gestionnaire_user)
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            facture = invoice_fixture(
                fournisseur=fournisseur,
                fichier=upload_fixture("facture.pdf", b"%PDF-1.7\npdf-content"),
                statut=Facture.Statut.LITIGE,
                note="Prix contesté.",
            )

            with patch("core.views.analyze_invoice", return_value=ai_data):
                response = self.client.post(
                    reverse("facture_ai_reanalyze", args=[facture.pk]),
                    follow=True,
                )
                from .services.jobs import process_next
                process_next()
                response = self.client.get(response.request['PATH_INFO'])

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Terminée")
        facture.refresh_from_db()
        self.assertEqual(facture.statut, Facture.Statut.LITIGE)
        self.assertEqual(facture.note, "Prix contesté.")
        self.assertEqual(facture.numero_facture, "FAC-LITIGE-IA")
        self.assertEqual(str(facture.montant_ttc), "120.00")

    def test_ai_relaunch_failure_keeps_document_data(self):
        restaurant = restaurant_fixture(nom="Restaurant Erreur Relance")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Fournisseur Erreur Relance")

        self.client.force_login(self.gestionnaire_user)
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            facture = invoice_fixture(
                fournisseur=fournisseur,
                fichier=upload_fixture("facture.pdf", b"%PDF-1.7\npdf-content"),
                statut=Facture.Statut.IMPORTEE,
                donnees_ia={"commentaire_analyse": "Ancienne analyse."},
                confiance_ia="0.11",
            )

            with patch(
                "core.views.apply_invoice_ai_analysis",
                side_effect=DocumentAIError("boom"),
            ):
                with self.assertLogs("core.services.jobs", level="ERROR"):
                    response = self.client.post(
                        reverse("facture_ai_reanalyze", args=[facture.pk]),
                        follow=True,
                    )
                    from .services.jobs import process_next
                    process_next()
                    response = self.client.get(response.request['PATH_INFO'])

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "L’analyse IA a échoué. Le document est conservé.")
        facture.refresh_from_db()
        self.assertEqual(facture.statut, Facture.Statut.ANALYSE_ECHOUEE)
        self.assertEqual(facture.donnees_ia, {"commentaire_analyse": "Ancienne analyse."})
        self.assertEqual(str(facture.confiance_ia), "0.11")

    def test_reception_cannot_relaunch_ai_analysis(self):
        restaurant = restaurant_fixture(nom="Restaurant Relance Acces")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Fournisseur Relance Acces")
        facture = invoice_fixture(
            fournisseur=fournisseur,
            fichier="factures/1/relance.pdf",
        )
        bon = BonLivraison.objects.create(
            fournisseur=fournisseur,
            fichier="bons_livraison/1/relance.pdf",
        )

        urls = [
            reverse("facture_ai_reanalyze", args=[facture.pk]),
            reverse("bon_livraison_ai_reanalyze", args=[bon.pk]),
        ]
        for url in urls:
            with self.subTest(url=url, role="anonymous"):
                response = self.client.post(url)
                self.assertEqual(response.status_code, 302)
                self.assertIn(reverse("login"), response["Location"])

        self.client.force_login(self.reception_user)
        for url in urls:
            with self.subTest(url=url, role=ROLE_RECEPTION):
                response = self.client.post(url)
                self.assertEqual(response.status_code, 403)

    def test_admin_can_open_raw_ai_data_on_document_detail(self):
        restaurant = restaurant_fixture(nom="Restaurant Brut IA")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Fournisseur Brut IA")
        bon = BonLivraison.objects.create(
            fournisseur=fournisseur,
            fichier="bons_livraison/1/bon-ia.pdf",
            donnees_ia={
                "champs_incertains": ["date_livraison"],
                "commentaire_analyse": "Date difficile a lire.",
            },
            confiance_ia="0.64",
        )

        self.client.force_login(self.admin_user)
        response = self.client.get(reverse("bon_livraison_detail", args=[bon.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Analyse automatique")
        self.assertContains(response, "Voir les données IA brutes")
        self.assertContains(response, "date_livraison")

    def test_document_file_views_require_authorized_roles(self):
        restaurant = restaurant_fixture(nom="Restaurant Acces Fichier")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Fournisseur Acces Fichier")

        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            facture = invoice_fixture(
                fournisseur=fournisseur,
                fichier=upload_fixture("facture.pdf", b"%PDF-1.7\npdf-content"),
            )
            bon = BonLivraison.objects.create(
                fournisseur=fournisseur,
                fichier=upload_fixture("bon.pdf", b"%PDF-1.7\nbl-content"),
            )
            urls = [
                reverse("facture_file", args=[facture.pk]),
                reverse("bon_livraison_file", args=[bon.pk]),
            ]

            for url in urls:
                with self.subTest(url=url, role="anonymous"):
                    response = self.client.get(url)
                    self.assertEqual(response.status_code, 302)
                    self.assertIn(reverse("login"), response["Location"])

            self.client.force_login(self.reception_user)
            for url in urls:
                with self.subTest(url=url, role=ROLE_RECEPTION):
                    response = self.client.get(url)
                    self.assertEqual(response.status_code, 403)

    def test_missing_document_file_returns_404_and_detail_message(self):
        restaurant = restaurant_fixture(nom="Restaurant Fichier Manquant")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Fournisseur Fichier Manquant")

        self.client.force_login(self.gestionnaire_user)
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            facture = invoice_fixture(
                fournisseur=fournisseur,
                fichier="factures/999/facture-manquante.pdf",
            )
            bon = BonLivraison.objects.create(
                fournisseur=fournisseur,
                fichier="bons_livraison/999/bon-manquant.pdf",
            )

            response = self.client.get(reverse("facture_detail", args=[facture.pk]))
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "Le fichier associé à ce document est introuvable.")
            self.assertNotContains(response, "Ouvrir dans un nouvel onglet")
            self.assertNotContains(response, "Télécharger")

            response = self.client.get(reverse("bon_livraison_detail", args=[bon.pk]))
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "Le fichier associé à ce document est introuvable.")
            self.assertNotContains(response, "Ouvrir dans un nouvel onglet")
            self.assertNotContains(response, "Télécharger")

            response = self.client.get(reverse("facture_file", args=[facture.pk]))
            self.assertContains(
                response,
                "Le fichier associé à ce document est introuvable.",
                status_code=404,
            )

            response = self.client.get(reverse("bon_livraison_file", args=[bon.pk]))
            self.assertContains(
                response,
                "Le fichier associé à ce document est introuvable.",
                status_code=404,
            )

    def test_media_urls_are_not_served_publicly(self):
        restaurant = restaurant_fixture(nom="Restaurant Media Public")
        fournisseur = Fournisseur.objects.create(restaurant=restaurant, nom="Fournisseur Media Public")

        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            facture = invoice_fixture(
                fournisseur=fournisseur,
                fichier=upload_fixture("facture.pdf", b"%PDF-1.7\npdf-content"),
            )

            response = self.client.get(f"/media/{facture.fichier.name}")

        self.assertEqual(response.status_code, 404)


class DocumentAIServiceTests(TestCase):
    def test_openai_config_prefers_environment_over_keys_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json") as keys_file:
            json.dump(
                {
                    "OPENAI_API_KEY": "file-test-key",
                    "OPENAI_MODEL": "file-test-model",
                },
                keys_file,
            )
            keys_file.flush()

            with override_settings(DEBUG=True), patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "env-test-key",
                    "OPENAI_MODEL": "env-test-model",
                    "OPENAI_KEYS_FILE": keys_file.name,
                },
                clear=True,
            ):
                config = get_openai_config()

        self.assertEqual(config["api_key"], "env-test-key")
        self.assertEqual(config["model"], "env-test-model")

    def test_openai_config_reads_keys_file_in_debug(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json") as keys_file:
            json.dump(
                {
                    "OPENAI_API_KEY": "file-test-key",
                    "OPENAI_MODEL": "file-test-model",
                },
                keys_file,
            )
            keys_file.flush()

            with override_settings(DEBUG=True), patch.dict(
                os.environ,
                {"OPENAI_KEYS_FILE": keys_file.name},
                clear=True,
            ):
                config = get_openai_config()

        self.assertEqual(config["api_key"], "file-test-key")
        self.assertEqual(config["model"], "file-test-model")

    def test_openai_config_refuses_keys_file_when_debug_false(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json") as keys_file:
            json.dump(
                {
                    "OPENAI_API_KEY": "file-test-key",
                    "OPENAI_MODEL": "file-test-model",
                },
                keys_file,
            )
            keys_file.flush()

            with override_settings(DEBUG=False), patch.dict(
                os.environ,
                {"OPENAI_KEYS_FILE": keys_file.name},
                clear=True,
            ):
                with self.assertRaises(OpenAIConfigError) as raised:
                    get_openai_config()

        self.assertIn("OPENAI_API_KEY", str(raised.exception))

    def test_openai_config_missing_keys_file_has_clear_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            missing_file = os.path.join(temp_dir, "missing.keys.json")

            with override_settings(DEBUG=True), patch.dict(
                os.environ,
                {"OPENAI_KEYS_FILE": missing_file},
                clear=True,
            ):
                with self.assertRaises(OpenAIConfigError) as raised:
                    get_openai_config()

        self.assertIn("introuvable", str(raised.exception))

    def test_openai_config_invalid_json_has_clear_error(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json") as keys_file:
            keys_file.write("{invalid json")
            keys_file.flush()

            with override_settings(DEBUG=True), patch.dict(
                os.environ,
                {"OPENAI_KEYS_FILE": keys_file.name},
                clear=True,
            ):
                with self.assertRaises(OpenAIConfigError) as raised:
                    get_openai_config()

        self.assertIn("JSON de configuration OpenAI invalide", str(raised.exception))

    def test_openai_config_error_message_does_not_expose_keys_file_secret(self):
        secret_key = ("sk" + "-secret-hidden-from-errors")

        with tempfile.NamedTemporaryFile("w", suffix=".json") as keys_file:
            json.dump({"OPENAI_API_KEY": secret_key}, keys_file)
            keys_file.flush()

            with override_settings(DEBUG=True), patch.dict(
                os.environ,
                {"OPENAI_KEYS_FILE": keys_file.name},
                clear=True,
            ):
                with self.assertRaises(OpenAIConfigError) as raised:
                    get_openai_config()

        self.assertNotIn(secret_key, str(raised.exception))

    def test_openai_invoice_analysis_uses_base64_file_and_strict_json_schema(self):
        openai_output = {
            "type_document": "facture",
            "numero_facture": "FAC-IA-001",
            "date_facture": "2026-06-10",
            "date_livraison_detectee": "2026-06-09",
            "montant_ht": "100.00",
            "montant_tva": "20.00",
            "montant_ttc": "120.00",
            "devise": "EUR",
            "fournisseur_detecte": "Fournisseur IA",
            "lignes": [
                {
                    "reference_produit": "REF-IA-001",
                    "ean": "7612345678999",
                    "designation": "Produit IA",
                    "quantite": "2",
                    "unite": "kg",
                    "prix_unitaire_ht": "50.00",
                    "montant_ht": "100.00",
                    "taux_tva": "20.00",
                    "montant_ttc": "120.00",
                }
            ],
            "confiance_ia": 0.91,
            "champs_incertains": ["montant_tva"],
            "commentaire_analyse": "Facture lisible.",
        }

        class FakeResponses:
            def __init__(self):
                self.calls = []

            def create(self, **kwargs):
                self.calls.append(kwargs)
                return type("FakeResponse", (), {"output_text": json.dumps(openai_output)})()

        class FakeClient:
            def __init__(self):
                self.responses = FakeResponses()

        fake_client = FakeClient()

        with tempfile.NamedTemporaryFile(suffix=".pdf") as document_file:
            document_file.write(b"%PDF-1.7\npdf-content")
            document_file.flush()

            with patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "test-key",
                    "OPENAI_MODEL": "test-model",
                },
            ):
                with patch("core.services.document_ai._create_openai_client", return_value=fake_client):
                    result = document_ai.analyze_invoice(document_file.name)

        self.assertEqual(result["numero_facture"], "FAC-IA-001")
        self.assertEqual(result["confiance_ia"], 0.91)
        call = fake_client.responses.calls[0]
        self.assertEqual(call["model"], "test-model")
        self.assertGreaterEqual(call["max_output_tokens"], 12000)
        self.assertEqual(call["text"]["format"]["type"], "json_schema")
        self.assertTrue(call["text"]["format"]["strict"])
        line_schema = call["text"]["format"]["schema"]["properties"]["lignes"]["items"]
        invoice_schema = call["text"]["format"]["schema"]
        self.assertIn("date_livraison_detectee", invoice_schema["properties"])
        self.assertIn("date_livraison_detectee", invoice_schema["required"])
        self.assertIn("reference_produit", line_schema["properties"])
        self.assertIn("ean", line_schema["properties"])
        self.assertIn("prix_unitaire_ht", line_schema["properties"])
        self.assertIn("taux_tva", line_schema["properties"])
        self.assertIn("reference_produit", line_schema["required"])
        self.assertIn("ean", line_schema["required"])
        self.assertNotIn("file_url", call["input"][1]["content"][0])
        self.assertEqual(call["input"][1]["content"][0]["type"], "input_file")
        self.assertTrue(call["input"][1]["content"][0]["file_data"].startswith("data:application/pdf;base64,"))

    def test_openai_catalogue_analysis_uses_base64_file_and_strict_json_schema(self):
        openai_output = sample_catalogue_ai_data()

        class FakeResponses:
            def __init__(self):
                self.calls = []

            def create(self, **kwargs):
                self.calls.append(kwargs)
                return type("FakeResponse", (), {"output_text": json.dumps(openai_output)})()

        class FakeClient:
            def __init__(self):
                self.responses = FakeResponses()

        fake_client = FakeClient()

        with tempfile.NamedTemporaryFile(suffix=".pdf") as document_file:
            document_file.write(b"%PDF-1.7\ncatalogue")
            document_file.flush()

            with patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "test-key",
                    "OPENAI_MODEL": "test-model",
                },
            ):
                with patch("core.services.document_ai._create_openai_client", return_value=fake_client):
                    result = catalogue_ai.analyze_supplier_catalog(document_file.name)

        self.assertEqual(result["nom_catalogue"], "Catalogue IA 2026")
        self.assertEqual(result["confiance_ia"], 0.84)
        call = fake_client.responses.calls[0]
        self.assertEqual(call["model"], "test-model")
        self.assertGreaterEqual(call["max_output_tokens"], 16000)
        self.assertEqual(call["text"]["format"]["type"], "json_schema")
        self.assertTrue(call["text"]["format"]["strict"])
        schema_properties = call["text"]["format"]["schema"]["properties"]
        self.assertNotIn("prix_hors_taxe", schema_properties)
        self.assertNotIn("type_prix", schema_properties)
        self.assertIn("lignes", schema_properties)
        line_schema = schema_properties["lignes"]["items"]
        self.assertIn("ean", line_schema["properties"])
        self.assertIn("prix_unitaire_ht", line_schema["properties"])
        self.assertNotIn("prix_net", line_schema["properties"])
        self.assertNotIn("prix_brut", line_schema["properties"])
        self.assertIn("reference_produit", line_schema["required"])
        self.assertNotIn("file_url", call["input"][1]["content"][0])
        self.assertEqual(call["input"][1]["content"][0]["type"], "input_file")
        self.assertTrue(call["input"][1]["content"][0]["file_data"].startswith("data:application/pdf;base64,"))

    def test_tabular_catalogue_analysis_keeps_all_csv_lines_from_real_file(self):
        openai_output = sample_catalogue_ai_data(
            lignes=[
                {
                    "reference_produit": f"AI-{index:03d}",
                    "ean": "",
                    "designation": f"Produit IA {index}",
                    "marque": "",
                    "unite": "",
                    "conditionnement": "",
                    "prix_unitaire_ht": "1.00",
                }
                for index in range(1, 23)
            ]
        )

        class FakeResponses:
            def __init__(self):
                self.calls = []

            def create(self, **kwargs):
                self.calls.append(kwargs)
                return type("FakeResponse", (), {"output_text": json.dumps(openai_output)})()

        class FakeClient:
            def __init__(self):
                self.responses = FakeResponses()

        csv_lines = ["reference;designation;prix ht"]
        csv_lines.extend(
            f"CAT-{index:03d};Produit catalogue {index};{index},50"
            for index in range(1, 31)
        )
        fake_client = FakeClient()

        with tempfile.NamedTemporaryFile(suffix=".csv") as document_file:
            document_file.write("\n".join(csv_lines).encode("utf-8"))
            document_file.flush()

            with patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "test-key",
                    "OPENAI_MODEL": "test-model",
                },
            ):
                with patch("core.services.document_ai._create_openai_client", return_value=fake_client):
                    result = catalogue_ai.analyze_supplier_catalog(document_file.name)

        self.assertEqual(len(result["lignes"]), 30)
        self.assertEqual(result["lignes"][0]["reference_produit"], "CAT-001")
        self.assertEqual(result["lignes"][-1]["reference_produit"], "CAT-030")
        self.assertEqual(result["lignes"][-1]["designation"], "Produit catalogue 30")
        self.assertEqual(result["lignes"][-1]["prix_unitaire_ht"], "30,50")
        self.assertIn("Import local du tableau", result["commentaire_analyse"])
        self.assertEqual(fake_client.responses.calls, [])

    def test_tabular_catalogue_analysis_reads_xlsx_lines_without_openpyxl(self):
        openai_output = sample_catalogue_ai_data(lignes=[])

        class FakeResponses:
            def __init__(self):
                self.calls = []

            def create(self, **kwargs):
                self.calls.append(kwargs)
                return type("FakeResponse", (), {"output_text": json.dumps(openai_output)})()

        class FakeClient:
            def __init__(self):
                self.responses = FakeResponses()

        sheet_rows = [
            [("A1", "reference"), ("B1", "designation"), ("C1", "prix ht")],
            [("A2", "XLS-001"), ("B2", "Produit tableur 1"), ("C2", "10.10")],
            [("A3", "XLS-002"), ("B3", "Produit tableur 2"), ("C3", "20.20")],
        ]
        sheet_rows_xml = "".join(
            "<row>"
            + "".join(
                f'<c r="{cell_ref}" t="inlineStr"><is><t>{value}</t></is></c>'
                for cell_ref, value in row
            )
            + "</row>"
            for row in sheet_rows
        )
        sheet_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"<sheetData>{sheet_rows_xml}</sheetData>"
            "</worksheet>"
        )
        workbook_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Catalogue" sheetId="1" r:id="rId1"/></sheets>'
            "</workbook>"
        )
        rels_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            'Target="worksheets/sheet1.xml"/>'
            "</Relationships>"
        )
        fake_client = FakeClient()

        with tempfile.NamedTemporaryFile(suffix=".xlsx") as document_file:
            import zipfile

            with zipfile.ZipFile(document_file.name, "w") as archive:
                archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
                archive.writestr("xl/workbook.xml", workbook_xml)
                archive.writestr("xl/_rels/workbook.xml.rels", rels_xml)
                archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)

            with patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "test-key",
                    "OPENAI_MODEL": "test-model",
                },
            ):
                with patch("core.services.document_ai._create_openai_client", return_value=fake_client):
                    result = catalogue_ai.analyze_supplier_catalog(document_file.name)

        self.assertEqual(len(result["lignes"]), 2)
        self.assertEqual(result["lignes"][0]["reference_produit"], "XLS-001")
        self.assertEqual(result["lignes"][1]["designation"], "Produit tableur 2")
        self.assertEqual(result["lignes"][1]["prix_unitaire_ht"], "20.20")

    def test_tabular_catalogue_analysis_reads_xslx_extension_as_xlsx(self):
        sheet_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetData>'
            '<row><c r="A1" t="inlineStr"><is><t>reference</t></is></c>'
            '<c r="B1" t="inlineStr"><is><t>designation</t></is></c>'
            '<c r="C1" t="inlineStr"><is><t>prix</t></is></c></row>'
            '<row><c r="A2" t="inlineStr"><is><t>XSLX-001</t></is></c>'
            '<c r="B2" t="inlineStr"><is><t>Produit extension xslx</t></is></c>'
            '<c r="C2" t="inlineStr"><is><t>12.30</t></is></c></row>'
            '</sheetData>'
            '</worksheet>'
        )
        workbook_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Catalogue" sheetId="1" r:id="rId1"/></sheets>'
            "</workbook>"
        )
        rels_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            'Target="worksheets/sheet1.xml"/>'
            "</Relationships>"
        )

        with tempfile.NamedTemporaryFile(suffix=".xslx") as document_file:
            import zipfile

            with zipfile.ZipFile(document_file.name, "w") as archive:
                archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
                archive.writestr("xl/workbook.xml", workbook_xml)
                archive.writestr("xl/_rels/workbook.xml.rels", rels_xml)
                archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)

            lines = catalogue_ai._extract_tabular_catalogue_lines(Path(document_file.name))

        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["reference_produit"], "XSLX-001")
        self.assertEqual(lines[0]["designation"], "Produit extension xslx")
        self.assertEqual(lines[0]["prix_unitaire_ht"], "12.30")

    def test_openai_delivery_note_analysis_uses_base64_image_input_for_images(self):
        openai_output = {
            "type_document": "bon_livraison",
            "delivery_note": {
                "number": "BL-IA-001",
                "date": "2026-06-10",
                "supplier_name": "Fournisseur IA",
                "restaurant_name": "Restaurant IA",
                "delivery_address": None,
                "currency": None,
                "confidence": 0.88,
                "warnings": [],
            },
            "lines": [
                {
                    "product_reference": None,
                    "product_name": "Produit IA",
                    "quantity": "2",
                    "unit": "kg",
                    "packaging": None,
                    "unit_price": None,
                    "line_total": None,
                    "confidence": 0.86,
                    "raw_text": "Produit IA 2 kg",
                }
            ],
        }

        class FakeResponses:
            def __init__(self):
                self.calls = []

            def create(self, **kwargs):
                self.calls.append(kwargs)
                return type("FakeResponse", (), {"output_text": json.dumps(openai_output)})()

        class FakeClient:
            def __init__(self):
                self.responses = FakeResponses()

        fake_client = FakeClient()

        with tempfile.NamedTemporaryFile(suffix=".png") as document_file:
            document_file.write(b"\x89PNG\r\n\x1a\npng-content")
            document_file.flush()

            with patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "test-key",
                    "OPENAI_MODEL": "test-model",
                },
            ):
                with patch("core.services.document_ai._create_openai_client", return_value=fake_client):
                    result = document_ai.analyze_delivery_note(document_file.name)

        self.assertEqual(result["numero_bl"], "BL-IA-001")
        call = fake_client.responses.calls[0]
        content_item = call["input"][1]["content"][0]
        self.assertEqual(content_item["type"], "input_image")
        self.assertTrue(content_item["image_url"].startswith("data:image/png;base64,"))
        self.assertNotIn("file_url", content_item)
        self.assertNotIn("file_data", content_item)

    def test_openai_delivery_note_analysis_repairs_wrapped_json_output(self):
        openai_output = {
            "type_document": "bon_livraison",
            "delivery_note": {
                "number": None,
                "date": "2026-06-10",
                "supplier_name": "Fournisseur IA",
                "restaurant_name": None,
                "delivery_address": None,
                "currency": None,
                "confidence": 0.7,
                "warnings": ["numero_absent"],
            },
            "lines": [
                {
                    "product_reference": None,
                    "product_name": "Produit sans prix",
                    "quantity": "1.5",
                    "unit": "kg",
                    "packaging": None,
                    "unit_price": None,
                    "line_total": None,
                    "confidence": 0.72,
                    "raw_text": "Produit sans prix 1.5 kg",
                }
            ],
        }

        class FakeResponses:
            def create(self, **kwargs):
                return type(
                    "FakeResponse",
                    (),
                    {"output_text": f"Voici le JSON:\n{json.dumps(openai_output)}\nFin."},
                )()

        class FakeClient:
            responses = FakeResponses()

        with tempfile.NamedTemporaryFile(suffix=".pdf") as document_file:
            document_file.write(b"%PDF-1.7\npdf-content")
            document_file.flush()

            with patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "test-key",
                    "OPENAI_MODEL": "test-model",
                },
            ):
                with patch("core.services.document_ai._create_openai_client", return_value=FakeClient()):
                    result = document_ai.analyze_delivery_note(document_file.name)

        self.assertEqual(result["date_livraison"], "2026-06-10")
        self.assertEqual(result["numero_bl"], "")
        self.assertEqual(result["lines"][0]["product_name"], "Produit sans prix")

    def test_openai_delivery_note_analysis_rejects_invalid_json_shape(self):
        class FakeResponses:
            def create(self, **kwargs):
                return type("FakeResponse", (), {"output_text": json.dumps({"numero_bl": "BL-IA-001"})})()

        class FakeClient:
            responses = FakeResponses()

        with tempfile.NamedTemporaryFile(suffix=".png") as document_file:
            document_file.write(b"\x89PNG\r\n\x1a\npng-content")
            document_file.flush()

            with patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "test-key",
                    "OPENAI_MODEL": "test-model",
                },
            ):
                with patch("core.services.document_ai._create_openai_client", return_value=FakeClient()):
                    with self.assertRaises(DocumentAIError):
                        document_ai.analyze_delivery_note(document_file.name)

    def test_openai_analysis_reports_incomplete_response_clearly(self):
        class FakeResponses:
            def create(self, **kwargs):
                return type(
                    "FakeResponse",
                    (),
                    {
                        "status": "incomplete",
                        "incomplete_details": {"reason": "max_output_tokens"},
                        "output_text": '{"type_document": "facture"',
                    },
                )()

        class FakeClient:
            responses = FakeResponses()

        with tempfile.NamedTemporaryFile(suffix=".pdf") as document_file:
            document_file.write(b"%PDF-1.7\npdf-content")
            document_file.flush()

            with patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "test-key",
                    "OPENAI_MODEL": "test-model",
                },
            ):
                with patch("core.services.document_ai._create_openai_client", return_value=FakeClient()):
                    with self.assertRaisesMessage(
                        DocumentAIError,
                        "La reponse OpenAI est incomplete (max_output_tokens).",
                    ):
                        document_ai.analyze_invoice(document_file.name)

    def test_openai_analysis_requires_api_key(self):
        with tempfile.NamedTemporaryFile(suffix=".pdf") as document_file:
            document_file.write(b"%PDF-1.7\npdf-content")
            document_file.flush()

            with patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "",
                    "OPENAI_MODEL": "test-model",
                },
            ):
                with self.assertRaises(DocumentAIConfigurationError):
                    document_ai.analyze_invoice(document_file.name)

    def test_openai_analysis_requires_model(self):
        with tempfile.NamedTemporaryFile(suffix=".pdf") as document_file:
            document_file.write(b"%PDF-1.7\npdf-content")
            document_file.flush()

            with patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "test-key",
                    "OPENAI_MODEL": "",
                },
            ):
                with patch("core.services.document_ai._create_openai_client"):
                    with self.assertRaises(DocumentAIConfigurationError):
                        document_ai.analyze_invoice(document_file.name)

    def test_ai_invoice_command_prints_real_service_json(self):
        result = sample_invoice_ai_data(numero_facture="FAC-CMD-001")
        stdout = StringIO()

        with patch("core.management.commands.test_ai_invoice.analyze_invoice", return_value=result):
            call_command("test_ai_invoice", "/app/media/factures/test.pdf", stdout=stdout)

        output = json.loads(stdout.getvalue())
        self.assertEqual(output["type_document"], "facture")
        self.assertEqual(output["numero_facture"], "FAC-CMD-001")

    def test_ai_delivery_command_prints_real_service_json(self):
        result = sample_delivery_ai_data(numero_bl="BL-CMD-001")
        stdout = StringIO()

        with patch("core.management.commands.test_ai_delivery.analyze_delivery_note", return_value=result):
            call_command("test_ai_delivery", "/app/media/bons_livraison/test.pdf", stdout=stdout)

        output = json.loads(stdout.getvalue())
        self.assertEqual(output["type_document"], "bon_livraison")
        self.assertEqual(output["numero_bl"], "BL-CMD-001")

    def test_ai_catalogue_command_prints_real_service_json(self):
        result = sample_catalogue_ai_data(nom_catalogue="Catalogue CMD")
        stdout = StringIO()

        with patch("core.management.commands.test_ai_catalogue.analyze_supplier_catalog", return_value=result):
            call_command("test_ai_catalogue", "/app/media/catalogues/test.pdf", stdout=stdout)

        output = json.loads(stdout.getvalue())
        self.assertEqual(output["nom_catalogue"], "Catalogue CMD")
        self.assertEqual(output["lignes"][0]["reference_produit"], "CAT-001")

    def test_ai_invoice_command_reports_service_error(self):
        with patch(
            "core.management.commands.test_ai_invoice.analyze_invoice",
            side_effect=DocumentAIConfigurationError("OPENAI_API_KEY n'est pas defini."),
        ):
            with self.assertRaises(CommandError):
                call_command("test_ai_invoice", "/app/media/factures/test.pdf")
