from django.db import migrations
from django.conf import settings


def preserve_access(apps, schema_editor):
    User=apps.get_model(settings.AUTH_USER_MODEL)
    Restaurant=apps.get_model('core','Restaurant')
    Access=apps.get_model('core','RestaurantAccess')
    Access.objects.bulk_create([Access(user_id=u,restaurant_id=r) for u in User.objects.values_list('pk',flat=True) for r in Restaurant.objects.values_list('pk',flat=True)],ignore_conflicts=True)
    # Historique validé : conserver le statut sans inventer un auteur ou refaire une validation.
    for model, status in [('Facture','validee'),('BonLivraison','valide')]:
        from django.db.models import F
        apps.get_model('core',model).objects.filter(statut=status).update(verified_at=F('date_modification'))

class Migration(migrations.Migration):
    dependencies=[('core','0013_loginattempt_bonlivraison_archived_at_and_more')]
    operations=[migrations.RunPython(preserve_access,migrations.RunPython.noop)]
