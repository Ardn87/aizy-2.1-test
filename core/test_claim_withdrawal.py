from django.contrib.auth.models import Group
from django.test import TestCase, Client
from django.urls import reverse
from . import test_price_review as fixtures
from .models import SupplierClaim, SupplierClaimLine, PriceReviewDecision, PriceReviewEvent, BusinessAudit
from .services.price_review import review_queue


class ClaimWithdrawalTests(TestCase):
    def setUp(self):
        fixtures.PriceReviewTests.setUp(self)
        fixtures.PriceReviewTests.action(self,'claim')
        self.claim = SupplierClaim.objects.get()
        self.item = self.claim.lines.get()

    def withdraw(self,action='cancel',**data):
        return self.client.post(reverse('claim_state',args=[self.claim.pk]),{'action':action,'confirmed':'1',**data})

    def test_cancel_preserves_history_and_returns_price_to_review(self):
        self.claim.state='sent';self.claim.save()
        saved=self.item.snapshot.copy()
        self.assertEqual(self.withdraw().status_code,302)
        self.claim.refresh_from_db();self.item.refresh_from_db()
        self.assertEqual(self.claim.state,'cancelled');self.assertEqual(self.item.snapshot,saved)
        self.assertFalse(PriceReviewDecision.objects.filter(line=self.line,state='claimed').exists())
        self.assertTrue(PriceReviewEvent.objects.get(action='claim').undone)
        self.assertTrue(BusinessAudit.objects.filter(action='claim_cancel',object_id=self.claim.pk).exists())
        self.assertEqual(len(review_queue()[0]),1)
        response=self.client.get(reverse('supplier_claims'))
        self.assertContains(response,'Annulée');self.assertNotContains(response,'Télécharger la réclamation')
        self.assertEqual(self.client.get(reverse('claim_document',args=[self.claim.pk])).status_code,302)

    def test_cancelled_claim_allows_same_price_to_be_claimed_again(self):
        self.withdraw()
        fixtures.PriceReviewTests.action(self,'claim')
        self.assertEqual(SupplierClaim.objects.filter(state='draft').count(),1)
        self.assertEqual(SupplierClaimLine.objects.filter(invoice_line=self.line).count(),2)
        self.assertEqual(review_queue()[0],[])

    def test_followup_prefills_original_price_differences_without_recording_recovery(self):
        from decimal import Decimal
        from .models import LigneFacture
        second = LigneFacture.objects.create(facture=self.invoice,reference_produit='P1',designation='Poulet',unite='kg',quantite=3,prix_unitaire_ht='9')
        fixtures.PriceReviewTests.action(self,'claim',line=second)
        self.claim.state='sent';self.claim.save()
        page=self.client.get(reverse('supplier_claims'),{'claim':self.claim.pk})
        # (8.84 - 8) * 20 + (9 - 8) * 3, from the claim's fixed snapshots.
        self.assertEqual(page.context['claim'].followup_form['recovered_amount'].value(),Decimal('19.80'))
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.recovered_amount,0)
        self.claim.recovered_amount=Decimal('5');self.claim.save()
        page=self.client.get(reverse('supplier_claims'),{'claim':self.claim.pk})
        self.assertEqual(page.context['claim'].followup_form['recovered_amount'].value(),Decimal('5'))

    def test_cancel_is_idempotent_and_cannot_be_reopened_by_old_action(self):
        self.withdraw();self.withdraw()
        self.assertEqual(BusinessAudit.objects.filter(action='claim_cancel',model_label='core.supplierclaim').count(),1)
        self.client.post(reverse('claim_state',args=[self.claim.pk]),{'action':'prepare'})
        self.claim.refresh_from_db();self.assertEqual(self.claim.state,'cancelled')

    def test_manager_cannot_delete(self):
        self.assertEqual(self.withdraw('delete').status_code,403)
        self.assertTrue(SupplierClaim.objects.filter(pk=self.claim.pk).exists())
        self.assertNotContains(self.client.get(reverse('supplier_claims')),'Supprimer le brouillon')

    def test_admin_can_delete_draft_with_complete_audit(self):
        self.user.groups.add(Group.objects.get_or_create(name='Admin')[0])
        self.assertContains(self.client.get(reverse('supplier_claims')),'Supprimer le brouillon')
        self.withdraw('delete')
        self.assertFalse(SupplierClaim.objects.filter(pk=self.claim.pk).exists())
        audit=BusinessAudit.objects.get(action='claim_delete',model_label='core.supplierclaim')
        self.assertEqual(audit.before['lines'][0]['snapshot'],self.item.snapshot)
        self.assertEqual(len(review_queue()[0]),1)
        fixtures.PriceReviewTests.action(self,'claim')
        self.assertEqual(SupplierClaimLine.objects.count(),1)

    def test_non_draft_cannot_be_deleted_even_by_admin(self):
        self.user.groups.add(Group.objects.get_or_create(name='Admin')[0])
        self.claim.state='prepared';self.claim.save()
        self.withdraw('delete')
        self.claim.refresh_from_db();self.assertEqual(self.claim.state,'prepared')

    def test_recovered_claim_cannot_be_cancelled(self):
        self.claim.state='credit_received';self.claim.recovered_amount=1;self.claim.save()
        self.withdraw()
        self.claim.refresh_from_db();self.assertEqual(self.claim.state,'credit_received')
        self.assertTrue(PriceReviewDecision.objects.filter(line=self.line,state='claimed').exists())

    def test_confirmation_post_and_csrf_required(self):
        url=reverse('claim_state',args=[self.claim.pk])
        self.assertEqual(self.client.get(url).status_code,405)
        self.withdraw(confirmed='')
        self.claim.refresh_from_db();self.assertEqual(self.claim.state,'draft')
        client=Client(enforce_csrf_checks=True);client.force_login(self.user)
        self.assertEqual(client.post(url,{'action':'cancel','confirmed':'1'}).status_code,403)

    def test_restricted_roles_cannot_cancel_or_delete(self):
        for role in ('Reception','Revue'):
            self.user.groups.clear();self.user.groups.add(Group.objects.get_or_create(name=role)[0])
            self.assertEqual(self.withdraw().status_code,403)
            self.assertEqual(self.withdraw('delete').status_code,403)

    def test_filters_follow_jump_and_remain_available_when_empty(self):
        self.withdraw()
        html=self.client.get(reverse('price_review')).content.decode()
        self.assertNotIn('Écarts de prix et prix catalogue à compléter.',html)
        self.assertLess(html.index('class="review-jump"'),html.index('class="review-filters"'))
        self.assertContains(self.client.get(reverse('price_review')+'?supplier=999999'),'class="review-filters"')

    def test_claim_cards_paginate_and_direct_link_selects_claim(self):
        self.claim.state='cancelled'; self.claim.save()
        newer=SupplierClaim.objects.create(fournisseur=self.claim.fournisseur,currency=self.claim.currency,state='draft',created_by=self.user)
        page=self.client.get(reverse('supplier_claims'))
        self.assertEqual(page.context['claim'].pk,newer.pk)
        self.assertEqual(page.context['next'].pk,self.claim.pk)
        self.assertContains(page,'class="claim-item review-card"',count=1)
        direct=self.client.get(reverse('supplier_claims'),{'claim':self.claim.pk})
        self.assertEqual(direct.context['position'],2)
        self.assertEqual(direct.context['claim'].pk,self.claim.pk)
        for position,expected in [('999',self.claim.pk),('-5',newer.pk),('invalid',newer.pk)]:
            page=self.client.get(reverse('supplier_claims'),{'position':position})
            self.assertEqual(page.context['claim'].pk,expected)

    def test_claim_progress_is_compact_and_empty_state_is_supported(self):
        for _ in range(20):
            SupplierClaim.objects.create(fournisseur=self.claim.fournisseur,currency=self.claim.currency,state='cancelled',created_by=self.user)
        page=self.client.get(reverse('supplier_claims'),{'position':10})
        self.assertLessEqual(len(page.context['numbers']),9)
        self.assertIn(None,page.context['numbers'])
        SupplierClaim.objects.all().delete()
        self.assertContains(self.client.get(reverse('supplier_claims')),'Aucune réclamation pour le moment')
