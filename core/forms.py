from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from .models import RestaurantAccess

from .access import DEFAULT_ROLES, ROLE_ADMIN
from .models import (
    BonLivraison,
    CatalogueFournisseur,
    Devise,
    Facture,
    Fournisseur,
    LigneCatalogue,
    LigneBonLivraison,
    LigneFacture,
    Restaurant,
)

ALLOWED_DOCUMENT_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}
DOCUMENT_TYPE_ERROR = "Seuls les fichiers PDF, JPG, JPEG ou PNG sont acceptés."
ALLOWED_CATALOGUE_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".csv", ".xlsx", ".xslx"}
CATALOGUE_TYPE_ERROR = "Seuls les fichiers PDF, JPG, JPEG, PNG, CSV ou XLSX sont acceptés."
LITIGE_NOTE_HELP_TEXT = "Obligatoire en cas de litige."
USER_ROLE_CHOICES = [(role, "Réception" if role == "Reception" else role) for role in DEFAULT_ROLES]
ROLE_MISSING_ERROR = "Ce rôle n'est pas disponible. Lancez python manage.py init_roles."


def validate_document_file(uploaded_file):
    from .services.uploads import validate_upload
    if not any(uploaded_file.name.lower().endswith(e) for e in ALLOWED_DOCUMENT_EXTENSIONS):
        raise forms.ValidationError(DOCUMENT_TYPE_ERROR)
    return validate_upload(uploaded_file)


def validate_catalogue_file(uploaded_file):
    from .services.uploads import validate_upload
    return validate_upload(uploaded_file, catalogue=True)


class FournisseurSelect(forms.Select):
    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex, attrs)
        if value:
            option["attrs"]["data-restaurant"] = value.instance.restaurant_id
        return option


class RestaurantDeviseSelect(forms.Select):
    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex, attrs)
        if value:
            option["attrs"]["data-devise"] = value.instance.devise_defaut
        return option


def apply_bootstrap_classes(fields):
    for field in fields.values():
        if isinstance(field.widget, forms.CheckboxInput):
            css_class = "form-check-input"
        elif isinstance(field.widget, forms.Select):
            css_class = "form-select"
        else:
            css_class = "form-control"
        existing_class = field.widget.attrs.get("class", "")
        field.widget.attrs["class"] = f"{existing_class} {css_class}".strip()


class BootstrapForm(forms.Form):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_bootstrap_classes(self.fields)


class BootstrapModelForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        self.upload_supplier = kwargs.pop('fournisseur', None)
        super().__init__(*args, **kwargs)
        if self.upload_supplier and any(k in self.fields for k in ('fichier','fichier_original')) and not self.instance.pk:
            self.fields['duplicate_reason'] = forms.CharField(required=False, label='Motif de conservation si ce fichier a déjà été déposé', max_length=2000)
        apply_bootstrap_classes(self.fields)

    def clean(self):
        data = super().clean()
        if self.upload_supplier and not self.instance.pk:
            from .services.uploads import content_hash
            field = 'fichier_original' if 'fichier_original' in data else 'fichier'
            file = data.get(field)
            if file:
                dup = type(self.instance).objects.filter(fournisseur=self.upload_supplier, file_sha256=content_hash(file)).first()
                if dup and len(data.get('duplicate_reason','').strip())<8:
                    self.add_error('duplicate_reason', f'Fichier déjà reçu (document {dup.pk}). Consultez-le ou indiquez un motif de conservation.')
                elif data.get('duplicate_reason'):
                    data['note'] = (data.get('note','') + '\nDoublon conservé : '+ data['duplicate_reason']).strip()
        return data


def get_user_app_role(user):
    group_names = {group.name for group in user.groups.all()}
    for role in DEFAULT_ROLES:
        if role in group_names:
            return role
    if user.is_superuser:
        return ROLE_ADMIN
    return ""


