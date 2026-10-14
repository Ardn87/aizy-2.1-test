from django.test import TestCase
from django.urls import reverse
from . import test_price_review as fixtures
from .models import SupplierClaim, Restaurant, Fournisseur


class ClaimFilterTests(TestCase):
    def setUp(self):
        fixtures.PriceReviewTests.setUp(self)
        self.claims={state:SupplierClaim.objects.create(fournisseur=self.supplier,currency='CHF',state=state,created_by=self.user)
                     for state in ('draft','prepared','sent','closed','cancelled')}

    def test_state_filters_and_counts_remain_restaurant_scoped(self):
        other=Restaurant.objects.create(nom='Autre établissement')
        supplier=Fournisseur.objects.create(restaurant=other,nom='Autre fournisseur')
        SupplierClaim.objects.create(fournisseur=supplier,currency='CHF',state='closed')
        for key,expected in [('all',5),('active',3),('draft',1),('prepared',1),('followup',1),('closed',1),('cancelled',1)]:
            response=self.client.get(reverse('supplier_claims'),{'filter':key})
            self.assertEqual(response.context['total'],expected)
            self.assertEqual(response.context['claim_filter'],key)
            self.assertEqual({tab['key']:tab['count'] for tab in response.context['quick_filters']}[key],expected)
            self.assertContains(response,'aria-label="État des réclamations"')
        response=self.client.get(reverse('supplier_claims'),{'filter':'closed','claim':self.claims['draft'].pk})
        self.assertEqual(response.context['claim'].pk,self.claims['closed'].pk)

    def test_navigation_retains_filter_and_invalid_filter_falls_back(self):
        SupplierClaim.objects.create(fournisseur=self.supplier,currency='CHF',state='prepared')
        response=self.client.get(reverse('supplier_claims'),{'filter':'prepared'})
        self.assertContains(response,'?filter=prepared&amp;position=2')
        self.assertContains(response,'name="filter" value="prepared"')
        response=self.client.get(reverse('supplier_claims'),{'filter':'invalid'})
        self.assertEqual(response.context['claim_filter'],'all')

    def test_followup_retains_selected_filter(self):
        claim=self.claims['draft']
        response=self.client.post(reverse('claim_state',args=[claim.pk]),{'action':'cancel','confirmed':'1','claim_filter':'active'})
        self.assertEqual(response.status_code,302)
        self.assertIn('filter=active',response.url)
        page=self.client.get(response.url)
        self.assertEqual(page.context['claim_filter'],'active')
        self.assertEqual(page.context['total'],2)
