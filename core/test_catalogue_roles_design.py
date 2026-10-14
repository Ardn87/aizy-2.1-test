from datetime import date
from decimal import Decimal
from io import StringIO
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.templatetags.static import static
from .models import (Restaurant, RestaurantAccess, Fournisseur, Facture, BonLivraison, LigneFacture,
                     LigneBonLivraison, CatalogueFournisseur, LigneCatalogue, CataloguePriceHistory)
from .services.catalogue_builder import build_supplier_catalogue, fill_catalogue_prices


class CatalogueRolesDesignTests(TestCase):
    def setUp(self):
        call_command('init_roles', stdout=StringIO())
        self.restaurant=Restaurant.objects.create(nom='Dahu',devise_defaut='CHF')
        self.supplier=Fournisseur.objects.create(restaurant=self.restaurant,nom='Fournisseur',devise_defaut='CHF')
        self.users={}
        for role in ('Admin','Gestionnaire','Reception','Revue'):
            user=get_user_model().objects.create_user(role,password='Test-aizy-2026-safe')
            user.groups.add(Group.objects.get(name=role))
            RestaurantAccess.objects.create(user=user,restaurant=self.restaurant)
            self.users[role]=user
        self.invoice=Facture.objects.create(fournisseur=self.supplier,fichier='proof.pdf',numero_facture='F-01',devise='CHF',date_facture=date(2026,7,1),statut='a_valider')
        self.invoice_line=LigneFacture.objects.create(facture=self.invoice,reference_produit='A',designation='Lait',unite='kg',prix_unitaire_ht=12,quantite=2)
        self.delivery=BonLivraison.objects.create(fournisseur=self.supplier,fichier='bl.pdf',numero_bon_livraison='BL-1',donnees_ia={'devise':'CHF'})
        LigneBonLivraison.objects.create(bon_livraison=self.delivery,reference_produit='A',designation='Lait',unite='kg',prix_unitaire=10)
        LigneBonLivraison.objects.create(bon_livraison=self.delivery,reference_produit='B',designation='Sans prix',unite='pc')
        self.catalogue=build_supplier_catalogue(self.supplier)[0]

    def test_prices_come_from_same_product_with_provenance_and_no_invention(self):
        result=fill_catalogue_prices(self.catalogue,self.users['Admin'])
        self.assertEqual(result,{'filled':1,'existing':0,'missing':1})
        line=self.catalogue.lignes.get(reference_produit='A')
        self.assertEqual(line.prix_unitaire_ht,Decimal('12.00'))
        self.assertEqual(line.price_source['kind'],'facture')
        self.assertEqual(line.price_source['document_id'],self.invoice.pk)
        self.assertEqual(line.statut_ligne,'a_verifier')
        self.assertIsNone(self.catalogue.lignes.get(reference_produit='B').prix_unitaire_ht)
        self.assertEqual(CataloguePriceHistory.objects.get(line=line).catalogue_snapshot['price_source']['line_id'],self.invoice_line.pk)
        count=CataloguePriceHistory.objects.count()
        fill_catalogue_prices(self.catalogue,self.users['Admin'])
        self.assertEqual(CataloguePriceHistory.objects.count(),count)
        self.invoice_line.refresh_from_db();self.assertEqual(self.invoice_line.prix_unitaire_ht,12)
        self.catalogue.refresh_from_db();self.assertEqual(self.catalogue.statut,'a_valider');self.assertIsNone(self.catalogue.date_debut)

    def test_existing_prices_are_preserved_and_invalid_prices_not_copied(self):
        line=self.catalogue.lignes.get(reference_produit='A');line.prix_unitaire_ht=9;line.save()
        missing=self.catalogue.lignes.get(reference_produit='B')
        missing.observations=[{'price':'NaN'},{'price':'-1'},{'price':'2.123'},{'price':'Infinity'}];missing.save()
        result=fill_catalogue_prices(self.catalogue,self.users['Admin'])
        self.assertEqual(result['filled'],0)
        line.refresh_from_db();self.assertEqual(line.prix_unitaire_ht,9)
        self.assertEqual(CataloguePriceHistory.objects.count(),0)

    def test_manual_add_and_refresh_are_distinct_and_duplicate_is_rejected(self):
        self.client.force_login(self.users['Gestionnaire']);url=reverse('supplier_catalogue',args=[self.supplier.pk])
        response=self.client.get(url)
        self.assertContains(response,'Ajouter un produit');self.assertContains(response,'Actualiser depuis les documents')
        response=self.client.post(url,{'action':'create','designation':'Tomate','reference_produit':'T','unite':'kg','prix_unitaire_ht':'4.50'})
        self.assertRedirects(response,url)
        line=self.catalogue.lignes.get(reference_produit='T');self.assertEqual(line.prix_unitaire_ht,Decimal('4.50'))
        self.assertTrue(line.corrigee_manuellement);self.assertEqual(line.observations,[])
        response=self.client.post(url,{'action':'create','designation':'Tomate','reference_produit':'T','unite':'kg','prix_unitaire_ht':'6'})
        self.assertContains(response,'existent déjà');self.assertEqual(self.catalogue.lignes.filter(reference_produit='T').count(),1)
        response=self.client.post(url,{'action':'create','designation':'','unite':'','prix_unitaire_ht':'-2'})
        self.assertEqual(response.status_code,200);self.assertEqual(self.catalogue.lignes.count(),3)
        self.client.post(url,{'action':'refresh'});line.refresh_from_db();self.assertEqual(line.prix_unitaire_ht,Decimal('4.50'))
        self.assertEqual(self.catalogue.lignes.get(reference_produit='A').prix_unitaire_ht,12)

    def test_create_first_catalogue_with_valid_manual_product(self):
        supplier=Fournisseur.objects.create(restaurant=self.restaurant,nom='Nouveau fournisseur')
        self.client.force_login(self.users['Gestionnaire'])
        url=reverse('supplier_catalogue',args=[supplier.pk])
        self.client.post(url,{'action':'create','designation':'Produit manuel','unite':'pc','prix_unitaire_ht':'2'})
        self.assertEqual(LigneCatalogue.objects.filter(catalogue__fournisseur=supplier).count(),1)

    def test_role_homes_login_redirects_and_navigation(self):
        for role,landing in [('Reception','document_deposit'),('Revue','price_review')]:
            self.client.force_login(self.users[role])
            self.assertRedirects(self.client.get(reverse('home')),reverse(landing))
            response=self.client.get(reverse(landing))
            self.assertEqual(response.status_code,200)
            for forbidden in ('dashboard','user_list','supplier_claims','catalogue_coverage','work_documents'):
                self.assertNotContains(response,f'href="{reverse(forbidden)}"')
            self.client.logout()
            response=self.client.post(reverse('login')+'?next=/utilisateurs/',{'username':role,'password':'Test-aizy-2026-safe'})
            self.assertEqual(response.url,reverse(landing));self.client.logout()

    def test_restricted_roles_reject_direct_get_and_post_without_writes(self):
        forbidden=['dashboard','user_list','user_create','catalogue_coverage','supplier_claims','audit_log','document_archives','bon_livraison_add','restaurant_list','fournisseur_list','admin:index']
        for role in ('Reception','Revue'):
            self.client.force_login(self.users[role])
            for name in forbidden + (['price_review','review_undo'] if role=='Reception' else ['document_deposit']):
                for method in ('get','post'):
                    with self.subTest(role=role,name=name,method=method):
                        self.assertEqual(getattr(self.client,method)(reverse(name)).status_code,403)
            for url in (reverse('supplier_catalogue',args=[self.supplier.pk]),reverse('facture_detail',args=[self.invoice.pk]),reverse('facture_file',args=[self.invoice.pk]),reverse('review_action',args=[self.invoice_line.pk])):
                if role=='Revue' and '/action/' in url: continue
                self.assertEqual(self.client.post(url,{'action':'create','designation':'Interdit','unite':'pc','prix_unitaire_ht':1}).status_code,403)
        self.assertFalse(LigneCatalogue.objects.filter(designation='Interdit').exists())

    def test_review_can_act_and_read_scoped_invoice_but_not_other_restaurant(self):
        self.client.force_login(self.users['Revue'])
        self.catalogue.date_debut=date(2026,1,1);self.catalogue.statut='valide';self.catalogue.save()
        line=self.catalogue.lignes.get(reference_produit='A');line.prix_unitaire_ht=10;line.save()
        response=self.client.get(reverse('price_review'));detail=response.context['current']
        response=self.client.post(reverse('review_action',args=[self.invoice_line.pk]),{'action':'defer','reason':'À revoir','fingerprint':detail['fingerprint']})
        self.assertEqual(response.status_code,302)
        with TemporaryDirectory() as media,override_settings(MEDIA_ROOT=media):
            Path(media,'proof.pdf').write_bytes(b'%PDF-1.7\nfixture')
            response=self.client.get(reverse('review_invoice_file',args=[self.invoice.pk]))
            self.assertEqual(response.status_code,200);self.assertIn(b'%PDF-',b''.join(response.streaming_content))
            self.assertEqual(response.headers['X-Frame-Options'],'SAMEORIGIN')
            other_restaurant=Restaurant.objects.create(nom='Autre')
            other_supplier=Fournisseur.objects.create(restaurant=other_restaurant,nom='Autre fournisseur')
            foreign=Facture.objects.create(fournisseur=other_supplier,fichier='proof.pdf',statut='a_valider')
            self.assertEqual(self.client.get(reverse('review_invoice_file',args=[foreign.pk])).status_code,404)
        self.invoice.statut='importee';self.invoice.save()
        self.assertEqual(self.client.get(reverse('review_invoice_file',args=[self.invoice.pk])).status_code,404)

    def test_review_role_available_and_switch_removes_previous_roles(self):
        from .forms import UserUpdateForm
        user=self.users['Reception']
        form=UserUpdateForm({'username':user.username,'role':'Revue','is_active':True,'restaurants':[self.restaurant.pk]},instance=user)
        self.assertTrue(form.is_valid(),form.errors);form.save()
        self.assertEqual(list(user.groups.values_list('name',flat=True)),['Revue'])
        self.assertEqual(Group.objects.get(name='Revue').permissions.count(),0)

    def test_common_design_stylesheet_on_major_pages(self):
        self.client.force_login(self.users['Admin'])
        for name in ('dashboard','document_deposit','price_review','work_documents','catalogue_coverage','user_list','user_create','restaurant_list','fournisseur_list','supplier_claims','audit_log','document_archives','own_password'):
            with self.subTest(name=name):
                self.assertContains(self.client.get(reverse(name)),static('css/interface.css'))

    def test_generated_catalogue_has_no_column_file_and_redirects_to_products(self):
        self.client.force_login(self.users['Gestionnaire'])
        self.assertRedirects(self.client.get(reverse('catalogue_columns',args=[self.catalogue.pk])),reverse('supplier_catalogue',args=[self.supplier.pk]))

    def test_recovered_price_is_visible_without_changing_original_extraction(self):
        line=self.catalogue.lignes.get(reference_produit='A')
        original=list(line.observations)
        line.prix_unitaire_ht=Decimal('11.50')
        line.price_source={**original[0],'price':'11.50','basis':'HT','read_from_image':True}
        line.save()
        self.client.force_login(self.users['Gestionnaire'])
        response=self.client.get(reverse('supplier_catalogue',args=[self.supplier.pk]))
        self.assertContains(response,'prix relu sur la photo')
        line.refresh_from_db();self.assertEqual(line.observations,original)
        self.invoice_line.refresh_from_db();self.assertEqual(self.invoice_line.prix_unitaire_ht,12)
        build_supplier_catalogue(self.supplier)
        line.refresh_from_db();self.assertEqual(line.prix_unitaire_ht,Decimal('11.50'))
        self.assertTrue(line.price_source['read_from_image'])
