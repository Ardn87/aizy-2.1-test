from django.contrib import admin

from .models import (
    AIDiagnosticEvent,
    BonLivraison,
    CatalogueFournisseur,
    DeliveryIssue,
    Facture,
    Fournisseur,
    LigneCatalogue,
    LigneBonLivraison,
    LigneFacture,
    Restaurant,
)


class AuditedAdmin(admin.ModelAdmin):
    def delete_model(self, request, obj):
        from .models import DocumentModel
        from .services.operations import archive_document
        if isinstance(obj, DocumentModel):
            archive_document(obj)
        elif hasattr(obj, 'archived_at'):
            from django.utils import timezone
            obj.archived_at=timezone.now();obj.save()
        else:
            from django.core.exceptions import PermissionDenied
            raise PermissionDenied('Désactivez cet objet pour conserver les documents liés.')
    def delete_queryset(self, request, queryset):
        for obj in queryset:
            self.delete_model(request,obj)
    def save_formset(self, request, form, formset, change):
        from .models import LigneCatalogue
        from .services.price_review import record_price
        instances=formset.save(commit=False)
        for instance in instances:
            old=type(instance).all_objects.filter(pk=instance.pk).first() if instance.pk else None
            if hasattr(instance,'corrigee_manuellement'):instance.corrigee_manuellement=True
            instance.save()
            if isinstance(instance,LigneCatalogue) and (old is None or old.prix_unitaire_ht != instance.prix_unitaire_ht):
                record_price(instance,request.user,old.prix_unitaire_ht if old else None)
        formset.save_m2m()

    def save_model(self, request, obj, form, change):
        if hasattr(obj, 'corrigee_manuellement'):
            obj.corrigee_manuellement=True
        from .models import LigneCatalogue
        from .services.price_review import record_price
        old = type(obj).all_objects.filter(pk=obj.pk).first() if obj.pk and hasattr(type(obj),'all_objects') else None
        super().save_model(request,obj,form,change)
        if isinstance(obj,LigneCatalogue) and (old is None or old.prix_unitaire_ht != obj.prix_unitaire_ht):
            record_price(obj,request.user,old.prix_unitaire_ht if old else None)

@admin.register(Restaurant)
class RestaurantAdmin(AuditedAdmin):
    list_display = ("nom", "devise_defaut", "actif", "date_creation")
    list_filter = ("devise_defaut", "actif", "date_creation")
    search_fields = ("nom", "adresse")
    ordering = ("nom",)


@admin.register(Fournisseur)
class FournisseurAdmin(AuditedAdmin):
    list_display = ("nom", "restaurant", "devise_defaut", "email", "telephone", "actif", "date_creation")
    list_filter = ("restaurant", "devise_defaut", "actif", "date_creation")
    search_fields = ("nom", "email", "telephone", "restaurant__nom")
    autocomplete_fields = ("restaurant",)
    list_select_related = ("restaurant",)
    ordering = ("restaurant__nom", "nom")


class LigneFactureInline(admin.TabularInline):
    model = LigneFacture
    extra = 0
    can_delete = False
    fields = (
        "ordre",
        "reference_produit",
        "ean",
        "designation",
        "quantite",
        "unite",
        "prix_unitaire_ht",
        "montant_ht",
        "taux_tva",
        "montant_ttc",
        "corrigee_manuellement",
    )


@admin.register(Facture)
class FactureAdmin(AuditedAdmin):
    list_display = (
        "numero_facture",
        "fournisseur",
        "date_facture",
        "date_livraison_detectee",
        "devise",
        "montant_ht",
        "montant_tva",
        "montant_ttc",
        "statut",
        "confiance_ia",
        "date_import",
    )
    list_filter = (
        "statut",
        "devise",
        "fournisseur__restaurant",
        "fournisseur",
        "date_facture",
        "date_livraison_detectee",
        "date_import",
    )
    search_fields = ("numero_facture", "fournisseur__nom", "fournisseur__restaurant__nom", "note")
    autocomplete_fields = ("fournisseur",)
    list_select_related = ("fournisseur", "fournisseur__restaurant")
    readonly_fields = ("date_import", "date_modification")
    date_hierarchy = "date_import"
    ordering = ("-date_import",)
    inlines = (LigneFactureInline,)


