"""
Service d'analyse documentaire OpenAI.

Ce module ne retourne jamais de donnees simulees. Si OpenAI n'est pas
configure ou si l'analyse echoue, il leve une erreur controlee.
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
from pathlib import Path

from .openai_config import (
    OpenAIConfigurationError,
    get_openai_config,
    get_openai_config_status,
)


IMAGE_MIME_TYPES = {"image/jpeg", "image/png"}
PDF_MIME_TYPE = "application/pdf"
CSV_MIME_TYPES = {"text/csv", "application/csv"}
XLSX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
SUPPORTED_DOCUMENT_MIME_TYPES = {*IMAGE_MIME_TYPES, PDF_MIME_TYPE, *CSV_MIME_TYPES, XLSX_MIME_TYPE}
SUPPORTED_DOCUMENT_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".csv", ".xlsx", ".xslx"}
ANALYSIS_MAX_OUTPUT_TOKENS = 12000
DOCUMENT_AI_MAX_BYTES = 25 * 1024 * 1024
OPENAI_TIMEOUT_SECONDS = 60
logger = logging.getLogger(__name__)


class DocumentAIError(Exception):
    """Erreur attendue pendant l'analyse automatique d'un document."""


class DocumentAIConfigurationError(DocumentAIError, OpenAIConfigurationError):
    """OpenAI n'est pas configure pour lancer l'analyse."""


def analyze_invoice(file_path):
    return _analyze_with_openai(
        file_path=file_path,
        schema_name="invoice_analysis",
        schema=_invoice_schema(),
        prompt=_invoice_prompt(),
        validator=_validate_invoice_data,
    )


def analyze_delivery_note(file_path):
    return _analyze_with_openai(
        file_path=file_path,
        schema_name="delivery_note_analysis",
        schema=_delivery_note_schema(),
        prompt=_delivery_note_prompt(),
        validator=_validate_delivery_note_data,
    )


def openai_api_key_configured():
    return get_openai_config_status()["api_key_configured"]


def configured_openai_model():
    return get_openai_config_status()["model"]


def openai_configuration_source():
    return get_openai_config_status()["source"]


def openai_configuration_status():
    return get_openai_config_status()


def test_openai_connection():
    config = _load_openai_config()
    client = _create_openai_client(config)
    try:
        response = client.responses.create(
            model=config["model"],
            input=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "Réponds uniquement avec le mot OK.",
                        }
                    ],
                }
            ],
            max_output_tokens=20,
        )
    except DocumentAIError:
        raise
    except Exception as exc:  # pragma: no cover - depend de l'API externe
        raise DocumentAIError("La connexion OpenAI a echoue.") from exc

    return _response_output_text(response)


def _analyze_with_openai(
    file_path,
    schema_name,
    schema,
    prompt,
    validator,
    max_output_tokens=ANALYSIS_MAX_OUTPUT_TOKENS,
):
    file_path = Path(file_path)
    if not file_path.exists():
        raise DocumentAIError(f"Fichier introuvable: {file_path}")
    _validate_document_file_for_ai(file_path)

    config = _load_openai_config()
    client = _create_openai_client(config)

    try:
        response = client.responses.create(
            model=config["model"],
            input=[
                {
                    "role": "system",
                    "content": (
                        "Tu extrais des donnees de documents fournisseurs pour une "
                        "application de gestion de restaurants. Reponds uniquement "
                        "avec le JSON conforme au schema fourni. N'invente pas de "
                        "valeur : utilise null ou une liste vide si l'information "
                        "n'est pas lisible. Ne cree aucun fournisseur, ne valide "
                        "aucun document et ne rapproche jamais facture et bon de "
                        "livraison."
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        _document_content_item(file_path),
                        {
                            "type": "input_text",
                            "text": prompt,
                        },
                    ],
                },
            ],
            text={
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "schema": schema,
                    "strict": True,
                },
            },
            max_output_tokens=max_output_tokens,
        )
    except DocumentAIError:
        raise
    except Exception as exc:  # pragma: no cover - depend de l'API externe
        raise DocumentAIError("L'appel OpenAI a echoue.") from exc

    _raise_if_response_incomplete(response)

    try:
        raw_output = _response_output_text(response)
        data = _loads_json_output(raw_output)
    except json.JSONDecodeError as exc:
        if "Unterminated string" in str(exc):
            raise DocumentAIError("La reponse OpenAI semble incomplete.") from exc
        raise DocumentAIError("La reponse OpenAI n'est pas un JSON valide.") from exc
    except (TypeError, ValueError) as exc:
        raise DocumentAIError("La reponse OpenAI n'est pas un JSON valide.") from exc

    return validator(data)


def _validate_document_file_for_ai(file_path):
    size = file_path.stat().st_size
    if size <= 0:
        raise DocumentAIError("Le fichier est vide.")
    if size > DOCUMENT_AI_MAX_BYTES:
        raise DocumentAIError("Le fichier est trop volumineux pour l'analyse automatique.")

    mime_type = _document_mime_type(file_path)
    if mime_type not in SUPPORTED_DOCUMENT_MIME_TYPES and file_path.suffix.lower() not in SUPPORTED_DOCUMENT_EXTENSIONS:
        raise DocumentAIError("Le type de fichier n'est pas supporte par l'analyse automatique.")

    logger.info(
        "Preparation analyse IA document: name=%s mime=%s size=%s",
        file_path.name,
        mime_type,
        size,
    )


def _load_openai_config():
    try:
        return get_openai_config()
    except OpenAIConfigurationError as exc:
        raise DocumentAIConfigurationError(str(exc)) from exc


def _create_openai_client(config):
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover - depend de l'image Docker
        raise DocumentAIConfigurationError("La librairie openai n'est pas installee.") from exc

    return OpenAI(api_key=config["api_key"], timeout=OPENAI_TIMEOUT_SECONDS)


def _document_content_item(file_path):
    mime_type = _document_mime_type(file_path)
    data_url = _file_data_url(file_path, mime_type)
    if mime_type in IMAGE_MIME_TYPES:
        return {
            "type": "input_image",
            "image_url": data_url,
        }
    return {
        "type": "input_file",
        "filename": file_path.name,
        "file_data": data_url,
    }


def _document_mime_type(file_path):
    return mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"


def _file_data_url(file_path, mime_type):
    with file_path.open("rb") as document_file:
        encoded = base64.b64encode(document_file.read()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _response_output_text(response):
    output_text = getattr(response, "output_text", None)
    if output_text:
        return output_text

    for item in getattr(response, "output", []) or []:
        item_type = _get_attr_or_key(item, "type")
        if item_type != "message":
            continue
        for content in _get_attr_or_key(item, "content") or []:
            if _get_attr_or_key(content, "type") == "output_text":
                text = _get_attr_or_key(content, "text")
                if text:
                    return text

    raise DocumentAIError("La reponse OpenAI ne contient pas de texte exploitable.")


def _loads_json_output(raw_output):
    try:
        return json.loads(raw_output)
    except json.JSONDecodeError:
        repaired = _extract_json_object(raw_output)
        if repaired and repaired != raw_output:
            logger.warning("Reponse OpenAI non strictement JSON, tentative de lecture de l'objet JSON extrait.")
            return json.loads(repaired)
        raise


def _extract_json_object(raw_output):
    if not isinstance(raw_output, str):
        return ""
    start = raw_output.find("{")
    end = raw_output.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return ""
    return raw_output[start : end + 1]


def _raise_if_response_incomplete(response):
    if _get_attr_or_key(response, "status") != "incomplete":
        return

    details = _get_attr_or_key(response, "incomplete_details") or {}
    reason = _get_attr_or_key(details, "reason") or "raison inconnue"
    raise DocumentAIError(f"La reponse OpenAI est incomplete ({reason}).")


def _get_attr_or_key(value, key):
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)


def _invoice_prompt():
    return (
        "Analyse cette facture reelle. Extrais uniquement les champs demandes. "
        "type_document doit valoir facture. Les dates doivent etre au format ISO "
        "YYYY-MM-DD si possible. Les montants doivent etre des chaines decimales "
        "sans symbole de devise. La confiance doit etre un nombre entre 0 et 1. "
        "Utilise null pour les champs non lisibles. Pour les lignes, conserve une "
        "ligne par produit ou service tel qu'il apparait sur la facture. N'inclus "
        "pas les sous-totaux, totaux generaux ou titres de sections comme des "
        "produits. Distingue bien prix unitaire et montant total de ligne. "
        "N'invente jamais une reference, un EAN, une quantite ou un prix : retourne "
        "null lorsque l'information est absente ou insuffisamment lisible."
    )


def _delivery_note_prompt():
    return (
        "Analyse ce bon de livraison reel, meme s'il s'agit d'un PDF scanne, "
        "d'une photo mobile, d'un tableau mal aligne ou d'un document incomplet. "
        "type_document doit valoir bon_livraison. Retourne uniquement le JSON "
        "strict demande. N'invente jamais une valeur : utilise null si une "
        "information est absente ou incertaine. La date doit etre au format ISO "
        "YYYY-MM-DD si possible. Les quantites, prix et totaux doivent etre des "
        "nombres ou des chaines sans symbole de devise lorsque la lecture est "
        "possible. Ne suppose pas que les prix existent sur un bon de livraison. "
        "Ne confonds pas quantite commandee, facturee et livree : la quantite "
        "importante est la quantite reellement livree. Conserve raw_text pour "
        "chaque ligne lorsque le texte source est exploitable. Ajoute un warning "
        "court pour chaque champ important absent, ambigu ou peu lisible."
    )


def _nullable_string(description):
    return {"type": ["string", "null"], "description": description}


def _nullable_number_or_string(description):
    return {"type": ["number", "string", "null"], "description": description}


def _line_invoice_schema():
    return {
        "type": "object",
        "properties": {
            "reference_produit": _nullable_string("Reference fournisseur ou produit."),
            "ean": _nullable_string("Code EAN si present."),
            "designation": _nullable_string("Designation de la ligne."),
            "quantite": _nullable_string("Quantite lue, sans unite."),
            "unite": _nullable_string("Unite lue si disponible."),
            "prix_unitaire_ht": _nullable_string("Prix unitaire HT."),
            "montant_ht": _nullable_string("Montant HT de la ligne."),
            "taux_tva": _nullable_string("Taux TVA de la ligne."),
            "montant_ttc": _nullable_string("Montant TTC de la ligne."),
            "source_page": {"type": ["integer", "null"], "description": "Numéro de page contenant cette ligne, à partir de 1. Null si non identifiable."},
        },
        "required": [
            "reference_produit",
            "ean",
            "designation",
            "quantite",
            "unite",
            "prix_unitaire_ht",
            "montant_ht",
            "taux_tva",
            "montant_ttc",
            "source_page",
        ],
        "additionalProperties": False,
    }


def _line_delivery_schema():
    return {
        "type": "object",
        "properties": {
            "product_reference": _nullable_string("Reference fournisseur ou produit si presente."),
            "product_name": _nullable_string("Nom du produit livre."),
            "quantity": _nullable_number_or_string("Quantite reellement livree."),
            "unit": _nullable_string("Unite lue si disponible."),
            "packaging": _nullable_string("Conditionnement si present."),
            "unit_price": _nullable_number_or_string("Prix unitaire si present."),
            "line_total": _nullable_number_or_string("Total de ligne si present."),
            "confidence": {
                "type": ["number", "null"],
                "description": "Niveau de confiance de la ligne entre 0 et 1.",
            },
            "raw_text": _nullable_string("Texte brut de la ligne si exploitable."),
            "source_page": {"type": ["integer", "null"], "description": "Numéro de page contenant cette ligne, à partir de 1. Null si non identifiable."},
        },
        "required": [
            "product_reference",
            "product_name",
            "quantity",
            "unit",
            "packaging",
            "unit_price",
            "line_total",
            "confidence",
            "raw_text",
            "source_page",
        ],
        "additionalProperties": False,
    }


def _delivery_note_header_schema():
    return {
        "type": "object",
        "properties": {
            "number": _nullable_string("Numero du bon de livraison si present."),
            "date": _nullable_string("Date du bon au format ISO YYYY-MM-DD si possible."),
            "supplier_name": _nullable_string("Fournisseur detecte si identifiable."),
            "restaurant_name": _nullable_string("Restaurant ou client livre si identifiable."),
            "delivery_address": _nullable_string("Adresse de livraison si presente."),
            "currency": _nullable_string("Devise si un prix est present."),
            "confidence": {
                "type": ["number", "null"],
                "description": "Niveau de confiance global entre 0 et 1.",
            },
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "number",
            "date",
            "supplier_name",
            "restaurant_name",
            "delivery_address",
            "currency",
            "confidence",
            "warnings",
        ],
        "additionalProperties": False,
    }


def _invoice_schema():
    return {
        "type": "object",
        "properties": {
            "type_document": {"type": "string", "enum": ["facture"]},
            "numero_facture": _nullable_string("Numero de facture."),
            "date_facture": _nullable_string("Date au format ISO YYYY-MM-DD."),
            "date_livraison_detectee": _nullable_string(
                "Date de livraison detectee sur la facture au format ISO YYYY-MM-DD."
            ),
            "montant_ht": _nullable_string("Montant HT total."),
            "montant_tva": _nullable_string("Montant TVA total."),
            "montant_ttc": _nullable_string("Montant TTC total."),
            "devise": _nullable_string("Devise, par exemple EUR ou CHF."),
            "fournisseur_detecte": _nullable_string("Nom fournisseur detecte dans le document."),
            "lignes": {"type": "array", "items": _line_invoice_schema()},
            "confiance_ia": {
                "type": ["number", "null"],
                "description": "Niveau de confiance entre 0 et 1.",
            },
            "champs_incertains": {"type": "array", "items": {"type": "string"}},
            "commentaire_analyse": _nullable_string("Commentaire court et comprehensible."),
        },
        "required": [
            "type_document",
            "numero_facture",
            "date_facture",
            "date_livraison_detectee",
            "montant_ht",
            "montant_tva",
            "montant_ttc",
            "devise",
            "fournisseur_detecte",
            "lignes",
            "confiance_ia",
            "champs_incertains",
            "commentaire_analyse",
        ],
        "additionalProperties": False,
    }


def _delivery_note_schema():
    return {
        "type": "object",
        "properties": {
            "type_document": {"type": "string", "enum": ["bon_livraison"]},
            "delivery_note": _delivery_note_header_schema(),
            "lines": {"type": "array", "items": _line_delivery_schema()},
        },
        "required": ["type_document", "delivery_note", "lines"],
        "additionalProperties": False,
    }


def _validate_invoice_data(data):
    cleaned = _validate_document_data(
        data,
        required_keys={
            "type_document",
            "numero_facture",
            "date_facture",
            "date_livraison_detectee",
            "montant_ht",
            "montant_tva",
            "montant_ttc",
            "devise",
            "fournisseur_detecte",
            "lignes",
            "confiance_ia",
            "champs_incertains",
            "commentaire_analyse",
        },
        nullable_string_keys={
            "numero_facture",
            "date_facture",
            "date_livraison_detectee",
            "montant_ht",
            "montant_tva",
            "montant_ttc",
            "devise",
            "fournisseur_detecte",
            "commentaire_analyse",
        },
        allowed_type_document="facture",
    )
    return cleaned


def _validate_delivery_note_data(data):
    if not isinstance(data, dict):
        raise DocumentAIError("La reponse IA doit etre un objet JSON.")
    if data.get("type_document") != "bon_livraison":
        raise DocumentAIError("Le type de document retourne par l'IA est invalide.")

    header = data.get("delivery_note")
    if not isinstance(header, dict):
        raise DocumentAIError("Le champ IA delivery_note doit etre un objet.")

    lines = data.get("lines")
    if lines is None:
        lines = []
    if not isinstance(lines, list):
        raise DocumentAIError("Le champ IA lines doit etre une liste.")

    cleaned_header = {
        "number": _optional_text(header.get("number")),
        "date": _optional_text(header.get("date")),
        "supplier_name": _optional_text(header.get("supplier_name")),
        "restaurant_name": _optional_text(header.get("restaurant_name")),
        "delivery_address": _optional_text(header.get("delivery_address")),
        "currency": _optional_text(header.get("currency")),
        "confidence": _clean_confidence(header.get("confidence")),
        "warnings": _clean_string_list(header.get("warnings")),
    }
    cleaned_lines = [_clean_delivery_line(line) for line in lines if isinstance(line, dict)]
    warnings = cleaned_header["warnings"]

    return {
        "type_document": "bon_livraison",
        "delivery_note": cleaned_header,
        "lines": cleaned_lines,
        "numero_bl": cleaned_header["number"] or "",
        "date_livraison": cleaned_header["date"] or "",
        "fournisseur_detecte": cleaned_header["supplier_name"] or "",
        "restaurant_detecte": cleaned_header["restaurant_name"] or "",
        "adresse_livraison": cleaned_header["delivery_address"] or "",
        "devise": cleaned_header["currency"] or "",
        "lignes": cleaned_lines,
        "confiance_ia": cleaned_header["confidence"],
        "champs_incertains": warnings,
        "commentaire_analyse": "; ".join(warnings) if warnings else "",
    }


def _optional_text(value):
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text or None


def _clean_string_list(value):
    if value is None:
        return []
    if not isinstance(value, list):
        raise DocumentAIError("Les avertissements IA doivent etre une liste.")
    return [str(item).strip() for item in value if str(item).strip()]


def _clean_delivery_line(line):
    cleaned = {
        "product_reference": _optional_text(line.get("product_reference")),
        "product_name": _optional_text(line.get("product_name")),
        "quantity": _optional_scalar(line.get("quantity")),
        "unit": _optional_text(line.get("unit")),
        "packaging": _optional_text(line.get("packaging")),
        "unit_price": _optional_scalar(line.get("unit_price")),
        "line_total": _optional_scalar(line.get("line_total")),
        "confidence": _clean_confidence(line.get("confidence")),
        "raw_text": _optional_text(line.get("raw_text")),
    }
    if 'source_page' in line:
        cleaned['source_page'] = clean_source_page(line.get('source_page'))
    return cleaned


def clean_source_page(value):
    return int(value) if not isinstance(value, bool) and str(value).isdigit() and 0 < int(value) <= 100000 else None


def _optional_scalar(value):
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return value
    text = str(value).strip()
    return text or None


def _validate_document_data(data, required_keys, nullable_string_keys, allowed_type_document):
    if not isinstance(data, dict):
        raise DocumentAIError("La reponse IA doit etre un objet JSON.")

    missing_keys = required_keys - set(data)
    if missing_keys:
        raise DocumentAIError(f"Champs IA manquants: {', '.join(sorted(missing_keys))}")

    cleaned = {key: data.get(key) for key in required_keys}
    if cleaned.get("type_document") != allowed_type_document:
        raise DocumentAIError("Le type de document retourne par l'IA est invalide.")

    for key in nullable_string_keys:
        value = cleaned.get(key)
        cleaned[key] = "" if value is None else str(value).strip()

    for key in ("lignes", "champs_incertains"):
        if cleaned.get(key) is None:
            cleaned[key] = []
        if not isinstance(cleaned[key], list):
            raise DocumentAIError(f"Le champ IA {key} doit etre une liste.")

    cleaned["champs_incertains"] = [str(item).strip() for item in cleaned["champs_incertains"] if item]
    cleaned["confiance_ia"] = _clean_confidence(cleaned.get("confiance_ia"))
    return cleaned


def _clean_confidence(value):
    if value in (None, ""):
        return None
    try:
        confidence = float(value)
    except (TypeError, ValueError) as exc:
        raise DocumentAIError("La confiance IA doit etre un nombre.") from exc
    if confidence < 0 or confidence > 1:
        raise DocumentAIError("La confiance IA doit etre comprise entre 0 et 1.")
    return round(confidence, 2)
