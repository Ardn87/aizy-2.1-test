from django import forms
from django.core.exceptions import ValidationError
from .forms import BootstrapForm, BootstrapModelForm, FournisseurSelect
from .models import Restaurant, Fournisseur, UnitConversion, Facture, CatalogueFournisseur, LigneCatalogue


class CatalogueProductPriceForm(BootstrapForm):
    price = forms.DecimalField(label='Prix catalogue HT', min_value=0, max_digits=12, decimal_places=2)
    previous_price = forms.CharField(required=False, widget=forms.HiddenInput)


class CataloguePeriodForm(BootstrapModelForm):
    def __init__(self,*args,require_date=True,**kwargs):
        self.require_date=require_date
        super().__init__(*args,**kwargs)
    class Meta:
        model = CatalogueFournisseur
        fields = ['devise', 'date_debut', 'date_fin']
        labels = {'devise':'Devise des prix catalogue', 'date_debut':'Applicable à partir du', 'date_fin':'Jusqu’au (facultatif)'}
        widgets = {'date_debut':forms.DateInput(format='%Y-%m-%d',attrs={'type':'date'}), 'date_fin':forms.DateInput(format='%Y-%m-%d',attrs={'type':'date'})}
    def clean(self):
        data = super().clean()
        if self.require_date and not data.get('date_debut'): self.add_error('date_debut','Précisez la date d’application des prix confirmés.')
        if data.get('devise')=='UNK': self.add_error('devise','Confirmez une devise pour appliquer les prix.')
        return data
from .services.uploads import validate_upload, content_hash

class MultipleInput(forms.ClearableFileInput):
    allow_multiple_selected = True

class MultipleFileField(forms.FileField):
    def clean(self, data, initial=None):
        clean = super().clean
        return [clean(item, initial) for item in data] if isinstance(data, (list, tuple)) else [clean(data, initial)]

class DocumentDepositForm(BootstrapForm):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields['restaurant'].queryset=Restaurant.objects.filter(actif=True)
        self.fields['fournisseur'].queryset=Fournisseur.objects.filter(actif=True,restaurant__actif=True)
        restaurants=list(self.fields['restaurant'].queryset[:2])
        self.single_restaurant=restaurants[0] if len(restaurants)==1 else None
        if self.single_restaurant:
            self.initial['restaurant']=self.single_restaurant.pk
            self.fields['restaurant'].required=False
        self.fields['fournisseur'].label_from_instance=lambda supplier:supplier.nom
        self.fields['fournisseur'].empty_label='Choisir un fournisseur'
        self.fields['restaurant'].empty_label='Choisir un restaurant'

    def clean_restaurant(self):
        return self.cleaned_data.get('restaurant') or self.single_restaurant

    kind = forms.ChoiceField(label='Document', choices=[('facture','Facture ou avoir'),('bon_livraison','Bon de livraison'),('combined','BL faisant aussi office de facture')])
    restaurant = forms.ModelChoiceField(label='Restaurant', queryset=Restaurant.objects.filter(actif=True))
    fournisseur = forms.ModelChoiceField(label='Fournisseur', queryset=Fournisseur.objects.filter(actif=True, restaurant__actif=True),widget=FournisseurSelect)
    files = MultipleFileField(label='Documents', widget=MultipleInput(attrs={'accept':'.pdf,.png,.jpg,.jpeg'}))
    delivery_comments = forms.BooleanField(
        label="J’ai des commentaires à apporter concernant ce BL",
        required=False,
    )
    duplicate_reason = forms.CharField(label='Motif si vous souhaitez conserver un doublon', required=False, max_length=2000)
    def clean_files(self):
        files = self.cleaned_data['files']
        if len(files) > 10 or sum(f.size for f in files) > 100*1024*1024:
            raise ValidationError('10 fichiers et 100 Mo maximum par dépôt.')
        return [validate_upload(f) for f in files]
    def clean(self):
        data = super().clean()
        supplier, restaurant = data.get('fournisseur'), data.get('restaurant')
        if supplier and restaurant and supplier.restaurant_id != restaurant.pk:
            self.add_error('fournisseur', 'Choisissez un fournisseur de ce restaurant.')
        if data.get('delivery_comments'):
            if data.get('kind') != 'bon_livraison':
                self.add_error('delivery_comments', 'Les commentaires de livraison sont disponibles uniquement pour un bon de livraison.')
            if len(data.get('files') or []) != 1:
                self.add_error('files', 'Déposez un seul bon de livraison pour commenter ses produits.')
        if supplier and data.get('files'):
            model = Facture if data.get('kind') in ('facture','combined') else __import__('core.models',fromlist=['BonLivraison']).BonLivraison
            hashes = set()
            for file in data['files']:
                h = content_hash(file)
                dup = model.objects.filter(fournisseur=supplier, file_sha256=h).first()
                if (dup or h in hashes) and len(data.get('duplicate_reason','').strip()) < 8:
                    self.add_error('duplicate_reason', f'{file.name} : fichier déjà reçu' + (f' (document {dup.pk})' if dup else ' dans ce dépôt') + '. Consultez-le ou indiquez un motif de conservation.')
                hashes.add(h)
        return data

