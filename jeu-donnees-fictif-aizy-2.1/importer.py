#!/usr/bin/env python3
"""Import explicite sur une installation vide ; aucun compte importé ou remplacé."""
import argparse, os, sys, json, shutil, tempfile, hashlib
from pathlib import Path
ROOT=Path(__file__).resolve().parent

def run():
    from django.apps import apps
    from django.conf import settings
    version_path=Path(settings.BASE_DIR)/'VERSION'
    if not version_path.is_file() or not version_path.read_text().strip().startswith('2.1.'):
        raise RuntimeError('Ce paquet cible Aizy 2.1 ; la version figée 2.0 ne doit pas être remplie.')
    from django.core.management import call_command
    from django.db import transaction
    fixture=ROOT/'donnees-fictives.json'
    records=json.loads(fixture.read_text(encoding='utf-8'))
    manifest=json.loads((ROOT/'manifest.json').read_text(encoding='utf-8'))
    checks=json.loads((ROOT/'empreintes.json').read_text(encoding='utf-8'))
    for rel,expected in checks.items():
        path=(ROOT/rel).resolve()
        if not path.is_relative_to(ROOT) or hashlib.sha256(path.read_bytes()).hexdigest()!=expected:
            raise RuntimeError('Fichier absent ou modifié : '+rel)
    allowed=set(manifest['counts'])
    if not manifest.get('fictif') or not records or any(x['model'] not in {'core.'+name for name in allowed} for x in records):
        raise RuntimeError('Paquet invalide : uniquement des données métier fictives sont acceptées.')
    media=Path(settings.MEDIA_ROOT).resolve(); dest=media/'fictif-aizy'
    for obj in records:
        for key in ('fichier','fichier_original'):
            filename=obj['fields'].get(key)
            if filename and (not filename.startswith('fictif-aizy/') or not (media/filename).resolve().is_relative_to(dest)):
                raise RuntimeError('Chemin de document invalide.')
    models=list(apps.get_app_config('core').get_models())
    media.mkdir(parents=True,exist_ok=True)
    if dest.exists(): raise RuntimeError('Les documents fictifs sont déjà présents. Aucun fichier remplacé.')
    staging=Path(tempfile.mkdtemp(prefix='.aizy-import-',dir=media)); published=False
    try:
        shutil.copytree(ROOT/'media'/'fictif-aizy',staging/'fictif-aizy')
        with transaction.atomic():
            # Verrouillage des tables métier sur PostgreSQL : aucune écriture concurrente pendant l'import.
            from django.db import connection
            if connection.vendor=='postgresql':
                with connection.cursor() as cursor:
                    tables=', '.join(connection.ops.quote_name(m._meta.db_table) for m in models)
                    cursor.execute('LOCK TABLE '+tables+' IN ACCESS EXCLUSIVE MODE')
            occupied=[m._meta.verbose_name for m in models if m._base_manager.exists()]
            if occupied: raise RuntimeError('Import refusé : la base contient déjà des données métier. Aucun effacement effectué.')
            call_command('loaddata',str(fixture),verbosity=0)
            for name,n in manifest['counts'].items():
                if apps.get_model('core',name)._base_manager.count()!=n:
                    raise RuntimeError('Nombre de lignes inattendu : '+name)
            for model in models:
                for field in model._meta.fields:
                    if field.get_internal_type()=='FileField':
                        for value in model._base_manager.exclude(**{field.name:''}).values_list(field.name,flat=True):
                            if not (staging/value).is_file(): raise RuntimeError('Document manquant : '+value)
            (staging/'fictif-aizy').rename(dest); published=True
        print(f"Import terminé : {manifest['cas']} scénarios fictifs. Comptes et mots de passe inchangés.")
        print('Pour les comptes limités, attribuez ensuite les accès aux restaurants fictifs dans Utilisateurs.')
    except BaseException:
        if published: shutil.rmtree(dest)
        raise
    finally:
        shutil.rmtree(staging,ignore_errors=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app',default=str(ROOT.parent))
    args=parser.parse_args(); app=Path(args.app).resolve()
    if not (app/'manage.py').is_file(): parser.error('Dossier application introuvable.')
    sys.path.insert(0,str(app)); os.environ.setdefault('DJANGO_SETTINGS_MODULE','facture_project.settings')
    import django
    django.setup()
    try: run()
    except Exception as exc:
        print('Import interrompu : '+str(exc),file=sys.stderr); sys.exit(1)
