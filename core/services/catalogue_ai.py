import csv
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from .document_ai import (
    DocumentAIError,
    _analyze_with_openai,
    _clean_confidence,
    _nullable_string,
    clean_source_page,
)


CATALOGUE_MAX_OUTPUT_TOKENS = 32000
TABULAR_CATALOGUE_EXTENSIONS = {".csv", ".xlsx", ".xslx"}
CATALOGUE_FIELDS = {
    "nom_catalogue",
    "fournisseur_detecte",
    "date_debut",
    "date_fin",
    "devise",
    "commentaire_analyse",
    "confiance_ia",
    "champs_incertains",
    "lignes",
}
CATALOGUE_NULLABLE_STRING_FIELDS = {
    "nom_catalogue",
    "fournisseur_detecte",
    "date_debut",
    "date_fin",
    "devise",
    "commentaire_analyse",
}
CATALOGUE_LINE_FIELDS = {
    "reference_produit",
    "ean",
    "designation",
    "marque",
    "unite",
    "conditionnement",
    "prix_unitaire_ht",
}


def analyze_supplier_catalog(file_path):
    file_path = Path(file_path)
    is_tabular_catalogue = file_path.suffix.lower() in TABULAR_CATALOGUE_EXTENSIONS
    if is_tabular_catalogue:
        rows = _read_tabular_rows(file_path)
        header, mapping = _detect_header_mapping(rows)
        lines = _extract_tabular_catalogue_lines(file_path)
        return {'nom_catalogue': file_path.stem, 'fournisseur_detecte': '', 'date_debut': '', 'date_fin': '',
            'devise': '', 'confiance_ia': None, 'champs_incertains': ['date_debut', 'devise'] + ([] if mapping else ['colonnes']),
            'commentaire_analyse': 'Import local du tableau. Confirmez les colonnes, la période et la devise.',
            'lignes': lines, 'tabular_mapping': {str(k): v for k,v in mapping.items()}, 'header_row': header}

    data = _analyze_with_openai(
        file_path=file_path,
        schema_name="supplier_catalog_analysis",
        schema=_supplier_catalog_schema(),
        prompt=_supplier_catalog_prompt(extract_product_lines=not is_tabular_catalogue),
        validator=_validate_catalogue_data,
        max_output_tokens=CATALOGUE_MAX_OUTPUT_TOKENS,
    )
    return _complete_tabular_catalogue_lines(file_path, data)


def _supplier_catalog_prompt(extract_product_lines=True):
    line_instruction = (
        "Conserve l'ordre des lignes produits du document et extrais toutes les lignes "
        "produits, pas seulement un extrait. Si le document est long, continue jusqu'a "
        "la derniere ligne lisible."
    )
    if not extract_product_lines:
        line_instruction = (
            "Pour ce fichier tabulaire, retourne lignes comme une liste vide. Les lignes "
            "produits seront extraites directement du fichier complet par l'application."
        )

    return (
        "Analyse ce catalogue fournisseur reel. Extrais uniquement les champs demandes. "
        "Identifie d’abord la nature réelle du fichier dans type_document. Une facture, un BL, un relevé de ventes ou des statistiques doivent être classés facture, bon_livraison ou autre, même s’ils contiennent des prix. "
        "Les dates doivent etre au format ISO YYYY-MM-DD si possible. Les prix doivent "
        "etre des chaines decimales sans symbole de devise. Utilise null pour les "
        "informations absentes ou non lisibles. Ne cree jamais de fournisseur. "
        "Ne valide jamais le catalogue. Ne jamais inventer une reference, un EAN ou un "
        "prix. N'inclus pas les titres, sous-totaux, totaux generaux, conditions "
        f"commerciales ou notes de bas de page comme des produits. {line_instruction} "
        "Tous les prix catalogue de l'application sont hors taxe : renseigne uniquement "
        "prix_unitaire_ht et ne separe jamais prix brut, net ou TTC. Identifie la devise "
        "si elle est presente, et les dates de validite si elles sont presentes."
    )


