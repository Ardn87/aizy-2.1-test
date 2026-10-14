from django.core.exceptions import ValidationError
from django.db import models
from django.conf import settings


class Devise(models.TextChoices):
    UNKNOWN = "UNK", "À confirmer"
    EUR = "EUR", "EUR"
    CHF = "CHF", "CHF"


def facture_upload_path(instance, filename):
    return f"factures/{instance.fournisseur_id}/{filename}"


def bon_livraison_upload_path(instance, filename):
    return f"bons_livraison/{instance.fournisseur_id}/{filename}"


def catalogue_upload_path(instance, filename):
    return f"catalogues/{instance.fournisseur_id}/{filename}"


class BusinessManager(models.Manager):
    def get_queryset(self):
        from .request_context import current_user, restaurant_ids
        qs = super().get_queryset()
        if any(f.name == "archived_at" for f in self.model._meta.fields):
            qs = qs.filter(archived_at__isnull=True)
        parent_path = {'lignefacture':'facture', 'lignecatalogue':'catalogue', 'lignebonlivraison':'bon_livraison'}.get(self.model._meta.model_name)
        if parent_path:
            qs = qs.filter(**{parent_path + '__archived_at__isnull':True})
        user = current_user.get()
        if user is not None and user.is_authenticated:
            from .access import user_can_access_admin
            if not user_can_access_admin(user):
                ids = restaurant_ids(user)
                path = self.model.restaurant_path
                qs = qs.filter(**{path + "__in": ids})
        return qs


class BusinessModel(models.Model):
    objects = BusinessManager()
    all_objects = models.Manager()
    class Meta:
        abstract = True


class DocumentModel(BusinessModel):
    archived_at = models.DateTimeField(null=True, blank=True, editable=False)
    file_sha256 = models.CharField(max_length=64, blank=True, db_index=True, editable=False)
    validation_exception = models.TextField(blank=True, verbose_name="Motif de dérogation")
    verified_at = models.DateTimeField(null=True, blank=True, editable=False)
    verified_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, blank=True, editable=False, related_name="+")
    def clean(self):
        super().clean()
        if getattr(self, 'statut', '') in ('validee','valide'):
            from .services.quality import validation_problems
            problems = validation_problems(self)
            if problems and len(self.validation_exception.strip()) < 8:
                raise ValidationError({'validation_exception':' '.join(problems) + ' Corrigez ou motivez la dérogation.'})

    class Meta:
        abstract = True


class Restaurant(BusinessModel):
    restaurant_path = "pk"
    nom = models.CharField(max_length=255)
    adresse = models.TextField(blank=True)
    devise_defaut = models.CharField(max_length=3, choices=Devise.choices, default=Devise.EUR)
    actif = models.BooleanField(default=True)
    date_creation = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["nom"]

    def __str__(self):
        return self.nom


class DeliveryMode(models.TextChoices):
    SEPARATE = "separate", "BL et facture séparés"
    INVOICE_ONLY = "invoice_only", "Facture uniquement"
    COMBINED = "combined", "Document BL et facture"
    MIXED = "mixed", "Variable · à confirmer"


class Fournisseur(BusinessModel):
    restaurant_path = "restaurant_id"
    control_revision = models.PositiveBigIntegerField(default=0, editable=False)
    delivery_mode = models.CharField(max_length=16, choices=DeliveryMode.choices, default=DeliveryMode.MIXED)
    delivery_wait_days = models.PositiveSmallIntegerField(default=14)
    restaurant = models.ForeignKey(
        Restaurant,
        on_delete=models.CASCADE,
        related_name="fournisseurs",
    )
    nom = models.CharField(max_length=255)
    email = models.EmailField(blank=True)
    telephone = models.CharField(max_length=30, blank=True)
    devise_defaut = models.CharField(max_length=3, choices=Devise.choices, default=Devise.EUR)
    actif = models.BooleanField(default=True)
    date_creation = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["restaurant__nom", "nom"]

    def __str__(self):
        return f"{self.nom} - {self.restaurant.nom}"


