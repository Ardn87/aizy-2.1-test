from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from core import test_reconciliation as fixtures
from core.models import (BonLivraison, LigneBonLivraison, DeliveryAllocation, InvoiceDeliveryLink,
                         BusinessAudit, DeliveryProductMapping)
from core.services import reconciliation as service
from core.services import automatic_reconciliation as automatic


class AutomaticReconciliationTests(TestCase):
    setUp = fixtures.ReconciliationTests.setUp
    make_invoice = fixtures.ReconciliationTests.make_invoice
    make_line = fixtures.ReconciliationTests.make_line

    def extra_delivery(self, quantity=10, reference='P1', **kwargs):
        doc = BonLivraison.objects.create(fournisseur=self.supplier,
            numero_bon_livraison='BL-extra', date_livraison=date(2026, 10, 2), fichier='extra.pdf')
        return LigneBonLivraison.objects.create(bon_livraison=doc, reference_produit=reference,
            designation='Pommes', quantite_livree=quantity, unite='kg', **kwargs)

    def decide(self, action='approve_auto', **extra):
        self.invoice.refresh_from_db()
        plan = automatic.build_plan(self.invoice)
        return self.client.post(reverse('invoice_reconcile', args=[self.invoice.pk]), {
            'action': action, 'token': service.review_token(self.invoice),
            'proposal': plan['signature'], **extra}, follow=True)

    def test_default_screen_computes_without_writing_and_has_no_product_selectors(self):
        response = self.client.get(reverse('invoice_reconcile', args=[self.invoice.pk]))
        self.assertContains(response, 'Valider le rapprochement')
        self.assertContains(response, '>Refuser</button>')
        self.assertNotContains(response, 'name="invoice_line"')
        self.assertNotContains(response, 'name="delivery_line"')
        self.assertNotContains(response, 'name="quantity"')
        self.assertFalse(DeliveryAllocation.objects.exists())
        self.assertFalse(InvoiceDeliveryLink.objects.exists())

    def test_section_opens_a_card_and_navigation_respects_search(self):
        other=self.make_invoice('F-2');self.make_line(other,10)
        url=reverse('reconciliation_queue')
        first=self.client.get(url,{'state':'all'})
        self.assertContains(first,'data-reconcile-invoice="%s"' % self.invoice.pk)
        self.assertNotContains(first,'class="reconcile-queue-card"')
        second=self.client.get(url,{'state':'all','position':2})
        self.assertEqual(second.context['invoice'].pk,other.pk)
        self.assertEqual(second.context['neighbors']['previous'].pk,self.invoice.pk)
        filtered=self.client.get(url,{'state':'all','q':'F-2'})
        self.assertEqual(filtered.context['invoice'].pk,other.pk)
        self.assertEqual(filtered.context['count'],1)
        self.assertIsNone(filtered.context['neighbors']['previous'])
        self.assertFalse(DeliveryAllocation.objects.exists())

    def test_decision_from_card_returns_to_filtered_selection(self):
        response=self.decide(from_queue='1',queue_state='all',queue_supplier=str(self.supplier.pk),queue_q='F-1',queue_position='1')
        self.assertEqual(response.context['invoice'].pk,self.invoice.pk)
        self.assertEqual(response.context['queue_scope']['q'],'F-1')
        self.assertEqual(response.context['state']['state'],'matched')
        self.assertEqual(DeliveryAllocation.objects.count(),1)

    def test_empty_card_selection_keeps_filters(self):
        response=self.client.get(reverse('reconciliation_queue'),{'q':'No matching document'})
        self.assertContains(response,'Aucune facture dans cette sélection')
        self.assertContains(response,'name="supplier"')
        self.assertNotContains(response,'data-reconcile-invoice=')

    def test_one_click_confirms_all_lines_not_only_first_page(self):
        for i in range(10):
            line = self.make_line(self.invoice, 10)
            line.reference_produit = f'P-{i}'; line.designation = f'Produit {i}'; line.save()
            dl = self.extra_delivery(reference=f'P-{i}')
            dl.designation = f'Produit {i}'; dl.save()
        response = self.decide()
        self.assertContains(response, 'Livraisons rapprochées')
        self.assertEqual(DeliveryAllocation.objects.count(), 11)
        self.assertEqual(InvoiceDeliveryLink.objects.count(), 11)
        self.assertTrue(BusinessAudit.objects.filter(action='approve_auto', actor=self.user).exists())

    def test_multiple_bls_are_combined_for_one_invoice(self):
        self.dl.quantite_livree = 4; self.dl.save()
        self.extra_delivery(6)
        plan = automatic.build_plan(self.invoice)
        self.assertTrue(plan['complete'])
        self.assertEqual(len(plan['documents']), 2)
        self.decide()
        self.assertEqual(service.reconciliation_state(self.invoice)['matched'], 1)

    def test_unique_combination_is_chosen_from_all_candidates(self):
        self.dl.quantite_livree = 30; self.dl.save()
        for value in (31, 32, 33, 34, 35):
            self.extra_delivery(value)
        a, b = self.extra_delivery(4), self.extra_delivery(6)
        plan = automatic.build_plan(self.invoice)
        self.assertEqual({e['delivery'].pk for e in plan['entries']}, {a.pk, b.pk})

    def test_equal_candidates_are_ambiguous_instead_of_picked_by_order(self):
        self.extra_delivery(10)
        plan = automatic.build_plan(self.invoice)
        self.assertFalse(plan['entries'])
        self.assertIn('Plusieurs BL', plan['unresolved'][0]['reason'])
        self.decide()
        self.assertFalse(DeliveryAllocation.objects.exists())

    def test_ambiguous_large_candidate_set_does_not_pick_first_five(self):
        for i in range(15):
            self.extra_delivery(10)
        self.assertFalse(automatic.build_plan(self.invoice)['entries'])

    def test_shortfall_remains_a_difference_after_partial_validation(self):
        self.dl.quantite_livree = 7; self.dl.save()
        plan = automatic.build_plan(self.invoice)
        self.assertFalse(plan['complete'])
        self.assertEqual(plan['entries'][0]['quantity'], 7)
        response = self.decide()
        self.assertNotContains(response, '<h3>Livraisons rapprochées</h3>')
        self.assertEqual(service.reconciliation_state(self.invoice)['rows'][0]['gap'], 3)

    def test_one_incomplete_row_does_not_block_other_usable_rows(self):
        line = self.make_line(self.invoice, 2)
        line.unite = ''; line.save()
        plan = automatic.build_plan(self.invoice)
        self.assertFalse(plan['complete'])
        self.assertEqual(plan['line_count'], 1)
        self.decide()
        self.assertEqual(DeliveryAllocation.objects.count(), 1)

    def test_surplus_is_explicit_and_only_invoice_quantity_is_used(self):
        self.dl.quantite_livree = 15; self.dl.save()
        plan = automatic.build_plan(self.invoice)
        self.assertTrue(plan['entries'][0]['partial_delivery'])
        self.assertTrue(plan['warnings'])
        self.decide()
        self.assertEqual(DeliveryAllocation.objects.get().quantity, 10)

    def test_same_delivery_for_two_invoice_lines_is_not_double_used(self):
        self.make_line(self.invoice, 10)
        plan = automatic.build_plan(self.invoice)
        self.assertFalse(plan['entries'])
        self.assertEqual(len(plan['unresolved']), 2)

    def test_refusal_persists_and_restore_is_explicit(self):
        self.decide('reject_auto')
        self.invoice.refresh_from_db()
        self.assertTrue(automatic.build_plan(self.invoice)['rejected'])
        response = self.decide()
        self.assertContains(response, 'Proposition refusée')
        self.assertFalse(DeliveryAllocation.objects.exists())
        self.decide('restore_auto')
        self.invoice.refresh_from_db()
        self.assertFalse(automatic.build_plan(self.invoice)['rejected'])
        self.decide()
        self.assertEqual(DeliveryAllocation.objects.count(), 1)

    def test_new_source_reopens_refused_proposal(self):
        self.decide('reject_auto')
        self.dl.quantite_livree = 8; self.dl.save()
        self.invoice.refresh_from_db()
        self.assertFalse(automatic.build_plan(self.invoice)['rejected'])

    def test_stale_confirmation_and_repeated_post_do_not_write(self):
        plan = automatic.build_plan(self.invoice)
        token = service.review_token(self.invoice)
        self.dl.quantite_livree = 8; self.dl.save()
        url = reverse('invoice_reconcile', args=[self.invoice.pk])
        data = {'action':'approve_auto', 'proposal':plan['signature'], 'token':token}
        response = self.client.post(url, data, follow=True)
        self.assertContains(response, 'Cette fiche a changé')
        self.assertFalse(DeliveryAllocation.objects.exists())
        self.decide()
        self.client.post(url, data)
        self.assertEqual(DeliveryAllocation.objects.count(), 1)
        self.assertEqual(DeliveryAllocation.objects.get().quantity, 8)

    def test_competing_invoice_consumption_invalidates_whole_proposal(self):
        plan = automatic.build_plan(self.invoice)
        other = self.make_invoice('F-2'); line = self.make_line(other, 6)
        service.allocate_delivery(other, line.pk, self.dl.pk, '6', self.user)
        with self.assertRaises(ValidationError):
            automatic.decide(self.invoice, plan['signature'], 'approve_auto', self.user)
        self.assertFalse(DeliveryAllocation.objects.filter(invoice_line=self.line).exists())

    def test_rollback_if_one_allocation_fails(self):
        self.dl.quantite_livree = 4; self.dl.save(); self.extra_delivery(6)
        plan = automatic.build_plan(self.invoice)
        original = service.allocate_delivery
        calls = []
        def failing(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise ValidationError('Source indisponible')
            return original(*args, **kwargs)
        with patch.object(service, 'allocate_delivery', side_effect=failing):
            with self.assertRaises(ValidationError):
                automatic.decide(self.invoice, plan['signature'], 'approve_auto', self.user)
        self.assertFalse(DeliveryAllocation.objects.exists())
        self.assertFalse(InvoiceDeliveryLink.objects.exists())

    def test_missing_units_dates_duplicates_and_no_bl_do_not_create_evidence(self):
        self.delivery.date_livraison = date(2004, 10, 1); self.delivery.save()
        self.assertFalse(automatic.build_plan(self.invoice)['entries'])
        self.delivery.date_livraison = date(2026, 10, 1); self.delivery.save()
        self.dl.unite = ''; self.dl.save()
        self.assertFalse(automatic.build_plan(self.invoice)['entries'])
        self.dl.unite = 'kg'; self.dl.save()
        other = self.make_invoice('F-1')
        self.assertFalse(automatic.build_plan(self.invoice)['entries'])
        other.delete()
        self.dl.delete(); self.delivery.delete()
        self.decide()
        self.assertFalse(DeliveryAllocation.objects.exists())

    def test_conversion_requires_previously_confirmed_mapping(self):
        self.dl.unite = 'carton'; self.dl.quantite_livree = 2; self.dl.save()
        self.assertFalse(automatic.build_plan(self.invoice)['entries'])
        DeliveryProductMapping.objects.create(fournisseur=self.supplier,
            invoice_key=service.product_key(self.line), delivery_key=service.product_key(self.dl),
            invoice_unit='kg', delivery_unit='carton', factor=Decimal('5'),
            reason='Carton contenant 5 kg', confirmed_by=self.user)
        self.assertTrue(automatic.build_plan(self.invoice)['complete'])
        self.decide()
        self.assertEqual(DeliveryAllocation.objects.get().factor, 5)

    def test_no_bl_policy_never_invents_received_quantities(self):
        self.invoice.delivery_mode = 'invoice_only'; self.invoice.save()
        self.assertFalse(automatic.build_plan(self.invoice)['entries'])
        self.decide()
        self.assertFalse(DeliveryAllocation.objects.exists())
        self.assertEqual(service.reconciliation_state(self.invoice)['rows'][0]['allocated'], 0)
        self.assertFalse(service.reconciliation_state(self.invoice)['rows'][0]['has_evidence'])
