# catalog/delivery/services.py
"""
Delivery Engine — the evaluator.

Turns (pincode, cart_items, customer_type, shipping_type) into a final
Decimal delivery charge, using DeliveryZone / DeliveryRule /
DeliveryRuleCondition / DeliveryRuleAction.

LOCKED SEMANTICS (do not change without updating this doc + tests):
  - BASE_CHARGE + combine_mode=OVERRIDE  → replaces the running total.
  - SURCHARGE   + combine_mode=ADD       → adds to the running total.
  - DISCOUNT    + combine_mode=ADD       → subtracts from the running total.
  - DISCOUNT    + combine_mode=OVERRIDE  → INVALID CONFIGURATION.
        Must be rejected at the model layer (DeliveryRuleAction.clean()).
        As defense-in-depth, the engine also skips (does not apply) any
        rule found in this state at evaluation time rather than silently
        zeroing out a running total — see _apply_rule_actions().
  - FREE_DELIVERY is TERMINAL: zeroes the running total, discards any
    breakdown accumulated so far, and stops further rule evaluation —
    regardless of that rule's own stop_after flag.
  - A rule with zero *active* actions contributes nothing and is SKIPPED
    entirely — it does not consume its stop_after (a misconfigured/
    incomplete rule must never block other valid rules from evaluating).
  - FIELD_CART_VALUE and FIELD_TOTAL_QUANTITY are evaluated against the
    WHOLE CART (global facts) — cart-level thresholds.
  - FIELD_WEIGHT and FIELD_QUANTITY are evaluated against the RULE'S
    SCOPE ONLY (items matched by the rule's targeting).
  - "percentage" pricing_mode is always against scope subtotal (matched
    items only), never whole-cart subtotal.
  - Cart eligibility (variant exists, active, and its product/category/
    subcategory are active + product is published) is enforced at
    hydration time — any variant_id that fails this raises
    CartValidationError. MOQ is NOT enforced here — separate concern.
  - A pincode's zone being inactive means the pincode is NOT serviceable
    via that zone — raises NotServiceableError.
"""

from decimal import Decimal, InvalidOperation

from django.db.models import Q, Prefetch
from django.utils import timezone

from ..models import (
    ServiceablePincode,
    ProductVariant,
    DeliveryRule,
    DeliveryRuleCondition,
    DeliveryRuleAction,
)
from .validators import normalize_pincode

MAX_QUANTITY_PER_ITEM = 100000


class NotServiceableError(Exception):
    """Raised when the given pincode (or its zone) is not serviceable."""
    pass


class CartValidationError(Exception):
    """
    Raised when cart_items input is malformed, contains duplicate/invalid/
    inactive/ineligible variant_ids, or invalid quantities.
    """

    def __init__(self, errors):
        self.errors = errors if isinstance(errors, list) else [errors]
        super().__init__("; ".join(self.errors))

def get_serviceable_location(pincode):
    """
    Resolve the active serviceable-pincode record and its active zone.

    Returns:
        ServiceablePincode instance.

    Raises:
        NotServiceableError:
            If the pincode does not exist, is inactive, or is mapped
            to an inactive delivery zone.
    """
    try:
        pincode = normalize_pincode(pincode)
    except ValueError as exc:
        raise CartValidationError([str(exc)])

    try:
        location = (
            ServiceablePincode.objects
            .select_related("zone")
            .get(
                pincode=pincode,
                is_active=True,
            )
        )
    except ServiceablePincode.DoesNotExist:
        raise NotServiceableError(
            f"Pincode {pincode} is not serviceable."
        )

    if location.zone is not None and not location.zone.active:
        raise NotServiceableError(
            f"Pincode {pincode} is mapped to an inactive delivery zone."
        )

    return location