class Facture(DocumentModel):
    restaurant_path = "fournisseur__restaurant_id"
    class Kind(models.TextChoices):
        INVOICE = "invoice", "Facture"
        CREDIT = "credit", "Avoir"
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.INVOICE)
    delivery_mode = models.CharField(max_length=16, choices=DeliveryMode.choices, blank=True)
    delivery_wait_until = models.DateField(null=True, blank=True)
    reconciliation_note = models.TextField(blank=True)
    duplicate_dismissals = models.JSONField(default=dict, blank=True, editable=False)
    reconciliation_decision = models.JSONField(default=dict, blank=True, editable=False)
    document_role = models.CharField(max_length=12, default='active', choices=[
        ('active', 'Facture à contrôler'), ('duplicate', 'Duplicata'),
        ('replaced', 'Facture remplacée'), ('statement', 'Relevé récapitulatif')])
    replaces_with = models.ForeignKey('self', on_delete=models.PROTECT, null=True, blank=True, related_name='replaced_documents')
    source_delivery = models.OneToOneField('BonLivraison', on_delete=models.PROTECT, null=True, blank=True, related_name='combined_invoice')

    class Statut(models.TextChoices):
        IMPORTEE = "importee", "Importée"
        ANALYSE_EN_COURS = "analyse_en_cours", "Analyse en cours"
        A_VALIDER = "a_valider", "À valider"
        ANALYSE_ECHOUEE = "analyse_echouee", "Analyse échouée"
        VALIDEE = "validee", "Validée"
        LITIGE = "litige", "Litige"

    fournisseur = models.ForeignKey(
        Fournisseur,
        on_delete=models.CASCADE,
        related_name="factures",
    )
    fichier = models.FileField(upload_to=facture_upload_path)
    numero_facture = models.CharField(max_length=100, blank=True)
    date_facture = models.DateField(null=True, blank=True)
    date_livraison_detectee = models.DateField(null=True, blank=True)
    devise = models.CharField(max_length=3, choices=Devise.choices, default=Devise.EUR)
    montant_ht = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    montant_tva = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    montant_ttc = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    statut = models.CharField(
        max_length=20,
        choices=Statut.choices,
        default=Statut.IMPORTEE,
    )
    note = models.TextField(blank=True)
    donnees_ia = models.JSONField(null=True, blank=True)
    confiance_ia = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    erreur_analyse = models.TextField(blank=True)
    date_import = models.DateTimeField(auto_now_add=True)
    date_modification = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["fournisseur","statut","date_import"])]
        ordering = ["-date_import"]

    def __str__(self):
        if self.numero_facture:
            return self.numero_facture
        return f"Facture {self.pk or ''}".strip()


class LigneFacture(BusinessModel):
    restaurant_path = "facture__fournisseur__restaurant_id"
    class Kind(models.TextChoices):
        PRODUCT = "product", "Produit"
        FEE = "fee", "Frais"
        DISCOUNT = "discount", "Remise"
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.PRODUCT)

    facture = models.ForeignKey(
        Facture,
        on_delete=models.CASCADE,
        related_name="lignes",
    )
    reference_produit = models.CharField(max_length=100, blank=True)
    ean = models.CharField(max_length=50, blank=True)
    designation = models.CharField(max_length=255, blank=True)
    quantite = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    unite = models.CharField(max_length=30, blank=True)
    prix_unitaire_ht = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    montant_ht = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    taux_tva = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    montant_ttc = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    ordre = models.PositiveIntegerField(default=0)
    corrigee_manuellement = models.BooleanField(default=False)

    source_key = models.CharField(max_length=80, blank=True, editable=False)
    source_data = models.JSONField(default=dict, blank=True, editable=False)
    source_page = models.PositiveIntegerField(null=True, blank=True)
    archived_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["facture","source_key"], condition=~models.Q(source_key=""), name="unique_facture_source_row")]
        ordering = ["ordre", "pk"]

    def __str__(self):
        return self.designation or self.reference_produit or f"Ligne facture {self.pk or ''}".strip()


