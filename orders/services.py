from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Callable, Iterable
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from accounts.models import Customer
from cart.models import Cart, CartItem
from catalog.models import ProductVariant

from .models import (
    InventoryReservation,
    Order,
    OrderItem,
    Payment,
)


# ============================================================================
# MONEY / QUANTITY
# ============================================================================

MONEY_QUANTUM = Decimal("0.01")


def money(value: Decimal | int | str) -> Decimal:
    """
    Normalize all monetary values to the database precision.

    Never use binary floating point for commercial calculations.
    """
    return Decimal(value).quantize(
        MONEY_QUANTUM,
        rounding=ROUND_HALF_UP,
    )


# ============================================================================
# ORDER ERRORS
# ============================================================================


class OrderError(Exception):
    """Base class for expected order-domain failures."""


class InvalidOrderError(OrderError):
    pass


class EmptyCartError(OrderError):
    pass


class CartOwnershipError(OrderError):
    pass


class VariantUnavailableError(OrderError):
    pass


class MinimumOrderQuantityError(OrderError):
    pass


class InsufficientStockError(OrderError):
    pass


class DeliveryValidationError(OrderError):
    pass


class IdempotencyConflictError(OrderError):
    pass


# ============================================================================
# SERVER-SIDE DELIVERY CONTRACT
# ============================================================================

@dataclass(frozen=True)
class DeliveryQuote:
    """
    Server-generated delivery result.

    IMPORTANT:
    This must be produced by your backend delivery engine.
    Never construct this from frontend totals/prices.

    `breakdown` is intended to be the exact customer-visible snapshot that
    will be stored on the Order.
    """

    charge: Decimal
    free_delivery: bool
    zone_id: int | None
    zone_name: str
    breakdown: list[dict]


DeliveryResolver = Callable[
    [
        str,
        list[dict],
        Customer,
    ],
    DeliveryQuote,
]


TaxResolver = Callable[
    [
        ProductVariant,
        Decimal,
    ],
    Decimal,
]


# ============================================================================
# INTERNAL HELPERS
# ============================================================================


def _public_order_number() -> str:
    """
    Generate a non-sequential public order number.

    DB primary keys must never be exposed as the public order identifier.
    """
    return f"BL-{uuid4().hex[:12].upper()}"


def _validate_shipping_snapshot(
    shipping: dict,
) -> None:
    required = (
        "full_name",
        "phone",
        "email",
        "address_line1",
        "city",
        "state",
        "pincode",
    )

    missing = [
        field
        for field in required
        if not str(shipping.get(field, "")).strip()
    ]

    if missing:
        raise InvalidOrderError(
            f"Missing required shipping fields: {', '.join(missing)}"
        )

    pincode = str(
        shipping["pincode"]
    ).strip()

    if len(pincode) != 6 or not pincode.isdigit():
        raise InvalidOrderError(
            "Enter a valid 6-digit delivery pincode."
        )


def _normalize_idempotency_key(
    value: str,
) -> str:
    key = str(value or "").strip()

    if not key:
        raise InvalidOrderError(
            "Idempotency key is required."
        )

    if len(key) > 128:
        raise InvalidOrderError(
            "Idempotency key is too long."
        )

    return key


def _build_snapshot_name(
    variant: ProductVariant,
) -> str:
    """
    Variant.display_name dynamically resolves mapped option values.
    """
    try:
        display_name = (
            variant.display_name
        )
    except Exception:
        display_name = ""

    return display_name or ""


def _ensure_customer(
    customer: Customer,
) -> None:
    if not isinstance(customer, Customer):
        raise CartOwnershipError(
            "Only customer accounts can place orders."
        )

    if not customer.is_active:
        raise CartOwnershipError(
            "This customer account is inactive."
        )


def _resolve_customer_cart(
    customer: Customer,
) -> Cart:
    try:
        return (
            Cart.objects
            .select_for_update()
            .get(customer=customer)
        )
    except Cart.DoesNotExist:
        raise EmptyCartError(
            "Your cart is empty."
        )


def _load_locked_cart_items(
    cart: Cart,
) -> list[CartItem]:
    """
    Lock cart rows for the complete order transaction.

    We lock both cart items and their variants below. The cart row itself is
    locked by `_resolve_customer_cart`.
    """
    items = list(
        CartItem.objects
        .select_for_update()
        .select_related(
            "variant",
            "variant__product",
        )
        .filter(cart=cart)
        .order_by("id")
    )

    if not items:
        raise EmptyCartError(
            "Your cart is empty."
        )

    return items


