"""Fixtures explicites pour les anciens tests, sans contourner les règles de production."""
from django.contrib.auth import get_user_model
from .models import Restaurant, RestaurantAccess, Facture

def restaurant_fixture(**kwargs):
    restaurant=Restaurant.objects.create(**kwargs)
    RestaurantAccess.objects.bulk_create([RestaurantAccess(user=user,restaurant=restaurant) for user in get_user_model().objects.all()])
    return restaurant

def invoice_fixture(**kwargs):
    if kwargs.get('statut')=='validee':
        kwargs.setdefault('validation_exception','Fixture : document contrôlé pour ce scénario de test.')
    return Facture.objects.create(**kwargs)

def upload_fixture(name, content, **kwargs):
    from django.core.files.uploadedfile import SimpleUploadedFile
    ext=name.lower().rsplit('.',1)[-1]
    if ext=='pdf' and not content.startswith(b'%PDF-'):content=b'%PDF-1.7\n'+content
    if ext=='png' and not content.startswith(b'\x89PNG\r\n\x1a\n'):content=b'\x89PNG\r\n\x1a\n'+content
    if ext in ('jpg','jpeg') and not content.startswith(b'\xff\xd8\xff'):content=b'\xff\xd8\xff'+content
    return SimpleUploadedFile(name,content,**kwargs)
