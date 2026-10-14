"""Validation des données, distincte des décisions commerciales."""
from decimal import Decimal

TOLERANCE = Decimal('0.02')

def invoice_checks(invoice, values=None):
    values = values or {}
    get = lambda key: values.get(key, getattr(invoice, key))
    problems = []
    for key, label in [('numero_facture', 'Numéro de facture'), ('date_facture', 'Date de facture'), ('devise', 'Devise'), ('montant_ht', 'Total HT'), ('montant_ttc', 'Total TTC')]:
        if get(key) in (None, '', 'UNK'):
            problems.append(label + ' à renseigner.')
    lines = list(invoice.lignes.all()) if invoice.pk else []
    if not lines:
        problems.append('Aucune ligne de facture renseignée.')
    complete_ht = True
    total_ht = Decimal('0')
    for line in lines:
        label = line.designation or line.reference_produit or f'Ligne {line.ordre + 1}'
        if line.kind == 'product' and (line.quantite is None or line.prix_unitaire_ht is None or not line.unite.strip()):
            problems.append(label + ' : quantité, prix ou unité à vérifier.')
        if line.montant_ht is None:
            complete_ht = False
        else:
            total_ht += line.montant_ht
        if line.quantite is not None and line.prix_unitaire_ht is not None and line.montant_ht is not None:
            if abs(line.quantite * line.prix_unitaire_ht - line.montant_ht) > TOLERANCE:
                problems.append(label + ' : quantité × prix différent du montant HT (remise ou frais à préciser).')
        if line.montant_ht is not None and line.montant_ttc is not None and line.taux_tva is not None:
            expected = line.montant_ht * (1 + line.taux_tva / 100)
            if abs(expected - line.montant_ttc) > TOLERANCE:
                problems.append(label + ' : TVA ou TTC à vérifier.')
        if get('kind') == 'invoice' and line.kind == 'product' and any(n is not None and n < 0 for n in (line.quantite, line.prix_unitaire_ht)):
            problems.append(label + ' : valeur négative, préciser Avoir ou Remise.')
    if lines and complete_ht and get('montant_ht') is not None and abs(total_ht - get('montant_ht')) > TOLERANCE:
        problems.append('Somme des lignes HT différente du total HT : frais, remises ou lignes manquantes à préciser.')
    ht, tva, ttc = get('montant_ht'), get('montant_tva'), get('montant_ttc')
    if all(n is not None for n in (ht, tva, ttc)) and abs(ht + tva - ttc) > TOLERANCE:
        problems.append('HT + TVA différent du TTC.')
    if invoice.donnees_ia and invoice.donnees_ia.get('champs_incertains'):
        # Original uncertainty remains visible; explicit validation confirms the corrected fields.
        pass
    return problems


def delivery_checks(document, values=None):
    values = values or {}
    problems = []
    if not values.get('date_livraison', document.date_livraison):
        problems.append('Date de livraison à renseigner.')
    # Un numéro BL absent est permis ; les quantités restent obligatoires pour le rapprochement.
    lines = list(document.lignes.exclude(statut_ligne='ignoree')) if document.pk else []
    if not lines:
        problems.append('Aucune ligne de livraison renseignée.')
    for line in lines:
        if not line.designation.strip() or line.quantite_livree is None or not line.unite.strip() or line.has_inconsistent_numbers():
            problems.append((line.designation or f'Ligne {line.ordre + 1}') + ' : produit, quantité ou unité à vérifier.')
    return problems


def validation_problems(document):
    from core.models import Facture, BonLivraison
    if isinstance(document, Facture):
        return invoice_checks(document)
    if isinstance(document, BonLivraison):
        return delivery_checks(document)
    return []


def document_is_verified(invoice):
    marked = bool(invoice.verified_at and invoice.statut in ('validee', 'litige') and invoice.devise != 'UNK')
    return marked and (len(invoice.validation_exception.strip()) >= 8 or not invoice_checks(invoice))
