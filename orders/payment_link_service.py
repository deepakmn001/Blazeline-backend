from __future__ import annotations

import logging
import os
from decimal import Decimal, InvalidOperation

import razorpay
from django.db import transaction
from django.utils import timezone

from .models import Order, Payment, PaymentLink


logger = logging.getLogger(__name__)


# ============================================================================
# CONFIG
# ============================================================================

RAZORPAY_KEY_ID = os.getenv(
    "RAZORPAY_KEY_ID",
    "",
).strip()

RAZORPAY_KEY_SECRET = os.getenv(
    "RAZORPAY_KEY_SECRET",
    "",
).strip()


# ============================================================================
# ERRORS
# ============================================================================


class PaymentLinkServiceError(Exception):
    """Base class for Payment Link failures."""


class PaymentLinkConfigurationError(
    PaymentLinkServiceError
):
    pass


class PaymentLinkValidationError(
    PaymentLinkServiceError
):
    pass


class PaymentLinkStateError(
    PaymentLinkServiceError
):
    pass


class PaymentLinkGatewayError(
    PaymentLinkServiceError
):
    pass


# ============================================================================
# RAZORPAY CLIENT
# ============================================================================


def _get_client() -> razorpay.Client:
    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        raise PaymentLinkConfigurationError(
            "Razorpay credentials are not configured."
        )

    return razorpay.Client(
        auth=(
            RAZORPAY_KEY_ID,
            RAZORPAY_KEY_SECRET,
        )
    )


# ============================================================================
# MONEY
# ============================================================================


def _money_to_paise(
    amount: Decimal,
) -> int:
    try:
        normalized = Decimal(amount).quantize(
            Decimal("0.01")
        )
    except (
        InvalidOperation,
        TypeError,
        ValueError,
    ) as exc:
        raise PaymentLinkValidationError(
            "Invalid payment amount."
        ) from exc

    if normalized <= Decimal("0.00"):
        raise PaymentLinkValidationError(
            "Payment amount must be greater than zero."
        )

    paise = normalized * Decimal("100")

    if paise != paise.to_integral_value():
        raise PaymentLinkValidationError(
            "Payment amount has invalid precision."
        )

    return int(paise)


# ============================================================================
# CREATE PAYMENT LINK
# ============================================================================