def _line_catalogue_schema():
    return {
        "type": "object",
        "properties": {
            "reference_produit": _nullable_string("Reference fournisseur ou produit."),
            "ean": _nullable_string("Code EAN si present."),
            "designation": _nullable_string("Designation de la ligne."),
            "marque": _nullable_string("Marque si presente."),
            "unite": _nullable_string("Unite lue si disponible."),
            "conditionnement": _nullable_string("Conditionnement lu si disponible."),
            "prix_unitaire_ht": _nullable_string("Prix unitaire hors taxe."),
            "source_page": {"type": ["integer", "null"], "description": "Numéro de page de cette ligne, à partir de 1. Null si non identifiable."},
        },
        "required": sorted(CATALOGUE_LINE_FIELDS | {'source_page'}),
        "additionalProperties": False,
    }


def _supplier_catalog_schema():
    return {
        "type": "object",
        "properties": {
            "type_document": {"type":"string","enum":["catalogue","facture","bon_livraison","autre"],"description":"Nature réelle du fichier : une facture, un BL ou des statistiques ne sont pas un catalogue tarifaire."},
            "nom_catalogue": _nullable_string("Nom ou titre du catalogue."),
            "fournisseur_detecte": _nullable_string("Nom fournisseur detecte dans le document."),
            "date_debut": _nullable_string("Date de debut de validite au format ISO YYYY-MM-DD."),
            "date_fin": _nullable_string("Date de fin de validite au format ISO YYYY-MM-DD."),
            "devise": _nullable_string("Devise, par exemple EUR ou CHF."),
            "commentaire_analyse": _nullable_string("Commentaire court et comprehensible."),
            "confiance_ia": {
                "type": ["number", "null"],
                "description": "Niveau de confiance entre 0 et 1.",
            },
            "champs_incertains": {"type": "array", "items": {"type": "string"}},
            "lignes": {"type": "array", "items": _line_catalogue_schema()},
        },
        "required": sorted(CATALOGUE_FIELDS | {'type_document'}),
        "additionalProperties": False,
    }


def _validate_catalogue_data(data):
    if not isinstance(data, dict):
        raise DocumentAIError("La reponse IA doit etre un objet JSON.")
    if data.get('type_document') not in (None,'catalogue'):
        raise DocumentAIError('Ce document n’est pas un catalogue tarifaire. Déposez les factures et BL depuis la réception des documents.')

    missing_keys = CATALOGUE_FIELDS - set(data)
    if missing_keys:
        raise DocumentAIError(f"Champs IA manquants: {', '.join(sorted(missing_keys))}")

    cleaned = {key: data.get(key) for key in CATALOGUE_FIELDS}
    if 'type_document' in data: cleaned['type_document']=data['type_document']
    for key in CATALOGUE_NULLABLE_STRING_FIELDS:
        value = cleaned.get(key)
        cleaned[key] = "" if value is None else str(value).strip()

    cleaned["confiance_ia"] = _clean_confidence(cleaned.get("confiance_ia"))

    for key in ("lignes", "champs_incertains"):
        if cleaned.get(key) is None:
            cleaned[key] = []
        if not isinstance(cleaned[key], list):
            raise DocumentAIError(f"Le champ IA {key} doit etre une liste.")

    cleaned["champs_incertains"] = [str(item).strip() for item in cleaned["champs_incertains"] if item]
    cleaned["lignes"] = [_clean_catalogue_line(line) for line in cleaned["lignes"] if isinstance(line, dict)]
    return cleaned


def _clean_catalogue_line(line):
    cleaned_line = {}
    for key in CATALOGUE_LINE_FIELDS:
        value = line.get(key)
        cleaned_line[key] = "" if value is None else str(value).strip()
    if 'source_page' in line:
        cleaned_line['source_page'] = clean_source_page(line.get('source_page'))
    return cleaned_line


def _complete_tabular_catalogue_lines(file_path, data):
    if file_path.suffix.lower() not in TABULAR_CATALOGUE_EXTENSIONS:
        return data

    extracted_lines = _extract_tabular_catalogue_lines(file_path)
    if len(extracted_lines) <= len(data.get("lignes", [])):
        return data

    data["lignes"] = extracted_lines
    data["commentaire_analyse"] = _append_analysis_comment(
        data.get("commentaire_analyse"),
        "Lignes extraites directement du fichier tabulaire complet.",
    )
    return data


