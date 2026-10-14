from decimal import Decimal
from django.db.models.signals import pre_save, post_save, post_delete
from django.dispatch import receiver
from django.db.models import F
from django.core.exceptions import ValidationError
from django.utils import timezone
from .models import (BusinessModel, DocumentModel, BusinessAudit, Fournisseur, Facture, BonLivraison,
    CatalogueFournisseur, LigneFacture, LigneCatalogue, LigneBonLivraison, UnitConversion,
    InvoiceControlCache, SupplierClaim)
from .request_context import current_user, current_reason


def record(instance):
    excluded = {'donnees_ia', 'source_data', 'control_revision'}
    return {f.attname: str(getattr(instance, f.attname)) if getattr(instance, f.attname) is not None else None
            for f in instance._meta.concrete_fields if f.name not in excluded}


def restaurant_id(instance):
    path = getattr(instance, 'restaurant_path', '')
    obj = instance
    try:
        for part in path.split('__'):
            obj = getattr(obj, part)
        return obj
    except (AttributeError, instance.DoesNotExist):
        return None


@receiver(pre_save)
def before_business_save(sender, instance, raw=False, **kwargs):
    if raw or not isinstance(instance, BusinessModel):
        return
    old = sender.all_objects.filter(pk=instance.pk).first() if instance.pk else None
    instance._audit_before = record(old) if old else {}
    if isinstance(instance, (Facture, BonLivraison)) and instance.statut in ('validee', 'valide'):
        from .services.quality import validation_problems
        # Only guard a new validation or an edited validated document, not unrelated background metadata.
        if old is None or old.statut != instance.statut or any(getattr(old,k)!=getattr(instance,k) for k in (['numero_facture','date_facture','devise','kind','montant_ht','montant_tva','montant_ttc'] if isinstance(instance,Facture) else ['date_livraison'])):
            problems = validation_problems(instance)
            if problems and len(instance.validation_exception.strip()) < 8:
                raise ValidationError(' '.join(problems) + ' Corrigez les données ou indiquez une dérogation motivée.')
            instance.verified_at = timezone.now()
            user = current_user.get()
            instance.verified_by = user if user and user.is_authenticated else None


@receiver(post_save)
def after_business_save(sender, instance, created=False, raw=False, **kwargs):
    if raw or not isinstance(instance, BusinessModel):
        return
    before = getattr(instance, '_audit_before', {})
    after = record(instance)
    if before != after:
        user = current_user.get()
        BusinessAudit.objects.create(restaurant_id=restaurant_id(instance), actor=user if user and user.is_authenticated else None,
            model_label=sender._meta.label_lower, object_id=instance.pk, action='create' if created else 'update',
            reason=current_reason.get() or getattr(instance, 'validation_exception', ''), before=before, after=after)
    invalidate(instance)


@receiver(post_delete)
def after_business_delete(sender, instance, **kwargs):
    if isinstance(instance, BusinessModel):
        user = current_user.get()
        BusinessAudit.objects.create(restaurant_id=restaurant_id(instance), actor=user if user and user.is_authenticated else None,
            model_label=sender._meta.label_lower, object_id=instance.pk, action='delete', reason=current_reason.get(), before=record(instance))
        invalidate(instance)


def invalidate(instance):
    if isinstance(instance, Fournisseur):
        supplier_id = instance.pk
    elif isinstance(instance, (Facture, BonLivraison, CatalogueFournisseur, UnitConversion, SupplierClaim)):
        supplier_id = instance.fournisseur_id
    elif isinstance(instance, LigneFacture):
        supplier_id = instance.facture.fournisseur_id
        if instance.corrigee_manuellement:
            Facture.all_objects.filter(pk=instance.facture_id, verified_at__isnull=False).update(verified_at=None, verified_by=None, statut='a_valider')
    elif isinstance(instance, LigneCatalogue):
        supplier_id = instance.catalogue.fournisseur_id
    elif isinstance(instance, LigneBonLivraison):
        supplier_id = instance.bon_livraison.fournisseur_id
        if instance.corrigee_manuellement:
            BonLivraison.all_objects.filter(pk=instance.bon_livraison_id, verified_at__isnull=False).update(verified_at=None, verified_by=None, statut='a_verifier')
    else:
        return
    Fournisseur.all_objects.filter(pk=supplier_id).update(control_revision=F('control_revision') + 1)
    InvoiceControlCache.objects.filter(invoice__fournisseur_id=supplier_id).delete()

from .models import PriceReviewEvent
@receiver(post_save,sender=PriceReviewEvent)
def journal_review_event(sender,instance,created=False,**kwargs):
    rid=instance.line.facture.fournisseur.restaurant_id if instance.line_id else None
    if rid is None and instance.before.get('claim_id'):
        claim=SupplierClaim.objects.filter(pk=instance.before['claim_id']).select_related('fournisseur').first()
        rid=claim.fournisseur.restaurant_id if claim else None
    BusinessAudit.objects.create(restaurant_id=rid,actor=instance.actor,model_label='core.pricereviewevent',object_id=instance.pk,
        action='undo' if instance.undone else instance.action,before=instance.before,after=instance.after,reason=current_reason.get())