class CatalogueFournisseur(DocumentModel):
    restaurant_path = "fournisseur__restaurant_id"
    OVERLAP_ERROR_MESSAGE = "Ce catalogue chevauche une période existante."

    class Origine(models.TextChoices):
        FOURNISSEUR = "fournisseur", "Tarif fourni par le fournisseur"
        DOCUMENTS = "documents", "Produits issus des BL et factures"

    origine = models.CharField(max_length=20, choices=Origine.choices, default=Origine.FOURNISSEUR)

    class TypePrix(models.TextChoices):
        HT = "ht", "HT"

    class Statut(models.TextChoices):
        IMPORTE = "importe", "Importé"
        ANALYSE_EN_COURS = "analyse_en_cours", "Analyse en cours"
        A_VALIDER = "a_valider", "À valider"
        ANALYSE_ECHOUEE = "analyse_echouee", "Analyse échouée"
        VALIDE = "valide", "Validé"
        ERREUR = "erreur", "Erreur"

    fournisseur = models.ForeignKey(
        Fournisseur,
        on_delete=models.CASCADE,
        related_name="catalogues",
    )
    nom = models.CharField(max_length=255, blank=True)
    fichier_original = models.FileField(upload_to=catalogue_upload_path, blank=True)
    date_debut = models.DateField(null=True, blank=True)
    date_fin = models.DateField(null=True, blank=True)
    devise = models.CharField(max_length=3, choices=Devise.choices, default=Devise.EUR)
    type_prix = models.CharField(
        max_length=20,
        choices=TypePrix.choices,
        default=TypePrix.HT,
    )
    statut = models.CharField(
        max_length=20,
        choices=Statut.choices,
        default=Statut.IMPORTE,
    )
    donnees_ia = models.JSONField(null=True, blank=True)
    confiance_ia = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    erreur_analyse = models.TextField(blank=True)
    note = models.TextField(blank=True)
    date_import = models.DateTimeField(auto_now_add=True)
    date_modification = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["fournisseur","statut","date_import"])]
        constraints = [models.UniqueConstraint(fields=['fournisseur'], condition=models.Q(origine='documents', archived_at__isnull=True), name='unique_active_document_catalogue')]
        ordering = ["fournisseur__restaurant__nom", "fournisseur__nom", "-date_debut", "-date_import"]

    def __init__(self, *args, **kwargs):
        self._devise_provided_on_init = "devise" in kwargs
        super().__init__(*args, **kwargs)

    def __str__(self):
        return self.nom or f"Catalogue {self.pk or ''}".strip()

    @property
    def is_initial_reference(self):
        return (self.origine == self.Origine.DOCUMENTS
                and self.statut == self.Statut.A_VALIDER
                and self.date_debut is None and self.date_fin is None
                and self.archived_at is None)

    def clean(self):
        super().clean()

        if self.date_debut and self.date_fin and self.date_fin < self.date_debut:
            raise ValidationError({"date_fin": "La date de fin doit être postérieure à la date de début."})

        if self.statut != self.Statut.VALIDE or not self.fournisseur_id:
            return

        valid_catalogues = CatalogueFournisseur.objects.filter(
            fournisseur_id=self.fournisseur_id,
            statut=self.Statut.VALIDE,
        )
        if self.pk:
            valid_catalogues = valid_catalogues.exclude(pk=self.pk)

        for catalogue in valid_catalogues:
            if self.period_overlaps(catalogue):
                raise ValidationError(self.OVERLAP_ERROR_MESSAGE)

    def save(self, *args, **kwargs):
        if self._state.adding and not self._devise_provided_on_init and self.fournisseur_id and self.devise == Devise.EUR:
            self.devise = self.fournisseur.devise_defaut
        self.full_clean()
        return super().save(*args, **kwargs)

    def period_overlaps(self, other):
        return (
            self.period_starts_before_or_on(other.date_fin)
            and other.period_starts_before_or_on(self.date_fin)
        )

    def period_starts_before_or_on(self, other_end):
        return other_end is None or self.date_debut is None or self.date_debut <= other_end