def set_user_app_role(user, role):
    app_groups = Group.objects.filter(name__in=DEFAULT_ROLES)
    user.groups.remove(*app_groups)
    group = Group.objects.get(name=role)
    user.groups.add(group)


def validate_role_exists(role):
    if role and not Group.objects.filter(name=role).exists():
        raise forms.ValidationError(ROLE_MISSING_ERROR)
    return role


class UserCreateForm(BootstrapForm):
    username = forms.CharField(
        label="Nom d'utilisateur",
        max_length=150,
        error_messages={"required": "Le nom d'utilisateur est obligatoire."},
    )
    email = forms.EmailField(label="Email", required=False)
    password1 = forms.CharField(
        label="Mot de passe",
        widget=forms.PasswordInput,
        error_messages={"required": "Le mot de passe est obligatoire."},
    )
    password2 = forms.CharField(
        label="Confirmation du mot de passe",
        widget=forms.PasswordInput,
        error_messages={"required": "Confirmez le mot de passe."},
    )
    restaurants = forms.ModelMultipleChoiceField(queryset=Restaurant.objects.all(), required=False,
        label="Restaurants accessibles", widget=forms.CheckboxSelectMultiple,
        help_text="Pour Réception, Revue et Gestionnaire. Sans affectation, aucun document n’est accessible.")
    role = forms.ChoiceField(
        label="Rôle",
        choices=[("", "Choisir un rôle"), *USER_ROLE_CHOICES],
        error_messages={"required": "Choisissez un rôle."},
    )
    is_active = forms.BooleanField(label="Actif", required=False, initial=True)

    def clean_username(self):
        username = self.cleaned_data["username"]
        user_model = get_user_model()
        if user_model.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError("Ce nom d'utilisateur existe déjà.")
        return username

    def clean_role(self):
        return validate_role_exists(self.cleaned_data["role"])

    def clean(self):
        cleaned_data = super().clean()
        password1 = cleaned_data.get("password1")
        password2 = cleaned_data.get("password2")

        if password1 and password2 and password1 != password2:
            self.add_error("password2", "Les deux mots de passe doivent correspondre.")
        if password1:
            user = getattr(self, 'user', None) or get_user_model()(username=cleaned_data.get('username', ''), email=cleaned_data.get('email', ''))
            try:
                validate_password(password1, user=user)
            except ValidationError as exc:
                self.add_error('password1', exc)


        return cleaned_data

    def save(self):
        user_model = get_user_model()
        user = user_model(
            username=self.cleaned_data["username"],
            email=self.cleaned_data.get("email", ""),
            is_active=self.cleaned_data.get("is_active", False),
        )
        user.set_password(self.cleaned_data["password1"])
        user.save()
        set_user_app_role(user, self.cleaned_data["role"])
        RestaurantAccess.objects.filter(user=user).delete()
        RestaurantAccess.objects.bulk_create([RestaurantAccess(user=user, restaurant=r) for r in self.cleaned_data.get('restaurants', [])])
        return user


