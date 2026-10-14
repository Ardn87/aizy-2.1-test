from collections import Counter
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from core.models import CatalogueFournisseur
from core.request_context import current_user, current_reason
from core.services.catalogue_builder import fill_catalogue_prices, usable_observation, supplier_products, product_key


class Command(BaseCommand):
    help = 'Renseigne les prix manquants depuis un document du même produit ; conserve le prix présent et la provenance.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--actor', type=int)

    def handle(self, *args, **options):
        actor = get_user_model().objects.filter(pk=options['actor'], is_active=True).first() if options['actor'] else None
        if options['apply'] and (actor is None or not (actor.is_superuser or actor.groups.filter(name='Admin').exists())):
            raise CommandError('Un administrateur identifié est requis.')
        token = current_user.set(actor)
        reason = current_reason.set('Initialisation des prix catalogue depuis les documents d’origine à la demande de l’utilisateur.')
        total = Counter()
        try:
            with transaction.atomic():
                for catalogue in CatalogueFournisseur.objects.select_related('fournisseur'):
                    if options['apply']:
                        result = fill_catalogue_prices(catalogue, actor)
                    else:
                        groups = supplier_products(catalogue.fournisseur)
                        result = Counter()
                        for line in catalogue.lignes.all():
                            observations = line.observations or groups.get(product_key(line.reference_produit, line.ean, line.designation, line.unite), [])
                            result['existing' if line.prix_unitaire_ht is not None else 'filled' if usable_observation(observations) else 'missing'] += 1
                    total.update(result)
                    self.stdout.write(f'{catalogue.fournisseur.nom} : {result.get("filled",0)} prix récupérables, {result.get("missing",0)} sans prix source')
                self.stdout.write(str(dict(total)))
        finally:
            current_reason.reset(reason)
            current_user.reset(token)
