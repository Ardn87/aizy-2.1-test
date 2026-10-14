"""Configuration locale explicite, atomique et réservée au serveur."""
import json
import os
import tempfile
from pathlib import Path
from django.conf import settings
from django.core.exceptions import ValidationError


def local_config_editable():
    return settings.DEBUG and not os.getenv('OPENAI_API_KEY', '').strip()


def local_config_path():
    return Path(os.getenv('OPENAI_KEYS_FILE', '') or settings.BASE_DIR / '.local' / 'openai.keys.json')


def save_local_openai_settings(api_key, model):
    if not local_config_editable():
        raise ValidationError('La configuration est gérée par les variables du serveur.')
    path=local_config_path()
    # Aucune écriture sur un périphérique ni un dossier désigné comme fichier.
    if path == Path('/dev/null') or (path.exists() and not path.is_file()):
        raise ValidationError('Le chemin de configuration local doit désigner un fichier JSON.')
    old={}
    if path.is_file():
        try: old=json.loads(path.read_text())
        except (OSError,ValueError): pass
    key=api_key.strip() or (old.get('OPENAI_API_KEY','') if isinstance(old,dict) else '')
    if not key:
        raise ValidationError('Renseignez la clé API pour activer l’analyse.')
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=path.parent,delete=False) as file:
            temporary=file.name
            os.chmod(temporary,0o600)
            json.dump({'OPENAI_API_KEY':key,'OPENAI_MODEL':model},file)
        os.replace(temporary,path)
    except OSError as exc:
        raise ValidationError('La configuration n’a pas pu être enregistrée. Vérifiez les droits du dossier local.') from exc
    finally:
        if temporary and os.path.exists(temporary):os.unlink(temporary)