class LigneCatalogue(BusinessModel):
    restaurant_path = "catalogue__fournisseur__restaurant_id"
    class StatutLigne(models.TextChoices):
        OK = "ok", "OK"
        A_VERIFIER = "a_verifier", "À vérifier"
        IGNOREE = "ignoree", "Ignorée"

    catalogue = models.ForeignKey(
        CatalogueFournisseur,
        on_delete=models.CASCADE,
        related_name="lignes",
    )
    reference_produit = models.CharField(max_length=100, blank=True)
    ean = models.CharField(max_length=50, blank=True)
    designation = models.CharField(max_length=255, blank=True)
    marque = models.CharField(max_length=100, blank=True)
    unite = models.CharField(max_length=30, blank=True)
    conditionnement = models.CharField(max_length=100, blank=True)
    prix_unitaire_ht = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    statut_ligne = models.CharField(
        max_length=20,
        choices=StatutLigne.choices,
        default=StatutLigne.OK,
    )
    ordre = models.PositiveIntegerField(default=0)
    corrigee_manuellement = models.BooleanField(default=False)
    observations = models.JSONField(default=list, blank=True, editable=False)
    price_source = models.JSONField(default=dict, blank=True, editable=False)

    source_key = models.CharField(max_length=80, blank=True, editable=False)
    source_data = models.JSONField(default=dict, blank=True, editable=False)
    source_page = models.PositiveIntegerField(null=True, blank=True)
    archived_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["catalogue","source_key"], condition=~models.Q(source_key=""), name="unique_catalogue_source_row")]
        ordering = ["ordre", "pk"]

    def __str__(self):
        return self.designation or self.reference_produit or self.ean or f"Ligne catalogue {self.pk or ''}".strip()

    def clean(self):
        super().clean()
        if self.statut_ligne == self.StatutLigne.IGNOREE:
            return
        if self.requires_verification():
            self.statut_ligne = self.StatutLigne.A_VERIFIER

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def requires_verification(self):
        return (
            self.is_missing_product_identifier()
            or self.is_missing_usable_price()
            or self.has_inconsistent_price()
        )

    def is_missing_product_identifier(self):
        return not self.reference_produit.strip() and not self.ean.strip()

    def is_missing_usable_price(self):
        return all(price is None for price in self.price_values())

    def has_inconsistent_price(self):
        if any(price is not None and price < 0 for price in self.price_values()):
            return True
        return False

    def price_values(self):
        return (
            self.prix_unitaire_ht,
        )


class BonLivraison(DocumentModel):
    restaurant_path = "fournisseur__restaurant_id"
    class Statut(models.TextChoices):
        UPLOADE = "uploade", "Uploadé"
        ANALYSE_EN_COURS = "analyse_en_cours", "Analyse en cours"
        IMPORTE = "importe", "Importé"
        A_VERIFIER = "a_verifier", "À vérifier"
        ANALYSE_ECHOUEE = "analyse_echouee", "Analyse échouée"
        VALIDE = "valide", "Validé"
        LITIGE = "litige", "Litige"

    fournisseur = models.ForeignKey(
        Fournisseur,
        on_delete=models.CASCADE,
        related_name="bons_livraison",
    )
    fichier = models.FileField(upload_to=bon_livraison_upload_path)
    numero_bon_livraison = models.CharField(max_length=100, blank=True)
    date_livraison = models.DateField(null=True, blank=True)
    statut = models.CharField(
        max_length=20,
        choices=Statut.choices,
        default=Statut.UPLOADE,
    )
    note = models.TextField(blank=True)
    donnees_ia = models.JSONField(null=True, blank=True)
    confiance_ia = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    erreur_analyse = models.TextField(blank=True)
    delivery_feedback_requested_at = models.DateTimeField(null=True, blank=True, editable=False)
    delivery_feedback_completed_at = models.DateTimeField(null=True, blank=True, editable=False)
    delivery_feedback_completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        editable=False,
        related_name="+",
    )
    date_import = models.DateTimeField(auto_now_add=True)
    date_modification = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["fournisseur","statut","date_import"])]
        ordering = ["-date_import"]

    def __str__(self):
        if self.numero_bon_livraison:
            return self.numero_bon_livraison
        return f"Bon livraison {self.pk or ''}".strip()


