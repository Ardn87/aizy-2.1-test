from urllib.parse import urlsplit, parse_qs

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse

from . import test_price_review as fixtures
from . import test_delivery_claims as delivery_fixtures
from .forms import FournisseurForm
from .models import Fournisseur, Restaurant, RestaurantAccess, SupplierClaim
from .services.claim_documents import mail_content


class ClaimMailTests(TestCase):
    setUp = fixtures.PriceReviewTests.setUp
    action = fixtures.PriceReviewTests.action

    def prepare(self):
        self.supplier.email = 'reclamations.bianchi@example.invalid'
        self.supplier.save(update_fields=['email'])
        self.action('claim')
        claim = SupplierClaim.objects.get()
        self.client.post(reverse('claim_state', args=[claim.pk]), {'action':'prepare'})
        claim.refresh_from_db()
        return claim

    def test_mailto_contains_full_utf8_text_and_address_without_sending(self):
        claim = self.prepare()
        data = mail_content(claim)
        parsed = urlsplit(data['url'])
        self.assertEqual(parsed.path, self.supplier.email)
        params = parse_qs(parsed.query)
        self.assertEqual(params['subject'], [f'Réclamation n° {claim.pk} — Bianchi'])
        self.assertIn('Facture F1', params['body'][0])
        self.assertIn('Poulet', params['body'][0])
        self.assertIn('8.84 CHF', params['body'][0])
        self.assertIn('16,80 CHF', params['body'][0])
        self.assertIn('%0D%0A', data['url'])
        page = self.client.get(reverse('supplier_claims'), {'claim':claim.pk})
        self.assertContains(page, 'Envoyer par mail')
        self.assertContains(page, 'Adresse fictive de test')
        self.assertNotContains(page, '.eml')
        self.assertNotContains(page, 'pièce jointe')
        self.assertEqual(set(params), {'subject', 'body'})
        claim.refresh_from_db()
        self.assertEqual(claim.state, 'prepared')

    def test_inline_preview_is_same_origin_and_download_remains_an_attachment(self):
        claim = self.prepare()
        url = reverse('claim_document', args=[claim.pk])
        preview = self.client.get(url, {'preview':'1'})
        self.assertContains(preview, 'Réclamation fournisseur')
        self.assertEqual(preview['X-Frame-Options'], 'SAMEORIGIN')
        self.assertTrue(preview['Content-Disposition'].startswith('inline;'))
        self.assertTrue(self.client.get(url)['Content-Disposition'].startswith('attachment;'))
        self.assertEqual(self.client.get(reverse('dashboard'))['X-Frame-Options'], 'DENY')

    def test_missing_or_invalid_contact_is_editable_and_never_injected(self):
        claim = self.prepare()
        for address in ('', 'invalid', 'safe@example.invalid\r\nBcc: hidden@example.invalid'):
            self.supplier.email=address
            self.supplier.save(update_fields=['email'])
            claim.fournisseur=self.supplier
            self.assertEqual(mail_content(claim)['url'], '')
            page = self.client.get(reverse('supplier_claims'), {'claim':claim.pk})
            self.assertContains(page, 'Renseigner le contact mail')
            self.assertNotContains(page, 'href="mailto:')
        self.assertEqual(FournisseurForm().fields['email'].label, 'Contact mail')

    def test_name_header_injection_and_html_are_not_executed(self):
        claim = self.prepare()
        self.supplier.nom='Bianchi\r\nBcc: hidden@example.invalid <script>alert(1)</script>'
        self.supplier.save(update_fields=['nom'])
        claim.fournisseur=self.supplier
        params=parse_qs(urlsplit(mail_content(claim)['url']).query)
        self.assertEqual(set(params), {'subject', 'body'})
        self.assertNotIn('\r', params['subject'][0])
        self.assertNotIn('\n', params['subject'][0])
        document=self.client.get(reverse('claim_document', args=[claim.pk]), {'preview':'1'})
        self.assertNotContains(document, '<script>')
        self.assertContains(document, '&lt;script&gt;')

    def test_draft_and_cancelled_cannot_download_preview_or_email(self):
        claim=self.prepare()
        for state in ('draft', 'cancelled'):
            claim.state=state; claim.save(update_fields=['state'])
            self.assertEqual(self.client.get(reverse('claim_document', args=[claim.pk]), {'preview':'1'}).status_code, 302)
            self.assertNotContains(self.client.get(reverse('supplier_claims'), {'claim':claim.pk}), 'href="mailto:')

    def test_role_and_restaurant_restrictions_apply_to_mail_and_preview(self):
        claim=self.prepare()
        other=Restaurant.objects.create(nom='Interdit')
        supplier=Fournisseur.objects.create(restaurant=other, nom='Invisible')
        foreign=SupplierClaim.objects.create(fournisseur=supplier, state='prepared')
        RestaurantAccess.objects.get_or_create(user=self.user, restaurant=self.restaurant)
        self.assertEqual(self.client.get(reverse('claim_document', args=[foreign.pk])).status_code, 404)
        self.assertNotContains(self.client.get(reverse('supplier_claims'), {'claim':foreign.pk}), 'Invisible')
        for role in ('Reception', 'Revue'):
            user=get_user_model().objects.create_user(role)
            user.groups.add(Group.objects.get_or_create(name=role)[0])
            self.client.force_login(user)
            self.assertEqual(self.client.get(reverse('claim_document', args=[claim.pk])).status_code, 403)
            self.assertEqual(self.client.get(reverse('supplier_claims')).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get(reverse('supplier_claims')).status_code, 302)


class DeliveryMailTests(TestCase):
    setUp = delivery_fixtures.DeliveryClaimTests.setUp
    confirm = delivery_fixtures.DeliveryClaimTests.confirm
    price_draft = delivery_fixtures.DeliveryClaimTests.price_draft

    def test_mixed_claim_contains_invoice_and_bl_comment_without_inventing_delivery_amount(self):
        claim=self.price_draft(currency='CHF')
        self.confirm(review_note='Remplacement demandé.')
        self.client.post(reverse('claim_state', args=[claim.pk]), {'action':'prepare'})
        claim.refresh_from_db()
        body=mail_content(claim)['body']
        self.assertIn('Facture F-01', body)
        self.assertIn('Bon de livraison BL-2026-01', body)
        self.assertIn(self.issue.comment, body)
        self.assertIn('Remplacement demandé.', body)
        self.assertIn('Montant à déterminer', body)
        self.assertIn('5,00 CHF', body)
        # Later changes must not rewrite the prepared document snapshot.
        self.issue.comment='Autre commentaire'; self.issue.save(update_fields=['comment'])
        self.assertEqual(body, mail_content(claim)['body'])