def _lock_variants_in_stable_order(
    cart_items: Iterable[CartItem],
) -> dict[int, ProductVariant]:
    """
    Lock every ProductVariant in deterministic primary-key order.

    Stable lock ordering substantially reduces deadlock risk when concurrent
    orders contain overlapping sets of variants.
    """
    variant_ids = sorted(
        {
            item.variant_id
            for item in cart_items
        }
    )

    variants = list(
        ProductVariant.objects
        .select_for_update()
        .select_related("product")
        .filter(pk__in=variant_ids)
        .order_by("pk")
    )

    variant_map = {
        variant.pk: variant
        for variant in variants
    }

    if len(variant_map) != len(
        variant_ids
    ):
        raise VariantUnavailableError(
            "One or more cart products are no longer available."
        )

    return variant_map


def _validate_variant_for_checkout(
    variant: ProductVariant,
    quantity: int,
) -> None:
    if not variant.active:
        raise VariantUnavailableError(
            f"{variant.sku} is no longer available."
        )

    if quantity < 1:
        raise InvalidOrderError(
            f"Invalid quantity for {variant.sku}."
        )

    minimum = int(
        variant.minimum_order_quantity or 1
    )

    if quantity < minimum:
        raise MinimumOrderQuantityError(
            f"{variant.sku} requires a minimum order quantity of {minimum}."
        )

    available_stock = int(
        variant.stock or 0
    )

    if quantity > available_stock:
        raise InsufficientStockError(
            f"Insufficient stock for {variant.sku}. "
            f"Available: {available_stock}."
        )