class LigneBonLivraison(BusinessModel):
    restaurant_path = "bon_livraison__fournisseur__restaurant_id"
    class StatutLigne(models.TextChoices):
        OK = "ok", "OK"
        A_VERIFIER = "a_verifier", "À vérifier"
        IGNOREE = "ignoree", "Ignorée"

    bon_livraison = models.ForeignKey(
        BonLivraison,
        on_delete=models.CASCADE,
        related_name="lignes",
    )
    reference_produit = models.CharField(max_length=100, blank=True)
    designation = models.CharField(max_length=255, blank=True)
    quantite_livree = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    quantite_originale = models.CharField(max_length=100, blank=True)
    unite = models.CharField(max_length=30, blank=True)
    conditionnement = models.CharField(max_length=100, blank=True)
    prix_unitaire = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    prix_unitaire_original = models.CharField(max_length=100, blank=True)
    total_ligne = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    total_ligne_original = models.CharField(max_length=100, blank=True)
    confiance_ia = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    texte_brut = models.TextField(blank=True)
    statut_ligne = models.CharField(
        max_length=20,
        choices=StatutLigne.choices,
        default=StatutLigne.OK,
    )
    ordre = models.PositiveIntegerField(default=0)
    corrigee_manuellement = models.BooleanField(default=False)

    source_key = models.CharField(max_length=80, blank=True, editable=False)
    source_data = models.JSONField(default=dict, blank=True, editable=False)
    source_page = models.PositiveIntegerField(null=True, blank=True)
    archived_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["bon_livraison","source_key"], condition=~models.Q(source_key=""), name="unique_bon_livraison_source_row")]
        ordering = ["ordre", "pk"]

    def __str__(self):
        return self.designation or self.reference_produit or f"Ligne BL {self.pk or ''}".strip()

    def clean(self):
        super().clean()
        if self.statut_ligne == self.StatutLigne.IGNOREE:
            return
        if self.requires_verification():
            self.statut_ligne = self.StatutLigne.A_VERIFIER

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def requires_verification(self):
        return (
            not self.designation.strip()
            or self.quantite_livree is None
            or self.has_inconsistent_numbers()
            or self.has_low_confidence()
        )

    def has_inconsistent_numbers(self):
        return any(
            value is not None and value < 0
            for value in (self.quantite_livree, self.prix_unitaire, self.total_ligne)
        )

    def has_low_confidence(self):
        return self.confiance_ia is not None and self.confiance_ia < 0.60


