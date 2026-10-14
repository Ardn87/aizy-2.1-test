from decimal import Decimal

from django.db.models import Q

from core.models import CatalogueFournisseur, LigneCatalogue, ProductAssociation, UnitConversion


PRICE_TOLERANCE = Decimal("0.02")
CONTROL_VERSION = 2

STATUS_PRODUIT_AMBIGU = "produit_ambigu"
STATUS_CONFORME = "conforme"
STATUS_ECART_PRIX = "ecart_prix"
STATUS_PRODUIT_NON_TROUVE = "produit_non_trouve"
STATUS_PRIX_MANQUANT = "prix_manquant"
STATUS_DEVISE_DIFFERENTE = "devise_differente"
STATUS_CATALOGUE_ABSENT = "catalogue_absent"
STATUS_UNITE_A_VERIFIER = "unite_a_verifier"
STATUS_DATE_ABSENTE = "date_absente"
STATUS_CATALOGUES_MULTIPLES = "catalogues_multiples"

LINE_STATUS_LABELS = {
    STATUS_PRODUIT_AMBIGU: "Plusieurs produits correspondent",
    STATUS_CONFORME: "Conforme",
    STATUS_ECART_PRIX: "Écart de prix",
    STATUS_PRODUIT_NON_TROUVE: "Produit non trouvé dans le catalogue",
    STATUS_PRIX_MANQUANT: "Prix manquant",
    STATUS_DEVISE_DIFFERENTE: "Devise différente",
    STATUS_CATALOGUE_ABSENT: "Catalogue absent",
    STATUS_UNITE_A_VERIFIER: "Unité à vérifier",
}


def control_invoice_against_catalogue(facture, *, catalogues=None, catalogue_indexes=None):
    control_date = facture.date_livraison_detectee or facture.date_facture
    result = empty_control_result(facture, control_date)
    if facture.document_role != 'active':
        result.update(status='document_exclu', message='Document exclu : duplicata, remplacement ou relevé.')
        return result

    if not control_date:
        result.update(
            {
                "status": STATUS_DATE_ABSENTE,
                "message": "Contrôle catalogue impossible : aucune date de contrôle disponible.",
            }
        )
        return result

    catalogues = list(applicable_catalogues(facture, control_date)[:2]) if catalogues is None else catalogues[:2]
    if not catalogues:
        result.update(
            {
                "status": STATUS_CATALOGUE_ABSENT,
                "message": "Aucun catalogue valide trouvé.",
            }
        )
        return result

    if len(catalogues) > 1:
        result.update(
            {
                "status": STATUS_CATALOGUES_MULTIPLES,
                "message": "Plusieurs catalogues correspondent à cette date. Vérifiez les catalogues du fournisseur.",
            }
        )
        return result

    catalogue = catalogues[0]
    result["catalogue"] = catalogue
    result["catalogue_devise"] = catalogue.devise

    if facture.devise == "UNK" or catalogue.devise == "UNK" or facture.devise != catalogue.devise:
        result.update(
            {
                "status": STATUS_DEVISE_DIFFERENTE,
                "message": "Devise différente, contrôle impossible.",
            }
        )
        return result

    result["status"] = "controle_effectue"
    result["initial_reference"] = catalogue.is_initial_reference
    result["message"] = ("Comparaison avec les prix de référence initiaux issus des documents. "
                         "Ils ne constituent pas un tarif fournisseur confirmé.") if catalogue.is_initial_reference else ""
    result["control_available"] = True
    catalogue_indexes = build_catalogue_indexes(catalogue) if catalogue_indexes is None else catalogue_indexes

    for invoice_line in facture.lignes.all():
        if invoice_line.kind != "product" or facture.kind == "credit":
            continue
        detail = control_invoice_line(invoice_line, catalogue_indexes)
        result["details"].append(detail)
        result["counts"][detail["status"]] += 1

    result["problem_details"] = [
        detail for detail in result["details"] if detail["status"] != STATUS_CONFORME
    ]
    result["has_problems"] = bool(result["problem_details"])
    return result


