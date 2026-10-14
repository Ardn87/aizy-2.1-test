"""Relances non destructrices : identités stables, différences à confirmer."""
import hashlib
from collections import defaultdict
from decimal import Decimal
from django.db import transaction
from django.core.exceptions import ValidationError
from django.utils import timezone
from core.models import AnalysisVersion, Devise
from core.request_context import current_user


def fields_for(model):
    exclude = {'id', 'facture', 'catalogue', 'bon_livraison', 'source_key', 'source_data', 'archived_at', 'corrigee_manuellement'}
    return [f.name for f in model._meta.fields if f.name not in exclude]


def line_data(line):
    def text(value):
        return str(value.normalize()) if isinstance(value, Decimal) else str(value) if value is not None else None
    return {key: text(getattr(line, key)) for key in fields_for(type(line))}


def document_type(document):
    return {'facture': 'facture', 'bonlivraison': 'bon_livraison', 'cataloguefournisseur': 'catalogue'}[document._meta.model_name]


def sync_lines(document, incoming):
    existing = list(document.lignes.model.all_objects.filter(**{document.lignes.field.name:document}).order_by('ordre','pk'))
    initial = not existing and not AnalysisVersion.objects.filter(document_type=document_type(document), document_id=document.pk).exists()
    indexed = {line.source_key: line for line in existing if line.source_key}
    used = set(); occurrences = defaultdict(int); changes = []
    for fresh in incoming:
        identity = '|'.join(str(getattr(fresh, key, '') or '').strip() for key in ('reference_produit', 'ean'))
        occurrences[identity] += 1
        key = 'row:' + hashlib.sha256(f'{identity}:{occurrences[identity]}'.encode()).hexdigest() if identity.strip('|') else f'position:{fresh.ordre}'
        old = indexed.get(key)
        if not old:
            candidates = [l for l in existing if l.pk not in used and l.ordre == fresh.ordre]
            if len(candidates) == 1:
                old = candidates[0]
        if old:
            used.add(old.pk)
            if not old.source_key:
                # Attach legacy rows without rewriting any commercial data.
                type(old).all_objects.filter(pk=old.pk).update(source_key=key)
                old.source_key = key
            data = line_data(fresh)
            baseline = old.source_data or line_data(old)
            if data != baseline or old.archived_at:
                changes.append({'operation': 'update', 'line_id': old.pk, 'before': line_data(old), 'before_archived': bool(old.archived_at), 'after': data, 'key': key})
        elif initial:
            fresh.source_key = key; fresh.source_data = line_data(fresh); fresh.save()
        else:
            changes.append({'operation': 'create', 'line_id': None, 'before': {}, 'after': line_data(fresh), 'key': key})
    for old in existing:
        if old.pk not in used and not old.archived_at:
            changes.append({'operation': 'archive', 'line_id': old.pk, 'before': line_data(old), 'before_archived': bool(old.archived_at), 'after': {}, 'key': old.source_key})
    document._extraction_changes = changes
    return len(incoming)


def record_version(document, data):
    changes = list(getattr(document, '_extraction_changes', []))
    header_fields = {'facture':['numero_facture','date_facture','date_livraison_detectee','devise','montant_ht','montant_tva','montant_ttc'],
        'bon_livraison':['numero_bon_livraison','date_livraison'], 'catalogue':['nom','date_debut','date_fin','devise']}[document_type(document)]
    aliases = {'numero_bon_livraison':'numero_bl','nom':'nom_catalogue'}
    before, after = {}, {}
    for key in header_fields:
        candidate=data.get(aliases.get(key,key))
        if candidate in (None,''):continue
        if key == 'devise' and candidate not in Devise.values:
            continue
        try:
            candidate=document._meta.get_field(key).to_python(candidate)
        except Exception:continue
        current=getattr(document,key)
        if candidate!=current:
            before[key]=str(current) if current is not None else None
            after[key]=str(candidate) if candidate is not None else None
    if after:
        changes.append({'operation':'header','line_id':None,'before':before,'after':after})
    user = current_user.get()
    return AnalysisVersion.objects.create(document_type=document_type(document), document_id=document.pk,
        payload=data, changes=changes, applied=not bool(changes), actor=user if user and user.is_authenticated else None)


@transaction.atomic
def apply_version(document, version_id):
    version = AnalysisVersion.objects.select_for_update().get(pk=version_id, document_type=document_type(document), document_id=document.pk)
    if version.applied:
        raise ValidationError('Cette version est déjà appliquée.')
    # Parent lock serializes competing reviews and analyses.
    document = type(document).objects.select_for_update().get(pk=document.pk)
    for change in version.changes:
        if change['operation']=='header':
            if any((str(getattr(document,k)) if getattr(document,k) is not None else None)!=v for k,v in change['before'].items()):
                raise ValidationError('Les données du document ont changé depuis cette extraction.')
        elif change['line_id']:
            old = document.lignes.model.all_objects.select_for_update().filter(pk=change['line_id'],**{document.lignes.field.name:document}).first()
            if not old or line_data(old) != change['before'] or bool(old.archived_at) != change.get('before_archived', False):
                raise ValidationError('Une ligne a été corrigée depuis cette analyse. Relancez l’analyse pour revoir les différences.')
    for change in version.changes:
        if change['operation']=='header':
            for key,value in change['after'].items():
                setattr(document,key,document._meta.get_field(key).to_python(value))
            continue
        if change['operation'] == 'create':
            line = document.lignes.model()
            setattr(line, document.lignes.field.name, document)
        else:
            line = document.lignes.model.all_objects.get(pk=change['line_id'],**{document.lignes.field.name:document})
        if change['operation'] == 'archive':
            line.archived_at = timezone.now()
        else:
            for key, val in change['after'].items():
                field = line._meta.get_field(key)
                setattr(line, key, field.to_python(val))
            line.archived_at = None
            line.source_key = change['key']; line.source_data = change['after']
            line.corrigee_manuellement = True
        line.save()
    if hasattr(document, 'verified_at'):
        document.verified_at = None; document.verified_by = None
        document.statut = 'a_valider' if document_type(document) != 'bon_livraison' else 'a_verifier'
        document.save()
    version.applied = True; version.save(update_fields=['applied'])
    return version