def _calculate_line_values(
    *,
    variant: ProductVariant,
    quantity: int,
    tax_resolver: TaxResolver,
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """
    Returns:
        unit_price
        tax_rate
        tax_amount
        line_total

    Tax is resolved by a server-side callback, never from request data.
    """
    unit_price = money(
        variant.selling_price
    )

    line_subtotal = money(
        unit_price * quantity
    )

    tax_rate = money(
        tax_resolver(
            variant,
            line_subtotal,
        )
    )

    if tax_rate < 0:
        raise InvalidOrderError(
            "Server tax configuration is invalid."
        )

    tax_amount = money(
        line_subtotal * tax_rate / Decimal("100")
    )

    line_total = money(
        line_subtotal + tax_amount
    )

    return (
        unit_price,
        tax_rate,
        tax_amount,
        line_total,
    )


def _validate_delivery_quote(
    quote: DeliveryQuote,
) -> None:
    charge = money(
        quote.charge
    )

    if charge < 0:
        raise DeliveryValidationError(
            "Delivery charge cannot be negative."
        )

    if not isinstance(
        quote.breakdown,
        list,
    ):
        raise DeliveryValidationError(
            "Invalid delivery breakdown."
        )

    if quote.zone_id is not None:
        if not isinstance(
            quote.zone_id,
            int,
        ) or quote.zone_id <= 0:
            raise DeliveryValidationError(
                "Invalid delivery zone."
            )


def _find_existing_idempotent_order(
    customer: Customer,
    idempotency_key: str,
) -> Order | None:
    """
    Read-only helper used before creation.

    Final race safety is still provided by Order.idempotency_key UNIQUE.
    """
    return (
        Order.objects
        .select_related("customer")
        .filter(
            customer=customer,
            idempotency_key=idempotency_key,
        )
        .first()
    )


def _assert_idempotent_replay_compatible(
    order: Order,
    *,
    customer: Customer,
    payment_method: str,
) -> None:
    if order.customer_id != customer.pk:
        raise IdempotencyConflictError(
            "This idempotency key belongs to another customer."
        )

    if order.payment_method != payment_method:
        raise IdempotencyConflictError(
            "The same idempotency key cannot be reused with a different "
            "payment method."
        )


def _create_payment_record(
    *,
    order: Order,
) -> Payment:
    """
    Creates the initial payment record.

    Gateway-specific IDs/signatures are intentionally NOT accepted from the
    frontend here. They belong to the payment gateway initiation/verification
    layer.
    """
    if order.payment_method == Order.PaymentMethod.COD:
        initial_status = Payment.Status.PENDING
        provider = "cod"
    elif order.payment_method == Order.PaymentMethod.CREDIT:
        initial_status = Payment.Status.PENDING
        provider = "credit_terms"
    else:
        initial_status = Payment.Status.CREATED
        provider = "razorpay"

    return Payment.objects.create(
        order=order,
        provider=provider,
        status=initial_status,
        amount=order.grand_total,
        currency=order.currency,
    )


def _reserve_inventory(
    *,
    order: Order,
    locked_variants: dict[int, ProductVariant],
    cart_items: Iterable[CartItem],
) -> list[InventoryReservation]:
    """
    Reserve stock inside the same database transaction as order creation.

    Since variants are already select_for_update() locked, no competing order
    can mutate the same stock row until this transaction commits/rolls back.
    """
    reservations: list[
        InventoryReservation
    ] = []

    for cart_item in cart_items:
        variant = locked_variants[
            cart_item.variant_id
        ]

        quantity = int(
            cart_item.quantity
        )

        _validate_variant_for_checkout(
            variant,
            quantity,
        )

        reservations.append(
            InventoryReservation.objects.create(
                order=order,
                variant=variant,
                quantity=quantity,
                status=InventoryReservation.Status.ACTIVE,
                expires_at=timezone.now()
                + timedelta(minutes=15),
            )
        )

        # Reserve, but do not permanently consume, stock yet.
        variant.stock = (
            variant.stock - quantity
        )
        variant.save(
            update_fields=[
                "stock",
                "updated_at",
            ]
        )

    return reservations


# ============================================================================
# PUBLIC ORDER CREATION SERVICE
# ============================================================================


@transaction.atomic
def create_order_from_customer_cart(
    *,
    customer: Customer,
    idempotency_key: str,
    payment_method: str,
    shipping: dict,
    delivery_quote: DeliveryQuote,
    tax_resolver: TaxResolver,
    currency: str = "INR",
    notes: str = "",
) -> Order:
    """
    Production order-creation transaction.

    Guarantees:

    1. Only a real active Customer can call it.
    2. Customer's cart row is locked.
    3. Cart items are locked.
    4. ProductVariant rows are locked in stable order.
    5. Current server-side price is used.
    6. Current stock is validated.
    7. MOQ is validated.
    8. Delivery quote is server-generated and revalidated.
    9. Order / item / payment / reservation are created atomically.
    10. Cart is deleted only after all database operations succeed.
    11. DB unique idempotency key prevents duplicate order creation.
    """
    _ensure_customer(
        customer
    )

    idempotency_key = _normalize_idempotency_key(
        idempotency_key
    )

    payment_method = str(
        payment_method or ""
    ).strip()

    allowed_payment_methods = {
        choice
        for choice, _label
        in Order.PaymentMethod.choices
    }

    if (
        payment_method
        not in allowed_payment_methods
    ):
        raise InvalidOrderError(
            "Unsupported payment method."
        )

    _validate_shipping_snapshot(
        shipping
    )

    _validate_delivery_quote(
        delivery_quote
    )

    currency = (
        str(currency or "INR")
        .strip()
        .upper()
    )

    if len(currency) != 3:
        raise InvalidOrderError(
            "Invalid currency."
        )

    # ------------------------------------------------------------------
    # IDEMPOTENCY: fast replay path
    # ------------------------------------------------------------------

    existing = _find_existing_idempotent_order(
        customer,
        idempotency_key,
    )

    if existing:
        _assert_idempotent_replay_compatible(
            existing,
            customer=customer,
            payment_method=payment_method,
        )
        return existing

    # ------------------------------------------------------------------
    # LOCK CUSTOMER CART
    # ------------------------------------------------------------------

    cart = _resolve_customer_cart(
        customer
    )

    cart_items = _load_locked_cart_items(
        cart
    )

    # ------------------------------------------------------------------
    # LOCK ALL PRODUCT VARIANTS IN DETERMINISTIC ORDER
    # ------------------------------------------------------------------

    locked_variants = (
        _lock_variants_in_stable_order(
            cart_items
        )
    )

    # ------------------------------------------------------------------
    # VALIDATE + CALCULATE FROM SERVER STATE
    # ------------------------------------------------------------------

    subtotal = Decimal("0.00")
    tax_amount_total = Decimal("0.00")

    line_calculations: list[
        dict
    ] = []

    for cart_item in cart_items:
        variant = locked_variants[
            cart_item.variant_id
        ]

        quantity = int(
            cart_item.quantity
        )

        _validate_variant_for_checkout(
            variant,
            quantity,
        )

        (
            unit_price,
            tax_rate,
            item_tax,
            line_total,
        ) = _calculate_line_values(
            variant=variant,
            quantity=quantity,
            tax_resolver=tax_resolver,
        )

        item_subtotal = money(
            unit_price * quantity
        )

        subtotal = money(
            subtotal + item_subtotal
        )

        tax_amount_total = money(
            tax_amount_total
            + item_tax
        )

        line_calculations.append(
            {
                "cart_item": cart_item,
                "variant": variant,
                "quantity": quantity,
                "unit_price": unit_price,
                "tax_rate": tax_rate,
                "tax_amount": item_tax,
                "line_total": line_total,
                "item_subtotal": item_subtotal,
            }
        )

    # ------------------------------------------------------------------
    # DELIVERY — SERVER AUTHORITATIVE
    # ------------------------------------------------------------------

    delivery_charge = money(
        delivery_quote.charge
    )

    # Discount is deliberately zero here because no authoritative discount
    # engine has been provided yet. Once introduced, it belongs here on the
    # backend and MUST NOT come from the frontend.
    discount_amount = Decimal(
        "0.00"
    )

    # ------------------------------------------------------------------
    # PAYMENT-METHOD BUSINESS RULES
    # ------------------------------------------------------------------

    if (
        payment_method
        == Order.PaymentMethod.COD
    ):
        # Current checkout UX exposes this fee. Keep the actual amount on
        # the server and change it later to a configured business setting.
        cod_fee = Decimal("99.00")
    else:
        cod_fee = Decimal("0.00")

    grand_total = money(
        subtotal
        + tax_amount_total
        + delivery_charge
        + cod_fee
        - discount_amount
    )

    if grand_total < 0:
        raise InvalidOrderError(
            "Calculated order total is invalid."
        )

    # ------------------------------------------------------------------
    # CREATE ORDER
    # ------------------------------------------------------------------

    order_status = (
        Order.Status.CONFIRMED
        if payment_method
        in {
            Order.PaymentMethod.COD,
            Order.PaymentMethod.CREDIT,
        }
        else Order.Status.PENDING_PAYMENT
    )

    order = Order.objects.create(
        order_number=_public_order_number(),
        customer=customer,
        idempotency_key=idempotency_key,
        status=order_status,
        payment_status=Order.PaymentStatus.PENDING,
        payment_method=payment_method,
        currency=currency,
        subtotal=money(subtotal),
        discount_amount=money(
            discount_amount
        ),
        delivery_charge=money(
            delivery_charge
        ),
        tax_amount=money(
            tax_amount_total
        ),
        cod_fee=money(cod_fee),
        grand_total=money(
            grand_total
        ),
        shipping_full_name=str(
            shipping["full_name"]
        ).strip(),
        shipping_phone=str(
            shipping["phone"]
        ).strip(),
        shipping_email=str(
            shipping["email"]
        ).strip().lower(),
        shipping_company=str(
            shipping.get("company", "")
        ).strip(),
        shipping_gstin=str(
            shipping.get("gstin", "")
        ).strip().upper(),
        shipping_address_line1=str(
            shipping["address_line1"]
        ).strip(),
        shipping_address_line2=str(
            shipping.get("address_line2", "")
        ).strip(),
        shipping_landmark=str(
            shipping.get("landmark", "")
        ).strip(),
        shipping_city=str(
            shipping["city"]
        ).strip(),
        shipping_state=str(
            shipping["state"]
        ).strip(),
        shipping_pincode=str(
            shipping["pincode"]
        ).strip(),
        delivery_zone_id=delivery_quote.zone_id,
        delivery_zone_name=delivery_quote.zone_name,
        delivery_breakdown=delivery_quote.breakdown,
        notes=str(
            notes or ""
        ).strip(),
    )

    # ------------------------------------------------------------------
    # CREATE IMMUTABLE ORDER ITEM SNAPSHOTS
    # ------------------------------------------------------------------

    for calculation in line_calculations:
        variant = calculation[
            "variant"
        ]

        OrderItem.objects.create(
            order=order,
            variant=variant,
            product_name=variant.product.name,
            sku=variant.sku,
            variant_name=_build_snapshot_name(
                variant
            ),
            quantity=calculation[
                "quantity"
            ],
            unit_price=calculation[
                "unit_price"
            ],
            tax_rate=calculation[
                "tax_rate"
            ],
            tax_amount=calculation[
                "tax_amount"
            ],
            discount_amount=Decimal(
                "0.00"
            ),
            line_total=calculation[
                "line_total"
            ],
            currency=currency,
            weight=money(
                variant.weight or 0
            ),
        )

    # ------------------------------------------------------------------
    # RESERVE INVENTORY
    # ------------------------------------------------------------------

    _reserve_inventory(
        order=order,
        locked_variants=locked_variants,
        cart_items=cart_items,
    )

    # ------------------------------------------------------------------
    # PAYMENT RECORD
    # ------------------------------------------------------------------

    _create_payment_record(
        order=order
    )

    # ------------------------------------------------------------------
    # CART CONSUMPTION
    #
    # We delete the customer's cart AFTER all order-side records have been
    # created. Because this entire function is atomic, any exception rolls
    # the complete operation back and leaves the cart intact.
    # ------------------------------------------------------------------

    cart.delete()

    return order


# ============================================================================
# IDempotency-safe public wrapper
# ============================================================================


def create_order_safely(
    *,
    customer: Customer,
    idempotency_key: str,
    payment_method: str,
    shipping: dict,
    delivery_quote: DeliveryQuote,
    tax_resolver: TaxResolver,
    currency: str = "INR",
    notes: str = "",
) -> Order:
    """
    Retry-safe entry point.

    PostgreSQL enforces Order.idempotency_key UNIQUE. In the rare case where
    two identical first-attempt requests race before either sees the other,
    the losing transaction may receive IntegrityError. We then fetch the
    canonical order and return it after verifying ownership/compatibility.
    """
    normalized_key = _normalize_idempotency_key(
        idempotency_key
    )

    try:
        return create_order_from_customer_cart(
            customer=customer,
            idempotency_key=normalized_key,
            payment_method=payment_method,
            shipping=shipping,
            delivery_quote=delivery_quote,
            tax_resolver=tax_resolver,
            currency=currency,
            notes=notes,
        )
    except IntegrityError:
        existing = (
            Order.objects
            .select_related("customer")
            .filter(
                idempotency_key=normalized_key
            )
            .first()
        )

        if not existing:
            raise

        _assert_idempotent_replay_compatible(
            existing,
            customer=customer,
            payment_method=payment_method,
        )

        return existing




# ...existing imports already present...
@transaction.atomic
def release_expired_inventory_reservations(
    *,
    batch_size: int = 100,
) -> int:
    """
    Release expired ACTIVE inventory reservations safely.

    Lifecycle:

        ACTIVE
          ↓ expiry
        EXPIRED
          ↓
        stock returned

    Once an unpaid order has no ACTIVE reservations remaining, the order
    is cancelled and its payment state becomes FAILED.

    The operation is idempotent and safe to run repeatedly.
    """

    if batch_size < 1:
        batch_size = 100

    now = timezone.now()

    reservations = list(
        InventoryReservation.objects
        .select_for_update(
            skip_locked=True,
        )
        .filter(
            status=InventoryReservation.Status.ACTIVE,
            expires_at__lte=now,
        )
        .order_by("id")[:batch_size]
    )

    released_count = 0
    affected_order_ids: set[int] = set()

    for reservation in reservations:
        affected_order_ids.add(reservation.order_id)

        variant = (
            ProductVariant.objects
            .select_for_update()
            .get(pk=reservation.variant_id)
        )

        variant.stock = (
            F("stock") + reservation.quantity
        )

        variant.save(
            update_fields=[
                "stock",
                "updated_at",
            ]
        )

        reservation.status = (
            InventoryReservation.Status.EXPIRED
        )
        reservation.released_at = now

        reservation.save(
            update_fields=[
                "status",
                "released_at",
            ]
        )

        released_count += 1

    # --------------------------------------------------------------
    # Cancel unpaid orders whose inventory window is now exhausted.
    # --------------------------------------------------------------

    for order_id in affected_order_ids:
        order = (
            Order.objects
            .select_for_update()
            .get(pk=order_id)
        )

        # A settled order must never be cancelled by the expiry worker.
        if order.payment_status in {
            Order.PaymentStatus.PAID,
            Order.PaymentStatus.REFUNDED,
            Order.PaymentStatus.PARTIALLY_REFUNDED,
        }:
            continue

        if order.status in {
            Order.Status.CANCELLED,
            Order.Status.FAILED,
            Order.Status.DELIVERED,
        }:
            continue

        has_active_reservations = (
            InventoryReservation.objects
            .filter(
                order_id=order.id,
                status=InventoryReservation.Status.ACTIVE,
            )
            .exists()
        )

        if has_active_reservations:
            continue

        order.status = Order.Status.CANCELLED
        order.payment_status = Order.PaymentStatus.FAILED

        order.cancelled_at = now
        Payment.objects.filter(
            order=order,
            provider="razorpay",
        ).exclude(
            status__in=[
                Payment.Status.CAPTURED,
                Payment.Status.REFUNDED,
            ]
        ).update(
            status=Payment.Status.CANCELLED,
            updated_at=now,
        )
        order.save(
            update_fields=[
                "status",
                "payment_status",
                "cancelled_at",
                "updated_at",
            ]
        )

    return released_count