def empty_control_result(facture, control_date):
    return {
        "status": "",
        "message": "",
        "catalogue": None,
        "catalogue_devise": "",
        "date_controle": control_date,
        "date_source": (
            "date de livraison détectée"
            if facture.date_livraison_detectee
            else "date de facture"
            if facture.date_facture
            else ""
        ),
        "devise": facture.devise,
        "control_available": False,
        "initial_reference": False,
        "counts": {
            STATUS_PRODUIT_AMBIGU: 0,
            STATUS_CONFORME: 0,
            STATUS_ECART_PRIX: 0,
            STATUS_PRODUIT_NON_TROUVE: 0,
            STATUS_PRIX_MANQUANT: 0,
            STATUS_DEVISE_DIFFERENTE: 0,
            STATUS_CATALOGUE_ABSENT: 0,
            STATUS_UNITE_A_VERIFIER: 0,
        },
        "details": [],
        "problem_details": [],
        "has_problems": False,
    }


def applicable_catalogues(facture, control_date):
    candidates = CatalogueFournisseur.objects.filter(fournisseur=facture.fournisseur).filter(
        Q(statut='valide') | Q(origine='documents', statut='a_valider', date_debut__isnull=True, date_fin__isnull=True)
    ).order_by('-date_debut', '-date_import', 'pk')
    return select_control_catalogues(candidates, control_date)


def select_control_catalogues(catalogues, control_date):
    """A confirmed applicable period takes precedence over an undated initial baseline."""
    if not control_date:
        return []
    catalogues = list(catalogues)
    confirmed = [c for c in catalogues if not c.archived_at and c.statut == 'valide'
                 and c.date_debut and c.date_debut <= control_date
                 and (c.date_fin is None or c.date_fin >= control_date)]
    return confirmed or [c for c in catalogues if c.is_initial_reference]


def build_catalogue_indexes(catalogue, *, aliases=None):
    catalogue_lines = catalogue.lignes.all()
    references = {}
    eans = {}
    duplicate_references, duplicate_eans = set(), set()
    reference_variants, ean_variants = {}, {}
    for line in catalogue_lines:
        if line.statut_ligne == LigneCatalogue.StatutLigne.IGNOREE:
            continue
        reference = normalized_identifier(line.reference_produit)
        if reference: reference_variants.setdefault(reference,[]).append(line)
        if reference in references:
            duplicate_references.add(reference)
        if reference and reference not in references:
            references[reference] = line

        ean = normalized_identifier(line.ean)
        if ean: ean_variants.setdefault(ean,[]).append(line)
        if ean in eans:
            duplicate_eans.add(ean)
        if ean and ean not in eans:
            eans[ean] = line

    return {
        'currency': catalogue.devise,
        'product_collection': catalogue.origine == 'documents',
        'reference_variants': reference_variants, 'ean_variants': ean_variants,
        "duplicate_references": duplicate_references,
        "duplicate_eans": duplicate_eans,
        "references": references,
        "eans": eans,
        "conversions": {(r.reference, normalized_unit(r.invoice_unit), normalized_unit(r.catalogue_unit)): r for r in UnitConversion.objects.filter(fournisseur_id=catalogue.fournisseur_id,active=True)},
        "aliases": aliases if aliases is not None else {a.identifier: a for a in ProductAssociation.objects.filter(fournisseur_id=catalogue.fournisseur_id)},
    }


def control_invoice_line(invoice_line, catalogue_indexes):
    if ambiguous_match(invoice_line, catalogue_indexes):
        return line_detail(invoice_line, status=STATUS_PRODUIT_AMBIGU)
    catalogue_line = matching_catalogue_line(invoice_line, catalogue_indexes)
    if catalogue_line is None:
        return line_detail(invoice_line, status=STATUS_PRODUIT_NON_TROUVE)

    invoice_price = invoice_line.prix_unitaire_ht
    catalogue_price = catalogue_line.prix_unitaire_ht
    source = catalogue_line.price_source
    if source.get('initialised_from_document') and source.get('currency') != catalogue_indexes['currency']:
        return line_detail(invoice_line, status=STATUS_DEVISE_DIFFERENTE, catalogue_line=catalogue_line)
    factor = conversion_factor(invoice_line, catalogue_line, catalogue_indexes)
    if catalogue_price is not None and factor is not None:
        catalogue_price = (catalogue_price * factor).quantize(Decimal('0.01'))
    ecart = None
    if invoice_price is not None and catalogue_price is not None:
        ecart = invoice_price - catalogue_price

    if invoice_price is None or catalogue_price is None:
        status = STATUS_PRIX_MANQUANT
    elif factor is None:
        status = STATUS_UNITE_A_VERIFIER
    elif abs(ecart) > PRICE_TOLERANCE:
        status = STATUS_ECART_PRIX
    else:
        status = STATUS_CONFORME

    detail = line_detail(
        invoice_line,
        catalogue_line=catalogue_line,
        status=status,
        catalogue_price=catalogue_price,
        ecart=ecart,
    )
    detail["conversion_factor"] = factor
    return detail


