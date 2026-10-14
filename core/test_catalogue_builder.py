from datetime import date
from decimal import Decimal
from io import StringIO
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from .models import (Restaurant,Fournisseur,RestaurantAccess,Facture,BonLivraison,LigneFacture,LigneBonLivraison,
    CatalogueFournisseur,LigneCatalogue,CataloguePriceHistory,AnalysisJob)
from .services.catalogue_builder import build_supplier_catalogue
from .services.catalogue_control import control_invoice_against_catalogue
from .services.document_ai import DocumentAIError
from .services.catalogue_ai import _validate_catalogue_data


class CatalogueBuilderTests(TestCase):
    def setUp(self):
        self.restaurant=Restaurant.objects.create(nom='Restaurant',devise_defaut='CHF')
        self.supplier=Fournisseur.objects.create(restaurant=self.restaurant,nom='Fournisseur',devise_defaut='CHF')
        self.user=get_user_model().objects.create_user('catalogues_manager',password='test-catalogues-password')
        self.user.groups.add(Group.objects.create(name='Gestionnaire'))
        RestaurantAccess.objects.create(user=self.user,restaurant=self.restaurant);self.client.force_login(self.user)
        self.invoice=Facture.objects.create(fournisseur=self.supplier,fichier='invoice.pdf',numero_facture='F-001',date_facture=date(2026,7,1),devise='CHF')
        self.invoice_line=LigneFacture.objects.create(facture=self.invoice,reference_produit='0001',ean='0000123',designation='Lait',quantite=2,unite='kg',prix_unitaire_ht=12)
        self.delivery=BonLivraison.objects.create(fournisseur=self.supplier,fichier='bl.pdf',numero_bon_livraison='BL-001',date_livraison=date(2026,7,2),donnees_ia={'devise':'CHF'})
        self.delivery_line=LigneBonLivraison.objects.create(bon_livraison=self.delivery,reference_produit='0001',designation='Lait',unite='Kg',prix_unitaire=10)
        LigneBonLivraison.objects.create(bon_livraison=self.delivery,reference_produit='0002',designation='Produit sans prix',unite='pc')

    def build(self):
        return build_supplier_catalogue(self.supplier)[0]

    def test_groups_products_and_preserves_observed_prices_with_sources(self):
        catalogue=self.build();self.assertEqual(catalogue.lignes.count(),2)
        line=catalogue.lignes.get(reference_produit='0001');self.assertIsNone(line.prix_unitaire_ht)
        self.assertEqual(line.ean,'0000123');self.assertEqual(len(line.observations),2)
        self.assertEqual({o['price'] for o in line.observations},{'10.00','12.00'})
        self.assertEqual({o['kind'] for o in line.observations},{'facture','bon_livraison'})
        self.assertEqual(catalogue.origine,'documents');self.assertEqual(catalogue.devise,'CHF')
        self.assertIsNone(catalogue.date_debut);self.assertEqual(catalogue.statut,'a_valider')

    def test_refresh_is_idempotent_and_preserves_confirmed_price_and_identity(self):
        cat=self.build();line=cat.lignes.get(reference_produit='0001');line.prix_unitaire_ht=9;line.save();original=line.pk
        self.delivery_line.prix_unitaire=13;self.delivery_line.save()
        with patch('core.services.catalogue_ai._analyze_with_openai',side_effect=AssertionError('No OCR')):
            for _ in range(2): build_supplier_catalogue(self.supplier)
        self.assertEqual(CatalogueFournisseur.objects.filter(origine='documents').count(),1)
        line.refresh_from_db();self.assertEqual(line.pk,original);self.assertEqual(line.prix_unitaire_ht,9)
        self.assertIn('13.00',{o['price'] for o in line.observations})

    def test_refresh_attaches_a_manually_added_product_without_duplication(self):
        cat=self.build()
        manual=LigneCatalogue.objects.create(catalogue=cat,reference_produit='NEW',designation='Produit ajouté',unite='pc',prix_unitaire_ht=5)
        LigneBonLivraison.objects.create(bon_livraison=self.delivery,reference_produit='NEW',designation='Produit livré',unite='pc',prix_unitaire=6)
        build_supplier_catalogue(self.supplier)
        manual.refresh_from_db()
        self.assertEqual(cat.lignes.filter(reference_produit='NEW').count(),1)
        self.assertEqual(manual.prix_unitaire_ht,5);self.assertEqual(len(manual.observations),1)

    def test_refresh_does_not_restore_an_archived_product(self):
        cat=self.build();line=cat.lignes.get(reference_produit='0001');line.archived_at=timezone.now();line.save()
        build_supplier_catalogue(self.supplier)
        self.assertFalse(cat.lignes.filter(reference_produit='0001').exists())
        self.assertEqual(LigneCatalogue.all_objects.filter(catalogue=cat,reference_produit='0001').count(),1)

    def test_units_remain_distinct_and_matching_uses_invoice_unit(self):
        LigneBonLivraison.objects.create(bon_livraison=self.delivery,reference_produit='0001',designation='Lait carton',unite='carton',prix_unitaire=40)
        cat=self.build();self.assertEqual(cat.lignes.filter(reference_produit='0001').count(),2)
        line=cat.lignes.get(reference_produit='0001',unite='Kg');line.prix_unitaire_ht=10;line.save()
        cat.date_debut=date(2026,1,1);cat.statut='valide';cat.save()
        result=control_invoice_against_catalogue(self.invoice)
        self.assertEqual(result['details'][0]['catalogue_line'].pk,line.pk)
        self.assertEqual(result['details'][0]['status'],'ecart_prix')

    def test_fees_credits_ignored_and_archived_sources_are_not_products(self):
        LigneFacture.objects.create(facture=self.invoice,designation='Frais',kind='fee')
        credit=Facture.objects.create(fournisseur=self.supplier,fichier='credit.pdf',kind='credit')
        LigneFacture.objects.create(facture=credit,reference_produit='CREDIT',designation='Avoir')
        LigneBonLivraison.objects.create(bon_livraison=self.delivery,designation='Ignoré',statut_ligne='ignoree')
        self.invoice.archived_at=timezone.now();self.invoice.save()
        cat=self.build();self.assertEqual(cat.lignes.count(),2)
        self.assertTrue(all(o['kind']=='bon_livraison' for l in cat.lignes.all() for o in l.observations))

    def test_unknown_currency_and_price_remain_unknown(self):
        self.invoice.archived_at=timezone.now();self.invoice.save();self.delivery.donnees_ia={'delivery_note':None};self.delivery.save()
        cat=self.build();self.assertEqual(cat.devise,'UNK')
        self.assertTrue(all(l.prix_unitaire_ht is None for l in cat.lignes.all()))
        self.assertEqual(cat.lignes.get(reference_produit='0001').observations[0]['currency'],'UNK')

    def test_empty_supplier_has_a_clear_empty_catalogue(self):
        supplier=Fournisseur.objects.create(restaurant=self.restaurant,nom='Sans lignes')
        cat,result=build_supplier_catalogue(supplier);self.assertEqual(result['products'],0)
        response=self.client.get(reverse('supplier_catalogue',args=[supplier.pk]))
        self.assertContains(response,'Aucun produit extrait');self.assertEqual(cat.fichier_original.name,'')

    def test_command_archives_misclassified_records_without_deleting_sources(self):
        old=CatalogueFournisseur.objects.create(fournisseur=self.supplier,nom='Facture F-001',fichier_original='original.pdf')
        LigneCatalogue.objects.create(catalogue=old,reference_produit='OLD',designation='Ancien produit',prix_unitaire_ht=100)
        self.user.is_superuser=True;self.user.save()
        call_command('build_supplier_catalogues',apply=True,actor=self.user.pk,archive_misclassified=[old.pk],stdout=StringIO())
        old.refresh_from_db();self.assertIsNotNone(old.archived_at);self.assertEqual(old.fichier_original.name,'original.pdf')
        self.assertEqual(Facture.objects.count(),1);self.assertEqual(BonLivraison.objects.count(),1)
        self.assertTrue(LigneCatalogue.all_objects.filter(catalogue=old).exists())
        self.assertEqual(CatalogueFournisseur.objects.count(),1)

    def test_dry_run_does_not_write_catalogues(self):
        call_command('build_supplier_catalogues',stdout=StringIO());self.assertEqual(CatalogueFournisseur.objects.count(),0)

    def test_pages_show_products_not_source_documents_as_catalogues(self):
        cat=self.build();response=self.client.get(reverse('catalogue_coverage'))
        self.assertContains(response,'Catalogues');self.assertNotContains(response,'Base prix');self.assertNotContains(response,'invoice.pdf')
        response=self.client.get(reverse('supplier_catalogue',args=[self.supplier.pk]))
        self.assertContains(response,'Lait');self.assertContains(response,'Prix relevé');self.assertContains(response,'À confirmer')
        self.assertContains(response,reverse('facture_detail',args=[self.invoice.pk]))
        self.assertRedirects(self.client.get(reverse('catalogue_detail',args=[cat.pk])),reverse('supplier_catalogue',args=[self.supplier.pk]))
        self.assertRedirects(self.client.get('/couverture-catalogues/'),reverse('catalogue_coverage'))

    def test_explicit_price_confirmation_has_history_and_stale_guard(self):
        cat=self.build();line=cat.lignes.get(reference_produit='0001')
        url=reverse('catalogue_product_price',args=[line.pk])
        self.client.post(url,{'price':'9.00','previous_price':''});line.refresh_from_db()
        self.assertEqual(line.prix_unitaire_ht,9);self.assertEqual(CataloguePriceHistory.objects.count(),1)
        self.client.post(url,{'price':'5.00','previous_price':''});line.refresh_from_db();self.assertEqual(line.prix_unitaire_ht,9)
        self.assertEqual(len(line.observations),2)

    def test_period_requires_price_and_currency_and_no_false_conformity(self):
        cat=self.build();url=reverse('supplier_catalogue',args=[self.supplier.pk])
        response=self.client.post(url,{'action':'activate','date_debut':'2026-01-01','devise':'CHF'})
        self.assertContains(response,'Renseignez au moins un prix');cat.refresh_from_db();self.assertEqual(cat.statut,'a_valider')
        line=cat.lignes.get(reference_produit='0001');line.prix_unitaire_ht=10;line.save()
        response=self.client.post(url,{'action':'activate','date_debut':'2026-01-01','devise':'CHF'})
        self.assertEqual(response.status_code,302);cat.refresh_from_db();self.assertEqual(cat.statut,'valide')
        self.assertEqual(control_invoice_against_catalogue(self.invoice)['details'][0]['status'],'ecart_prix')

    def test_unknown_currency_can_be_set_before_activating(self):
        cat=self.build();cat.devise='UNK';cat.save()
        response=self.client.post(reverse('supplier_catalogue',args=[self.supplier.pk]),{'action':'settings','devise':'CHF'})
        self.assertEqual(response.status_code,302);cat.refresh_from_db();self.assertEqual(cat.devise,'CHF');self.assertIsNone(cat.date_debut)

    def test_access_is_scoped_and_reception_cannot_modify_catalogue(self):
        other=Restaurant.objects.create(nom='Autre');supplier=Fournisseur.objects.create(restaurant=other,nom='Privé')
        cat,_=build_supplier_catalogue(supplier);line=LigneCatalogue.objects.create(catalogue=cat,reference_produit='X',unite='kg')
        self.assertEqual(self.client.get(reverse('supplier_catalogue',args=[supplier.pk])).status_code,404)
        self.assertEqual(self.client.post(reverse('catalogue_product_price',args=[line.pk]),{'price':1}).status_code,404)
        self.user.groups.clear();self.user.groups.add(Group.objects.create(name='Reception'))
        self.assertEqual(self.client.get(reverse('catalogue_coverage')).status_code,403)

    def test_generated_catalogue_does_not_enqueue_ocr(self):
        cat=self.build();self.client.post(reverse('catalogue_ai_reanalyze',args=[cat.pk]));self.assertEqual(AnalysisJob.objects.count(),0)

    def test_ocr_rejects_invoice_or_delivery_as_tariff(self):
        for kind in ['facture','bon_livraison','autre']:
            with self.assertRaises(DocumentAIError):_validate_catalogue_data({'type_document':kind})