class DeliveryIssue(BusinessModel):
    """Problème de livraison signalé sur un produit extrait d'un BL."""

    restaurant_path = "delivery__fournisseur__restaurant_id"

    class State(models.TextChoices):
        PENDING = "pending", "À vérifier"
        CONFIRMED = "confirmed", "Confirmé"
        DISMISSED = "dismissed", "Classé sans suite"

    delivery = models.ForeignKey(
        BonLivraison,
        on_delete=models.PROTECT,
        related_name="delivery_issues",
    )
    line = models.OneToOneField(
        LigneBonLivraison,
        on_delete=models.PROTECT,
        related_name="delivery_issue",
    )
    state = models.CharField(max_length=12, choices=State.choices, default=State.PENDING)
    comment = models.TextField(max_length=2000)
    source_fingerprint = models.CharField(max_length=64)
    snapshot = models.JSONField(default=dict, blank=True)
    reported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    review_note = models.TextField(max_length=2000, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at", "pk"]
        indexes = [models.Index(fields=["state", "created_at"])]

    def __str__(self):
        return f"{self.get_state_display()} · {self.line}"

    def clean(self):
        super().clean()
        if self.delivery_id and self.line_id and self.line.bon_livraison_id != self.delivery_id:
            raise ValidationError({"line": "Le produit doit appartenir au bon de livraison signalé."})
        self.comment = (self.comment or "").strip()
        if not 5 <= len(self.comment) <= 2000:
            raise ValidationError({"comment": "Décrivez le défaut en 5 à 2 000 caractères."})

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class AIDiagnosticEvent(models.Model):
    class EventType(models.TextChoices):
        SUCCESS = "success", "Succès"
        ERROR = "error", "Erreur"

    event_type = models.CharField(max_length=20, choices=EventType.choices)
    source = models.CharField(max_length=50)
    document_type = models.CharField(max_length=30, blank=True)
    document_id = models.PositiveIntegerField(null=True, blank=True)
    message = models.TextField(blank=True)
    date_creation = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date_creation"]

    def __str__(self):
        return f"{self.get_event_type_display()} - {self.source}"


class ProductAssociation(BusinessModel):
    restaurant_path = "fournisseur__restaurant_id"
    fournisseur = models.ForeignKey(Fournisseur, on_delete=models.CASCADE)
    identifier = models.CharField(max_length=160)
    target_reference = models.CharField(max_length=100, blank=True)
    target_ean = models.CharField(max_length=50, blank=True)
    date_modification = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["fournisseur", "identifier"], name="unique_supplier_product_alias")]


class PriceReviewDecision(models.Model):
    class State(models.TextChoices):
        DEFERRED = "deferred", "En attente"
        RESOLVED = "resolved", "Traité"
        CLAIMED = "claimed", "Réclamation"

    line = models.OneToOneField(LigneFacture, on_delete=models.CASCADE, related_name="price_decision")
    fingerprint = models.CharField(max_length=64)
    state = models.CharField(max_length=12, choices=State.choices)
    reason = models.TextField(blank=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    date_modification = models.DateTimeField(auto_now=True)


class SupplierClaim(BusinessModel):
    restaurant_path = "fournisseur__restaurant_id"
    class State(models.TextChoices):
        DRAFT = "draft", "Brouillon"
        PREPARED = "prepared", "Préparée"
        SENT = "sent", "Envoi confirmé"
        REPLIED = "replied", "Réponse reçue"
        AWAITING_CREDIT = "credit_due", "Avoir attendu"
        CREDIT_RECEIVED = "credit_received", "Avoir reçu"
        CLOSED = "closed", "Clôturée"
        CANCELLED = "cancelled", "Annulée"

    fournisseur = models.ForeignKey(Fournisseur, on_delete=models.PROTECT)
    currency = models.CharField(max_length=3, choices=Devise.choices)
    state = models.CharField(max_length=20, choices=State.choices, default=State.DRAFT)
    recovered_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    due_date = models.DateField(null=True, blank=True)
    followup_note = models.TextField(blank=True)
    credit_invoice = models.ForeignKey(Facture, on_delete=models.PROTECT, null=True, blank=True, related_name="credit_claims")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    date_creation = models.DateTimeField(auto_now_add=True)
    date_modification = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date_creation"]
        constraints = [models.UniqueConstraint(fields=["fournisseur", "currency"], condition=models.Q(state="draft"), name="unique_supplier_currency_draft")]


class SupplierClaimLine(models.Model):
    category = models.CharField(max_length=12, default='price', choices=[('price','Prix'),('quantity','Quantité'),('delivery','Livraison')])
    claim = models.ForeignKey(SupplierClaim, on_delete=models.CASCADE, related_name="lines")
    invoice_line = models.ForeignKey(LigneFacture, on_delete=models.SET_NULL, null=True)
    delivery_issue = models.ForeignKey(DeliveryIssue, on_delete=models.PROTECT, null=True, blank=True, related_name='claim_lines')
    fingerprint = models.CharField(max_length=64)
    snapshot = models.JSONField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["claim", "invoice_line"], name="unique_claim_invoice_line"),
            models.UniqueConstraint(fields=["claim", "delivery_issue"], name="unique_claim_delivery_issue"),
        ]


