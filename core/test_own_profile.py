from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse


class OwnProfileTests(TestCase):
    def setUp(self):
        self.users = {}
        for role in ('Admin', 'Gestionnaire', 'Reception', 'Revue'):
            user = get_user_model().objects.create_user(username=role)
            group, _ = Group.objects.get_or_create(name=role)
            user.groups.add(group)
            self.users[role] = user

    def test_header_links_to_allowed_profile_and_has_no_password_link(self):
        for role, user in self.users.items():
            with self.subTest(role=role):
                self.client.force_login(user)
                response = self.client.get(reverse('own_profile'))
                self.assertEqual(response.status_code, 200)
                expected = reverse('user_update', args=[user.pk]) if role == 'Admin' else reverse('own_profile')
                self.assertContains(response, f'class="profile-name" href="{expected}"')
                self.assertNotContains(response, '>Mon mot de passe</a>')
                self.assertContains(response, 'Changer mon mot de passe')
                self.assertContains(response, 'Déconnexion')

    def test_self_edit_cannot_change_permissions_or_target_other_account(self):
        for role in ('Gestionnaire', 'Reception', 'Revue'):
            with self.subTest(role=role):
                user = self.users[role]
                self.client.force_login(user)
                response = self.client.post(reverse('own_profile'), {
                    'username': role + '-modifie', 'first_name': 'Camille',
                    'last_name': 'Martin', 'email': 'camille@example.test',
                    'pk': self.users['Admin'].pk, 'is_superuser': '1',
                    'is_staff': '1', 'is_active': '', 'role': 'Admin',
                    'groups': Group.objects.get(name='Admin').pk,
                    'password': 'forged-password',
                })
                self.assertRedirects(response, reverse('own_profile'))
                user.refresh_from_db()
                self.assertEqual(user.first_name, 'Camille')
                self.assertEqual(user.username, role + '-modifie')
                self.assertEqual(user.email, 'camille@example.test')
                self.assertFalse(user.is_superuser)
                self.assertFalse(user.is_staff)
                self.assertTrue(user.is_active)
                self.assertFalse(user.has_usable_password())
                self.assertEqual(list(user.groups.values_list('name', flat=True)), [role])
                self.assertEqual(self.client.get(reverse('user_update', args=[self.users['Admin'].pk])).status_code, 403)
        self.users['Admin'].refresh_from_db()
        self.assertEqual(self.users['Admin'].username, 'Admin')

    def test_validation_preserves_profile(self):
        user = self.users['Revue']
        self.client.force_login(user)
        for data, field in (({'username': 'Admin'}, 'username'), ({'username': ''}, 'username'),
                            ({'username': 'Revue', 'email': 'invalid'}, 'email')):
            response = self.client.post(reverse('own_profile'), data)
            self.assertEqual(response.status_code, 200)
            self.assertIn(field, response.context['form'].errors)
        user.refresh_from_db()
        self.assertEqual(user.username, 'Revue')

    def test_anonymous_cannot_read_or_edit_profile(self):
        for method in (self.client.get, self.client.post):
            response = method(reverse('own_profile'))
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response.url.startswith(reverse('login')))
