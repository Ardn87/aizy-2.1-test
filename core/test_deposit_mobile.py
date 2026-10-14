import tempfile
from pathlib import Path
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from .models import Restaurant,RestaurantAccess,Fournisseur,Facture,BonLivraison,AnalysisJob


class MobileDepositTests(TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        override=override_settings(MEDIA_ROOT=self.temp.name);override.enable();self.addCleanup(override.disable)
        self.restaurant=Restaurant.objects.create(nom='Un seul restaurant')
        self.supplier=Fournisseur.objects.create(restaurant=self.restaurant,nom='Fournisseur')
        self.user=get_user_model().objects.create_user('reception_mobile',password='a-test-password')
        self.user.groups.add(Group.objects.create(name='Reception'))
        RestaurantAccess.objects.create(user=self.user,restaurant=self.restaurant);self.client.force_login(self.user)
        self.url=reverse('document_deposit')
    def photo(self,name='photo.jpg',content=b'\xff\xd8\xff\xe0photo'):
        return SimpleUploadedFile(name,content,content_type='image/jpeg')

    def test_camera_and_file_choices_render_with_sole_restaurant_hidden(self):
        response=self.client.get(self.url)
        self.assertContains(response,'capture="environment"')
        self.assertContains(response,'Prendre une photo');self.assertContains(response,'Ajouter une photo ou un fichier')
        self.assertContains(response,'type="hidden" name="restaurant"')
        self.assertEqual(response.context['form'].single_restaurant,self.restaurant)

    def test_photo_submission_infers_only_authorized_restaurant_and_retains_original(self):
        content=b'\xff\xd8\xff\xe0original photo'
        response=self.client.post(self.url,{'kind':'bon_livraison','fournisseur':self.supplier.pk,'files':self.photo(content=content)},HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code,201);self.assertEqual(response.json()['received'],1)
        doc=BonLivraison.objects.get();self.assertEqual(doc.fournisseur_id,self.supplier.pk)
        self.assertEqual(Path(doc.fichier.path).read_bytes(),content);self.assertEqual(AnalysisJob.objects.count(),1)

    def test_camera_and_pdf_are_received_together(self):
        response=self.client.post(self.url,{'kind':'facture','fournisseur':self.supplier.pk,'files':[self.photo(),SimpleUploadedFile('document.pdf',b'%PDF-1.7\nPDF',content_type='application/pdf')]},HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code,201);self.assertEqual(response.json()['received'],2)
        self.assertEqual(Facture.objects.count(),2);self.assertEqual(AnalysisJob.objects.count(),2)
        self.assertEqual(Facture.objects.filter(devise='UNK').count(),2)

    def test_json_failure_returns_fields_without_writing_a_document(self):
        response=self.client.post(self.url,{'kind':'facture','fournisseur':self.supplier.pk,'files':SimpleUploadedFile('bad.jpg',b'invalid')},HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code,400);self.assertIn('files',response.json()['errors'])
        self.assertEqual(Facture.objects.count(),0);self.assertEqual(AnalysisJob.objects.count(),0)

    def test_json_duplicate_requires_reason_and_no_second_document_is_written(self):
        data={'kind':'facture','fournisseur':self.supplier.pk}
        self.client.post(self.url,{**data,'files':self.photo()},HTTP_ACCEPT='application/json')
        response=self.client.post(self.url,{**data,'files':self.photo()},HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code,400);self.assertIn('duplicate_reason',response.json()['errors'])
        self.assertEqual(Facture.objects.count(),1)
        response=self.client.post(self.url,{**data,'files':self.photo(),'duplicate_reason':'Copie demandée pour contrôle.'},HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code,201);self.assertEqual(Facture.objects.count(),2)

    def test_multiple_restaurants_are_not_guessed(self):
        other=Restaurant.objects.create(nom='Deuxième restaurant');RestaurantAccess.objects.create(user=self.user,restaurant=other)
        response=self.client.get(self.url);self.assertIsNone(response.context['form'].single_restaurant)
        response=self.client.post(self.url,{'kind':'facture','fournisseur':self.supplier.pk,'files':self.photo()},HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code,400);self.assertIn('restaurant',response.json()['errors'])

    def test_foreign_restaurant_is_not_replaced_by_single_restaurant_default(self):
        foreign=Restaurant.objects.create(nom='Restaurant interdit')
        response=self.client.post(self.url,{'kind':'facture','restaurant':foreign.pk,'fournisseur':self.supplier.pk,'files':self.photo()},HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code,400);self.assertIn('restaurant',response.json()['errors'])
        self.assertEqual(Facture.objects.count(),0)