@admin.register(LigneFacture)
class LigneFactureAdmin(AuditedAdmin):
    list_display = (
        "facture",
        "ordre",
        "reference_produit",
        "ean",
        "designation",
        "quantite",
        "prix_unitaire_ht",
        "montant_ht",
        "montant_ttc",
        "corrigee_manuellement",
    )
    list_filter = ("corrigee_manuellement", "facture__fournisseur__restaurant", "facture__fournisseur")
    search_fields = (
        "reference_produit",
        "ean",
        "designation",
        "facture__numero_facture",
        "facture__fournisseur__nom",
    )
    autocomplete_fields = ("facture",)
    ordering = ("facture", "ordre", "pk")


class LigneCatalogueInline(admin.TabularInline):
    model = LigneCatalogue
    extra = 0
    can_delete = False
    fields = (
        "ordre",
        "reference_produit",
        "ean",
        "designation",
        "marque",
        "unite",
        "conditionnement",
        "prix_unitaire_ht",
        "statut_ligne",
        "corrigee_manuellement",
    )


@admin.register(CatalogueFournisseur)
class CatalogueFournisseurAdmin(AuditedAdmin):
    list_display = (
        "nom",
        "fournisseur",
        "date_debut",
        "date_fin",
        "devise",
        "statut",
        "confiance_ia",
        "date_import",
    )
    list_filter = (
        "statut",
        "devise",
        "fournisseur__restaurant",
        "fournisseur",
        "date_debut",
        "date_fin",
        "date_import",
    )
    search_fields = ("nom", "fournisseur__nom", "fournisseur__restaurant__nom", "note")
    autocomplete_fields = ("fournisseur",)
    list_select_related = ("fournisseur", "fournisseur__restaurant")
    readonly_fields = ("date_import", "date_modification")
    date_hierarchy = "date_import"
    ordering = ("-date_import",)
    inlines = (LigneCatalogueInline,)


@admin.register(LigneCatalogue)
class LigneCatalogueAdmin(AuditedAdmin):
    list_display = (
        "catalogue",
        "ordre",
        "reference_produit",
        "ean",
        "designation",
        "prix_unitaire_ht",
        "statut_ligne",
        "corrigee_manuellement",
    )
    list_filter = (
        "statut_ligne",
        "corrigee_manuellement",
        "catalogue__statut",
        "catalogue__fournisseur__restaurant",
        "catalogue__fournisseur",
    )
    search_fields = (
        "reference_produit",
        "ean",
        "designation",
        "marque",
        "catalogue__nom",
        "catalogue__fournisseur__nom",
    )
    autocomplete_fields = ("catalogue",)
    ordering = ("catalogue", "ordre", "pk")


class LigneBonLivraisonInline(admin.TabularInline):
    model = LigneBonLivraison
    extra = 0
    can_delete = False
    fields = (
        "ordre",
        "reference_produit",
        "designation",
        "quantite_livree",
        "quantite_originale",
        "unite",
        "conditionnement",
        "prix_unitaire",
        "total_ligne",
        "confiance_ia",
        "statut_ligne",
        "corrigee_manuellement",
    )


class DeliveryIssueInline(admin.TabularInline):
    model = DeliveryIssue
    fk_name = "delivery"
    extra = 0
    can_delete = False
    fields = ("line", "state", "comment", "reported_by", "created_at", "reviewed_by", "reviewed_at")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(BonLivraison)