class PriceReviewEvent(models.Model):
    line = models.ForeignKey(LigneFacture, on_delete=models.SET_NULL, null=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    action = models.CharField(max_length=30)
    before = models.JSONField(default=dict)
    after = models.JSONField(default=dict)
    date_creation = models.DateTimeField(auto_now_add=True)
    undone = models.BooleanField(default=False)

    class Meta:
        ordering = ["-date_creation", "-pk"]


class CataloguePriceHistory(models.Model):
    line = models.ForeignKey(LigneCatalogue, on_delete=models.SET_NULL, null=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    old_price = models.DecimalField(max_digits=12, decimal_places=2, null=True)
    new_price = models.DecimalField(max_digits=12, decimal_places=2, null=True)
    catalogue_snapshot = models.JSONField(default=dict)
    date_creation = models.DateTimeField(auto_now_add=True)


class RestaurantAccess(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="restaurant_access")
    restaurant = models.ForeignKey(Restaurant, on_delete=models.CASCADE)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "restaurant"], name="unique_restaurant_access")]


class BusinessAudit(models.Model):
    restaurant = models.ForeignKey(Restaurant, on_delete=models.SET_NULL, null=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    model_label = models.CharField(max_length=100)
    object_id = models.PositiveBigIntegerField()
    action = models.CharField(max_length=30)
    reason = models.TextField(blank=True)
    before = models.JSONField(default=dict)
    after = models.JSONField(default=dict)
    date_creation = models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering = ["-date_creation", "-pk"]


class AnalysisVersion(models.Model):
    document_type = models.CharField(max_length=30)
    document_id = models.PositiveBigIntegerField()
    payload = models.JSONField()
    applied = models.BooleanField(default=False)
    changes = models.JSONField(default=list)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    date_creation = models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering = ["-date_creation", "-pk"]


class AnalysisJob(models.Model):
    document_type = models.CharField(max_length=30)
    document_id = models.PositiveBigIntegerField()
    state = models.CharField(max_length=12, default="queued", choices=[("queued", "En attente"), ("running", "En cours"), ("done", "Terminée"), ("failed", "Échouée")])
    attempts = models.PositiveIntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True)
    token = models.UUIDField(null=True, blank=True, editable=False)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    date_creation = models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering = ["date_creation", "pk"]
        constraints = [models.UniqueConstraint(fields=["document_type", "document_id"], condition=models.Q(state__in=["queued", "running"]), name="unique_active_analysis_job")]
        indexes = [models.Index(fields=["state", "date_creation"])]


class UnitConversion(BusinessModel):
    active = models.BooleanField(default=True, verbose_name="Règle active")
    restaurant_path = "fournisseur__restaurant_id"
    fournisseur = models.ForeignKey(Fournisseur, on_delete=models.CASCADE, related_name="unit_conversions")
    reference = models.CharField(max_length=100, blank=True, help_text="Vide : règle générale du fournisseur")
    invoice_unit = models.CharField(max_length=30)
    catalogue_unit = models.CharField(max_length=30)
    factor = models.DecimalField(max_digits=12, decimal_places=6, help_text="Nombre d’unités catalogue dans une unité facturée")
    reason = models.TextField()
    confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["fournisseur", "reference", "invoice_unit", "catalogue_unit"], name="unique_confirmed_unit_conversion"), models.CheckConstraint(condition=models.Q(factor__gt=0), name="positive_unit_factor")]


class InvoiceDeliveryLink(BusinessModel):
    restaurant_path = "invoice__fournisseur__restaurant_id"
    invoice = models.ForeignKey(Facture, on_delete=models.CASCADE, related_name="delivery_links")
    delivery = models.ForeignKey(BonLivraison, on_delete=models.PROTECT, related_name="invoice_links")
    confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    date_creation = models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["invoice", "delivery"], name="unique_invoice_delivery_link")]
    def clean(self):
        if self.invoice_id and self.delivery_id and self.invoice.fournisseur_id != self.delivery.fournisseur_id:
            raise ValidationError("Le BL et la facture doivent appartenir au même fournisseur.")


