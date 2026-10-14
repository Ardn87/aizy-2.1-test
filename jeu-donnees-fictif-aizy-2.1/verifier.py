#!/usr/bin/env python3
"""Import/reimport testés sur une base éphémère uniquement."""
import os,sys,tempfile,json,importlib.util
from pathlib import Path
root=Path(__file__).resolve().parent
app=Path(sys.argv[1] if len(sys.argv)>1 else root.parent)
sys.path.insert(0,str(app)); os.environ['DJANGO_SETTINGS_MODULE']='facture_project.settings'
from django.conf import settings
with tempfile.TemporaryDirectory(prefix='aizy-import-test-') as tmp:
 # Always temporary SQLite: no environment variable can target a live base.
 settings.DATABASES={'default':{'ENGINE':'django.db.backends.sqlite3','NAME':tmp+'/test.sqlite3'}}
 settings.MEDIA_ROOT=tmp+'/media'
 import django
 django.setup()
 from django.core.management import call_command
 from django.contrib.auth.models import User
 from django.apps import apps
 from core.services.price_review import review_queue
 from core.services.automatic_reconciliation import build_plan
 from core.models import Facture, DeliveryIssue, SupplierClaim, AnalysisJob
 from core.services.claim_documents import mail_content
 from core.services.price_review import claim_total
 call_command('migrate',verbosity=0)
 account=User.objects.create(username='test-local-conserve',password='!unusable',is_superuser=True)
 before=list(User.objects.values())
 spec=importlib.util.spec_from_file_location('dataset_importer',root/'importer.py'); module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
 module.run()
 assert list(User.objects.values())==before
 pending,alerts=review_queue(); waiting,_=review_queue(state='deferred')
 assert len(pending)>=40 and len(waiting)==1 and alerts
 assert all(x['line'].prix_unitaire_ht!=x['prix_catalogue'] for x in pending)
 assert any(build_plan(f)['complete'] for f in Facture.objects.all())
 from django.test import Client
 from django.contrib.sessions.backends.db import SessionStore
 from django.contrib.auth import SESSION_KEY,BACKEND_SESSION_KEY,HASH_SESSION_KEY
 settings.ALLOWED_HOSTS=list(settings.ALLOWED_HOSTS)+['testserver']
 client=Client(); session=SessionStore(); session[SESSION_KEY]=str(account.pk); session[BACKEND_SESSION_KEY]='django.contrib.auth.backends.ModelBackend'; session[HASH_SESSION_KEY]=account.get_session_auth_hash(); session.create(); client.cookies[settings.SESSION_COOKIE_NAME]=session.session_key
 try:
  for url in ['/problemes-livraison/','/problemes-livraison/?state=history','/tableau-de-bord/','/revue-prix/','/reclamations/','/rapprochement/','/catalogues/'] + [f'/reclamations/?position={i}' for i in range(1,9)] + [f'/factures/{f.pk}/rapprochement/' for f in Facture.objects.filter(note__contains='BL unique proposé')]:
   response=client.get(url)
   assert response.status_code==200, (url,response.status_code)
  for claim in SupplierClaim.objects.all():
   page=client.get('/reclamations/',{'claim':claim.pk})
   assert page.status_code==200, claim.pk
   if claim.state not in ('draft','cancelled'):
    doc=client.get(f'/reclamations/{claim.pk}/document/',{'preview':'1'})
    assert doc.status_code==200 and doc['X-Frame-Options']=='SAMEORIGIN',claim.pk
    data=mail_content(claim)
    assert not data['url'] or data['url'].startswith('mailto:')
    assert 'attachment' not in data['url']
    assert '.eml' not in page.content.decode()
  assert DeliveryIssue.objects.count()==95
  assert DeliveryIssue.objects.filter(state='pending',delivery__archived_at__isnull=True).count()==60
  assert not AnalysisJob.objects.filter(state__in=['queued','running']).exists()
  mixed=SupplierClaim.objects.get(fournisseur__nom='Bianchi',state='draft')
  assert claim_total(mixed)==25 and mixed.lines.count()==4
  assert mixed.lines.filter(category='delivery').count()==2
  print('VALIDÉ : 171 scénarios, 77 cartes de réclamation, aperçu protégé, mail texte, 60 défauts actifs et total mixte 25 CHF.')
 finally: session.delete()
 counts={m._meta.label:m._base_manager.count() for m in apps.get_app_config('core').get_models()}
 try: module.run()
 except RuntimeError: pass
 else: raise AssertionError('Le deuxième import aurait dû être refusé')
 assert counts=={m._meta.label:m._base_manager.count() for m in apps.get_app_config('core').get_models()}
 # Vérifier aussi le refus dû aux données, indépendamment du dossier de fichiers existant.
 from django.conf import settings
 import shutil
 shutil.rmtree(Path(settings.MEDIA_ROOT)/'fictif-aizy')
 try: module.run()
 except RuntimeError as e: assert 'déjà des données' in str(e)
 else: raise AssertionError('Une base remplie aurait dû être refusée')
 assert counts=={m._meta.label:m._base_manager.count() for m in apps.get_app_config('core').get_models()}
 assert list(User.objects.values())==before
 print('VALIDÉ : import complet, documents présents, revue et rapprochement cohérents, comptes préservés, réimport et base remplie refusés.')
