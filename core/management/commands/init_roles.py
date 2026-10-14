from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand

from core.access import ROLE_ADMIN, ROLE_GESTIONNAIRE, ROLE_RECEPTION, ROLE_REVUE
from core.models import BonLivraison, CatalogueFournisseur, Facture, Fournisseur, LigneCatalogue, Restaurant


def model_permissions(model, actions):
    content_type = ContentType.objects.get_for_model(model)
    codenames = [f"{action}_{model._meta.model_name}" for action in actions]
    return Permission.objects.filter(content_type=content_type, codename__in=codenames)


class Command(BaseCommand):
    help = "Cree ou met a jour les groupes Reception, Revue, Gestionnaire et Admin."

    def handle(self, *args, **options):
        reception_group, _ = Group.objects.get_or_create(name=ROLE_RECEPTION)
        review_group, _ = Group.objects.get_or_create(name=ROLE_REVUE)
        gestionnaire_group, _ = Group.objects.get_or_create(name=ROLE_GESTIONNAIRE)
        admin_group, _ = Group.objects.get_or_create(name=ROLE_ADMIN)

        reception_permissions = (model_permissions(BonLivraison, ["add"]) | model_permissions(Facture, ["add"]))

        gestionnaire_permissions = (
            model_permissions(Restaurant, ["add", "change", "view"])
            | model_permissions(Fournisseur, ["add", "change", "view"])
            | model_permissions(Facture, ["add", "change", "view"])
            | model_permissions(CatalogueFournisseur, ["add", "change", "view"])
            | model_permissions(LigneCatalogue, ["add", "change", "view"])
            | model_permissions(BonLivraison, ["add", "change", "view"])
        )

        admin_permissions = Permission.objects.all()

        review_group.permissions.clear()  # Les actions Revue sont autorisées uniquement par leurs vues dédiées.
        reception_group.permissions.set(reception_permissions)
        gestionnaire_group.permissions.set(gestionnaire_permissions)
        admin_group.permissions.set(admin_permissions)

        self.stdout.write(self.style.SUCCESS("Groupes et permissions initialises."))
        self.stdout.write(f"- {ROLE_REVUE}: accès limité à la revue des prix")
        self.stdout.write(f"- {ROLE_RECEPTION}: {reception_permissions.count()} permission(s)")
        self.stdout.write(f"- {ROLE_GESTIONNAIRE}: {gestionnaire_permissions.count()} permission(s)")
        self.stdout.write(f"- {ROLE_ADMIN}: {admin_permissions.count()} permission(s)")