def matching_catalogue_line(invoice_line, catalogue_indexes):
    if catalogue_indexes.get('product_collection'):
        candidates = collection_candidates(invoice_line,catalogue_indexes)
        return candidates[0] if len(candidates)==1 else None
    reference = normalized_identifier(invoice_line.reference_produit)
    ean = normalized_identifier(invoice_line.ean)
    identifier = "ref:" + reference if reference else "ean:" + ean if ean else ""
    association = catalogue_indexes.get("aliases", {}).get(identifier)
    if association:
        target = catalogue_indexes["references"].get(association.target_reference) or catalogue_indexes["eans"].get(association.target_ean)
        if target:
            return target
    if reference:
        catalogue_line = catalogue_indexes["references"].get(reference)
        if catalogue_line:
            return catalogue_line

    ean = normalized_identifier(invoice_line.ean)
    if ean:
        return catalogue_indexes["eans"].get(ean)

    return None


def line_detail(invoice_line, status, catalogue_line=None, catalogue_price=None, ecart=None):
    return {
        "invoice_line": invoice_line,
        "catalogue_line": catalogue_line,
        "produit": invoice_line.designation or invoice_line.reference_produit or invoice_line.ean,
        "reference_produit": invoice_line.reference_produit,
        "ean": invoice_line.ean,
        "prix_facture": invoice_line.prix_unitaire_ht,
        "prix_catalogue": catalogue_price,
        "ecart": ecart,
        "status": status,
        "status_label": LINE_STATUS_LABELS[status],
    }


def normalized_identifier(value):
    return (value or "").strip()


def normalized_unit(value):
    return (value or "").strip().lower()


def units_must_be_checked(invoice_line, catalogue_line):
    invoice_unit = normalized_unit(invoice_line.unite)
    catalogue_unit = normalized_unit(catalogue_line.unite)
    return not invoice_unit or not catalogue_unit or invoice_unit != catalogue_unit


def ambiguous_match(line, indexes):
    if indexes.get('product_collection'):
        return len(collection_candidates(line,indexes)) > 1
    reference, ean = normalized_identifier(line.reference_produit), normalized_identifier(line.ean)
    identifier = 'ref:' + reference if reference else 'ean:' + ean if ean else ''
    alias = indexes.get('aliases', {}).get(identifier)
    if alias:
        if alias.target_reference in indexes['references']:
            return alias.target_reference in indexes.get('duplicate_references', set())
        if alias.target_ean in indexes['eans']:
            return alias.target_ean in indexes.get('duplicate_eans', set())
    if reference in indexes['references']:
        return reference in indexes.get('duplicate_references', set())
    return bool(ean and ean in indexes.get('duplicate_eans', set()))


def collection_candidates(line, indexes):
    reference, ean = normalized_identifier(line.reference_produit), normalized_identifier(line.ean)
    identifier = 'ref:' + reference if reference else 'ean:' + ean if ean else ''
    alias = indexes.get('aliases', {}).get(identifier)
    candidates=[]
    if alias:
        candidates=indexes['reference_variants'].get(alias.target_reference) or indexes['ean_variants'].get(alias.target_ean) or []
    if not candidates:
        candidates=indexes['reference_variants'].get(reference) or indexes['ean_variants'].get(ean) or []
    same_unit=[c for c in candidates if normalized_unit(line.unite) and normalized_unit(c.unite)==normalized_unit(line.unite)]
    return same_unit or candidates


def conversion_factor(invoice_line, catalogue_line, indexes):
    invoice_unit, catalogue_unit = normalized_unit(invoice_line.unite), normalized_unit(catalogue_line.unite)
    if not invoice_unit or not catalogue_unit:
        return None
    if invoice_unit == catalogue_unit:
        return Decimal('1')
    rules = indexes.get('conversions', {})
    rule = rules.get((invoice_line.reference_produit.strip(), invoice_unit, catalogue_unit)) or rules.get(('', invoice_unit, catalogue_unit))
    return rule.factor if rule and rule.factor > 0 else None
