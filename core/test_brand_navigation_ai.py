import json,os,stat
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase,override_settings
from django.urls import reverse
from core.models import Restaurant,RestaurantAccess
from core.services.ai_settings import save_local_openai_settings
from core.services.openai_config import get_openai_config_status
from django.core.exceptions import ValidationError


class BrandNavigationAITests(TestCase):
    def setUp(self):
        self.restaurant=Restaurant.objects.create(nom='Restaurant')
        self.users={}
        for role in ['Admin','Gestionnaire','Reception','Revue']:
            group,_=Group.objects.get_or_create(name=role)
            user=get_user_model().objects.create_user(role,password='Local-aizy-test-2026')
            user.groups.add(group)
            RestaurantAccess.objects.create(user=user,restaurant=self.restaurant)
            self.users[role]=user

    def test_login_displays_supplied_brand_and_slogans(self):
        response=self.client.get(reverse('login'))
        self.assertContains(response,'brand/aizy-logo-avec-nom.')
        self.assertContains(response,'brand/aizy-icone.')
        self.assertContains(response,'Chaque écart compte.')
        self.assertContains(response,'Simplifiez vos contrôles,')
        self.assertContains(response,'chaque jour.')
        self.assertContains(response,'data-password-toggle')
        self.assertNotContains(response,'Documents fournisseurs')

    def test_header_home_links_preserve_restricted_roles(self):
        for role,name in [('Reception','document_deposit'),('Revue','price_review')]:
            self.client.force_login(self.users[role])
            response=self.client.get(reverse(name));self.assertEqual(response.status_code,200)
            self.assertContains(response,'id="home-menu-link"')
            self.assertNotContains(response,'id="mobile-menu"')
            self.assertEqual(response.context['nav_home_url'],reverse(name))
            self.assertEqual(len(response.context['nav_sections']),1)
            self.assertEqual(response.context['nav_sections'][0]['url'],reverse(name))
            self.assertContains(response,'#nav-menu')
            self.assertNotContains(response,reverse('ai_diagnostic'))

    def test_admin_has_illustrated_sections_and_configuration_shortcut(self):
        self.client.force_login(self.users['Admin'])
        response=self.client.get(reverse('dashboard'))
        sections=response.context['nav_sections']
        self.assertTrue(all(s['icon'] for s in sections))
        self.assertIn(reverse('ai_diagnostic'),[s['url'] for s in sections])
        self.assertContains(response,'#home-catalogue')
        self.assertEqual([g['key'] for g in response.context['nav_groups']],['daily','documents','reference','history','admin'])
        self.assertEqual({s['name'] for s in response.context['nav_groups'][0]['sections']},{'price_review','reconciliation_queue','delivery_issue_review','supplier_claims'})
        self.assertNotContains(response,'class="app-mobile-nav"')
        self.assertEqual(response.context['nav_home_url'],reverse('dashboard'))
        self.client.force_login(self.users['Gestionnaire'])
        response=self.client.get(reverse('dashboard'))
        admin_group=next(g for g in response.context['nav_groups'] if g['key']=='admin')
        self.assertEqual({s['name'] for s in admin_group['sections']},{'work_documents','restaurant_list'})
        self.assertNotContains(response,'Déposer, contrôler et traiter les écarts.')
        self.assertNotContains(response,reverse('user_list'))

    @override_settings(DEBUG=True)
    def test_admin_saves_configuration_privately_and_never_renders_the_key(self):
        self.client.force_login(self.users['Admin'])
        with TemporaryDirectory() as folder,patch.dict(os.environ,{'OPENAI_KEYS_FILE':str(Path(folder)/'openai.keys.json'),'OPENAI_API_KEY':'','OPENAI_MODEL':''}):
            key=('sk' + '-test-never-render-this-value')
            response=self.client.post(reverse('ai_diagnostic'),{'action':'configure','api_key':key,'model':'gpt-4.1-mini'},follow=True)
            self.assertContains(response,'Configuration enregistrée')
            self.assertNotContains(response,key)
            path=Path(folder)/'openai.keys.json'
            self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o600)
            self.assertEqual(json.loads(path.read_text())['OPENAI_API_KEY'],key)
            self.assertTrue(get_openai_config_status()['configured'])
            response=self.client.post(reverse('ai_diagnostic'),{'action':'configure','api_key':'','model':'gpt-4.1'},follow=True)
            self.assertEqual(json.loads(path.read_text())['OPENAI_API_KEY'],key)
            self.assertEqual(get_openai_config_status()['model'],'gpt-4.1')
            self.assertNotContains(response,key)
            with patch('core.views.test_openai_connection',return_value='OK') as call:
                response=self.client.post(reverse('ai_diagnostic'),{'action':'test'},follow=True)
                call.assert_called_once();self.assertContains(response,'Connexion OpenAI OK')

    @override_settings(DEBUG=True)
    def test_missing_key_invalid_model_and_nonadmin_cannot_write_configuration(self):
        with TemporaryDirectory() as folder,patch.dict(os.environ,{'OPENAI_KEYS_FILE':str(Path(folder)/'openai.keys.json'),'OPENAI_API_KEY':'','OPENAI_MODEL':''}):
            path=Path(folder)/'openai.keys.json'
            self.client.force_login(self.users['Admin'])
            response=self.client.post(reverse('ai_diagnostic'),{'action':'configure','api_key':'','model':'gpt-4.1-mini'})
            self.assertContains(response,'Renseignez la clé API');self.assertFalse(path.exists())
            response=self.client.post(reverse('ai_diagnostic'),{'action':'configure','api_key':('sk' + '-test-hidden'),'model':'bad model'})
            self.assertNotContains(response,('sk' + '-test-hidden'));self.assertFalse(path.exists())
            for role in ['Gestionnaire','Reception','Revue']:
                self.client.force_login(self.users[role])
                self.assertEqual(self.client.post(reverse('ai_diagnostic'),{'action':'configure','api_key':('sk' + '-test-hidden'),'model':'gpt-4.1-mini'}).status_code,403)
            self.assertFalse(path.exists())

    @override_settings(DEBUG=False)
    def test_production_configuration_remains_environment_only(self):
        self.client.force_login(self.users['Admin'])
        with TemporaryDirectory() as folder,patch.dict(os.environ,{'OPENAI_KEYS_FILE':str(Path(folder)/'openai.keys.json'),'OPENAI_API_KEY':'','OPENAI_MODEL':''}):
            self.assertEqual(self.client.post(reverse('ai_diagnostic'),{'action':'configure','api_key':('sk' + '-test-hidden'),'model':'gpt-4.1-mini'}).status_code,403)
            self.assertFalse((Path(folder)/'openai.keys.json').exists())

    @override_settings(DEBUG=True)
    def test_empty_configuration_shows_model_without_claiming_analysis_is_active(self):
        with TemporaryDirectory() as folder,patch.dict(os.environ,{'OPENAI_KEYS_FILE':str(Path(folder)/'openai.keys.json'),'OPENAI_API_KEY':'','OPENAI_MODEL':''}):
            (Path(folder)/'openai.keys.json').write_text(json.dumps({'OPENAI_API_KEY':'','OPENAI_MODEL':'gpt-4.1-mini'}))
            status=get_openai_config_status();self.assertFalse(status['configured']);self.assertEqual(status['model'],'gpt-4.1-mini')
            self.client.force_login(self.users['Admin'])
            response=self.client.get(reverse('ai_diagnostic'))
            self.assertContains(response,'L’analyse est en attente de configuration')
            self.assertContains(response,'disabled')

    @override_settings(DEBUG=True)
    def test_device_or_directory_is_rejected_without_overwriting_it(self):
        with patch.dict(os.environ,{'OPENAI_KEYS_FILE':'/dev/null','OPENAI_API_KEY':'','OPENAI_MODEL':''}):
            with self.assertRaises(ValidationError):save_local_openai_settings('test','gpt-4.1-mini')
        with TemporaryDirectory() as folder,patch.dict(os.environ,{'OPENAI_KEYS_FILE':folder,'OPENAI_API_KEY':'','OPENAI_MODEL':''}):
            with self.assertRaises(ValidationError):save_local_openai_settings('test','gpt-4.1-mini')
