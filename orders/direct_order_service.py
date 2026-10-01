from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from uuid import uuid4

from django.db import transaction

from accounts.models import Customer

from .models import Order, OrderItem, Payment


MONEY_QUANTUM = Decimal("0.01")


def money(value: Decimal | int | str) -> Decimal:
    """
    Normalize commercial values to 2 decimal places.
    """
    return Decimal(value).quantize(
        MONEY_QUANTUM,
        rounding=ROUND_HALF_UP,
    )


class DirectOrderError(Exception):
    """Base exception for Direct Order failures."""


class DirectOrderValidationError(DirectOrderError):
    pass


class DirectOrderIdempotencyConflict(DirectOrderError):
    pass


def _text(value: object) -> str:
    return str(value or "").strip()


def _decimal(
    value: object,
    *,
    field: str,
) -> Decimal:
    try:
        result = money(value)
    except Exception as exc:
        raise DirectOrderValidationError(
            f"Invalid value for {field}."
        ) from exc

    if result < Decimal("0.00"):
        raise DirectOrderValidationError(
            f"{field} cannot be negative."
        )

    return result


def _positive_int(
    value: object,
    *,
    field: str,
) -> int:
    try:
        result = int(value)
    except Exception as exc:
        raise DirectOrderValidationError(
            f"Invalid value for {field}."
        ) from exc

    if result < 1:
        raise DirectOrderValidationError(
            f"{field} must be at least 1."
        )

    return result


def _normalize_idempotency_key(value: object) -> str:
    key = _text(value)

    if not key:
        raise DirectOrderValidationError(
            "Idempotency key is required."
        )

    if len(key) > 128:
        raise DirectOrderValidationError(
            "Idempotency key is too long."
        )

    return key


def _validate_shipping(shipping: dict) -> None:
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
        if not _text(shipping.get(field))
    ]

    if missing:
        raise DirectOrderValidationError(
            "Missing required shipping fields: "
            + ", ".join(missing)
        )

    pincode = _text(shipping["pincode"])

    if len(pincode) != 6 or not pincode.isdigit():
        raise DirectOrderValidationError(
            "Enter a valid 6-digit pincode."
        )


def _get_or_create_customer(
    *,
    customer: dict,
) -> Customer:
    full_name = _text(customer.get("full_name"))
    email = _text(customer.get("email")).lower()
    phone = _text(customer.get("phone"))

    if not full_name:
        raise DirectOrderValidationError(
            "Customer name is required."
        )

    if not email and not phone:
        raise DirectOrderValidationError(
            "Customer email or phone is required."
        )

    existing_by_phone = None
    existing_by_email = None

    if phone:
        existing_by_phone = (
            Customer.objects
            .filter(phone=phone)
            .first()
        )

    if email:
        existing_by_email = (
            Customer.objects
            .filter(email=email)
            .first()
        )

    # Same two identifiers pointing to different customers
    if (
        existing_by_phone
        and existing_by_email
        and existing_by_phone.pk
        != existing_by_email.pk
    ):
        raise DirectOrderValidationError(
            "Phone and email belong to different customers."
        )

    existing = (
        existing_by_phone
        or existing_by_email
    )

    if existing:
        if not existing.is_active:
            raise DirectOrderValidationError(
                "This customer account is inactive."
            )

        update_fields = []

        if not existing.full_name and full_name:
            existing.full_name = full_name
            update_fields.append("full_name")

        if not existing.email and email:
            existing.email = email
            update_fields.append("email")

        if not existing.phone and phone:
            existing.phone = phone
            update_fields.append("phone")

        if update_fields:
            existing.save(
                update_fields=[
                    *update_fields,
                    "updated_at",
                ]
            )

        return existing

    return Customer.objects.create(
        full_name=full_name,
        email=email or None,
        phone=phone or None,
        is_active=True,
    )


def _calculate_item(
    *,
    item: dict,
    currency: str,
) -> dict:
    product_name = _text(
        item.get("product_name")
    )

    if not product_name:
        raise DirectOrderValidationError(
            "Product name is required."
        )

    quantity = _positive_int(
        item.get("quantity"),
        field="quantity",
    )

    rate = _decimal(
        item.get("rate"),
        field="rate",
    )

    discount_percent = _decimal(
        item.get("discount_percent", "0"),
        field="discount_percent",
    )

    tax_rate = _decimal(
        item.get("tax_rate", "18"),
        field="tax_rate",
    )

    if discount_percent > Decimal("100.00"):
        raise DirectOrderValidationError(
            "Discount percentage cannot exceed 100%."
        )

    if tax_rate > Decimal("100.00"):
        raise DirectOrderValidationError(
            "Tax rate cannot exceed 100%."
        )

    gross_amount = money(
        rate * quantity
    )

    discount_amount = money(
        gross_amount
        * discount_percent
        / Decimal("100")
    )

    taxable_amount = money(
        gross_amount - discount_amount
    )

    tax_amount = money(
        taxable_amount
        * tax_rate
        / Decimal("100")
    )

    line_total = money(
        taxable_amount + tax_amount
    )

    sku = _text(item.get("sku"))

    if not sku:
        sku = (
            f"DIRECT-{uuid4().hex[:10].upper()}"
        )

    return {
        "product_name": product_name,
        "sku": sku,
        "variant_name": _text(
            item.get("variant_name")
        ),
        "quantity": quantity,
        "unit_price": rate,
        "tax_rate": tax_rate,
        "tax_amount": tax_amount,
        "discount_amount": discount_amount,
        "line_total": line_total,
        "currency": currency,
        "weight": Decimal("0.00"),
        "gross_amount": gross_amount,
        "taxable_amount": taxable_amount,
    }