class UnitConversionForm(BootstrapModelForm):
    class Meta:
        model = UnitConversion
        fields = ['reference','invoice_unit','catalogue_unit','factor','reason','active']
        labels = {'reference':'Référence produit (facultatif)', 'invoice_unit':'Unité facturée','catalogue_unit':'Unité catalogue','factor':'Unités catalogue par unité facturée','reason':'Justification du conditionnement'}
    def clean(self):
        data = super().clean()
        for key in ('invoice_unit','catalogue_unit'):
            data[key] = (data.get(key) or '').strip().lower()
        if not data.get('reason','').strip():
            self.add_error('reason','Confirmez le conditionnement et sa source.')
        return data

class ClaimFollowupForm(BootstrapForm):
    action = forms.ChoiceField(label='Étape',choices=[('replied','Réponse reçue'),('credit_due','Avoir attendu'),('credit_received','Avoir reçu'),('closed','Clôturée')])
    recovered_amount = forms.DecimalField(label='Montant récupéré', min_value=0, max_digits=12, decimal_places=2)
    due_date = forms.DateField(label='Échéance', required=False,widget=forms.DateInput(attrs={'type':'date'}))
    followup_note = forms.CharField(label='Note de suivi', required=False, max_length=3000, widget=forms.Textarea(attrs={'rows':2}))
    credit_invoice = forms.ModelChoiceField(label='Avoir justificatif', required=False, queryset=Facture.objects.filter(kind='credit'))


class CatalogueProductCreateForm(BootstrapModelForm):
    class Meta:
        model = LigneCatalogue
        fields = ['designation', 'reference_produit', 'unite', 'prix_unitaire_ht']
        labels = {'designation': 'Nom du produit', 'reference_produit': 'Référence (facultative)',
                  'unite': 'Unité', 'prix_unitaire_ht': 'Prix catalogue HT'}
        widgets = {'prix_unitaire_ht': forms.NumberInput(attrs={'step': '0.01', 'min': '0'})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['designation'].required = True
        self.fields['unite'].required = True

    def clean_prix_unitaire_ht(self):
        price = self.cleaned_data['prix_unitaire_ht']
        if price is not None and price < 0:
            raise forms.ValidationError('Le prix doit être positif ou nul.')
        return price


class OpenAISettingsForm(BootstrapForm):
    api_key=forms.CharField(label='Clé API OpenAI',required=False,max_length=512,
        strip=True,widget=forms.PasswordInput(attrs={'autocomplete':'new-password'}),
        help_text='La clé reste sur le serveur. Laissez vide pour conserver celle déjà enregistrée.')
    model=forms.RegexField(label='Modèle',regex=r'^[a-zA-Z0-9._/-]+$',max_length=200,
        initial='gpt-4.1-mini',error_messages={'invalid':'Indiquez un nom de modèle sans espace.'})
