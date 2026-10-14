from django import forms
from .models import LigneFacture


class ReviewLineForm(forms.ModelForm):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields['kind'].required=False
    def clean_kind(self):
        return self.cleaned_data.get('kind') or self.instance.kind

    class Meta:
        model = LigneFacture
        fields = ['kind', 'designation', 'reference_produit', 'ean', 'quantite', 'unite', 'prix_unitaire_ht']
        labels = {'kind': 'Type de ligne', 'designation': 'Produit', 'reference_produit': 'Référence', 'ean': 'EAN',
                  'quantite': 'Quantité facturée', 'unite': 'Unité', 'prix_unitaire_ht': 'Prix unitaire HT'}

    def clean(self):
        cleaned = super().clean()
        for key in ('quantite', 'prix_unitaire_ht'):
            if self.instance.facture.kind != 'credit' and cleaned.get('kind', self.instance.kind) == 'product' and cleaned.get(key) is not None and cleaned[key] < 0:
                self.add_error(key, 'Cette valeur doit être positive ou nulle.')
        return cleaned
