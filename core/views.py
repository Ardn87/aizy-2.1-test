from datetime import date
from decimal import Decimal, InvalidOperation
import logging
import mimetypes
from pathlib import Path

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import close_old_connections, transaction
from django.db.models import Count, F, Max, Q
from django.db.models.functions import Greatest
from django.http import FileResponse, HttpResponseNotFound
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_http_methods

from .services.document_ai import clean_source_page
from .access import ROLE_ADMIN, ROLE_GESTIONNAIRE, ROLE_RECEPTION, ROLE_REVUE, role_required, user_has_role
from .forms import (
    BonLivraisonForm,
    BonLivraisonUploadForm,
    CatalogueInfoForm,
    CatalogueUploadForm,
    FactureForm,
    FactureUploadForm,
    LigneCatalogueForm,
    LigneBonLivraisonFormSet,
    FournisseurForm,
    LigneFactureFormSet,
    ReceptionBonLivraisonForm,
    RestaurantForm,
    UserCreateForm,
    UserPasswordForm,
    UserUpdateForm,
    get_user_app_role,
)
from .models import (
    AIDiagnosticEvent,
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
from .services.catalogue_ai import analyze_supplier_catalog
from .services.price_review import record_price
from .services.catalogue_control import control_invoice_against_catalogue
from .services.document_ai import (
    DocumentAIConfigurationError,
    DocumentAIError,
    analyze_delivery_note,
    analyze_invoice,
    openai_configuration_status,
    test_openai_connection,
)


DOCUMENT_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
DOCUMENT_PDF_EXTENSION = ".pdf"
DOCUMENT_AI_IMPORT_SUCCESS_MESSAGE = "Document importé. Analyse automatique terminée."
DOCUMENT_AI_IMPORT_STARTED_MESSAGE = "Document importé. Analyse IA en cours..."
DOCUMENT_AI_FAILURE_MESSAGE = (
    "Document importé, mais l’analyse automatique a échoué. "
    "Vous pouvez compléter les informations manuellement."
)
DOCUMENT_AI_RETRY_NOT_CONFIGURED_MESSAGE = "OpenAI n’est pas configuré."
DOCUMENT_AI_RETRY_FAILURE_MESSAGE = "L’analyse IA a échoué. Le document est conservé."
DOCUMENT_AI_RETRY_SUCCESS_MESSAGE = "Analyse IA terminée."
CATALOGUE_AI_IMPORT_SUCCESS_MESSAGE = "Catalogue importé. Analyse automatique terminée."
CATALOGUE_AI_IMPORT_STARTED_MESSAGE = "Catalogue importé. Analyse IA en cours..."
CATALOGUE_AI_IMPORT_FAILURE_MESSAGE = "Catalogue importé, mais l’analyse automatique a échoué."
DOCUMENT_ANALYSIS_PENDING_MESSAGE = (
    "Le document est encore en cours d’analyse. "
    "Vous pourrez le consulter une fois le traitement terminé."
)
logger = logging.getLogger(__name__)

DELIVERY_UNIT_ALIASES = {
    "kg": "kg",
    "kgs": "kg",
    "kilo": "kg",
    "kilos": "kg",
    "kilogramme": "kg",
    "kilogrammes": "kg",
    "g": "g",
    "gr": "g",
    "gramme": "g",
    "grammes": "g",
    "l": "l",
    "litre": "l",
    "litres": "l",
    "ml": "ml",
    "millilitre": "ml",
    "millilitres": "ml",
    "u": "unité",
    "unite": "unité",
    "unites": "unité",
    "piece": "pièce",
    "pieces": "pièce",
    "pc": "pièce",
    "pcs": "pièce",
    "carton": "carton",
    "cartons": "carton",
    "colis": "colis",
    "barquette": "barquette",
    "barquettes": "barquette",
    "bouteille": "bouteille",
    "bouteilles": "bouteille",
}
DELIVERY_STANDARD_UNITS = set(DELIVERY_UNIT_ALIASES.values())

DOCUMENT_STATUS_BADGE_CLASSES = {
    Facture.Statut.IMPORTEE: "text-bg-secondary",
    Facture.Statut.ANALYSE_EN_COURS: "text-bg-info",
    BonLivraison.Statut.UPLOADE: "text-bg-secondary",
    BonLivraison.Statut.ANALYSE_EN_COURS: "text-bg-info",
    BonLivraison.Statut.IMPORTE: "text-bg-primary",
    Facture.Statut.A_VALIDER: "text-bg-warning",
    BonLivraison.Statut.A_VERIFIER: "text-bg-warning",
    Facture.Statut.ANALYSE_ECHOUEE: "text-bg-danger",
    BonLivraison.Statut.ANALYSE_ECHOUEE: "text-bg-danger",
    Facture.Statut.VALIDEE: "text-bg-success",
    BonLivraison.Statut.VALIDE: "text-bg-success",
    CatalogueFournisseur.Statut.VALIDE: "text-bg-success",
    Facture.Statut.LITIGE: "text-bg-danger",
    BonLivraison.Statut.LITIGE: "text-bg-danger",
    CatalogueFournisseur.Statut.ERREUR: "text-bg-danger",
    CatalogueFournisseur.Statut.IMPORTE: "text-bg-secondary",
    CatalogueFournisseur.Statut.ANALYSE_EN_COURS: "text-bg-info",
    CatalogueFournisseur.Statut.A_VALIDER: "text-bg-warning",
    CatalogueFournisseur.Statut.ANALYSE_ECHOUEE: "text-bg-danger",
}


def record_ai_success(source, document_type="", document_id=None, message=""):
    AIDiagnosticEvent.objects.create(
        event_type=AIDiagnosticEvent.EventType.SUCCESS,
        source=source,
        document_type=document_type,
        document_id=document_id,
        message=message,
    )


def record_ai_error(source, error, document_type="", document_id=None):
    AIDiagnosticEvent.objects.create(
        event_type=AIDiagnosticEvent.EventType.ERROR,
        source=source,
        document_type=document_type,
        document_id=document_id,
        message=str(error)[:1000],
    )


def latest_ai_success_date():
    latest_event = (
        AIDiagnosticEvent.objects
        .filter(event_type=AIDiagnosticEvent.EventType.SUCCESS)
        .order_by("-date_creation")
        .first()
    )
    if latest_event:
        return latest_event.date_creation

    latest_facture = Facture.objects.filter(donnees_ia__isnull=False).aggregate(
        latest=Max("date_modification")
    )["latest"]
    latest_bon = BonLivraison.objects.filter(donnees_ia__isnull=False).aggregate(
        latest=Max("date_modification")
    )["latest"]
    dates = [value for value in (latest_facture, latest_bon) if value]
    if not dates:
        return None
    return max(dates)


def latest_ai_error():
    return (
        AIDiagnosticEvent.objects
        .filter(event_type=AIDiagnosticEvent.EventType.ERROR)
        .order_by("-date_creation")
        .first()
    )


from .auth_views import SafeLoginView
login_view = SafeLoginView.as_view()


@login_required
def home(request):
    if user_has_role(request.user, ROLE_ADMIN, ROLE_GESTIONNAIRE):
        return redirect("dashboard")
    if user_has_role(request.user, ROLE_RECEPTION):
        return redirect("document_deposit")
    if user_has_role(request.user, ROLE_REVUE):
        return redirect("price_review")
    raise PermissionDenied


@login_required
@require_http_methods(["GET", "POST"])
def logout_view(request):
    if request.method == "POST":
        logout(request)
        messages.success(request, "Vous êtes déconnecté.")
        return redirect("login")
    return render(request, "core/logout_confirm.html")


def fournisseurs_with_document_counts(active_only=True, search_query=""):
    fournisseurs = Fournisseur.objects.select_related("restaurant").filter(restaurant__actif=True)
    if active_only:
        fournisseurs = fournisseurs.filter(actif=True)
    if search_query:
        fournisseurs = fournisseurs.filter(nom__icontains=search_query)

    return (
        fournisseurs
        .annotate(
            factures_count=Count("factures", filter=Q(factures__archived_at__isnull=True), distinct=True),
            bons_livraison_count=Count("bons_livraison", filter=Q(bons_livraison__archived_at__isnull=True), distinct=True),
            latest_update=Greatest(
                Max('factures__date_modification', filter=Q(factures__archived_at__isnull=True)),
                Max('bons_livraison__date_modification', filter=Q(bons_livraison__archived_at__isnull=True)),
                Max('catalogues__date_modification', filter=Q(catalogues__archived_at__isnull=True)),
            ),
            factures_a_valider_count=Count(
                "factures",
                filter=Q(factures__statut__in=[Facture.Statut.A_VALIDER, Facture.Statut.ANALYSE_ECHOUEE]) & Q(factures__archived_at__isnull=True),
                distinct=True,
            ),
            bons_a_valider_count=Count(
                "bons_livraison",
                filter=Q(bons_livraison__statut__in=[BonLivraison.Statut.A_VERIFIER, BonLivraison.Statut.ANALYSE_ECHOUEE]) & Q(bons_livraison__archived_at__isnull=True),
                distinct=True,
            ),
            factures_litige_count=Count(
                "factures",
                filter=Q(factures__statut=Facture.Statut.LITIGE) & Q(factures__archived_at__isnull=True),
                distinct=True,
            ),
            bons_litige_count=Count(
                "bons_livraison",
                filter=Q(bons_livraison__statut=BonLivraison.Statut.LITIGE) & Q(bons_livraison__archived_at__isnull=True),
                distinct=True,
            ),
        )
        .annotate(
            documents_a_valider_count=F("factures_a_valider_count") + F("bons_a_valider_count"),
            litiges_count=F("factures_litige_count") + F("bons_litige_count"),
        )
        .order_by("nom")
    )


def clean_status_filter(value, choices):
    allowed_statuses = {choice[0] for choice in choices}
    if value in allowed_statuses:
        return value
    return ""


def document_status_badge_class(status):
    return DOCUMENT_STATUS_BADGE_CLASSES.get(status, "text-bg-secondary")


def document_analysis_is_pending(document):
    return getattr(document, "statut", "") == "analyse_en_cours"


def redirect_if_analysis_pending(request, document):
    return None  # Le document reçu reste consultable pendant son analyse.


def cascade_restaurant_currency(restaurant, previous_currency):
    new_currency = restaurant.devise_defaut
    if previous_currency == new_currency:
        return {
            "fournisseurs": 0,
            "factures": 0,
            "catalogues": 0,
        }

    # Une préférence ne réétiquette jamais les montants historiques.
    return {"fournisseurs": 0, "factures": 0, "catalogues": 0}


def detail_form_data_with_quick_status(request, choices):
    if request.method != "POST":
        return None

    form_data = request.POST.copy()
    quick_status = form_data.get("quick_status")
    if quick_status in {choice[0] for choice in choices}:
        form_data["statut"] = quick_status
    return form_data


def get_document_filters(request, prefix, choices):
    return {
        "q": request.GET.get(f"{prefix}_q", "").strip(),
        "statut": clean_status_filter(request.GET.get(f"{prefix}_statut", ""), choices),
        "date": request.GET.get(f"{prefix}_date", "").strip(),
    }


def filter_factures(queryset, filters):
    if filters["q"]:
        queryset = queryset.filter(numero_facture__icontains=filters["q"])
    if filters["statut"]:
        queryset = queryset.filter(statut=filters["statut"])

    filter_date = parse_iso_date(filters["date"])
    if filter_date:
        queryset = queryset.filter(date_facture=filter_date)

    return queryset.order_by("-date_facture", "-date_import")


def filter_bons_livraison(queryset, filters):
    if filters["q"]:
        queryset = queryset.filter(numero_bon_livraison__icontains=filters["q"])
    if filters["statut"]:
        queryset = queryset.filter(statut=filters["statut"])

    filter_date = parse_iso_date(filters["date"])
    if filter_date:
        queryset = queryset.filter(date_livraison=filter_date)

    return queryset.order_by("-date_livraison", "-date_import")


def catalogue_counts_annotations():
    return {
        "lignes_count": Count("lignes", distinct=True),
        "lignes_ok_count": Count(
            "lignes",
            filter=Q(lignes__statut_ligne=LigneCatalogue.StatutLigne.OK),
            distinct=True,
        ),
        "lignes_a_verifier_count": Count(
            "lignes",
            filter=Q(lignes__statut_ligne=LigneCatalogue.StatutLigne.A_VERIFIER),
            distinct=True,
        ),
        "lignes_ignorees_count": Count(
            "lignes",
            filter=Q(lignes__statut_ligne=LigneCatalogue.StatutLigne.IGNOREE),
            distinct=True,
        ),
    }


def catalogues_with_line_counts(queryset):
    return queryset.annotate(**catalogue_counts_annotations())


def current_valid_catalogue(queryset):
    today = timezone.localdate()
    return (
        catalogues_with_line_counts(queryset)
        .filter(
            statut=CatalogueFournisseur.Statut.VALIDE,
            date_debut__lte=today,
        )
        .filter(Q(date_fin__isnull=True) | Q(date_fin__gte=today))
        .order_by("-date_debut", "-date_import")
        .first()
    )


def catalogue_line_filters(request, catalogue):
    requested_status = request.GET.get("statut", "").strip()
    allowed_statuses = {choice[0] for choice in LigneCatalogue.StatutLigne.choices}

    if requested_status not in allowed_statuses:
        requested_status = ""

    if "statut" not in request.GET and catalogue.lignes.filter(
        statut_ligne=LigneCatalogue.StatutLigne.A_VERIFIER
    ).exists():
        requested_status = LigneCatalogue.StatutLigne.A_VERIFIER

    return {
        "q": request.GET.get("q", "").strip(),
        "statut": requested_status,
    }


def filter_catalogue_lines(queryset, filters):
    if filters["q"]:
        queryset = queryset.filter(
            Q(reference_produit__icontains=filters["q"])
            | Q(designation__icontains=filters["q"])
            | Q(ean__icontains=filters["q"])
        )
    if filters["statut"]:
        queryset = queryset.filter(statut_ligne=filters["statut"])
    return queryset.order_by("ordre", "pk")


def paginate_queryset(request, queryset, per_page=100):
    paginator = Paginator(queryset, per_page)
    return paginator.get_page(request.GET.get("page"))


def parse_iso_date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def parse_decimal(value):
    if value in (None, ""):
        return None
    try:
        cleaned_value = (
            str(value)
            .strip()
            .replace("\xa0", "")
            .replace(" ", "")
            .replace("'", "")
        )
        for currency_symbol in ("EUR", "CHF", "eur", "chf", "€", "%"):
            cleaned_value = cleaned_value.replace(currency_symbol, "")
        if "," in cleaned_value and "." in cleaned_value:
            if cleaned_value.rfind(",") > cleaned_value.rfind("."):
                cleaned_value = cleaned_value.replace(".", "").replace(",", ".")
            else:
                cleaned_value = cleaned_value.replace(",", "")
        elif "," in cleaned_value:
            cleaned_value = cleaned_value.replace(",", ".")
        return Decimal(cleaned_value)
    except (InvalidOperation, ValueError):
        return None


def document_file_exists(field_file):
    return bool(
        field_file
        and field_file.name
        and field_file.storage.exists(field_file.name)
    )


def secure_document_response(field_file, download=False):
    if not document_file_exists(field_file):
        return HttpResponseNotFound("Le fichier associé à ce document est introuvable.")

    filename = Path(field_file.name).name
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return FileResponse(
        field_file.storage.open(field_file.name, "rb"),
        as_attachment=download,
        filename=filename,
        content_type=content_type,
    )


def document_preview_type(field_file):
    if not document_file_exists(field_file):
        return ""

    extension = Path(field_file.name).suffix.lower()
    if extension == DOCUMENT_PDF_EXTENSION:
        return "pdf"
    if extension in DOCUMENT_IMAGE_EXTENSIONS:
        return "image"
    return ""


def delete_physical_file(storage, file_name, object_type, object_id):
    if not file_name:
        return

    try:
        if storage.exists(file_name):
            storage.delete(file_name)
        else:
            logger.warning(
                "Fichier introuvable lors de la suppression %s id=%s fichier=%s",
                object_type,
                object_id,
                file_name,
            )
    except Exception:
        logger.exception(
            "Suppression du fichier echouee pour %s id=%s fichier=%s",
            object_type,
            object_id,
            file_name,
        )


def log_admin_deletion(object_type, object_id, user, deletion_date):
    logger.info(
        "Suppression %s id=%s admin=%s date=%s",
        object_type,
        object_id,
        user.get_username(),
        timezone.localtime(deletion_date).isoformat(),
    )


def clean_ai_line_text(value, max_length):
    if value in (None, ""):
        return ""
    return str(value).strip()[:max_length]


def normalize_unit_key(value):
    return (
        str(value or "")
        .strip()
        .lower()
        .replace(".", "")
        .replace("é", "e")
        .replace("è", "e")
        .replace("ê", "e")
        .replace("à", "a")
    )


def normalize_delivery_unit(value):
    raw_unit = str(value or "").strip()
    if not raw_unit:
        return ""
    return DELIVERY_UNIT_ALIASES.get(normalize_unit_key(raw_unit), raw_unit)[:30]


def delivery_unit_requires_verification(value):
    raw_unit = str(value or "").strip()
    if not raw_unit:
        return False
    return normalize_delivery_unit(raw_unit) not in DELIVERY_STANDARD_UNITS


def ai_line_has_content(line):
    return any(
        line.get(field) not in (None, "")
        for field in (
            "reference_produit",
            "ean",
            "designation",
            "quantite",
            "unite",
            "prix_unitaire_ht",
            "montant_ht",
            "taux_tva",
            "montant_ttc",
        )
    )


def sync_invoice_lines_from_ai(facture, ai_lines):

    if not isinstance(ai_lines, list):
        return

    lines_to_create = []
    for index, line in enumerate(ai_lines):
        if not isinstance(line, dict) or not ai_line_has_content(line):
            continue
        lines_to_create.append(
            LigneFacture(
                facture=facture,
                reference_produit=clean_ai_line_text(line.get("reference_produit"), 100),
                ean=clean_ai_line_text(line.get("ean"), 50),
                designation=clean_ai_line_text(line.get("designation"), 255),
                quantite=parse_decimal(line.get("quantite")),
                unite=clean_ai_line_text(line.get("unite"), 30),
                prix_unitaire_ht=parse_decimal(line.get("prix_unitaire_ht")),
                montant_ht=parse_decimal(line.get("montant_ht")),
                taux_tva=parse_decimal(line.get("taux_tva")),
                montant_ttc=parse_decimal(line.get("montant_ttc")),
                source_page=clean_source_page(line.get("source_page")),
                ordre=index,
                corrigee_manuellement=False,
            )
        )

    from .services.extraction import sync_lines
    sync_lines(facture, lines_to_create)


def delivery_line_has_content(line):
    return any(
        line.get(field) not in (None, "")
        for field in (
            "product_reference",
            "product_name",
            "quantity",
            "unit",
            "packaging",
            "unit_price",
            "line_total",
            "raw_text",
        )
    )


def sync_delivery_note_lines_from_ai(bon_livraison, ai_lines):

    if not isinstance(ai_lines, list):
        return {"created": 0, "to_verify": False}

    next_order = -1
    incoming = []
    created_count = 0
    has_line_to_verify = False

    for line in ai_lines:
        if not isinstance(line, dict) or not delivery_line_has_content(line):
            continue
        next_order += 1
        unit = normalize_delivery_unit(line.get("unit"))
        delivery_line = LigneBonLivraison(
            bon_livraison=bon_livraison,
            reference_produit=clean_ai_line_text(line.get("product_reference"), 100),
            designation=clean_ai_line_text(line.get("product_name"), 255),
            quantite_livree=parse_decimal(line.get("quantity")),
            quantite_originale=clean_ai_line_text(line.get("quantity"), 100),
            unite=unit,
            conditionnement=clean_ai_line_text(line.get("packaging"), 100),
            prix_unitaire=parse_decimal(line.get("unit_price")),
            prix_unitaire_original=clean_ai_line_text(line.get("unit_price"), 100),
            total_ligne=parse_decimal(line.get("line_total")),
            total_ligne_original=clean_ai_line_text(line.get("line_total"), 100),
            confiance_ia=parse_decimal(line.get("confidence")),
            texte_brut=clean_ai_line_text(line.get("raw_text"), 1000),
            source_page=clean_source_page(line.get("source_page")),
            ordre=next_order,
            corrigee_manuellement=False,
        )
        if delivery_unit_requires_verification(line.get("unit")):
            delivery_line.statut_ligne = LigneBonLivraison.StatutLigne.A_VERIFIER
        try:
            delivery_line.full_clean()
            incoming.append(delivery_line)
        except ValidationError:
            logger.exception(
                "Ligne de bon de livraison IA ignoree apres validation: bon_livraison_id=%s ordre=%s",
                bon_livraison.pk,
                next_order,
            )
            continue
        created_count += 1
        if delivery_line.statut_ligne == LigneBonLivraison.StatutLigne.A_VERIFIER:
            has_line_to_verify = True

    from .services.extraction import sync_lines
    sync_lines(bon_livraison, incoming)
    return {"created": created_count, "to_verify": has_line_to_verify}


def delivery_note_status_from_ai(bon_livraison, ai_data, line_stats):
    if bon_livraison.statut == BonLivraison.Statut.LITIGE:
        return BonLivraison.Statut.LITIGE

    confidence = parse_decimal(ai_data.get("confiance_ia"))
    needs_verification = (
        not bon_livraison.numero_bon_livraison
        or not bon_livraison.date_livraison
        or bool(ai_data.get("champs_incertains"))
        or line_stats["created"] == 0
        or line_stats["to_verify"]
        or confidence is None
        or confidence < Decimal("0.75")
    )
    if needs_verification:
        return BonLivraison.Statut.A_VERIFIER
    return BonLivraison.Statut.IMPORTE


def sum_decimal_field(objects, field_name):
    total = Decimal("0")
    has_value = False
    for obj in objects:
        value = getattr(obj, field_name)
        if value is not None:
            total += value
            has_value = True
    return total if has_value else None


def invoice_lines_context(facture):
    lines = list(facture.lignes.all())
    total_ht = sum_decimal_field(lines, "montant_ht")
    total_ttc = sum_decimal_field(lines, "montant_ttc")
    total_differs = False

    if total_ttc is not None and facture.montant_ttc is not None:
        total_differs = abs(total_ttc - facture.montant_ttc) >= Decimal("0.01")

    return {
        "lignes_facture": lines,
        "lignes_total_ht": total_ht,
        "lignes_total_ttc": total_ttc,
        "lignes_total_differs": total_differs,
    }


def delivery_note_lines_context(bon_livraison):
    lines = list(bon_livraison.lignes.all())
    return {
        "lignes_bon_livraison": lines,
        "lignes_total_livraison": sum_decimal_field(lines, "total_ligne"),
    }


def normalized_devise(value):
    devise = str(value or "").strip().upper()
    if devise in {choice[0] for choice in Devise.choices}:
        return devise
    return ""


def invoice_ai_currency_context(facture):
    detected_currency = normalized_devise((facture.donnees_ia or {}).get("devise"))
    return {
        "devise_ia_detectee": detected_currency,
        "devise_ia_mismatch": bool(
            detected_currency and detected_currency != facture.fournisseur.devise_defaut
        ),
    }


def invoice_catalogue_control_context(facture):
    if facture.date_livraison_detectee:
        return {
            "catalogue_control_date": facture.date_livraison_detectee,
            "catalogue_control_date_source": "date de livraison détectée",
        }
    return {
        "catalogue_control_date": facture.date_facture,
        "catalogue_control_date_source": "date de facture",
    }


def catalogue_control_display_context(request, facture):
    catalogue_control = control_invoice_against_catalogue(facture)
    show_all_lines = request.GET.get("catalogue_all") == "1"
    displayed_details = (
        catalogue_control["details"]
        if show_all_lines
        else catalogue_control["problem_details"]
    )
    return {
        "catalogue_control": catalogue_control,
        "catalogue_control_show_all": show_all_lines,
        "catalogue_control_details": displayed_details,
    }


def invoice_line_exists_in_catalogue(catalogue, invoice_line):
    filters = Q()
    if invoice_line.reference_produit:
        filters |= Q(reference_produit=invoice_line.reference_produit)
    if invoice_line.ean:
        filters |= Q(ean=invoice_line.ean)
    return bool(filters and catalogue.lignes.filter(filters).exists())


def create_catalogue_line_from_invoice_line(catalogue, invoice_line):
    next_order = catalogue.lignes.aggregate(max_order=Max("ordre"))["max_order"]
    next_order = 0 if next_order is None else next_order + 1
    catalogue_line = LigneCatalogue(
        catalogue=catalogue,
        reference_produit=clean_catalogue_line_text(invoice_line.reference_produit, 100),
        ean=clean_catalogue_line_text(invoice_line.ean, 50),
        designation=clean_catalogue_line_text(invoice_line.designation, 255),
        unite=clean_catalogue_line_text(invoice_line.unite, 30),
        prix_unitaire_ht=invoice_line.prix_unitaire_ht,
        ordre=next_order,
        corrigee_manuellement=True,
    )
    catalogue_line.save()
    return catalogue_line


def clean_catalogue_line_text(value, max_length):
    if value in (None, ""):
        return ""
    return str(value).strip()[:max_length]


def sync_catalogue_lines_from_ai(catalogue, ai_lines):

    if not isinstance(ai_lines, list):
        return

    next_order = -1
    incoming = []

    for line in ai_lines:
        if not isinstance(line, dict):
            continue
        next_order += 1
        fresh = LigneCatalogue(
            catalogue=catalogue,
            reference_produit=clean_catalogue_line_text(line.get("reference_produit"), 100),
            ean=clean_catalogue_line_text(line.get("ean"), 50),
            designation=clean_catalogue_line_text(line.get("designation"), 255),
            marque=clean_catalogue_line_text(line.get("marque"), 100),
            unite=clean_catalogue_line_text(line.get("unite"), 30),
            conditionnement=clean_catalogue_line_text(line.get("conditionnement"), 100),
            prix_unitaire_ht=parse_decimal(line.get("prix_unitaire_ht")),
            source_page=clean_source_page(line.get("source_page")),
            ordre=next_order,
            corrigee_manuellement=False,
        )
        fresh.full_clean()
        incoming.append(fresh)

    from .services.extraction import sync_lines
    sync_lines(catalogue, incoming)

def apply_catalogue_ai_analysis(catalogue):
    ai_data = analyze_supplier_catalog(catalogue.fichier_original.path)
    with transaction.atomic():
        catalogue.donnees_ia = ai_data
        catalogue.confiance_ia = parse_decimal(ai_data.get("confiance_ia"))
        catalogue.erreur_analyse = ""
        catalogue.statut = CatalogueFournisseur.Statut.A_VALIDER

        if not catalogue.nom and ai_data.get("nom_catalogue"):
            catalogue.nom = str(ai_data["nom_catalogue"]).strip()[:255]
        detected_date_debut = parse_iso_date(ai_data.get("date_debut"))
        detected_date_fin = parse_iso_date(ai_data.get("date_fin"))
        if detected_date_debut and detected_date_fin and detected_date_fin < detected_date_debut:
            detected_date_fin = None
        if not catalogue.date_debut:
            catalogue.date_debut = detected_date_debut
        if not catalogue.date_fin:
            detected_start = catalogue.date_debut or detected_date_debut
            if not detected_start or not detected_date_fin or detected_date_fin >= detected_start:
                catalogue.date_fin = detected_date_fin

        detected_currency = normalized_devise(ai_data.get("devise"))
        if detected_currency and catalogue.devise == Devise.UNKNOWN:
            catalogue.devise = detected_currency

        catalogue.type_prix = CatalogueFournisseur.TypePrix.HT

        catalogue.save(
            update_fields=[
                "donnees_ia",
                "confiance_ia",
                "erreur_analyse",
                "statut",
                "nom",
                "date_debut",
                "date_fin",
                "devise",
                "type_prix",
                "date_modification",
            ]
        )
        sync_catalogue_lines_from_ai(catalogue, ai_data.get("lignes", []))
        from .services.extraction import record_version
        record_version(catalogue, ai_data)

    record_ai_success("analyse_catalogue", "catalogue", catalogue.pk, "Analyse catalogue réussie.")
    return catalogue


def mark_catalogue_ai_failed(catalogue, error):
    catalogue.statut = CatalogueFournisseur.Statut.ANALYSE_ECHOUEE
    catalogue.erreur_analyse = str(error)[:1000]
    catalogue.save(update_fields=["statut", "erreur_analyse", "date_modification"])
    logger.warning(
        "Analyse IA catalogue marquee en echec: catalogue_id=%s erreur=%s",
        catalogue.pk,
        type(error).__name__,
    )


def save_invoice_line_formset(formset, facture):
    for form in formset.deleted_forms:
        if form.instance.pk:
            form.instance.archived_at = timezone.now()
            form.instance.corrigee_manuellement = True
            form.instance.save()

    next_order = facture.lignes.aggregate(max_order=Max("ordre"))["max_order"]
    next_order = -1 if next_order is None else next_order

    for form in formset.forms:
        if form in formset.deleted_forms or not form.cleaned_data:
            continue
        if form.cleaned_data.get("DELETE"):
            continue
        if not form.has_changed() and not form.instance.pk:
            continue

        line = form.save(commit=False)
        line.facture = facture
        if line.pk is None:
            next_order += 1
            line.ordre = next_order
        if form.has_changed():
            line.corrigee_manuellement = True
        line.save()


def save_delivery_note_line_formset(formset, bon_livraison):
    for form in formset.deleted_forms:
        if form.instance.pk:
            form.instance.archived_at = timezone.now()
            form.instance.corrigee_manuellement = True
            form.instance.save()

    next_order = bon_livraison.lignes.aggregate(max_order=Max("ordre"))["max_order"]
    next_order = -1 if next_order is None else next_order

    for form in formset.forms:
        if form in formset.deleted_forms or not form.cleaned_data:
            continue
        if form.cleaned_data.get("DELETE"):
            continue
        if not form.has_changed() and not form.instance.pk:
            continue

        line = form.save(commit=False)
        line.bon_livraison = bon_livraison
        if line.pk is None:
            next_order += 1
            line.ordre = next_order
        if form.has_changed():
            line.corrigee_manuellement = True
        line.save()


def apply_invoice_ai_analysis(facture):
    ai_data = analyze_invoice(facture.fichier.path)
    with transaction.atomic():
        facture.donnees_ia = ai_data
        facture.confiance_ia = parse_decimal(ai_data.get("confiance_ia"))
        facture.erreur_analyse = ""
        if facture.statut != Facture.Statut.LITIGE:
            facture.statut = Facture.Statut.A_VALIDER

        if not facture.numero_facture:
            facture.numero_facture = ai_data.get("numero_facture", "")
        if not facture.date_facture:
            facture.date_facture = parse_iso_date(ai_data.get("date_facture"))
        if not facture.date_livraison_detectee:
            facture.date_livraison_detectee = parse_iso_date(ai_data.get("date_livraison_detectee"))
        detected_currency = normalized_devise(ai_data.get("devise"))
        if detected_currency and (
            not facture.devise or facture.devise == Devise.UNKNOWN
        ):
            facture.devise = detected_currency
        if facture.montant_ht is None:
            facture.montant_ht = parse_decimal(ai_data.get("montant_ht"))
        if facture.montant_tva is None:
            facture.montant_tva = parse_decimal(ai_data.get("montant_tva"))
        if facture.montant_ttc is None:
            facture.montant_ttc = parse_decimal(ai_data.get("montant_ttc"))

        facture.save(
            update_fields=[
                "donnees_ia",
                "confiance_ia",
                "erreur_analyse",
                "statut",
                "numero_facture",
                "date_facture",
                "date_livraison_detectee",
                "devise",
                "montant_ht",
                "montant_tva",
                "montant_ttc",
                "date_modification",
            ]
        )
        sync_invoice_lines_from_ai(facture, ai_data.get("lignes", []))
        from .services.extraction import record_version
        record_version(facture, ai_data)

    record_ai_success("analyse_facture", "facture", facture.pk, "Analyse facture réussie.")
    return facture


def mark_invoice_ai_failed(facture, error):
    facture.statut = Facture.Statut.ANALYSE_ECHOUEE
    facture.erreur_analyse = str(error)[:1000]
    facture.save(update_fields=["statut", "erreur_analyse", "date_modification"])
    logger.warning(
        "Analyse IA facture marquee en echec: facture_id=%s erreur=%s",
        facture.pk,
        type(error).__name__,
    )


def apply_delivery_note_ai_analysis(bon_livraison):
    logger.info(
        "Analyse IA bon de livraison demarree: bon_livraison_id=%s fichier=%s",
        bon_livraison.pk,
        bon_livraison.fichier.name,
    )
    if bon_livraison.statut != BonLivraison.Statut.LITIGE:
        bon_livraison.statut = BonLivraison.Statut.ANALYSE_EN_COURS
    bon_livraison.erreur_analyse = ""
    bon_livraison.save(update_fields=["statut", "erreur_analyse", "date_modification"])

    ai_data = analyze_delivery_note(bon_livraison.fichier.path)
    with transaction.atomic():
        bon_livraison.donnees_ia = ai_data
        bon_livraison.confiance_ia = parse_decimal(ai_data.get("confiance_ia"))
        bon_livraison.erreur_analyse = ""

        if not bon_livraison.numero_bon_livraison:
            bon_livraison.numero_bon_livraison = ai_data.get("numero_bl") or ""
        if not bon_livraison.date_livraison:
            bon_livraison.date_livraison = parse_iso_date(ai_data.get("date_livraison"))

        line_stats = sync_delivery_note_lines_from_ai(
            bon_livraison,
            ai_data.get("lines", ai_data.get("lignes", [])),
        )
        from .services.extraction import record_version
        record_version(bon_livraison, ai_data)
        bon_livraison.statut = delivery_note_status_from_ai(bon_livraison, ai_data, line_stats)
        bon_livraison.save(
            update_fields=[
                "donnees_ia",
                "confiance_ia",
                "statut",
                "numero_bon_livraison",
                "date_livraison",
                "erreur_analyse",
                "date_modification",
            ]
        )

    logger.info(
        "Analyse IA bon de livraison terminee: bon_livraison_id=%s statut=%s lignes=%s",
        bon_livraison.pk,
        bon_livraison.statut,
        bon_livraison.lignes.count(),
    )
    record_ai_success(
        "analyse_bon_livraison",
        "bon_livraison",
        bon_livraison.pk,
        "Analyse bon de livraison réussie.",
    )
    return bon_livraison


def mark_delivery_note_ai_failed(bon_livraison, error):
    bon_livraison.statut = BonLivraison.Statut.ANALYSE_ECHOUEE
    bon_livraison.erreur_analyse = str(error)[:1000]
    bon_livraison.save(update_fields=["statut", "erreur_analyse", "date_modification"])
    logger.warning(
        "Analyse IA bon de livraison marquee en echec: bon_livraison_id=%s erreur=%s",
        bon_livraison.pk,
        type(error).__name__,
    )


def enqueue_ai_analysis(document_type, document_id):
    from .services.jobs import enqueue
    return enqueue(document_type, document_id)


def run_background_ai_analysis(document_type, document_id, manage_connections=False):
    if manage_connections:
        close_old_connections()
    try:
        event_document_type = "bon_livraison" if document_type == "reception_bon_livraison" else document_type
        try:
            document, runner, failure_marker, source = background_ai_analysis_context(
                document_type,
                document_id,
            )
        except ObjectDoesNotExist:
            logger.warning(
                "Analyse IA en arriere-plan ignoree: document introuvable type=%s id=%s",
                document_type,
                document_id,
            )
            return

        try:
            runner(document)
        except DocumentAIConfigurationError as exc:
            record_ai_error(source, exc, event_document_type, document_id)
            failure_marker(document, exc)
        except DocumentAIError as exc:
            record_ai_error(source, exc, event_document_type, document_id)
            failure_marker(document, exc)
            logger.exception(
                "Analyse IA en arriere-plan echouee: type=%s id=%s",
                document_type,
                document_id,
            )
        except Exception as exc:
            record_ai_error(source, exc, event_document_type, document_id)
            failure_marker(document, exc)
            logger.exception(
                "Erreur inattendue pendant l'analyse IA en arriere-plan: type=%s id=%s",
                document_type,
                document_id,
            )
    finally:
        if manage_connections:
            close_old_connections()


def background_ai_analysis_context(document_type, document_id):
    if document_type == "facture":
        return (
            Facture.objects.get(pk=document_id),
            apply_invoice_ai_analysis,
            mark_invoice_ai_failed,
            "upload_facture",
        )
    if document_type == "bon_livraison":
        return (
            BonLivraison.objects.get(pk=document_id),
            apply_delivery_note_ai_analysis,
            mark_delivery_note_ai_failed,
            "upload_bon_livraison",
        )
    if document_type == "reception_bon_livraison":
        return (
            BonLivraison.objects.get(pk=document_id),
            apply_delivery_note_ai_analysis,
            mark_delivery_note_ai_failed,
            "reception_bon_livraison",
        )
    if document_type == "catalogue":
        return (
            CatalogueFournisseur.objects.get(pk=document_id),
            apply_catalogue_ai_analysis,
            mark_catalogue_ai_failed,
            "upload_catalogue",
        )
    raise ValueError(f"Type de document inconnu: {document_type}")


@role_required(ROLE_GESTIONNAIRE, ROLE_ADMIN)
def dashboard(request):
    search_query = request.GET.get("q", "").strip()
    current_tab = request.GET.get("tab", "all")
    current_sort = request.GET.get("sort", "name")
    allowed_tabs = {"all", "attention", "litiges", "factures", "bons"}
    allowed_sorts = {"name", "restaurant", "attention"}
    if current_tab not in allowed_tabs:
        current_tab = "all"
    if current_sort not in allowed_sorts:
        current_sort = "name"

    fournisseurs_base = fournisseurs_with_document_counts(search_query=search_query)
    dashboard_counts = {
        "all": fournisseurs_base.count(),
        "attention": fournisseurs_base.filter(documents_a_valider_count__gt=0).count(),
        "litiges": fournisseurs_base.filter(litiges_count__gt=0).count(),
        "factures": fournisseurs_base.filter(factures_count__gt=0).count(),
        "bons": fournisseurs_base.filter(bons_livraison_count__gt=0).count(),
    }

    fournisseurs = fournisseurs_base
    if current_tab == "attention":
        fournisseurs = fournisseurs.filter(documents_a_valider_count__gt=0)
    elif current_tab == "litiges":
        fournisseurs = fournisseurs.filter(litiges_count__gt=0)
    elif current_tab == "factures":
        fournisseurs = fournisseurs.filter(factures_count__gt=0)
    elif current_tab == "bons":
        fournisseurs = fournisseurs.filter(bons_livraison_count__gt=0)

    if current_sort == "restaurant":
        fournisseurs = fournisseurs.order_by("restaurant__nom", "nom")
    elif current_sort == "attention":
        fournisseurs = fournisseurs.order_by("-litiges_count", "-documents_a_valider_count", "nom")
    else:
        fournisseurs = fournisseurs.order_by("nom")

    from .models import SupplierClaim, AnalysisJob
    work_counts = {
        'data': Facture.objects.exclude(statut__in=['validee','litige']).count() + BonLivraison.objects.exclude(statut__in=['valide','litige']).count() + CatalogueFournisseur.objects.exclude(statut='valide').count(),
        'failed': Facture.objects.filter(statut='analyse_echouee').count() + BonLivraison.objects.filter(statut='analyse_echouee').count() + CatalogueFournisseur.objects.filter(statut='analyse_echouee').count(),
        'claims': SupplierClaim.objects.exclude(state__in=['closed', 'cancelled']).count(),
        'running': Facture.objects.filter(statut='analyse_en_cours').count() + BonLivraison.objects.filter(statut='analyse_en_cours').count() + CatalogueFournisseur.objects.filter(statut='analyse_en_cours').count(),
    }
    from .services.home_summary import home_summary
    return render(
        request,
        "core/dashboard.html",
        {
            "fournisseurs": fournisseurs,
            "search_query": search_query,
            "current_tab": current_tab,
            "current_sort": current_sort,
            "dashboard_counts": dashboard_counts,
            "work_counts": work_counts,
            "home_counts": home_summary(),
        },
    )


@role_required(ROLE_ADMIN)
def user_list(request):
    user_model = get_user_model()
    users = list(user_model.objects.prefetch_related("groups").order_by("username"))
    for managed_user in users:
        managed_user.app_role = get_user_app_role(managed_user) or "Aucun rôle"
    return render(request, "core/user_list.html", {"users": users})


@role_required(ROLE_ADMIN)
@require_http_methods(["GET", "POST"])
def ai_diagnostic(request):
    from .operations_forms import OpenAISettingsForm
    from .services.ai_settings import local_config_editable, save_local_openai_settings
    config_status=openai_configuration_status()
    editable=local_config_editable()
    initial_model=config_status['model'] if config_status['model'] != 'Non défini' else 'gpt-4.1-mini'
    form=OpenAISettingsForm(request.POST if request.method=='POST' and request.POST.get('action')=='configure' else None,initial={'model':initial_model})
    if request.method == 'POST':
        if request.POST.get('action')=='configure':
            if not editable:
                raise PermissionDenied
            if form.is_valid():
                try:
                    save_local_openai_settings(form.cleaned_data['api_key'],form.cleaned_data['model'])
                except ValidationError as exc:
                    form.add_error(None,exc)
                else:
                    messages.success(request,'Configuration enregistrée. Testez la connexion avant de relancer les analyses échouées.')
                    return redirect('ai_diagnostic')
        else:
            try:
                test_openai_connection()
                record_ai_success('test_openai',message='Test de connexion OpenAI réussi.')
                messages.success(request,'Connexion OpenAI OK')
            except DocumentAIError as exc:
                record_ai_error('test_openai',exc)
                logger.exception('Test de connexion OpenAI echoue')
                messages.warning(request,'Connexion OpenAI échouée. Vérifiez la configuration, la validité de la clé et l’accès au modèle.')
            return redirect('ai_diagnostic')
    return render(request,'core/ai_diagnostic.html',{
        'api_key_configured':config_status['api_key_configured'],'openai_model':config_status['model'],
        'config_source':config_status['source'],'configured':config_status['configured'],
        'config_form':form,'config_editable':editable,
    })


@role_required(ROLE_ADMIN)
@require_http_methods(["GET", "POST"])
def user_create(request):
    form = UserCreateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Utilisateur créé avec succès.")
        return redirect("user_list")
    return render(
        request,
        "core/user_form.html",
        {
            "form": form,
            "title": "Ajouter un utilisateur",
            "submit_label": "Creer",
        },
    )


@role_required(ROLE_ADMIN)
@require_http_methods(["GET", "POST"])
def user_update(request, pk):
    user_model = get_user_model()
    managed_user = get_object_or_404(user_model, pk=pk)
    form = UserUpdateForm(request.POST or None, instance=managed_user)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Utilisateur modifié avec succès.")
        return redirect("user_list")
    return render(
        request,
        "core/user_form.html",
        {
            "form": form,
            "target_user": managed_user,
            "title": "Modifier un utilisateur",
            "submit_label": "Sauvegarder",
        },
    )


@role_required(ROLE_ADMIN)
@require_http_methods(["GET", "POST"])
def user_password_update(request, pk):
    user_model = get_user_model()
    managed_user = get_object_or_404(user_model, pk=pk)
    form = UserPasswordForm(request.POST or None, user=managed_user)
    if request.method == "POST" and form.is_valid():
        form.save(managed_user)
        messages.success(request, "Mot de passe mis à jour.")
        return redirect("user_list")
    return render(
        request,
        "core/user_password_form.html",
        {
            "form": form,
            "target_user": managed_user,
        },
    )


@role_required(ROLE_GESTIONNAIRE, ROLE_ADMIN)
def restaurant_list(request):
    restaurants = (
        Restaurant.objects.filter(actif=True)
        .annotate(
            fournisseurs_actifs_count=Count(
                "fournisseurs",
                filter=Q(fournisseurs__actif=True),
                distinct=True,
            )
        )
        .order_by("nom")
    )
    return render(request, "core/restaurant_list.html", {"restaurants": restaurants})


@role_required(ROLE_GESTIONNAIRE, ROLE_ADMIN)
@require_http_methods(["GET", "POST"])
def restaurant_create(request):
    form = RestaurantForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        restaurant = form.save()
        from .models import RestaurantAccess
        RestaurantAccess.objects.get_or_create(user=request.user,restaurant=restaurant)
        messages.success(request, "Restaurant créé avec succès.")
        return redirect("restaurant_detail", pk=restaurant.pk)
    return render(
        request,
        "core/restaurant_form.html",
        {
            "form": form,
            "title": "Ajouter un restaurant",
            "submit_label": "Creer",
        },
    )


@role_required(ROLE_GESTIONNAIRE, ROLE_ADMIN)
def restaurant_detail(request, pk):
    restaurant = get_object_or_404(
        Restaurant.objects.annotate(
            fournisseurs_actifs_count=Count(
                "fournisseurs",
                filter=Q(fournisseurs__actif=True),
                distinct=True,
            )
        ),
        pk=pk,
    )
    fournisseurs = restaurant.fournisseurs.filter(actif=True).order_by("nom")
    return render(
        request,
        "core/restaurant_detail.html",
        {
            "restaurant": restaurant,
            "fournisseurs": fournisseurs,
        },
    )


@role_required(ROLE_GESTIONNAIRE, ROLE_ADMIN)
@require_http_methods(["GET", "POST"])
def restaurant_update(request, pk):
    restaurant = get_object_or_404(Restaurant, pk=pk)
    previous_currency = restaurant.devise_defaut
    form = RestaurantForm(request.POST or None, instance=restaurant)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            restaurant = form.save()
            cascade_counts = cascade_restaurant_currency(restaurant, previous_currency)
        if cascade_counts["factures"] or cascade_counts["fournisseurs"] or cascade_counts["catalogues"]:
            messages.success(
                request,
                (
                    "Restaurant modifié avec succès. "
                    "La devise par défaut concerne les prochains imports."
                ),
            )
        else:
            messages.success(request, "Restaurant modifié avec succès. La devise des documents existants est conservée.")
        return redirect("restaurant_detail", pk=restaurant.pk)
    return render(
        request,
        "core/restaurant_form.html",
        {
            "form": form,
            "restaurant": restaurant,
            "title": "Modifier le restaurant",
            "submit_label": "Sauvegarder",
        },
    )


@role_required(ROLE_GESTIONNAIRE, ROLE_ADMIN)
def fournisseur_list(request):
    fournisseurs = fournisseurs_with_document_counts()
    return render(request, "core/fournisseur_list.html", {"fournisseurs": fournisseurs})


@role_required(ROLE_GESTIONNAIRE, ROLE_ADMIN)
@require_http_methods(["GET", "POST"])
def fournisseur_create(request):
    initial = {}
    restaurant_id = request.GET.get("restaurant")
    if restaurant_id:
        restaurant = Restaurant.objects.filter(pk=restaurant_id, actif=True).first()
        if restaurant:
            initial["restaurant"] = restaurant
    form = FournisseurForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        fournisseur = form.save()
        messages.success(request, "Fournisseur créé avec succès.")
        return redirect("fournisseur_detail", pk=fournisseur.pk)
    return render(
        request,
        "core/fournisseur_form.html",
        {
            "form": form,
            "title": "Ajouter un fournisseur",
            "submit_label": "Creer",
        },
    )


@role_required(ROLE_GESTIONNAIRE, ROLE_ADMIN)
def fournisseur_detail(request, pk):
    fournisseur = get_object_or_404(
        fournisseurs_with_document_counts(active_only=False),
        pk=pk,
    )
    facture_filters = get_document_filters(request, "facture", Facture.Statut.choices)
    bon_filters = get_document_filters(request, "bon", BonLivraison.Statut.choices)
    factures = filter_factures(fournisseur.factures.all(), facture_filters)
    bons_livraison = filter_bons_livraison(
        fournisseur.bons_livraison.annotate(lignes_count=Count("lignes", distinct=True)),
        bon_filters,
    )
    catalogues_queryset = fournisseur.catalogues.all()
    catalogue_actuel = current_valid_catalogue(catalogues_queryset)
    catalogues_count = catalogues_queryset.count()
    catalogues_recents = catalogues_with_line_counts(catalogues_queryset).order_by("-date_import")[:5]
    has_pending_documents = (
        factures.filter(statut=Facture.Statut.ANALYSE_EN_COURS).exists()
        or bons_livraison.filter(statut=BonLivraison.Statut.ANALYSE_EN_COURS).exists()
        or catalogues_queryset.filter(statut=CatalogueFournisseur.Statut.ANALYSE_EN_COURS).exists()
    )
    from .services.operations import catalogue_health
    facture_page = Paginator(factures, 25).get_page(request.GET.get('facture_page'))
    bon_page = Paginator(bons_livraison, 25).get_page(request.GET.get('bon_page'))
    return render(
        request,
        "core/fournisseur_detail.html",
        {
            "fournisseur": fournisseur,
            "factures": facture_page,
            "bons_livraison": bon_page,
            "catalogue_health": catalogue_health(fournisseur),
            "facture_page": facture_page, "bon_page": bon_page,
            "catalogue_actuel": catalogue_actuel,
            "catalogues_recents": catalogues_recents,
            "factures_count": factures.count(),
            "bons_livraison_count": bons_livraison.count(),
            "catalogues_count": catalogues_count,
            "anciens_catalogues_count": max(catalogues_count - (1 if catalogue_actuel else 0), 0),
            "has_pending_documents": has_pending_documents,
            "facture_filters": facture_filters,
            "bon_filters": bon_filters,
            "facture_filters_active": any(facture_filters.values()),
            "bon_filters_active": any(bon_filters.values()),
            "facture_statut_choices": Facture.Statut.choices,
            "bon_statut_choices": BonLivraison.Statut.choices,
        },
    )


@role_required(ROLE_GESTIONNAIRE, ROLE_ADMIN)
@require_http_methods(["GET", "POST"])
def fournisseur_update(request, pk):
    fournisseur = get_object_or_404(
        Fournisseur.objects.select_related("restaurant").filter(restaurant__actif=True),
        pk=pk,
    )
    form = FournisseurForm(request.POST or None, instance=fournisseur)
    if request.method == "POST" and form.is_valid():
        fournisseur = form.save()
        messages.success(request, "Fournisseur modifié avec succès.")
        return redirect("fournisseur_detail", pk=fournisseur.pk)
    return render(
        request,
        "core/fournisseur_form.html",
        {
            "form": form,
            "fournisseur": fournisseur,
            "title": "Modifier le fournisseur",
            "submit_label": "Sauvegarder",
        },
    )


@role_required(ROLE_GESTIONNAIRE, ROLE_ADMIN)
@require_http_methods(["GET", "POST"])
def facture_detail(request, pk):
    facture = get_object_or_404(
        Facture.objects.select_related("fournisseur", "fournisseur__restaurant").prefetch_related("lignes"),
        pk=pk,
    )
    pending_redirect = redirect_if_analysis_pending(request, facture)
    if pending_redirect:
        return pending_redirect

    form = FactureForm(
        detail_form_data_with_quick_status(request, Facture.Statut.choices),
        instance=facture,
    )
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Facture sauvegardée.")
        return redirect("facture_detail", pk=facture.pk)
    return render(
        request,
        "core/facture_detail.html",
        {
            "facture": facture,
            **document_workflow_context(facture, "facture"),
            "form": form,
            "file_exists": document_file_exists(facture.fichier),
            "document_preview_type": document_preview_type(facture.fichier),
            "document_file_url": reverse("facture_file", args=[facture.pk]),
            "document_download_url": f"{reverse('facture_file', args=[facture.pk])}?download=1",
            "status_badge_class": document_status_badge_class(facture.statut),
            **invoice_lines_context(facture),
            **invoice_ai_currency_context(facture),
            **invoice_catalogue_control_context(facture),
            **catalogue_control_display_context(request, facture),
        },
    )


@role_required(ROLE_GESTIONNAIRE, ROLE_ADMIN)
@require_http_methods(["GET", "POST"])
@transaction.atomic
def facture_lignes_update(request, pk):
    if request.method == "POST":
        supplier_id = get_object_or_404(Facture, pk=pk).fournisseur_id
        Fournisseur.objects.select_for_update().get(pk=supplier_id)
    facture = get_object_or_404(
        Facture.objects.select_related("fournisseur", "fournisseur__restaurant").prefetch_related("lignes"),
        pk=pk,
    )
    formset = LigneFactureFormSet(
        request.POST or None,
        instance=facture,
        queryset=facture.lignes.all().order_by("ordre", "pk"),
    )
    if request.method == "POST" and formset.is_valid():
        save_invoice_line_formset(formset, facture)
        messages.success(request, "Lignes de facture sauvegardées.")
        return redirect("facture_detail", pk=facture.pk)

    return render(
        request,
        "core/facture_lignes_form.html",
        {
            "facture": facture,
            "preview_is_image": Path(facture.fichier.name).suffix.lower() in {".png", ".jpg", ".jpeg"},
            "formset": formset,
        },
    )


@role_required(ROLE_GESTIONNAIRE, ROLE_ADMIN)
@require_http_methods(["POST"])
@transaction.atomic
def facture_line_add_to_catalogue(request, facture_pk, pk):
    if request.method == "POST":
        supplier_id = get_object_or_404(Facture, pk=facture_pk).fournisseur_id
        Fournisseur.objects.select_for_update().get(pk=supplier_id)
    facture = get_object_or_404(
        Facture.objects.select_related("fournisseur", "fournisseur__restaurant"),
        pk=facture_pk,
    )
    invoice_line = get_object_or_404(LigneFacture, pk=pk, facture=facture)
    catalogue_control = control_invoice_against_catalogue(facture)
    catalogue = catalogue_control.get("catalogue")
    redirect_url = f"{reverse('facture_detail', args=[facture.pk])}#controle-catalogue"

    if not catalogue_control.get("control_available") or catalogue is None:
        messages.warning(request, "Aucun catalogue valide disponible pour ajouter ce produit.")
        return redirect(redirect_url)

    if invoice_line_exists_in_catalogue(catalogue, invoice_line):
        messages.info(request, "Ce produit existe déjà dans le catalogue.")
        return redirect(redirect_url)

    catalogue_line = create_catalogue_line_from_invoice_line(catalogue, invoice_line)
    record_price(catalogue_line, request.user, None)
    messages.success(
        request,
        f"Produit ajouté au catalogue : {catalogue_line.designation or catalogue_line.reference_produit or 'ligne à vérifier'}.",
    )
    return redirect(redirect_url)


@role_required(ROLE_GESTIONNAIRE, ROLE_ADMIN)
@xframe_options_sameorigin
def facture_file(request, pk):
    facture = get_object_or_404(Facture, pk=pk)
    return secure_document_response(
        facture.fichier,
        download=request.GET.get("download") == "1",
    )


@role_required(ROLE_GESTIONNAIRE, ROLE_ADMIN)
@require_http_methods(["POST"])
def facture_ai_reanalyze(request, pk):
    document = get_object_or_404(Facture, pk=pk)
    enqueue_ai_analysis("facture", document.pk)
    messages.info(request, "Analyse programmée. Les corrections existantes sont conservées ; les différences seront à confirmer.")
    return redirect("facture_detail", pk=pk)


@role_required(ROLE_ADMIN)
@require_http_methods(["GET", "POST"])
def facture_delete(request, pk):
    facture = get_object_or_404(
        Facture.objects.select_related("fournisseur", "fournisseur__restaurant"),
        pk=pk,
    )
    fournisseur = facture.fournisseur
    if request.method == "POST":
        facture_id = facture.pk
        file_storage = facture.fichier.storage
        file_name = facture.fichier.name
        deletion_date = timezone.now()

        with transaction.atomic():
            from .services.operations import archive_document
            archive_document(facture)


        log_admin_deletion("facture", facture_id, request.user, deletion_date)
        messages.success(request, "Facture archivée. Le fichier et l’historique sont conservés.")
        return redirect("fournisseur_detail", pk=fournisseur.pk)

    summary_items = [
        ("Fournisseur", fournisseur.nom),
        ("Numéro de facture", facture.numero_facture or "Non renseigné"),
        ("Date", facture.date_facture.strftime("%d/%m/%Y") if facture.date_facture else "Non renseignée"),
        (
            "Montant TTC",
            f"{facture.montant_ttc} {facture.devise}" if facture.montant_ttc is not None else "Non renseigné",
        ),
    ]
    return render(
        request,
        "core/delete_confirm.html",
        {
            "title": "Supprimer la facture",
            "object_label": "facture",
            "object_name": facture.numero_facture or f"Facture {facture.pk}",
            "summary_items": summary_items,
            "cancel_url": reverse("facture_detail", args=[facture.pk]),
            "confirm_label": "Archiver et conserver le fichier",
        },
    )


@role_required(ROLE_GESTIONNAIRE, ROLE_ADMIN)
@require_http_methods(["GET", "POST"])
def bon_livraison_detail(request, pk):
    bon_livraison = get_object_or_404(
        BonLivraison.objects.select_related("fournisseur", "fournisseur__restaurant").prefetch_related("lignes"),
        pk=pk,
    )
    pending_redirect = redirect_if_analysis_pending(request, bon_livraison)
    if pending_redirect:
        return pending_redirect

    form = BonLivraisonForm(
        detail_form_data_with_quick_status(request, BonLivraison.Statut.choices),
        instance=bon_livraison,
    )
    if request.method == "POST" and form.is_valid():
        bon_livraison = form.save()
        messages.success(request, "Bon de livraison sauvegardé.")
        return redirect("bon_livraison_detail", pk=bon_livraison.pk)
    return render(
        request,
        "core/bon_livraison_detail.html",
        {
            "bon_livraison": bon_livraison,
            **document_workflow_context(bon_livraison, "bon_livraison"),
            "form": form,
            "file_exists": document_file_exists(bon_livraison.fichier),
            "document_preview_type": document_preview_type(bon_livraison.fichier),
            "document_file_url": reverse("bon_livraison_file", args=[bon_livraison.pk]),
            "document_download_url": (
                f"{reverse('bon_livraison_file', args=[bon_livraison.pk])}?download=1"
            ),
            "status_badge_class": document_status_badge_class(bon_livraison.statut),
            "openai_status": openai_configuration_status(),
            "delivery_lines_count": bon_livraison.lignes.count(),
            **delivery_note_lines_context(bon_livraison),
        },
    )


@role_required(ROLE_GESTIONNAIRE, ROLE_ADMIN)
@require_http_methods(["GET", "POST"])
def bon_livraison_lignes_update(request, pk):
    bon_livraison = get_object_or_404(
        BonLivraison.objects.select_related("fournisseur", "fournisseur__restaurant").prefetch_related("lignes"),
        pk=pk,
    )
    formset = LigneBonLivraisonFormSet(
        request.POST or None,
        instance=bon_livraison,
        queryset=bon_livraison.lignes.all().order_by("ordre", "pk"),
    )
    if request.method == "POST" and formset.is_valid():
        save_delivery_note_line_formset(formset, bon_livraison)
        messages.success(request, "Lignes du bon de livraison sauvegardées.")
        return redirect("bon_livraison_detail", pk=bon_livraison.pk)

    return render(
        request,
        "core/bon_livraison_lignes_form.html",
        {
            "bon_livraison": bon_livraison,
            "preview_is_image": Path(bon_livraison.fichier.name).suffix.lower() in {".png", ".jpg", ".jpeg"},
            "formset": formset,
        },
    )


@role_required(ROLE_GESTIONNAIRE, ROLE_ADMIN)
@xframe_options_sameorigin
def bon_livraison_file(request, pk):
    bon_livraison = get_object_or_404(BonLivraison, pk=pk)
    return secure_document_response(
        bon_livraison.fichier,
        download=request.GET.get("download") == "1",
    )


@role_required(ROLE_GESTIONNAIRE, ROLE_ADMIN)
@require_http_methods(["POST"])
def bon_livraison_ai_reanalyze(request, pk):
    document = get_object_or_404(BonLivraison, pk=pk)
    enqueue_ai_analysis("bon_livraison", document.pk)
    messages.info(request, "Analyse programmée. Les corrections existantes sont conservées ; les différences seront à confirmer.")
    return redirect("bon_livraison_detail", pk=pk)


@role_required(ROLE_ADMIN)
@require_http_methods(["GET", "POST"])
def bon_livraison_delete(request, pk):
    bon_livraison = get_object_or_404(
        BonLivraison.objects.select_related("fournisseur", "fournisseur__restaurant"),
        pk=pk,
    )
    fournisseur = bon_livraison.fournisseur
    if request.method == "POST":
        bon_livraison_id = bon_livraison.pk
        file_storage = bon_livraison.fichier.storage
        file_name = bon_livraison.fichier.name
        deletion_date = timezone.now()

        with transaction.atomic():
            from .services.operations import archive_document
            archive_document(bon_livraison)


        log_admin_deletion("bon_livraison", bon_livraison_id, request.user, deletion_date)
        messages.success(request, "Bon de livraison archivé. Le fichier est conservé.")
        return redirect("fournisseur_detail", pk=fournisseur.pk)

    summary_items = [
        ("Fournisseur", fournisseur.nom),
        ("Numéro de BL", bon_livraison.numero_bon_livraison or "Non renseigné"),
        (
            "Date",
            bon_livraison.date_livraison.strftime("%d/%m/%Y")
            if bon_livraison.date_livraison
            else "Non renseignée",
        ),
    ]
    return render(
        request,
        "core/delete_confirm.html",
        {
            "title": "Supprimer le bon de livraison",
            "object_label": "bon de livraison",
            "object_name": bon_livraison.numero_bon_livraison or f"Bon de livraison {bon_livraison.pk}",
            "summary_items": summary_items,
            "cancel_url": reverse("bon_livraison_detail", args=[bon_livraison.pk]),
            "confirm_label": "Archiver et conserver le fichier",
        },
    )


@role_required(ROLE_GESTIONNAIRE, ROLE_ADMIN)
def facture_add(request):
    from .operations_views import deposit
    return deposit(request)


@role_required(ROLE_GESTIONNAIRE, ROLE_ADMIN)
@require_http_methods(["GET", "POST"])
def fournisseur_facture_add(request, pk):
    fournisseur = get_object_or_404(
        Fournisseur.objects.select_related("restaurant").filter(actif=True, restaurant__actif=True),
        pk=pk,
    )
    form = FactureUploadForm(request.POST or None, request.FILES or None, fournisseur=fournisseur)
    if request.method == "POST" and form.is_valid():
        facture = form.save(fournisseur=fournisseur)
        enqueue_ai_analysis("facture", facture.pk)
        messages.info(request, DOCUMENT_AI_IMPORT_STARTED_MESSAGE)
        return redirect("fournisseur_detail", pk=fournisseur.pk)
    return render(
        request,
        "core/facture_add.html",
        {"form": form, "fournisseur": fournisseur},
    )


@role_required(ROLE_GESTIONNAIRE, ROLE_ADMIN)
@require_http_methods(["GET", "POST"])
def fournisseur_catalogue_import(request, pk):
    fournisseur = get_object_or_404(
        Fournisseur.objects.select_related("restaurant").filter(actif=True, restaurant__actif=True),
        pk=pk,
    )
    form = CatalogueUploadForm(request.POST or None, request.FILES or None, fournisseur=fournisseur)
    if request.method == "POST" and form.is_valid():
        catalogue = form.save(fournisseur=fournisseur)
        enqueue_ai_analysis("catalogue", catalogue.pk)
        messages.info(request, CATALOGUE_AI_IMPORT_STARTED_MESSAGE)
        return redirect("fournisseur_detail", pk=fournisseur.pk)
    return render(
        request,
        "core/catalogue_import.html",
        {"form": form, "fournisseur": fournisseur},
    )


@role_required(ROLE_GESTIONNAIRE, ROLE_ADMIN)
@require_http_methods(["GET"])
def catalogue_detail(request, pk):
    catalogue = get_object_or_404(
        catalogues_with_line_counts(
            CatalogueFournisseur.objects.all()
        )
        .select_related("fournisseur", "fournisseur__restaurant")
        .prefetch_related("lignes"),
        pk=pk,
    )
    if catalogue.origine == 'documents':
        return redirect('supplier_catalogue',pk=catalogue.fournisseur_id)
    pending_redirect = redirect_if_analysis_pending(request, catalogue)
    if pending_redirect:
        return pending_redirect

    filters = catalogue_line_filters(request, catalogue)
    lignes = filter_catalogue_lines(catalogue.lignes.all(), filters)
    lignes_page = paginate_queryset(request, lignes)
    return render(
        request,
        "core/catalogue_detail.html",
        {
            "catalogue": catalogue,
            **document_workflow_context(catalogue, "catalogue"),
            "lignes_page": lignes_page,
            "line_filters": filters,
            "line_filters_active": bool(filters["q"] or filters["statut"]),
            "statut_ligne_choices": LigneCatalogue.StatutLigne.choices,
            "file_exists": document_file_exists(catalogue.fichier_original),
            "document_preview_type": document_preview_type(catalogue.fichier_original),
            "document_file_url": reverse("catalogue_file", args=[catalogue.pk]),
            "document_download_url": f"{reverse('catalogue_file', args=[catalogue.pk])}?download=1",
            "status_badge_class": document_status_badge_class(catalogue.statut),
        },
    )


@role_required(ROLE_GESTIONNAIRE, ROLE_ADMIN)
@xframe_options_sameorigin
def catalogue_file(request, pk):
    catalogue = get_object_or_404(CatalogueFournisseur, pk=pk)
    return secure_document_response(
        catalogue.fichier_original,
        download=request.GET.get("download") == "1",
    )


def validation_error_messages(error):
    if hasattr(error, "message_dict"):
        messages_list = []
        for field_errors in error.message_dict.values():
            messages_list.extend(field_errors)
        return messages_list
    return error.messages


@role_required(ROLE_GESTIONNAIRE, ROLE_ADMIN)
def fournisseur_catalogue_list(request, pk):
    fournisseur = get_object_or_404(
        Fournisseur.objects.select_related("restaurant"),
        pk=pk,
    )
    catalogues = catalogues_with_line_counts(
        fournisseur.catalogues.all().order_by("-date_debut", "-date_import")
    )
    has_pending_documents = catalogues.filter(statut=CatalogueFournisseur.Statut.ANALYSE_EN_COURS).exists()
    return render(
        request,
        "core/catalogue_list.html",
        {
            "fournisseur": fournisseur,
            "catalogues": catalogues,
            "has_pending_documents": has_pending_documents,
        },
    )


@role_required(ROLE_GESTIONNAIRE, ROLE_ADMIN)
@require_http_methods(["GET", "POST"])
@transaction.atomic
def catalogue_update(request, pk):
    if request.method == "POST":
        supplier_id = get_object_or_404(CatalogueFournisseur, pk=pk).fournisseur_id
        Fournisseur.objects.select_for_update().get(pk=supplier_id)
    catalogue = get_object_or_404(
        CatalogueFournisseur.objects.select_related("fournisseur", "fournisseur__restaurant"),
        pk=pk,
    )
    form = CatalogueInfoForm(request.POST or None, instance=catalogue)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Informations du catalogue sauvegardées.")
        return redirect("catalogue_detail", pk=catalogue.pk)
    return render(
        request,
        "core/catalogue_form.html",
        {
            "catalogue": catalogue,
            "form": form,
            "title": "Modifier les informations",
        },
    )


@role_required(ROLE_GESTIONNAIRE, ROLE_ADMIN)
@require_http_methods(["POST"])
@transaction.atomic
def catalogue_validate(request, pk):
    if request.method == "POST":
        supplier_id = get_object_or_404(CatalogueFournisseur, pk=pk).fournisseur_id
        Fournisseur.objects.select_for_update().get(pk=supplier_id)
    catalogue = get_object_or_404(
        CatalogueFournisseur.objects.select_related("fournisseur"),
        pk=pk,
    )

    if not catalogue.date_debut:
        messages.error(request, "La date de début est obligatoire pour valider un catalogue.")
        return redirect("catalogue_detail", pk=catalogue.pk)
    if not catalogue.devise or catalogue.devise == Devise.UNKNOWN:
        messages.error(request, "La devise est obligatoire pour valider un catalogue.")
        return redirect("catalogue_detail", pk=catalogue.pk)

    catalogue.statut = CatalogueFournisseur.Statut.VALIDE
    try:
        catalogue.save(update_fields=["statut", "date_modification"])
    except ValidationError as exc:
        for error_message in validation_error_messages(exc):
            messages.error(request, error_message)
        return redirect("catalogue_detail", pk=catalogue.pk)

    messages.success(request, "Catalogue validé.")
    return redirect("catalogue_detail", pk=catalogue.pk)


@role_required(ROLE_GESTIONNAIRE, ROLE_ADMIN)
@require_http_methods(["POST"])
def catalogue_ai_reanalyze(request, pk):
    document = get_object_or_404(CatalogueFournisseur, pk=pk)
    if document.origine == 'documents':
        messages.info(request,'Enrichissez ce catalogue depuis ses BL et factures ; il n’y a pas de fichier catalogue à analyser.')
        return redirect('supplier_catalogue',pk=document.fournisseur_id)
    enqueue_ai_analysis("catalogue", document.pk)
    messages.info(request, "Analyse programmée. Les corrections existantes sont conservées ; les différences seront à confirmer.")
    return redirect("catalogue_detail", pk=pk)


@role_required(ROLE_ADMIN)
@require_http_methods(["GET", "POST"])
def catalogue_delete(request, pk):
    catalogue = get_object_or_404(
        CatalogueFournisseur.objects.select_related("fournisseur", "fournisseur__restaurant"),
        pk=pk,
    )
    fournisseur = catalogue.fournisseur
    if request.method == "POST":
        catalogue_id = catalogue.pk
        file_storage = catalogue.fichier_original.storage
        file_name = catalogue.fichier_original.name
        deletion_date = timezone.now()

        with transaction.atomic():
            from .services.operations import archive_document
            archive_document(catalogue)


        log_admin_deletion("catalogue", catalogue_id, request.user, deletion_date)
        messages.success(request, "Catalogue archivé. Le fichier et les tarifs sont conservés.")
        return redirect("fournisseur_detail", pk=fournisseur.pk)

    summary_items = [
        ("Fournisseur", fournisseur.nom),
        ("Catalogue", catalogue.nom or "Sans nom"),
        (
            "Période",
            (
                f"{catalogue.date_debut.strftime('%d/%m/%Y') if catalogue.date_debut else 'Début non renseigné'}"
                f" - {catalogue.date_fin.strftime('%d/%m/%Y') if catalogue.date_fin else 'Jusqu’à nouvel ordre'}"
            ),
        ),
        ("Lignes catalogue", catalogue.lignes.count()),
    ]
    return render(
        request,
        "core/delete_confirm.html",
        {
            "title": "Supprimer le catalogue",
            "object_label": "catalogue",
            "object_name": catalogue.nom or f"Catalogue {catalogue.pk}",
            "summary_items": summary_items,
            "cancel_url": reverse("catalogue_detail", args=[catalogue.pk]),
            "confirm_label": "Archiver et conserver le fichier",
        },
    )


@role_required(ROLE_GESTIONNAIRE, ROLE_ADMIN)
@require_http_methods(["GET", "POST"])
@transaction.atomic
def ligne_catalogue_update(request, catalogue_pk, pk):
    if request.method == "POST":
        supplier_id = get_object_or_404(LigneCatalogue, pk=pk, catalogue_id=catalogue_pk).catalogue.fournisseur_id
        Fournisseur.objects.select_for_update().get(pk=supplier_id)
    ligne = get_object_or_404(
        LigneCatalogue.objects.select_related(
            "catalogue",
            "catalogue__fournisseur",
            "catalogue__fournisseur__restaurant",
        ),
        pk=pk,
        catalogue_id=catalogue_pk,
    )
    old_price = ligne.prix_unitaire_ht
    form = LigneCatalogueForm(request.POST or None, instance=ligne)
    if request.method == "POST" and form.is_valid():
        ligne = form.save(commit=False)
        ligne.corrigee_manuellement = True
        ligne.save()
        if old_price != ligne.prix_unitaire_ht:
            record_price(ligne, request.user, old_price)
        messages.success(request, "Ligne catalogue sauvegardée.")
        return redirect("catalogue_detail", pk=ligne.catalogue_id)
    return render(
        request,
        "core/ligne_catalogue_form.html",
        {
            "ligne": ligne,
            "catalogue": ligne.catalogue,
            "form": form,
        },
    )


@role_required(ROLE_GESTIONNAIRE, ROLE_ADMIN)
@require_http_methods(["GET", "POST"])
def fournisseur_bon_livraison_add(request, pk):
    fournisseur = get_object_or_404(
        Fournisseur.objects.select_related("restaurant").filter(actif=True, restaurant__actif=True),
        pk=pk,
    )
    form = BonLivraisonUploadForm(request.POST or None, request.FILES or None, fournisseur=fournisseur)
    if request.method == "POST" and form.is_valid():
        bon_livraison = form.save(fournisseur=fournisseur)
        enqueue_ai_analysis("bon_livraison", bon_livraison.pk)
        messages.info(request, DOCUMENT_AI_IMPORT_STARTED_MESSAGE)
        return redirect("fournisseur_detail", pk=fournisseur.pk)
    return render(
        request,
        "core/bon_livraison_upload.html",
        {"form": form, "fournisseur": fournisseur},
    )


@role_required(ROLE_GESTIONNAIRE, ROLE_ADMIN)
@require_http_methods(["GET", "POST"])
def bon_livraison_add(request):
    form = ReceptionBonLivraisonForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        bon_livraison = form.save()
        enqueue_ai_analysis("reception_bon_livraison", bon_livraison.pk)
        messages.info(request, DOCUMENT_AI_IMPORT_STARTED_MESSAGE)
        if user_has_role(request.user, ROLE_GESTIONNAIRE, ROLE_ADMIN):
            return redirect("fournisseur_detail", pk=bon_livraison.fournisseur_id)
        return redirect(f"{reverse('bon_livraison_add')}?envoye=1")

    return render(
        request,
        "core/bon_livraison_add.html",
        {
            "form": form,
            "sent": request.GET.get("envoye") == "1",
        },
    )


def document_workflow_context(document, kind):
    from .models import AnalysisVersion, AnalysisJob
    from .services.quality import validation_problems
    latest = AnalysisVersion.objects.filter(document_type=kind, document_id=document.pk).first()
    job = AnalysisJob.objects.filter(document_type=kind,document_id=document.pk).order_by('-pk').first()
    problems = validation_problems(document)
    verified = bool(document.verified_at and document.statut in ('validee','valide','litige') and (not problems or len(document.validation_exception.strip()) >= 8))
    if kind == 'catalogue': verified = document.statut == 'valide' and document.devise != 'UNK' and bool(document.date_debut)
    return {'workflow_document':document, 'workflow_kind':kind, 'data_verified':verified, 'quality_problems':problems,
        'pending_extraction':latest if latest and latest.changes and not latest.applied else None,
        'analysis_job':job,
        'duplicate_content':type(document).objects.filter(file_sha256=document.file_sha256).exclude(pk=document.pk).exists() if document.file_sha256 else False,
        'duplicate_number': document_number_duplicate(document)}


def document_number_duplicate(document):
    field = 'numero_facture' if isinstance(document, Facture) else 'numero_bon_livraison' if isinstance(document,BonLivraison) else None
    if not field or not getattr(document,field): return False
    return type(document).objects.filter(fournisseur_id=document.fournisseur_id, **{field:getattr(document,field)}).exclude(pk=document.pk).exists()