class DeliveryAllocation(BusinessModel):
    restaurant_path = "invoice_line__facture__fournisseur__restaurant_id"
    invoice_line = models.ForeignKey(LigneFacture, on_delete=models.PROTECT, related_name="delivery_allocations")
    delivery_line = models.ForeignKey(LigneBonLivraison, on_delete=models.PROTECT, related_name="invoice_allocations")
    quantity = models.DecimalField(max_digits=12, decimal_places=3)
    factor = models.DecimalField(max_digits=12, decimal_places=6, default=1)
    reason = models.TextField(blank=True)
    source_fingerprint = models.CharField(max_length=64, blank=True)
    confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["invoice_line", "delivery_line"], name="unique_delivery_allocation"), models.CheckConstraint(condition=models.Q(quantity__gt=0, factor__gt=0), name="positive_allocated_quantity")]


class InvoiceControlCache(models.Model):
    invoice = models.OneToOneField(Facture, on_delete=models.CASCADE)
    revision = models.PositiveBigIntegerField()
    result = models.JSONField()

class LoginAttempt(models.Model):
    key = models.CharField(max_length=64, unique=True)
    failures = models.PositiveIntegerField(default=0)
    window_start = models.DateTimeField()


class DeliveryReceipt(BusinessModel):
    """Réception constatée, distincte de la quantité annoncée par le BL."""
    restaurant_path = 'line__bon_livraison__fournisseur__restaurant_id'
    line = models.OneToOneField(LigneBonLivraison, on_delete=models.PROTECT, related_name='receipt')
    received = models.DecimalField(max_digits=12, decimal_places=3)
    returned = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    reason = models.TextField()
    source_fingerprint = models.CharField(max_length=64)
    confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    updated_at = models.DateTimeField(auto_now=True)
    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(received__gte=0, returned__gte=0, returned__lte=models.F('received')), name='valid_delivery_receipt')]


class InvoiceReceipt(BusinessModel):
    restaurant_path = 'line__facture__fournisseur__restaurant_id'
    line = models.OneToOneField(LigneFacture, on_delete=models.PROTECT, related_name='receipt')
    quantity = models.DecimalField(max_digits=12, decimal_places=3)
    reason = models.TextField()
    source_fingerprint = models.CharField(max_length=64)
    confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    updated_at = models.DateTimeField(auto_now=True)
    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(quantity__gte=0), name='positive_invoice_receipt')]


class DeliveryProductMapping(BusinessModel):
    restaurant_path = 'fournisseur__restaurant_id'
    fournisseur = models.ForeignKey(Fournisseur, on_delete=models.CASCADE)
    invoice_key = models.CharField(max_length=300)
    delivery_key = models.CharField(max_length=300)
    invoice_unit = models.CharField(max_length=30)
    delivery_unit = models.CharField(max_length=30)
    factor = models.DecimalField(max_digits=12, decimal_places=6)
    reason = models.TextField()
    confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['fournisseur','invoice_key','delivery_key','invoice_unit','delivery_unit'], name='unique_delivery_mapping'), models.CheckConstraint(condition=models.Q(factor__gt=0), name='positive_delivery_factor')]


class QuantityCredit(BusinessModel):
    restaurant_path = 'invoice_line__facture__fournisseur__restaurant_id'
    invoice_line = models.ForeignKey(LigneFacture, on_delete=models.PROTECT, related_name='quantity_credits')
    credit_line = models.ForeignKey(LigneFacture, on_delete=models.PROTECT, related_name='credit_uses')
    quantity = models.DecimalField(max_digits=12, decimal_places=3, help_text='Quantité dans l’unité de l’avoir')
    factor = models.DecimalField(max_digits=12, decimal_places=6, default=1)
    reason = models.TextField()
    source_fingerprint = models.CharField(max_length=64)
    confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['invoice_line','credit_line'], name='unique_quantity_credit'), models.CheckConstraint(condition=models.Q(quantity__gt=0, factor__gt=0), name='positive_quantity_credit')]