def calculate_delivery(pincode, cart_items, customer_type=None, shipping_type=None):
    """
    pincode: str
    cart_items: list of {"variant_id": int, "quantity": int}
    customer_type / shipping_type: optional strings, matched against
        FIELD_CUSTOMER_TYPE / FIELD_SHIPPING_TYPE conditions.

    Returns:
        {
            "zone": DeliveryZone | None,
            "total": Decimal,
            "breakdown": [{"rule": str, "label": str, "amount": Decimal}, ...],
        }

    Raises:
        NotServiceableError if pincode/zone is not serviceable.
        CartValidationError if pincode format or cart_items is malformed,
            or cart_items reference invalid/inactive/ineligible variants.
    """
    try:
        pincode = normalize_pincode(pincode)
    except ValueError as e:
        raise CartValidationError([str(e)])

    zone = _resolve_zone(pincode)

    _validate_cart_items_shape(cart_items)
    hydrated = _hydrate_cart(cart_items)

    global_facts = _global_facts(hydrated, customer_type, shipping_type)

    candidate_rules = (
        DeliveryRule.objects
        .filter(active=True)
        .filter(_zone_q(zone))
        .prefetch_related(
            "conditions",
            Prefetch(
                "actions",
                queryset=DeliveryRuleAction.objects.filter(active=True),
                to_attr="active_actions",
            ),
        )
    )

    now = timezone.now()
    applicable = []
    for rule in candidate_rules:
        if rule.starts_at and rule.starts_at > now:
            continue
        if rule.ends_at and rule.ends_at < now:
            continue

        # Misconfigured/incomplete rule — contributes nothing, must not
        # occupy an evaluation slot or trigger stop_after.
        if not rule.active_actions:
            continue

        scope_items = _scope_items(rule, hydrated)
        if not scope_items:
            continue

        facts = _scope_facts(scope_items, global_facts)

        try:
            passes = _conditions_pass(rule, facts)
        except (InvalidOperation, ValueError, TypeError):
            # Malformed condition value on this rule — skip the rule,
            # don't crash the whole request.
            continue

        if not passes:
            continue

        applicable.append((rule, facts))

    # broadest first, then lowest priority first — later = higher precedence
    applicable.sort(key=lambda t: (t[0].specificity, t[0].priority))

    running_total = Decimal("0.00")
    breakdown = []

    for rule, facts in applicable:
        result = _apply_rule_actions(rule, facts)
        if result is None:
            # Invalid config (discount + override) slipped past model
            # validation — skip defensively rather than corrupt the total.
            continue

        rule_charge, rule_breakdown, is_free = result

        if is_free:
            running_total = Decimal("0.00")
            breakdown = [{"rule": rule.name, "label": "Free Delivery", "amount": Decimal("0.00")}]
            break

        if rule.combine_mode == DeliveryRule.COMBINE_OVERRIDE:
            running_total = rule_charge
            breakdown = rule_breakdown
        else:  # COMBINE_ADD
            running_total += rule_charge
            breakdown.extend(rule_breakdown)

        if running_total < 0:
            running_total = Decimal("0.00")

        if rule.stop_after:
            break

    return {
    "zone": zone,
    "subtotal": global_facts["cart_value"],
    "weight": sum(
        (
            item["line_weight"]
            for item in hydrated
        ),
        Decimal("0.00"),
    ),
    "total": running_total.quantize(
        Decimal("0.01")
    ),
    "breakdown": breakdown,
}

# ==========================================================
# internals — validation
# ==========================================================

def _validate_cart_items_shape(cart_items):
    errors = []

    if not isinstance(cart_items, list) or len(cart_items) == 0:
        raise CartValidationError(["cart_items must be a non-empty list."])

    seen_ids = set()
    for idx, item in enumerate(cart_items):
        if not isinstance(item, dict):
            errors.append(f"Item at index {idx} must be an object.")
            continue

        variant_id = item.get("variant_id")
        quantity = item.get("quantity")

        if not isinstance(variant_id, int) or isinstance(variant_id, bool) or variant_id <= 0:
            errors.append(f"Item at index {idx}: variant_id must be a positive integer.")
        elif variant_id in seen_ids:
            errors.append(f"Duplicate variant_id {variant_id} in cart_items.")
        else:
            seen_ids.add(variant_id)

        if not isinstance(quantity, int) or isinstance(quantity, bool):
            errors.append(f"Item at index {idx}: quantity must be an integer.")
        elif quantity < 1:
            errors.append(f"Item at index {idx}: quantity must be >= 1.")
        elif quantity > MAX_QUANTITY_PER_ITEM:
            errors.append(f"Item at index {idx}: quantity exceeds maximum allowed ({MAX_QUANTITY_PER_ITEM}).")

    if errors:
        raise CartValidationError(errors)


def _resolve_zone(pincode):
    location = get_serviceable_location(pincode)
    return location.zone


def _zone_q(zone):
    if zone is None:
        return Q(zone__isnull=True)
    return Q(zone__isnull=True) | Q(zone_id=zone.id)


def _hydrate_cart(cart_items):
    ids = [ci["variant_id"] for ci in cart_items]
    qty_by_id = {ci["variant_id"]: ci["quantity"] for ci in cart_items}

    # Eligibility enforced right here: variant + product + subcategory +
    # category must all be active, and product must be published.
    variants = (
        ProductVariant.objects
        .filter(
            id__in=ids,
            active=True,
            product__active=True,
            product__status="published",
            product__category__active=True,
            product__subcategory__active=True,
        )
        .select_related("product", "product__category", "product__subcategory")
    )

    found_by_id = {v.id: v for v in variants}
    missing = [vid for vid in ids if vid not in found_by_id]
    if missing:
        raise CartValidationError(
            [f"variant_id {vid} is invalid, inactive, or not currently purchasable." for vid in missing]
        )

    hydrated = []
    for vid in ids:
        v = found_by_id[vid]
        qty = qty_by_id[vid]
        hydrated.append({
            "variant": v,
            "product": v.product,
            "quantity": qty,
            "line_value": v.selling_price * qty,
            "line_weight": v.weight * qty,
        })
    return hydrated