class BonLivraisonAdmin(AuditedAdmin):
    list_display = (
        "numero_bon_livraison",
        "fournisseur",
        "date_livraison",
        "statut",
        "delivery_feedback_requested_at",
        "delivery_feedback_completed_at",
        "confiance_ia",
        "date_import",
    )
    list_filter = (
        "statut",
        "fournisseur__restaurant",
        "fournisseur",
        "date_livraison",
        "date_import",
    )
    search_fields = (
        "numero_bon_livraison",
        "fournisseur__nom",
        "fournisseur__restaurant__nom",
        "note",
    )
    autocomplete_fields = ("fournisseur",)
    list_select_related = ("fournisseur", "fournisseur__restaurant")
    readonly_fields = (
        "delivery_feedback_requested_at",
        "delivery_feedback_completed_at",
        "delivery_feedback_completed_by",
        "date_import",
        "date_modification",
    )
    date_hierarchy = "date_import"
    ordering = ("-date_import",)
    inlines = (LigneBonLivraisonInline, DeliveryIssueInline)


@admin.register(LigneBonLivraison)
class LigneBonLivraisonAdmin(AuditedAdmin):
    list_display = (
        "bon_livraison",
        "ordre",
        "reference_produit",
        "designation",
        "quantite_livree",
        "unite",
        "prix_unitaire",
        "total_ligne",
        "statut_ligne",
        "confiance_ia",
        "corrigee_manuellement",
    )
    list_filter = (
        "statut_ligne",
        "corrigee_manuellement",
        "bon_livraison__statut",
        "bon_livraison__fournisseur__restaurant",
        "bon_livraison__fournisseur",
    )
    search_fields = (
        "reference_produit",
        "designation",
        "texte_brut",
        "bon_livraison__numero_bon_livraison",
        "bon_livraison__fournisseur__nom",
    )
    autocomplete_fields = ("bon_livraison",)
    ordering = ("bon_livraison", "ordre", "pk")


@admin.register(DeliveryIssue)
class DeliveryIssueAdmin(AuditedAdmin):
    list_display = (
        "state",
        "line",
        "delivery",
        "reported_by",
        "created_at",
        "reviewed_by",
        "reviewed_at",
    )
    list_filter = (
        "state",
        "delivery__fournisseur__restaurant",
        "delivery__fournisseur",
        "created_at",
        "reviewed_at",
    )
    search_fields = (
        "comment",
        "review_note",
        "line__reference_produit",
        "line__designation",
        "delivery__numero_bon_livraison",
        "delivery__fournisseur__nom",
    )
    autocomplete_fields = ("reviewed_by",)
    readonly_fields = (
        "delivery",
        "line",
        "comment",
        "source_fingerprint",
        "snapshot",
        "reported_by",
        "created_at",
        "updated_at",
    )
    list_select_related = (
        "line",
        "delivery",
        "delivery__fournisseur",
        "reported_by",
        "reviewed_by",
    )
    date_hierarchy = "created_at"
    ordering = ("-created_at", "-pk")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AIDiagnosticEvent)
class AIDiagnosticEventAdmin(AuditedAdmin):
    list_display = ("event_type", "source", "document_type", "document_id", "date_creation")
    list_filter = ("event_type", "source", "document_type", "date_creation")
    search_fields = ("source", "document_type", "message")
    readonly_fields = ("event_type", "source", "document_type", "document_id", "message", "date_creation")
    ordering = ("-date_creation",)


from .models import BusinessAudit, AnalysisJob, AnalysisVersion, RestaurantAccess

@admin.register(BusinessAudit)
class BusinessAuditAdmin(admin.ModelAdmin):
    list_display=('date_creation','actor','model_label','object_id','action')
    readonly_fields=tuple(f.name for f in BusinessAudit._meta.fields)
    def has_add_permission(self, request): return False
    def has_delete_permission(self, request,obj=None): return False
    def has_change_permission(self, request,obj=None): return False

@admin.register(RestaurantAccess)
class RestaurantAccessAdmin(admin.ModelAdmin):
    list_display=('user','restaurant')
    autocomplete_fields=('user','restaurant')

@admin.register(AnalysisJob)
class AnalysisJobAdmin(admin.ModelAdmin):
    list_display=('document_type','document_id','state','attempts','started_at','finished_at')
    readonly_fields=tuple(f.name for f in AnalysisJob._meta.fields)
    def has_add_permission(self,request):return False
    def has_delete_permission(self,request,obj=None):return False
    def has_change_permission(self,request,obj=None):return False