def _append_analysis_comment(current_comment, added_comment):
    current_comment = (current_comment or "").strip()
    if not current_comment:
        return added_comment
    return f"{current_comment} {added_comment}"


def _extract_tabular_catalogue_lines(file_path):
    rows = _read_tabular_rows(file_path)
    if not rows:
        return []

    header_index, mapping = _detect_header_mapping(rows)
    if not mapping:
        return []

    lines = []
    for row in rows[header_index + 1:]:
        line = _catalogue_line_from_row(row, mapping)
        if _tabular_line_has_product_content(line):
            lines.append(line)
    return lines


def _read_tabular_rows(file_path):
    if file_path.suffix.lower() == ".csv":
        return _read_csv_rows(file_path)
    if file_path.suffix.lower() in {".xlsx", ".xslx"}:
        return _read_xlsx_rows(file_path)
    return []


def _read_csv_rows(file_path):
    raw_bytes = file_path.read_bytes()
    text = raw_bytes.decode("utf-8-sig", errors="replace")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,|\t,")
        reader = csv.reader(text.splitlines(), dialect)
    except csv.Error:
        reader = csv.reader(text.splitlines(), delimiter=";")

    return [
        [_clean_cell_value(cell) for cell in row]
        for row in reader
        if any(_clean_cell_value(cell) for cell in row)
    ]


def _read_xlsx_rows(file_path):
    with zipfile.ZipFile(file_path) as archive:
        from .uploads import check_archive
        check_archive(archive)
        shared_strings = _xlsx_shared_strings(archive)
        sheet_path = _first_xlsx_sheet_path(archive)
        if not sheet_path:
            return []
        sheet_root = ElementTree.fromstring(archive.read(sheet_path))

    rows = []
    namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    for row in sheet_root.findall(".//x:sheetData/x:row", namespace):
        values_by_index = {}
        for cell in row.findall("x:c", namespace):
            cell_ref = cell.attrib.get("r", "")
            column_index = _xlsx_column_index(cell_ref)
            if column_index is None or column_index > 1023:
                continue
            values_by_index[column_index] = _xlsx_cell_value(cell, shared_strings, namespace)

        if values_by_index:
            max_index = max(values_by_index)
            values = [_clean_cell_value(values_by_index.get(index, "")) for index in range(max_index + 1)]
            if any(values):
                rows.append(values)
    if len(rows) > 50000:
        raise DocumentAIError("Classeur trop long (50 000 lignes maximum).")
    return rows


def _xlsx_shared_strings(archive):
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []

    root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    strings = []
    for item in root.findall("x:si", namespace):
        text_parts = [text_node.text or "" for text_node in item.findall(".//x:t", namespace)]
        strings.append("".join(text_parts))
    return strings