class UserUpdateForm(BootstrapModelForm):
    restaurants = forms.ModelMultipleChoiceField(queryset=Restaurant.objects.all(), required=False,
        label="Restaurants accessibles", widget=forms.CheckboxSelectMultiple,
        help_text="Pour Réception, Revue et Gestionnaire. Sans affectation, aucun document n’est accessible.")
    role = forms.ChoiceField(
        label="Rôle",
        choices=[("", "Choisir un rôle"), *USER_ROLE_CHOICES],
        error_messages={"required": "Choisissez un rôle."},
    )

    class Meta:
        model = get_user_model()
        fields = ["username", "email", "role", "is_active"]
        labels = {
            "username": "Nom d'utilisateur",
            "email": "Email",
            "is_active": "Actif",
        }
        error_messages = {
            "username": {
                "required": "Le nom d'utilisateur est obligatoire.",
            },
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields["role"].initial = get_user_app_role(self.instance)
            self.fields["restaurants"].initial = self.instance.restaurant_access.values_list("restaurant_id", flat=True)

    def clean_username(self):
        username = self.cleaned_data["username"]
        user_model = get_user_model()
        duplicate = user_model.objects.filter(username__iexact=username)
        if self.instance.pk:
            duplicate = duplicate.exclude(pk=self.instance.pk)
        if duplicate.exists():
            raise forms.ValidationError("Ce nom d'utilisateur existe déjà.")
        return username

    def clean_role(self):
        return validate_role_exists(self.cleaned_data["role"])

    def save(self, commit=True):
        user = super().save(commit=commit)
        if commit:
            set_user_app_role(user, self.cleaned_data["role"])
            RestaurantAccess.objects.filter(user=user).delete()
            RestaurantAccess.objects.bulk_create([RestaurantAccess(user=user, restaurant=r) for r in self.cleaned_data.get('restaurants', [])])
        return user


class UserPasswordForm(BootstrapForm):
    def __init__(self, *args, user=None, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)

    password1 = forms.CharField(
        label="Nouveau mot de passe",
        widget=forms.PasswordInput,
        error_messages={"required": "Le nouveau mot de passe est obligatoire."},
    )
    password2 = forms.CharField(
        label="Confirmation",
        widget=forms.PasswordInput,
        error_messages={"required": "Confirmez le nouveau mot de passe."},
    )

    def clean(self):
        cleaned_data = super().clean()
        password1 = cleaned_data.get("password1")
        password2 = cleaned_data.get("password2")

        if password1 and password2 and password1 != password2:
            self.add_error("password2", "Les deux mots de passe doivent correspondre.")
        if password1:
            user = getattr(self, 'user', None) or get_user_model()(username=cleaned_data.get('username', ''), email=cleaned_data.get('email', ''))
            try:
                validate_password(password1, user=user)
            except ValidationError as exc:
                self.add_error('password1', exc)


        return cleaned_data

    def save(self, user):
        user.set_password(self.cleaned_data["password1"])
        user.save(update_fields=["password"])
        return user


class RestaurantForm(BootstrapModelForm):
    class Meta:
        model = Restaurant
        fields = ["nom", "adresse", "devise_defaut", "actif"]
        labels = {
            "nom": "Nom du restaurant",
            "adresse": "Adresse",
            "devise_defaut": "Devise par défaut",
            "actif": "Actif",
        }
        error_messages = {
            "nom": {
                "required": "Le nom du restaurant est obligatoire.",
            },
        }
        widgets = {
            "adresse": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["devise_defaut"].required = False

    def clean_nom(self):
        nom = self.cleaned_data["nom"]
        duplicate = Restaurant.objects.filter(nom=nom)
        if self.instance.pk:
            duplicate = duplicate.exclude(pk=self.instance.pk)
        if duplicate.exists():
            raise forms.ValidationError("Ce restaurant existe déjà.")
        return nom

    def clean_devise_defaut(self):
        return self.cleaned_data.get("devise_defaut") or Devise.EUR


class FournisseurForm(BootstrapModelForm):
    def clean_delivery_mode(self):
        return self.cleaned_data.get('delivery_mode') or (self.instance.delivery_mode if self.instance.pk else 'mixed')

    def clean_delivery_wait_days(self):
        value=self.cleaned_data.get('delivery_wait_days')
        if value is None: return self.instance.delivery_wait_days if self.instance.pk else 14
        if value>365: raise forms.ValidationError('365 jours maximum.')
        return value

    class Meta:
        model = Fournisseur
        fields = ["restaurant", "nom", "devise_defaut", "email", "telephone", "actif", "delivery_mode", "delivery_wait_days"]
        labels = {
            "restaurant": "Restaurant",
            "nom": "Nom du fournisseur",
            "devise_defaut": "Devise par défaut",
            "email": "Contact mail",
            "telephone": "Téléphone",
            "actif": "Actif",
        }
        error_messages = {
            "restaurant": {
                "required": "Choisissez un restaurant.",
            },
            "nom": {
                "required": "Le nom du fournisseur est obligatoire.",
            },
        }
        widgets = {
            "restaurant": RestaurantDeviseSelect(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['delivery_mode'].required=False
        self.fields['delivery_mode'].label='Documents habituels'
        self.fields['delivery_wait_days'].required=False
        self.fields['delivery_wait_days'].label='Délai d’attente du BL (jours après import)'
        self.fields["restaurant"].queryset = Restaurant.objects.filter(actif=True).order_by("nom")
        self.fields["devise_defaut"].required = False
        if not self.instance.pk:
            restaurant = self.selected_restaurant()
            if restaurant:
                self.fields["devise_defaut"].initial = restaurant.devise_defaut

    def selected_restaurant(self):
        restaurant = self.initial.get("restaurant")
        if self.data:
            restaurant = self.data.get("restaurant")
        if isinstance(restaurant, Restaurant):
            return restaurant
        if restaurant:
            return Restaurant.objects.filter(pk=restaurant, actif=True).first()
        return None

    def clean(self):
        cleaned_data = super().clean()
        restaurant = cleaned_data.get("restaurant")
        nom = cleaned_data.get("nom")
        devise_defaut = cleaned_data.get("devise_defaut")

        if not devise_defaut:
            if self.instance.pk:
                cleaned_data["devise_defaut"] = self.instance.devise_defaut
            elif restaurant:
                cleaned_data["devise_defaut"] = restaurant.devise_defaut
            else:
                cleaned_data["devise_defaut"] = Devise.EUR

        if restaurant and nom:
            duplicate = Fournisseur.objects.filter(restaurant=restaurant, nom=nom)
            if self.instance.pk:
                duplicate = duplicate.exclude(pk=self.instance.pk)
            if duplicate.exists():
                self.add_error("nom", "Ce fournisseur existe déjà pour ce restaurant.")

        return cleaned_data


class FactureForm(BootstrapModelForm):
    class Meta:
        model = Facture
        fields = [
            "kind",
            "validation_exception",
            "numero_facture",
            "date_facture",
            "date_livraison_detectee",
            "devise",
            "montant_ht",
            "montant_tva",
            "montant_ttc",
            "statut",
            "note",
        ]
        labels = {
            "numero_facture": "Numéro de facture",
            "date_facture": "Date de facture",
            "date_livraison_detectee": "Date de livraison détectée",
            "devise": "Devise",
            "montant_ht": "Montant HT",
            "montant_tva": "Montant TVA",
            "montant_ttc": "Montant TTC",
            "statut": "Statut",
            "note": "Note",
        }
        help_texts = {
            "note": LITIGE_NOTE_HELP_TEXT,
        }
        widgets = {
            "date_facture": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
            "date_livraison_detectee": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
            "note": forms.Textarea(attrs={"rows": 4}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["devise"].required = False
        self.fields["kind"].required = False

    def clean(self):
        cleaned_data = super().clean()
        cleaned_data["kind"] = cleaned_data.get("kind") or self.instance.kind
        statut = cleaned_data.get("statut")
        note = cleaned_data.get("note", "")
        if not cleaned_data.get("devise"):
            cleaned_data["devise"] = (
                self.instance.devise or Devise.UNKNOWN
            )

        if statut == Facture.Statut.LITIGE and not note.strip():
            self.add_error("note", "Une note est obligatoire pour passer cette facture en litige.")

        if statut == Facture.Statut.VALIDEE:
            from .services.quality import invoice_checks
            problems = invoice_checks(self.instance, cleaned_data)
            if problems and len(cleaned_data.get('validation_exception', '').strip()) < 8:
                self.add_error(None, ' '.join(problems))
                self.add_error('validation_exception', 'Corrigez les données ou indiquez une dérogation motivée (8 caractères minimum).')
        return cleaned_data


class FactureUploadForm(BootstrapModelForm):
    class Meta:
        model = Facture
        fields = [
            "fichier",
            "note",
        ]
        labels = {
            "fichier": "Fichier",
            "note": "Note",
        }
        error_messages = {
            "fichier": {
                "required": "Ajoutez un fichier PDF, JPG, JPEG ou PNG.",
            },
        }
        widgets = {
            "fichier": forms.ClearableFileInput(
                attrs={"accept": ".pdf,.jpg,.jpeg,.png,application/pdf,image/jpeg,image/png"}
            ),
            "note": forms.Textarea(attrs={"rows": 3}),
        }

    def clean_fichier(self):
        return validate_document_file(self.cleaned_data["fichier"])

    def save(self, fournisseur, commit=True):
        facture = super().save(commit=False)
        facture.fournisseur = fournisseur
        facture.devise = Devise.UNKNOWN
        from .services.uploads import content_hash
        facture.file_sha256 = content_hash(facture.fichier)
        facture.statut = Facture.Statut.ANALYSE_EN_COURS
        if commit:
            facture.save()
        return facture


class CatalogueUploadForm(BootstrapModelForm):
    class Meta:
        model = CatalogueFournisseur
        fields = [
            "fichier_original",
            "nom",
            "date_debut",
            "date_fin",
            "note",
        ]
        labels = {
            "fichier_original": "Fichier",
            "nom": "Nom",
            "date_debut": "Date de début",
            "date_fin": "Date de fin",
            "note": "Note",
        }
        error_messages = {
            "fichier_original": {
                "required": "Ajoutez un fichier PDF, JPG, JPEG, PNG, CSV ou XLSX.",
            },
        }
        widgets = {
            "fichier_original": forms.ClearableFileInput(
                attrs={
                    "accept": ".pdf,.jpg,.jpeg,.png,.csv,.xlsx,.xslx,application/pdf,image/jpeg,image/png,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                }
            ),
            "date_debut": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
            "date_fin": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
            "note": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["fichier_original"].required = True

    def clean_fichier_original(self):
        return validate_catalogue_file(self.cleaned_data["fichier_original"])

    def save(self, fournisseur, commit=True):
        catalogue = super().save(commit=False)
        catalogue.fournisseur = fournisseur
        catalogue.devise = Devise.UNKNOWN
        from .services.uploads import content_hash
        catalogue.file_sha256 = content_hash(catalogue.fichier_original)
        catalogue.type_prix = CatalogueFournisseur.TypePrix.HT
        catalogue.statut = CatalogueFournisseur.Statut.ANALYSE_EN_COURS
        if commit:
            catalogue.save()
        return catalogue


class CatalogueInfoForm(BootstrapModelForm):
    class Meta:
        model = CatalogueFournisseur
        fields = [
            "nom",
            "date_debut",
            "date_fin",
            "devise",
            "note",
        ]
        labels = {
            "nom": "Nom",
            "date_debut": "Date de début",
            "date_fin": "Date de fin",
            "devise": "Devise",
            "note": "Note",
        }
        widgets = {
            "date_debut": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
            "date_fin": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
            "note": forms.Textarea(attrs={"rows": 4}),
        }


class LigneCatalogueForm(BootstrapModelForm):
    class Meta:
        model = LigneCatalogue
        fields = [
            "reference_produit",
            "ean",
            "designation",
            "marque",
            "unite",
            "conditionnement",
            "prix_unitaire_ht",
            "statut_ligne",
        ]
        labels = {
            "reference_produit": "Référence produit",
            "ean": "EAN",
            "designation": "Désignation",
            "marque": "Marque",
            "unite": "Unité",
            "conditionnement": "Conditionnement",
            "prix_unitaire_ht": "Prix HT",
            "statut_ligne": "Statut ligne",
        }
        widgets = {
            "prix_unitaire_ht": forms.NumberInput(attrs={"step": "0.01"}),
        }


class LigneFactureForm(BootstrapModelForm):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields['kind'].required=False
    def clean_kind(self):
        return self.cleaned_data.get('kind') or self.instance.kind or 'product'

    class Meta:
        model = LigneFacture
        fields = [
            "kind",
            "taux_tva",
            "reference_produit",
            "ean",
            "designation",
            "quantite",
            "unite",
            "prix_unitaire_ht",
            "montant_ht",
            "montant_ttc",
        ]
        labels = {
            "kind": "Type de ligne",
            "taux_tva": "TVA (%)",
            "reference_produit": "Référence",
            "ean": "EAN",
            "designation": "Désignation",
            "quantite": "Quantité",
            "unite": "Unité",
            "prix_unitaire_ht": "Prix unitaire HT",
            "montant_ht": "Montant HT",
            "montant_ttc": "Montant TTC",
        }
        widgets = {
            "reference_produit": forms.TextInput(attrs={"placeholder": "Référence"}),
            "designation": forms.TextInput(attrs={"placeholder": "Produit ou service"}),
            "quantite": forms.NumberInput(attrs={"step": "0.001"}),
            "unite": forms.TextInput(attrs={"placeholder": "Unité"}),
            "prix_unitaire_ht": forms.NumberInput(attrs={"step": "0.01"}),
            "montant_ht": forms.NumberInput(attrs={"step": "0.01"}),
            "montant_ttc": forms.NumberInput(attrs={"step": "0.01"}),
        }


LigneFactureFormSet = forms.inlineformset_factory(
    Facture,
    LigneFacture,
    form=LigneFactureForm,
    extra=1,
    can_delete=True,
)


class BonLivraisonForm(BootstrapModelForm):
    class Meta:
        model = BonLivraison
        fields = [
            "validation_exception",
            "numero_bon_livraison",
            "date_livraison",
            "statut",
            "note",
        ]
        labels = {
            "numero_bon_livraison": "Numéro de bon de livraison",
            "date_livraison": "Date de livraison",
            "statut": "Statut",
            "note": "Note",
        }
        help_texts = {
            "note": LITIGE_NOTE_HELP_TEXT,
        }
        widgets = {
            "date_livraison": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
            "note": forms.Textarea(attrs={"rows": 4}),
        }

    def clean(self):
        cleaned_data = super().clean()
        statut = cleaned_data.get("statut")
        note = cleaned_data.get("note", "")

        if statut == BonLivraison.Statut.LITIGE and not note.strip():
            self.add_error("note", "Une note est obligatoire pour passer ce bon de livraison en litige.")

        if statut == BonLivraison.Statut.VALIDE:
            from .services.quality import delivery_checks
            problems = delivery_checks(self.instance, cleaned_data)
            if problems and len(cleaned_data.get('validation_exception', '').strip()) < 8:
                self.add_error(None, ' '.join(problems))
                self.add_error('validation_exception', 'Corrigez les données ou indiquez une dérogation motivée.')
        return cleaned_data


class BonLivraisonUploadForm(BootstrapModelForm):
    class Meta:
        model = BonLivraison
        fields = [
            "fichier",
            "note",
        ]
        labels = {
            "fichier": "Fichier",
            "note": "Note",
        }
        error_messages = {
            "fichier": {
                "required": "Ajoutez un fichier PDF, JPG, JPEG ou PNG.",
            },
        }
        widgets = {
            "fichier": forms.ClearableFileInput(
                attrs={"accept": ".pdf,.jpg,.jpeg,.png,application/pdf,image/jpeg,image/png"}
            ),
            "note": forms.Textarea(attrs={"rows": 3}),
        }

    def clean_fichier(self):
        return validate_document_file(self.cleaned_data["fichier"])

    def save(self, fournisseur, commit=True):
        bon_livraison = super().save(commit=False)
        bon_livraison.fournisseur = fournisseur
        from .services.uploads import content_hash
        bon_livraison.file_sha256 = content_hash(bon_livraison.fichier)
        bon_livraison.statut = BonLivraison.Statut.ANALYSE_EN_COURS
        if commit:
            bon_livraison.save()
        return bon_livraison


class LigneBonLivraisonForm(BootstrapModelForm):
    class Meta:
        model = LigneBonLivraison
        fields = [
            "reference_produit",
            "designation",
            "quantite_livree",
            "unite",
            "conditionnement",
            "prix_unitaire",
            "total_ligne",
            "statut_ligne",
        ]
        labels = {
            "reference_produit": "Référence",
            "designation": "Produit",
            "quantite_livree": "Quantité livrée",
            "unite": "Unité",
            "conditionnement": "Conditionnement",
            "prix_unitaire": "Prix unitaire",
            "total_ligne": "Total ligne",
            "statut_ligne": "Statut ligne",
        }


LigneBonLivraisonFormSet = forms.inlineformset_factory(
    BonLivraison,
    LigneBonLivraison,
    form=LigneBonLivraisonForm,
    extra=1,
    can_delete=True,
)


class ReceptionBonLivraisonForm(forms.Form):
    restaurant = forms.ModelChoiceField(
        label="Restaurant",
        queryset=Restaurant.objects.filter(actif=True).order_by("nom"),
        empty_label="Choisir un restaurant",
    )
    fournisseur = forms.ModelChoiceField(
        label="Fournisseur",
        queryset=Fournisseur.objects.filter(actif=True, restaurant__actif=True)
        .select_related("restaurant")
        .order_by("restaurant__nom", "nom"),
        empty_label="Choisir un fournisseur",
        widget=FournisseurSelect,
    )
    fichier = forms.FileField(
        label="Fichier ou photo",
        error_messages={
            "required": "Ajoutez un fichier PDF, JPG, JPEG ou PNG.",
        },
        widget=forms.ClearableFileInput(
            attrs={
                "accept": ".pdf,.jpg,.jpeg,.png,application/pdf,image/jpeg,image/png",
                "capture": "environment",
            }
        ),
    )
    note = forms.CharField(
        label="Note",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )

    duplicate_reason = forms.CharField(label="Motif si doublon conservé", required=False, max_length=2000)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["restaurant"].queryset=Restaurant.objects.filter(actif=True)
        self.fields["fournisseur"].queryset=Fournisseur.objects.filter(actif=True,restaurant__actif=True)
        for field in self.fields.values():
            css_class = "form-select" if isinstance(field.widget, forms.Select) else "form-control"
            field.widget.attrs["class"] = f"{field.widget.attrs.get('class', '')} {css_class}".strip()

    def clean(self):
        cleaned_data = super().clean()
        restaurant = cleaned_data.get("restaurant")
        fournisseur = cleaned_data.get("fournisseur")

        if restaurant and fournisseur and fournisseur.restaurant_id != restaurant.pk:
            self.add_error("fournisseur", "Ce fournisseur n'appartient pas au restaurant choisi.")

        if fournisseur and cleaned_data.get('fichier'):
            from .services.uploads import content_hash
            existing=BonLivraison.objects.filter(fournisseur=fournisseur,file_sha256=content_hash(cleaned_data['fichier'])).first()
            if existing and len(cleaned_data.get('duplicate_reason','').strip())<8:
                self.add_error('duplicate_reason',f'Fichier déjà reçu (BL {existing.pk}). Indiquez un motif pour conserver ce doublon.')
        return cleaned_data

    def clean_fichier(self):
        return validate_document_file(self.cleaned_data["fichier"])

    def save(self):
        from .services.uploads import content_hash
        return BonLivraison.objects.create(
            file_sha256=content_hash(self.cleaned_data["fichier"]),
            fournisseur=self.cleaned_data["fournisseur"],
            fichier=self.cleaned_data["fichier"],
            note=self.cleaned_data.get("note", ""),
            statut=BonLivraison.Statut.ANALYSE_EN_COURS,
        )
