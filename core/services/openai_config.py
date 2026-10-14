import json
import os
from pathlib import Path

from django.conf import settings


class OpenAIConfigurationError(Exception):
    """Configuration OpenAI absente ou invalide."""


ENV_SOURCE = "Variable d’environnement"
FILE_SOURCE = "Fichier local de développement"
UNCONFIGURED_SOURCE = "Non configuré"


def get_openai_config():
    env_api_key = _env_value("OPENAI_API_KEY")
    env_model = _env_value("OPENAI_MODEL")

    if env_api_key and env_model:
        return {
            "api_key": env_api_key,
            "model": env_model,
        }

    if not settings.DEBUG:
        if not env_api_key:
            raise OpenAIConfigurationError("OPENAI_API_KEY n'est pas defini.")
        raise OpenAIConfigurationError("OPENAI_MODEL n'est pas defini.")

    file_config = _load_keys_file_if_needed(env_api_key, env_model)
    api_key = env_api_key or _clean_config_value(file_config.get("OPENAI_API_KEY"))
    model = env_model or _clean_config_value(file_config.get("OPENAI_MODEL"))

    if not api_key:
        raise OpenAIConfigurationError("OPENAI_API_KEY n'est pas defini.")
    if not model:
        raise OpenAIConfigurationError("OPENAI_MODEL n'est pas defini.")

    return {
        "api_key": api_key,
        "model": model,
    }


def get_openai_config_status():
    try:
        config = get_openai_config()
    except OpenAIConfigurationError as exc:
        api_key_configured = _api_key_available()
        return {
            "configured": False,
            "api_key_configured": api_key_configured,
            "source": _config_source() if api_key_configured else UNCONFIGURED_SOURCE,
            "model": _configured_model_or_empty() or "Non défini",
            "error": str(exc),
        }

    return {
        "configured": True,
        "api_key_configured": True,
        "source": _config_source(),
        "model": config["model"],
        "error": "",
    }


def _env_value(name):
    return os.getenv(name, "").strip()


def _clean_config_value(value):
    if value is None:
        return ""
    return str(value).strip()


def _load_keys_file_if_needed(env_api_key, env_model):
    if env_api_key and env_model:
        return {}

    keys_file = _env_value("OPENAI_KEYS_FILE")
    if not keys_file:
        return {}

    path = Path(keys_file)
    if not path.exists():
        raise OpenAIConfigurationError("Fichier de configuration OpenAI introuvable.")
    if not path.is_file():
        raise OpenAIConfigurationError("Le chemin de configuration OpenAI n'est pas un fichier.")

    try:
        with path.open("r", encoding="utf-8") as config_file:
            data = json.load(config_file)
    except json.JSONDecodeError as exc:
        raise OpenAIConfigurationError("JSON de configuration OpenAI invalide.") from exc
    except OSError as exc:
        raise OpenAIConfigurationError("Fichier de configuration OpenAI inaccessible.") from exc

    if not isinstance(data, dict):
        raise OpenAIConfigurationError("JSON de configuration OpenAI invalide.")

    return data


def _config_source():
    if _env_value("OPENAI_API_KEY"):
        return ENV_SOURCE
    if settings.DEBUG and _env_value("OPENAI_KEYS_FILE"):
        return FILE_SOURCE
    return UNCONFIGURED_SOURCE


def _api_key_available():
    if _env_value("OPENAI_API_KEY"):
        return True
    if not settings.DEBUG or not _env_value("OPENAI_KEYS_FILE"):
        return False
    try:
        file_config = _load_keys_file_if_needed("", "")
    except OpenAIConfigurationError:
        return False
    return bool(_clean_config_value(file_config.get("OPENAI_API_KEY")))


def _configured_model_or_empty():
    if _env_value('OPENAI_MODEL'):
        return _env_value('OPENAI_MODEL')
    if settings.DEBUG:
        try:
            data=_load_keys_file_if_needed('', '')
            return _clean_config_value(data.get('OPENAI_MODEL'))
        except OpenAIConfigurationError:
            pass
    return ''
