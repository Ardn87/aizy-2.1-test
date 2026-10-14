from django.core.management.base import BaseCommand

from core.models import DeliveryIssue
from core.services.delivery_claims import add_delivery_issue_to_claim


class Command(BaseCommand):
    help = "Rattache les problèmes de livraison déjà confirmés aux brouillons fournisseurs, sans doublon."

    def handle(self, *args, **options):
        added = 0
        for issue in DeliveryIssue.objects.filter(state=DeliveryIssue.State.CONFIRMED).iterator():
            _, created = add_delivery_issue_to_claim(issue.pk)
            added += int(created)
        self.stdout.write(self.style.SUCCESS(f"{added} problème(s) de livraison ajouté(s) aux réclamations."))