def _first_xlsx_sheet_path(archive):
    workbook_rels_path = "xl/_rels/workbook.xml.rels"
    workbook_path = "xl/workbook.xml"
    if workbook_rels_path not in archive.namelist() or workbook_path not in archive.namelist():
        return None

    rels_root = ElementTree.fromstring(archive.read(workbook_rels_path))
    workbook_root = ElementTree.fromstring(archive.read(workbook_path))
    rels = {
        rel.attrib.get("Id"): rel.attrib.get("Target")
        for rel in rels_root
        if rel.attrib.get("Id") and rel.attrib.get("Target")
    }
    namespace = {
        "x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
        "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    }
    first_sheet = workbook_root.find(".//x:sheets/x:sheet", namespace)
    if first_sheet is None:
        return None

    rel_id = first_sheet.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
    target = rels.get(rel_id)
    if not target:
        return None
    if target.startswith("/"):
        return target.lstrip("/")
    if target.startswith("xl/"):
        return target
    return f"xl/{target}"


def _xlsx_cell_value(cell, shared_strings, namespace):
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return "".join(text_node.text or "" for text_node in cell.findall(".//x:t", namespace))

    value_node = cell.find("x:v", namespace)
    if value_node is None or value_node.text is None:
        return ""

    value = value_node.text
    if cell_type == "s":
        try:
            return shared_strings[int(value)]
        except (IndexError, TypeError, ValueError):
            return ""
    return value


def _xlsx_column_index(cell_ref):
    match = re.match(r"([A-Z]+)", cell_ref.upper())
    if not match:
        return None
    column_index = 0
    for char in match.group(1):
        column_index = column_index * 26 + (ord(char) - ord("A") + 1)
    return column_index - 1


def _detect_header_mapping(rows):
    best_index = -1
    best_mapping = {}
    best_score = 0
    for index, row in enumerate(rows[:30]):
        mapping = _header_mapping(row)
        score = len(mapping)
        if "designation" in mapping.values():
            score += 2
        if "reference_produit" in mapping.values() or "ean" in mapping.values():
            score += 1
        if _mapped_price_fields(mapping):
            score += 2
        if score > best_score:
            best_index = index
            best_mapping = mapping
            best_score = score

    if best_score < 2 or not _mapped_price_fields(best_mapping):
        return -1, {}
    return best_index, best_mapping


def _header_mapping(row):
    mapping = {}
    used_fields = set()
    price_candidate = None
    for index, cell in enumerate(row):
        field = _field_for_header(cell)
        if not field or field in used_fields:
            continue
        if field == "prix_unitaire_ht":
            priority = _price_header_priority(cell)
            if price_candidate is None or priority > price_candidate[0]:
                price_candidate = (priority, index)
            continue
        mapping[index] = field
        used_fields.add(field)
    if price_candidate is not None:
        mapping[price_candidate[1]] = "prix_unitaire_ht"
    return mapping


def _field_for_header(header):
    normalized = _normalize_header(header)
    if not normalized:
        return ""

    if any(token in normalized for token in ("ean", "gencod", "gtin", "code barre", "barcode")):
        return "ean"
    if any(token in normalized for token in ("reference", "référence", "ref", "réf", "code article", "code produit", "sku")):
        return "reference_produit"
    if any(token in normalized for token in ("designation", "désignation", "libelle", "libellé", "nom produit", "produit", "article")):
        return "designation"
    if any(token in normalized for token in ("marque", "brand")):
        return "marque"
    if any(token in normalized for token in ("conditionnement", "colisage", "packaging", "emballage")):
        return "conditionnement"
    if any(token in normalized for token in ("unite", "unité", "unit", "uvc")):
        return "unite"
    if _is_price_header(normalized):
        return "prix_unitaire_ht"
    return ""


def _is_price_header(normalized):
    return any(token in normalized for token in ("prix", "tarif", "pu", "p u"))


def _price_header_priority(header):
    normalized = _normalize_header(header)
    if any(token in normalized for token in ("prix ht", "pu ht", "p u ht", "prix unitaire ht", "tarif ht")):
        return 4
    if "net" in normalized:
        return 3
    if "ttc" in normalized:
        return 1
    if "brut" in normalized:
        return 0
    return 2


def _normalize_header(value):
    return re.sub(r"\s+", " ", str(value or "").strip().lower().replace("_", " ").replace("-", " "))


def _mapped_price_fields(mapping):
    return {field for field in mapping.values() if field.startswith("prix_")}


def _catalogue_line_from_row(row, mapping):
    line = {field: "" for field in CATALOGUE_LINE_FIELDS}
    for index, field in mapping.items():
        if index >= len(row):
            continue
        line[field] = _clean_cell_value(row[index])
    return line


def _tabular_line_has_product_content(line):
    if not any(line.get(field) for field in ("reference_produit", "ean", "designation")):
        return False
    designation = line.get("designation", "").strip().lower()
    if designation in {"total", "sous total", "sous-total"}:
        return False
    return any(line.get(field) for field in CATALOGUE_LINE_FIELDS)


def _clean_cell_value(value):
    if value in (None, ""):
        return ""
    return str(value).strip()