@transaction.atomic
def create_razorpay_payment_link(
    *,
    order: Order,
) -> PaymentLink:
    """
    Create or reuse an active Razorpay Payment Link for a Direct Order.

    The order amount stored in the database is the only amount used.
    Frontend-supplied totals are never trusted here.
    """

    if order.source != Order.Source.DIRECT:
        raise PaymentLinkStateError(
            "Payment Links can only be created for Direct Orders."
        )

    if order.payment_method != (
        Order.PaymentMethod.PAYMENT_LINK
    ):
        raise PaymentLinkStateError(
            "This order is not configured for Payment Link payment."
        )

    if order.currency.upper() != "INR":
        raise PaymentLinkValidationError(
            "Direct Order Payment Links currently support INR only."
        )

    if order.status in {
        Order.Status.CANCELLED,
        Order.Status.FAILED,
        Order.Status.DELIVERED,
    }:
        raise PaymentLinkStateError(
            "Payment Link cannot be created for this order."
        )

    if order.payment_status in {
        Order.PaymentStatus.PAID,
        Order.PaymentStatus.REFUNDED,
        Order.PaymentStatus.PARTIALLY_REFUNDED,
    }:
        raise PaymentLinkStateError(
            "This order has already been paid or refunded."
        )

    # Reuse an already-active link instead of creating duplicates.
    existing_link = (
        PaymentLink.objects
        .select_for_update()
        .filter(
            payment__order=order,
            provider="razorpay",
            status__in=[
                PaymentLink.Status.CREATED,
                PaymentLink.Status.ACTIVE,
            ],
        )
        .order_by("-created_at")
        .first()
    )

    if existing_link:
        return existing_link

    payment = (
        Payment.objects
        .select_for_update()
        .filter(
            order=order,
            provider="razorpay",
        )
        .exclude(
            status__in=[
                Payment.Status.FAILED,
                Payment.Status.CANCELLED,
                Payment.Status.REFUNDED,
            ]
        )
        .order_by("-created_at")
        .first()
    )

    if payment is None:
        payment = Payment.objects.create(
            order=order,
            provider="razorpay",
            status=Payment.Status.CREATED,
            amount=order.grand_total,
            currency=order.currency,
            raw_metadata={
                "source": "direct_order",
            },
        )

    # Payment amount must always equal the current order total.
    if payment.amount != order.grand_total:
        payment.amount = order.grand_total
        payment.currency = order.currency
        payment.save(
            update_fields=[
                "amount",
                "currency",
                "updated_at",
            ]
        )

    amount_paise = _money_to_paise(
        order.grand_total
    )

    customer_name = (
        order.shipping_full_name.strip()
    )

    customer_email = (
        order.shipping_email.strip()
    )

    customer_contact = (
        order.shipping_phone.strip()
    )

    reference_id = order.order_number

    description = (
        f"BlazeLine Order {order.order_number}"
    )

    payload = {
        "amount": amount_paise,
        "currency": order.currency.upper(),
        "accept_partial": False,
        "description": description,
        "reference_id": reference_id,
        "customer": {
            "name": customer_name,
            "email": customer_email,
            "contact": customer_contact,
        },
        # Admin will share the generated link manually.
        "notify": {
            "sms": False,
            "email": False,
        },
        "reminder_enable": False,
        "notes": {
            "order_number": order.order_number,
            "order_source": Order.Source.DIRECT,
            "blazeline_payment_id": str(
                payment.id
            ),
        },
    }

    client = _get_client()

    try:
        response = client.payment_link.create(
            payload
        )
    except Exception as exc:
        logger.exception(
            "Razorpay Payment Link creation failed",
            extra={
                "order_number": order.order_number,
            },
        )
        raise PaymentLinkGatewayError(
            "Unable to create Razorpay Payment Link."
        ) from exc

    if not isinstance(response, dict):
        raise PaymentLinkGatewayError(
            "Razorpay returned an invalid Payment Link response."
        )

    provider_link_id = str(
        response.get("id") or ""
    ).strip()

    short_url = str(
        response.get("short_url") or ""
    ).strip()

    if not provider_link_id:
        raise PaymentLinkGatewayError(
            "Razorpay response did not contain a Payment Link ID."
        )

    if not short_url:
        raise PaymentLinkGatewayError(
            "Razorpay response did not contain a Payment Link URL."
        )

    response_currency = str(
        response.get("currency")
        or order.currency
    ).upper()

    if response_currency != order.currency.upper():
        raise PaymentLinkGatewayError(
            "Payment Link currency does not match the order."
        )

    response_amount = response.get(
        "amount"
    )

    if response_amount is not None:
        try:
            if int(response_amount) != amount_paise:
                raise PaymentLinkGatewayError(
                    "Payment Link amount does not match the order."
                )
        except (
            TypeError,
            ValueError,
        ) as exc:
            raise PaymentLinkGatewayError(
                "Razorpay returned an invalid Payment Link amount."
            ) from exc

    response_status = str(
        response.get("status")
        or PaymentLink.Status.CREATED
    ).strip().lower()

    status_map = {
        "created": PaymentLink.Status.CREATED,
        "active": PaymentLink.Status.ACTIVE,
        "paid": PaymentLink.Status.PAID,
        "expired": PaymentLink.Status.EXPIRED,
        "cancelled": PaymentLink.Status.CANCELLED,
    }

    local_status = status_map.get(
        response_status,
        PaymentLink.Status.CREATED,
    )

    expire_by = response.get(
        "expire_by"
    )

    expires_at = None

    if expire_by:
        try:
            expires_at = timezone.datetime.fromtimestamp(
                int(expire_by),
                tz=timezone.get_current_timezone(),
            )
        except (
            TypeError,
            ValueError,
            OverflowError,
        ):
            expires_at = None

    payment_link = PaymentLink.objects.create(
        payment=payment,
        provider="razorpay",
        provider_link_id=provider_link_id,
        reference_id=reference_id,
        short_url=short_url,
        amount=order.grand_total,
        currency=order.currency.upper(),
        status=local_status,
        expires_at=expires_at,
        raw_metadata=response,
    )

    return payment_link