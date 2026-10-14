from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from core.models import CatalogueFournisseur, Fournisseur
from core.request_context import current_user, current_reason
from core.services.catalogue_builder import build_supplier_catalogue, supplier_products
from core.services.operations import archive_document


class Command(BaseCommand):
    help = 'Regroupe les produits des BL et factures dans un catalogue par fournisseur ; ne confirme aucun prix.'
    def add_arguments(self, parser):
        parser.add_argument('--apply',action='store_true')
        parser.add_argument('--archive-misclassified',nargs='*',type=int,default=[])
        parser.add_argument('--actor',type=int)
    def handle(self,*args,**options):
        actor = get_user_model().objects.filter(pk=options['actor'],is_active=True).first() if options['actor'] else None
        if options['apply'] and (actor is None or not (actor.is_superuser or actor.groups.filter(name='Admin').exists())):
            raise CommandError('Un administrateur identifié est requis pour appliquer cette reconstruction.')
        if not options['apply']:
            for supplier in Fournisseur.objects.all():
                self.stdout.write(f'{supplier.nom} : {len(supplier_products(supplier))} produits')
            return
        user_token = current_user.set(actor)
        reason_token = current_reason.set('Reconstruction demandée : produits issus des BL et factures ; anciens documents classés à tort comme catalogues archivés.')
        try:
            with transaction.atomic():
                targets = list(CatalogueFournisseur.objects.filter(pk__in=options['archive_misclassified'],origine='fournisseur'))
                if len(targets)!=len(set(options['archive_misclassified'])):
                    raise CommandError('Certains catalogues à archiver sont introuvables ou déjà reconstruits.')
                for catalogue in targets: archive_document(catalogue)
                total = 0
                for supplier in Fournisseur.objects.all():
                    catalogue, result = build_supplier_catalogue(supplier)
                    total += result['products']
                    self.stdout.write(f'{supplier.nom} : catalogue #{catalogue.pk}, {result["products"]} produits ({result["added"]} ajoutés)')
                self.stdout.write(self.style.SUCCESS(f'{total} produits regroupés. {len(targets)} anciens documents archivés, fichiers conservés. Aucun prix de référence confirmé automatiquement.'))
        finally:
            current_reason.reset(reason_token);current_user.reset(user_token)
