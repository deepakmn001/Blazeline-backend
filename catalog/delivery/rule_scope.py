"""
Shared status/scope logic for the Admin Delivery API.

Mirrors — does NOT reimplement independently — the two window checks
calculate_delivery() already performs inline in
catalog/delivery/services.py:

    if rule.starts_at and rule.starts_at > now: continue   # scheduled
    if rule.ends_at and rule.ends_at < now: continue        # expired

Two flavors are provided for every concept:
  - compute_rule_status() / resolve_rule_scope()  — per-instance Python
    helpers, used by serializers to annotate a single already-fetched
    object for API output. This is normal DRF SerializerMethodField
    work (one object at a time, on an already-paginated page) and is
    NOT the "Python loop over the full queryset" pattern the
    performance requirement is about.
  - status_query() / scope_query()  — return a Django Q object that
    pushes the SAME logic down to SQL, used by DeliveryRuleFilter and
    DeliveryOverviewAPIView so status/scope filtering and counting
    never has to load full tables into Python.

Both flavors must stay logically identical. If services.py's window
semantics ever change, update all four functions here together.
"""

from django.db.models import Q
from django.utils import timezone


# ==========================================================
# PER-INSTANCE (serializers)
# ==========================================================

def compute_rule_status(rule, now=None) -> str:
    """Returns one of: 'inactive', 'scheduled', 'expired', 'active'."""
    now = now or timezone.now()

    if not rule.active:
        return "inactive"
    if rule.starts_at and rule.starts_at > now:
        return "scheduled"
    if rule.ends_at and rule.ends_at < now:
        return "expired"
    return "active"


def resolve_rule_scope(rule) -> tuple[str, str]:
    """
    Most-specific-wins precedence — matches DeliveryRule.specificity's
    own ordering and the frontend's scopeOfPayload(). Returns (type, label).
    """
    if rule.variant_id:
        sku = getattr(rule.variant, "sku", None) or str(rule.variant_id)
        return "variant", f"Variant: {sku}"
    if rule.product_id:
        name = getattr(rule.product, "name", None) or str(rule.product_id)
        return "product", f"Product: {name}"
    if rule.subcategory_id:
        name = getattr(rule.subcategory, "name", None) or str(rule.subcategory_id)
        return "subcategory", f"Subcategory: {name}"
    if rule.category_id:
        name = getattr(rule.category, "name", None) or str(rule.category_id)
        return "category", f"Category: {name}"
    if rule.zone_id:
        name = getattr(rule.zone, "name", None) or str(rule.zone_id)
        return "zone", f"Zone: {name}"
    return "global", "Global — all orders"


# ==========================================================
# DB-SIDE (filters, overview aggregation)
# ==========================================================

def status_query(value: str, now=None) -> Q:
    """
    Q object equivalent of compute_rule_status(), built to be pushed
    down to SQL via .filter() or Count(..., filter=...). Each branch
    mirrors the same sequential logic (inactive -> scheduled ->
    expired -> active) as an exclusive condition, so no double-counting
    across branches.
    """
    now = now or timezone.now()

    if value == "inactive":
        return Q(active=False)

    if value == "scheduled":
        # starts_at__gt excludes NULL automatically at the SQL level.
        return Q(active=True, starts_at__gt=now)

    if value == "expired":
        return (
            Q(active=True)
            & (Q(starts_at__isnull=True) | Q(starts_at__lte=now))
            & Q(ends_at__lt=now)
        )

    if value == "active":
        return (
            Q(active=True)
            & (Q(starts_at__isnull=True) | Q(starts_at__lte=now))
            & (Q(ends_at__isnull=True) | Q(ends_at__gte=now))
        )

    # Unknown value — match nothing rather than silently matching everything.
    return Q(pk__in=[])


def scope_query(value: str) -> Q:
    """Q object equivalent of resolve_rule_scope()'s precedence."""
    if value == "variant":
        return Q(variant__isnull=False)
    if value == "product":
        return Q(variant__isnull=True, product__isnull=False)
    if value == "subcategory":
        return Q(variant__isnull=True, product__isnull=True, subcategory__isnull=False)
    if value == "category":
        return Q(
            variant__isnull=True, product__isnull=True,
            subcategory__isnull=True, category__isnull=False,
        )
    if value == "zone":
        return Q(
            variant__isnull=True, product__isnull=True,
            subcategory__isnull=True, category__isnull=True, zone__isnull=False,
        )
    if value == "global":
        return Q(
            variant__isnull=True, product__isnull=True, subcategory__isnull=True,
            category__isnull=True, zone__isnull=True,
        )
    return Q(pk__in=[])