def _global_facts(hydrated, customer_type, shipping_type):
    return {
        "cart_value": sum((i["line_value"] for i in hydrated), Decimal("0.00")),
        "total_quantity": sum(i["quantity"] for i in hydrated),
        "customer_type": customer_type,
        "shipping_type": shipping_type,
    }


def _scope_items(rule, hydrated):
    matched = []
    for item in hydrated:
        product = item["product"]
        if rule.category_id and product.category_id != rule.category_id:
            continue
        if rule.subcategory_id and product.subcategory_id != rule.subcategory_id:
            continue
        if rule.product_id and product.id != rule.product_id:
            continue
        if rule.variant_id and item["variant"].id != rule.variant_id:
            continue
        matched.append(item)
    return matched


def _scope_facts(scope_items, global_facts):
    return {
        "cart_value": global_facts["cart_value"],          # global
        "total_quantity": global_facts["total_quantity"],  # global
        "customer_type": global_facts["customer_type"],
        "shipping_type": global_facts["shipping_type"],
        "weight": sum((i["line_weight"] for i in scope_items), Decimal("0.00")),  # scope
        "quantity": sum(i["quantity"] for i in scope_items),                       # scope
        "_scope_subtotal": sum((i["line_value"] for i in scope_items), Decimal("0.00")),
        "_scope_item_count": len(scope_items),
        "_scope_quantity": sum(i["quantity"] for i in scope_items),
    }


def _conditions_pass(rule, facts):
    for cond in rule.conditions.all():
        if not _evaluate_condition(cond, facts):
            return False
    return True


def _evaluate_condition(cond, facts):
    field = cond.field
    op = cond.operator
    raw = cond.value

    actual = facts.get(field)
    if actual is None:
        return False

    if op == DeliveryRuleCondition.OP_IN:
        allowed = [v.strip() for v in raw.split(",")]
        return str(actual) in allowed

    if field in (DeliveryRuleCondition.FIELD_CART_VALUE, DeliveryRuleCondition.FIELD_WEIGHT):
        target = Decimal(raw)          # may raise InvalidOperation — caught by caller
        actual = Decimal(actual)
    elif field in (DeliveryRuleCondition.FIELD_QUANTITY, DeliveryRuleCondition.FIELD_TOTAL_QUANTITY):
        target = int(raw)              # may raise ValueError — caught by caller
        actual = int(actual)
    else:
        target = raw
        actual = str(actual)

    if op == DeliveryRuleCondition.OP_GT:
        return actual > target
    if op == DeliveryRuleCondition.OP_GTE:
        return actual >= target
    if op == DeliveryRuleCondition.OP_LT:
        return actual < target
    if op == DeliveryRuleCondition.OP_LTE:
        return actual <= target
    if op == DeliveryRuleCondition.OP_EQ:
        return actual == target

    return False


def _apply_rule_actions(rule, facts):
    """
    Returns (charge, breakdown, is_free) or None if the rule's
    action_type/combine_mode combination is invalid (discount action
    under an OVERRIDE rule) — caller skips the rule entirely in that case.
    """
    if rule.combine_mode == DeliveryRule.COMBINE_OVERRIDE:
        if any(a.action_type == DeliveryRuleAction.ACTION_DISCOUNT for a in rule.active_actions):
            return None

    scope_subtotal = facts["_scope_subtotal"]
    scope_weight = facts["weight"]
    scope_item_count = facts["_scope_item_count"]
    scope_quantity = facts["_scope_quantity"]

    total = Decimal("0.00")
    breakdown = []
    is_free = False

    for action in rule.active_actions:  # prefetched, already filtered active=True
        if action.action_type == DeliveryRuleAction.ACTION_FREE_DELIVERY:
            is_free = True
            continue

        charge = _price_action(action, scope_item_count, scope_quantity, scope_weight, scope_subtotal)

        if action.action_type == DeliveryRuleAction.ACTION_DISCOUNT:
            charge = -charge

        total += charge
        breakdown.append({
            "rule": rule.name,
            "label": action.label or action.get_action_type_display(),
            "amount": charge.quantize(Decimal("0.01")),
        })

    if is_free:
        return Decimal("0.00"), [], True

    return total, breakdown, False


def _price_action(action, scope_item_count, scope_quantity, scope_weight, scope_subtotal):
    mode = action.pricing_mode
    amount = action.amount

    if mode == DeliveryRuleAction.PRICING_FIXED:
        return amount
    if mode == DeliveryRuleAction.PRICING_PER_ITEM:
        return amount * scope_item_count
    if mode == DeliveryRuleAction.PRICING_PER_UNIT:
        return amount * scope_quantity
    if mode == DeliveryRuleAction.PRICING_PER_KG:
        return amount * scope_weight
    if mode == DeliveryRuleAction.PRICING_PERCENTAGE:
        return (amount / Decimal("100")) * scope_subtotal

    return Decimal("0.00")