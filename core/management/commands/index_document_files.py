import hashlib
from django.core.management.base import BaseCommand
from core.models import Facture,BonLivraison,CatalogueFournisseur

class Command(BaseCommand):
    help='Indexe les empreintes des fichiers existants sans changer leurs données métier ni supprimer de doublon.'
    def handle(self,*args,**options):
        count=0
        for model,field in [(Facture,'fichier'),(BonLivraison,'fichier'),(CatalogueFournisseur,'fichier_original')]:
            for doc in model.all_objects.filter(file_sha256='').iterator():
                source=getattr(doc,field)
                if not source:continue
                try:
                    with source.open('rb') as f:
                        h=hashlib.sha256()
                        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
                    model.all_objects.filter(pk=doc.pk).update(file_sha256=h.hexdigest());count+=1
                except OSError:self.stderr.write(f'Fichier absent : {model._meta.model_name} #{doc.pk}')
        self.stdout.write(f'{count} empreintes indexées. Aucun document supprimé.')