def _find_existing_order(
    *,
    idempotency_key: str,
) -> Order | None:
    return (
        Order.objects
        .select_related("customer")
        .filter(
            idempotency_key=idempotency_key,
        )
        .first()
    )


@transaction.atomic
def create_direct_order(
    *,
    idempotency_key: str,
    customer_data: dict,
    shipping: dict,
    items: list[dict],
    delivery_charge: object = "0.00",
    currency: str = "INR",
    notes: str = "",
) -> Order:
    """
    Create a normal BlazeLine Order from the Admin Direct Order Builder.

    Direct items are stored as OrderItem snapshots with variant=None.

    No inventory reservation is created because these products are
    not necessarily catalog products.
    """

    idempotency_key = _normalize_idempotency_key(
        idempotency_key
    )

    currency = _text(currency or "INR").upper()

    if len(currency) != 3:
        raise DirectOrderValidationError(
            "Invalid currency."
        )

    if not items:
        raise DirectOrderValidationError(
            "At least one item is required."
        )

    _validate_shipping(shipping)

    existing = _find_existing_order(
        idempotency_key=idempotency_key,
    )

    if existing:
        if existing.source != Order.Source.DIRECT:
            raise DirectOrderIdempotencyConflict(
                "This idempotency key is already used "
                "by a different order type."
            )

        return existing

    customer = _get_or_create_customer(
        customer=customer_data,
    )

    calculated_items = [
        _calculate_item(
            item=item,
            currency=currency,
        )
        for item in items
    ]

    subtotal = money(
        sum(
            (
                item["gross_amount"]
                for item in calculated_items
            ),
            Decimal("0.00"),
        )
    )

    discount_amount = money(
        sum(
            (
                item["discount_amount"]
                for item in calculated_items
            ),
            Decimal("0.00"),
        )
    )

    tax_amount = money(
        sum(
            (
                item["tax_amount"]
                for item in calculated_items
            ),
            Decimal("0.00"),
        )
    )

    delivery = _decimal(
        delivery_charge,
        field="delivery_charge",
    )

    grand_total = money(
        subtotal
        - discount_amount
        + tax_amount
        + delivery
    )

    if grand_total < Decimal("0.00"):
        raise DirectOrderValidationError(
            "Calculated order total is invalid."
        )

    order = Order.objects.create(
        customer=customer,
        idempotency_key=idempotency_key,
        source=Order.Source.DIRECT,
        status=Order.Status.PENDING_PAYMENT,
        payment_status=Order.PaymentStatus.PENDING,
        payment_method=Order.PaymentMethod.PAYMENT_LINK,
        currency=currency,
        subtotal=subtotal,
        discount_amount=discount_amount,
        promotion_snapshot={
            "source": "direct_order",
        },
        delivery_charge=delivery,
        tax_amount=tax_amount,
        cod_fee=Decimal("0.00"),
        grand_total=grand_total,
        shipping_full_name=_text(
            shipping["full_name"]
        ),
        shipping_phone=_text(
            shipping["phone"]
        ),
        shipping_email=_text(
            shipping["email"]
        ),
        shipping_company=_text(
            shipping.get("company")
        ),
        shipping_gstin=_text(
            shipping.get("gstin")
        ),
        shipping_address_line1=_text(
            shipping["address_line1"]
        ),
        shipping_address_line2=_text(
            shipping.get("address_line2")
        ),
        shipping_landmark=_text(
            shipping.get("landmark")
        ),
        shipping_city=_text(
            shipping["city"]
        ),
        shipping_state=_text(
            shipping["state"]
        ),
        shipping_pincode=_text(
            shipping["pincode"]
        ),
        delivery_zone_id=None,
        delivery_zone_name="",
        delivery_breakdown=[],
        notes=_text(notes),
    )

    for item in calculated_items:
        OrderItem.objects.create(
            order=order,
            variant=None,
            product_name=item["product_name"],
            sku=item["sku"],
            variant_name=item["variant_name"],
            quantity=item["quantity"],
            unit_price=item["unit_price"],
            tax_rate=item["tax_rate"],
            tax_amount=item["tax_amount"],
            discount_amount=item["discount_amount"],
            line_total=item["line_total"],
            currency=item["currency"],
            weight=item["weight"],
        )

    Payment.objects.create(
        order=order,
        provider="razorpay",
        status=Payment.Status.CREATED,
        amount=grand_total,
        currency=currency,
        raw_metadata={
            "source": "direct_order",
        },
    )

    return order