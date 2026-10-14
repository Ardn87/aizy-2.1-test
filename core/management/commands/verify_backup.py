from pathlib import Path
import hashlib
import json
from django.core.management.base import BaseCommand, CommandError
from django.conf import settings
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from core.models import Facture,BonLivraison,CatalogueFournisseur

class Command(BaseCommand):
    help='Vérifie migrations, références media, empreintes et relations métier après restauration isolée.'
    def add_arguments(self,parser):
        parser.add_argument('--manifest')
    def handle(self,*args,**options):
        if MigrationExecutor(connection).migration_plan(MigrationExecutor(connection).loader.graph.leaf_nodes()):
            raise CommandError('Migrations non appliquées à la sauvegarde.')
        root=Path(settings.MEDIA_ROOT).resolve();files={};errors=[]
        for model,field in [(Facture,'fichier'),(BonLivraison,'fichier'),(CatalogueFournisseur,'fichier_original')]:
            for doc in model.all_objects.all().iterator():
                name=getattr(doc,field).name
                if not name:continue
                path=(root/name).resolve()
                if not path.is_relative_to(root) or not path.is_file():errors.append(f'{model._meta.model_name} #{doc.pk} : fichier absent');continue
                h=hashlib.sha256()
                with path.open('rb') as f:
                    for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
                digest=h.hexdigest();files[name]=digest
                if doc.file_sha256 and doc.file_sha256!=digest:errors.append(f'{model._meta.model_name} #{doc.pk} : empreinte différente')
        if options.get('manifest'):
            expected=json.loads(Path(options['manifest']).read_text())
            if any(files.get(name)!=digest for name,digest in expected.get('files',{}).items()):errors.append('Empreintes différentes du manifeste.')
        if errors:raise CommandError('; '.join(errors[:20]))
        self.stdout.write(json.dumps({'files':files,'counts':{m._meta.model_name:m.all_objects.count() for m in (Facture,BonLivraison,CatalogueFournisseur)}},sort_keys=True))
