from datetime import date
from urllib.parse import urlencode

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from .access import ROLE_ADMIN, ROLE_GESTIONNAIRE, role_required
from .models import DeliveryIssue, Fournisseur, Restaurant
from .services.reconciliation import line_fingerprint
from .services.delivery_claims import add_delivery_issue_to_claim
from .supplier_logos import supplier_logo


manage = role_required(ROLE_ADMIN, ROLE_GESTIONNAIRE)


def _filters(source):
    return {
        key: source.get(key, "") if source.get(key, "").isdigit() else ""
        for key in ("restaurant", "supplier")
    }


def _review_url(source):
    scope = _filters(source)
    scope["state"] = "history" if source.get("state") == "history" else "pending"
    position = source.get("position", "1")
    scope["position"] = position if position.isdigit() else "1"
    return reverse("delivery_issue_review") + "?" + urlencode(scope)


def _source_modified(issue):
    return issue.source_fingerprint != line_fingerprint(issue.line)


def _prepare_display(issue):
    if issue is None:
        return None
    snapshot = issue.snapshot or {}
    issue.display_product = snapshot.get("designation") or issue.line.designation or "Produit à identifier"
    issue.display_reference = (
        snapshot.get("reference")
        or snapshot.get("reference_produit")
        or issue.line.reference_produit
        or "Non renseignée"
    )
    issue.display_quantity = snapshot.get("quantity")
    if issue.display_quantity is None:
        issue.display_quantity = snapshot.get("quantite_livree")
    if issue.display_quantity is None:
        issue.display_quantity = issue.line.quantite_livree
    issue.display_unit = snapshot.get("unit") or snapshot.get("unite") or issue.line.unite
    issue.display_supplier = snapshot.get("supplier") or issue.delivery.fournisseur.nom
    issue.display_restaurant = snapshot.get("restaurant") or issue.delivery.fournisseur.restaurant.nom
    issue.display_delivery_number = (
        snapshot.get("delivery_number") or issue.delivery.numero_bon_livraison or "Numéro à compléter"
    )
    issue.display_delivery_date = issue.delivery.date_livraison
    if snapshot.get("delivery_date"):
        try:
            issue.display_delivery_date = date.fromisoformat(snapshot["delivery_date"])
        except (TypeError, ValueError):
            pass
    issue.delivery_archived = bool(issue.delivery.archived_at)
    return issue


@manage
@require_http_methods(["GET"])
def delivery_issue_review(request):
    state = "history" if request.GET.get("state") == "history" else "pending"
    scope = _filters(request.GET)
    base = DeliveryIssue.objects.select_related(
        "delivery__fournisseur__restaurant", "line", "reported_by", "reviewed_by"
    )
    if scope["restaurant"]:
        base = base.filter(delivery__fournisseur__restaurant_id=scope["restaurant"])
    if scope["supplier"]:
        base = base.filter(delivery__fournisseur_id=scope["supplier"])

    pending = base.filter(
        state=DeliveryIssue.State.PENDING, delivery__archived_at__isnull=True
    )
    history = base.exclude(state=DeliveryIssue.State.PENDING)
    pending_count = pending.count()
    history_count = history.count()
    if state == "history":
        issues = history.order_by("-reviewed_at", "-pk")
    else:
        issues = pending.order_by("created_at", "pk")

    total = issues.count()
    try:
        position = max(1, min(int(request.GET.get("position", 1)), total)) if total else 1
    except ValueError:
        position = 1

    paginator = Paginator(issues, 40)
    page = paginator.get_page((position - 1) // 40 + 1)
    page_items = list(page.object_list)
    local_position = (position - 1) % 40
    current = page_items[local_position] if page_items else None
    previous = issues[position - 2] if position > 1 else None
    following = issues[position] if position < total else None

    numbers = sorted(
        {1, total, *range(max(1, position - 2), min(total, position + 2) + 1)}
    ) if total else []
    progress = []
    prior = 0
    for number in numbers:
        if prior and number - prior > 1:
            progress.append(None)
        progress.append(number)
        prior = number

    suppliers = Fournisseur.objects.all()
    if scope["restaurant"]:
        suppliers = suppliers.filter(restaurant_id=scope["restaurant"])

    previous = _prepare_display(previous)
    current = _prepare_display(current)
    following = _prepare_display(following)
    if current:
        from .views import document_preview_type
        current.source_modified = _source_modified(current)
        current.logo = supplier_logo(current.display_supplier)
        current.preview_type = document_preview_type(current.delivery.fichier) if not current.delivery_archived else ""
        linked = current.claim_lines.exclude(claim__state="cancelled").select_related("claim").first()
        current.claim_id_for_display = linked.claim_id if linked else None

    return render(request, "core/delivery_issue_review.html", {
        "current": current,
        "previous": previous,
        "next": following,
        "position": position,
        "total": total,
        "page": page,
        "numbers": progress,
        "state": state,
        "scope": scope,
        "pending_count": pending_count,
        "history_count": history_count,
        "restaurants": Restaurant.objects.all(),
        "suppliers": suppliers,
        "base_query": urlencode({**scope, "state": state}),
    })


@manage
@require_http_methods(["POST"])
def delivery_issue_action(request, pk):
    try:
        with transaction.atomic():
            issue = get_object_or_404(
                DeliveryIssue.objects.select_related("delivery"), pk=pk,
                delivery__archived_at__isnull=True)
            Fournisseur.objects.select_for_update().get(pk=issue.delivery.fournisseur_id)
            issue = get_object_or_404(
                DeliveryIssue.objects.select_for_update().select_related("delivery__fournisseur", "line"),
                pk=pk,
                delivery__archived_at__isnull=True,
            )
            if issue.state != DeliveryIssue.State.PENDING:
                raise ValidationError("Ce problème de livraison a déjà été traité.")
            action = request.POST.get("action")
            if action not in ("confirm", "dismiss"):
                raise ValidationError("Choisissez une décision valide.")
            review_note = request.POST.get("review_note", "").strip()
            if len(review_note) > 2000:
                raise ValidationError("La note de revue ne peut pas dépasser 2 000 caractères.")
            issue.state = (
                DeliveryIssue.State.CONFIRMED
                if action == "confirm"
                else DeliveryIssue.State.DISMISSED
            )
            issue.review_note = review_note
            issue.reviewed_by = request.user
            issue.reviewed_at = timezone.now()
            issue.save(update_fields=["state", "review_note", "reviewed_by", "reviewed_at", "updated_at"])
            if action == "confirm":
                claim, _ = add_delivery_issue_to_claim(issue.pk, request.user)
        messages.success(
            request,
            f"Problème confirmé et ajouté au brouillon de réclamation n° {claim.pk} du fournisseur."
            if action == "confirm"
            else "Signalement écarté.",
        )
    except ValidationError as exc:
        messages.warning(request, " ".join(exc.messages))
    return redirect(_review_url(request.POST))
