"""Add confirmed BL defects to the supplier's editable claim draft."""
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from core.models import BusinessAudit, DeliveryIssue, Fournisseur, SupplierClaim, SupplierClaimLine


@transaction.atomic
def add_delivery_issue_to_claim(issue_id, actor=None):
    supplier_id = DeliveryIssue.objects.get(pk=issue_id).delivery.fournisseur_id
    supplier = Fournisseur.objects.select_for_update().get(pk=supplier_id)
    issue = DeliveryIssue.objects.select_for_update(of=("self",)).select_related(
        "delivery", "line", "reviewed_by"
    ).get(pk=issue_id)
    if issue.state != DeliveryIssue.State.CONFIRMED:
        raise ValidationError("Confirmez le problème de livraison avant de l'ajouter à une réclamation.")
    existing = SupplierClaimLine.objects.filter(delivery_issue=issue).exclude(
        claim__state=SupplierClaim.State.CANCELLED
    ).select_related("claim").first()
    if existing:
        return existing.claim, False

    drafts = list(SupplierClaim.objects.filter(fournisseur=supplier, state=SupplierClaim.State.DRAFT))
    claim = next((draft for draft in drafts if draft.currency == supplier.devise_defaut), None)
    # A BL defect has no financial amount yet: an existing single draft is safe,
    # even when its currency differs from the supplier's default.
    if claim is None and len(drafts) == 1:
        claim = drafts[0]
    reviewer = actor or issue.reviewed_by
    if claim is None:
        claim = SupplierClaim.objects.create(
            fournisseur=supplier, currency=supplier.devise_defaut, created_by=reviewer
        )
    source = issue.snapshot or {}
    data = {
        "category": "delivery", "delivery_issue_id": issue.pk,
        "delivery_id": issue.delivery_id, "supplier_id": supplier.pk,
        "delivery_number": source.get("delivery_number") or issue.delivery.numero_bon_livraison,
        "delivery_date": source.get("delivery_date") or (
            issue.delivery.date_livraison.isoformat() if issue.delivery.date_livraison else None),
        "product": source.get("designation") or issue.line.designation,
        "reference": source.get("reference") or source.get("reference_produit") or issue.line.reference_produit,
        "quantity": source.get("quantity", source.get("quantite_livree")),
        "unit": source.get("unit") or source.get("unite") or issue.line.unite,
        "comment": issue.comment, "review_note": issue.review_note,
        "reviewed_by_id": issue.reviewed_by_id,
        "reviewed_at": issue.reviewed_at.isoformat() if issue.reviewed_at else None,
        # The full quantity on the BL is not the defective quantity.
        # Without the latter and a confirmed value, no loss can be calculated.
        "impact": None,
    }
    if data["quantity"] is None and issue.line.quantite_livree is not None:
        data["quantity"] = str(issue.line.quantite_livree)
    item = SupplierClaimLine.objects.create(
        claim=claim, delivery_issue=issue, category="delivery",
        fingerprint=issue.source_fingerprint, snapshot=data
    )
    claim.save(update_fields=["date_modification"])
    BusinessAudit.objects.create(
        restaurant=supplier.restaurant, actor=reviewer, model_label="core.supplierclaim",
        object_id=claim.pk, action="claim_delivery_add", before={},
        after={"claim_line_id": item.pk, "delivery_issue_id": issue.pk, "snapshot": data}
    )
    return claim, True


def reopen_delivery_issues(claim):
    """Called with the supplier and claim locked by cancellation/deletion."""
    ids = list(claim.lines.filter(category="delivery", delivery_issue__isnull=False)
               .values_list("delivery_issue_id", flat=True))
    still_claimed = SupplierClaimLine.objects.filter(delivery_issue_id__in=ids).exclude(
        claim=claim
    ).exclude(claim__state=SupplierClaim.State.CANCELLED).values_list("delivery_issue_id", flat=True)
    DeliveryIssue.objects.filter(pk__in=ids, state=DeliveryIssue.State.CONFIRMED).exclude(
        pk__in=still_claimed
    ).update(state=DeliveryIssue.State.PENDING, updated_at=timezone.now())
