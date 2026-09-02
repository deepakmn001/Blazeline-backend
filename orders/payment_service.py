from __future__ import annotations

import hashlib
import hmac
import logging
import os
from decimal import Decimal, InvalidOperation

import razorpay
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import (
    InventoryReservation,
    Order,
    Payment,
    PaymentEvent,
)


logger = logging.getLogger(__name__)


# ============================================================================
# CONFIG
# ============================================================================

RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "").strip()
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "").strip()
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "").strip()


# ============================================================================
# DOMAIN ERRORS
# ============================================================================


class PaymentServiceError(Exception):
    """Base class for expected payment-domain failures."""


class PaymentConfigurationError(PaymentServiceError):
    pass


class PaymentValidationError(PaymentServiceError):
    pass


class PaymentOwnershipError(PaymentServiceError):
    pass


class PaymentStateError(PaymentServiceError):
    pass


class PaymentGatewayError(PaymentServiceError):
    pass


class PaymentSignatureError(PaymentServiceError):
    pass


class PaymentWebhookError(PaymentServiceError):
    pass


# ============================================================================
# RAZORPAY CLIENT
# ============================================================================


def _get_client() -> razorpay.Client:
    """
    Lazily create the gateway client.

    Keeping client construction lazy means importing Django modules doesn't
    require payment credentials to exist in local development/test contexts.
    """
    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        raise PaymentConfigurationError(
            "Razorpay credentials are not configured."
        )

    return razorpay.Client(
        auth=(
            RAZORPAY_KEY_ID,
            RAZORPAY_KEY_SECRET,
        )
    )


def _fetch_gateway_payment(
    *,
    provider_payment_id: str,
) -> dict:
    """Fetch a payment directly from Razorpay for server-side reconciliation."""
    provider_payment_id = str(provider_payment_id or "").strip()
    if not provider_payment_id:
        raise PaymentValidationError("Missing gateway payment ID.")

    client = _get_client()
    try:
        gateway_payment = client.payment.fetch(provider_payment_id)
    except Exception as exc:
        logger.exception(
            "Razorpay payment fetch failed",
            extra={"provider_payment_id": provider_payment_id},
        )
        raise PaymentGatewayError(
            "Unable to verify payment with the gateway."
        ) from exc

    if not isinstance(gateway_payment, dict):
        raise PaymentGatewayError(
            "Payment gateway returned an invalid response."
        )

    return gateway_payment


# ============================================================================
# MONEY
# ============================================================================


def _money_to_paise(amount: Decimal) -> int:
    """
    Convert INR amount to the integer subunit expected by Razorpay.

    Never send Decimal/float directly to the gateway.
    """
    try:
        normalized = Decimal(amount).quantize(
            Decimal("0.01")
        )
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise PaymentValidationError(
            "Invalid payment amount."
        ) from exc

    if normalized < 0:
        raise PaymentValidationError(
            "Payment amount cannot be negative."
        )

    # Razorpay amount is an integer number of currency subunits.
    paise = normalized * Decimal("100")

    if paise != paise.to_integral_value():
        raise PaymentValidationError(
            "Payment amount has invalid precision."
        )

    value = int(paise)

    if value <= 0:
        raise PaymentValidationError(
            "Payment amount must be greater than zero."
        )

    return value


# ============================================================================
# ORDER / PAYMENT VALIDATION
# ============================================================================


def _get_owned_order(
    *,
    order_number: str,
    customer,
) -> Order:
    """
    Resolve order strictly through the authenticated customer.

    Never fetch a public order by order_number alone for payment creation.
    """
    order = (
        Order.objects
        .select_for_update()
        .filter(
            order_number=order_number,
            customer=customer,
        )
        .first()
    )

    if not order:
        raise PaymentOwnershipError(
            "Order not found."
        )

    return order


def _get_or_create_gateway_payment(
    *,
    order: Order,
) -> Payment:
    """
    Return the latest non-terminal Razorpay payment for this order.

    Multiple attempts can legitimately exist, but an already-live gateway
    order should be reused instead of blindly creating another one.
    """
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

    if payment:
        return payment

    return Payment.objects.create(
        order=order,
        provider="razorpay",
        status=Payment.Status.CREATED,
        amount=order.grand_total,
        currency=order.currency,
    )


def _assert_online_payment_allowed(
    order: Order,
) -> None:
    if order.payment_method not in {
        Order.PaymentMethod.UPI,
        Order.PaymentMethod.CARD,
        Order.PaymentMethod.NETBANKING,
    }:
        raise PaymentStateError(
            "This order does not require an online Razorpay payment."
        )

    if order.status in {
        Order.Status.CANCELLED,
        Order.Status.FAILED,
        Order.Status.DELIVERED,
    }:
        raise PaymentStateError(
            "Payment cannot be initiated for this order."
        )

    if order.payment_status in {
        Order.PaymentStatus.PAID,
        Order.PaymentStatus.REFUNDED,
        Order.PaymentStatus.PARTIALLY_REFUNDED,
    }:
        raise PaymentStateError(
            "This order has already been paid."
        )

    if order.grand_total <= 0:
        raise PaymentValidationError(
            "Order amount must be greater than zero."
        )


# ============================================================================
# CREATE RAZORPAY ORDER
# ============================================================================


def _payment_initialization_is_active(payment: Payment) -> bool:
    """
    Return True when another request currently owns the short-lived gateway
    order creation lease for this payment attempt.

    The lease lives in raw_metadata rather than requiring a database transaction
    to remain open while the Razorpay network call is in flight.
    """
    metadata = payment.raw_metadata if isinstance(payment.raw_metadata, dict) else {}
    lease = metadata.get("gateway_order_creation")
    if not isinstance(lease, dict):
        return False

    if lease.get("status") != "in_progress":
        return False

    started_at_raw = str(lease.get("started_at") or "").strip()
    if not started_at_raw:
        return False

    from datetime import datetime

    try:
        started_at = datetime.fromisoformat(
            started_at_raw.replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        return False

    if timezone.is_naive(started_at):
        started_at = timezone.make_aware(
            started_at,
            timezone.get_current_timezone(),
        )

    # A crashed worker must not permanently block payment initialization.
    return (timezone.now() - started_at).total_seconds() < 120


def _mark_payment_initialization_started(payment: Payment) -> None:
    metadata = payment.raw_metadata if isinstance(payment.raw_metadata, dict) else {}
    payment.raw_metadata = {
        **metadata,
        "gateway_order_creation": {
            "status": "in_progress",
            "started_at": timezone.now().isoformat(),
        },
    }
    payment.save(update_fields=["raw_metadata", "updated_at"])


def _clear_payment_initialization_lease(payment: Payment) -> None:
    metadata = payment.raw_metadata if isinstance(payment.raw_metadata, dict) else {}
    gateway_creation = metadata.get("gateway_order_creation")
    if not isinstance(gateway_creation, dict):
        return

    cleaned = dict(metadata)
    cleaned.pop("gateway_order_creation", None)
    payment.raw_metadata = cleaned
    payment.save(update_fields=["raw_metadata", "updated_at"])


def _build_razorpay_payment_response(
    *,
    order: Order,
    payment: Payment,
) -> dict:
    return {
        "order_number": order.order_number,
        "payment_id": payment.id,
        "razorpay_key_id": RAZORPAY_KEY_ID,
        "razorpay_order_id": payment.provider_order_id,
        "amount": _money_to_paise(payment.amount),
        "currency": payment.currency,
    }


def create_razorpay_order_for_customer(
    *,
    customer,
    order_number: str,
) -> dict:
    """
    Create/reuse the Razorpay Order for an existing BlazeLine order.

    Production invariants:
    - BlazeLine's order amount/currency remain authoritative.
    - Customer ownership is checked under a DB row lock.
    - No DB transaction remains open during Razorpay network I/O.
    - A short DB-backed creation lease prevents ordinary concurrent requests
      from issuing duplicate gateway-order calls for the same payment attempt.
    - The final persistence step locks and re-validates the payment before
      attaching the gateway order ID.
    """
    # ------------------------------------------------------------------
    # Phase 1: validate and establish a short-lived creation lease.
    # No external network I/O happens while this transaction is open.
    # ------------------------------------------------------------------
    with transaction.atomic():
        order = _get_owned_order(
            order_number=order_number,
            customer=customer,
        )

        _assert_online_payment_allowed(order)

        payment = _get_or_create_gateway_payment(order=order)

        if payment.provider_order_id:
            return _build_razorpay_payment_response(
                order=order,
                payment=payment,
            )

        if _payment_initialization_is_active(payment):
            raise PaymentStateError(
                "Payment initialization is already in progress. Please retry shortly."
            )

        amount_paise = _money_to_paise(order.grand_total)

        if payment.amount != order.grand_total:
            raise PaymentValidationError(
                "Payment amount does not match the order."
            )

        if str(payment.currency or "").upper() != str(order.currency or "").upper():
            raise PaymentValidationError(
                "Payment currency does not match the order."
            )

        _mark_payment_initialization_started(payment)

    # ------------------------------------------------------------------
    # Phase 2: gateway network I/O occurs completely outside the DB
    # transaction. This prevents long-lived DB locks and connection
    # contention when Razorpay is slow/unavailable.
    # ------------------------------------------------------------------
    client = _get_client()

    gateway_payload = {
        "amount": amount_paise,
        "currency": order.currency,
        "receipt": order.order_number,
        "notes": {
            "blazeline_order": order.order_number,
        },
    }

    try:
        gateway_order = client.order.create(data=gateway_payload)
    except Exception as exc:
        logger.exception(
            "Razorpay order creation failed",
            extra={
                "order_number": order.order_number,
                "payment_id": payment.id,
            },
        )

        # Best-effort lease cleanup. Never hide the gateway failure because
        # cleanup itself failed.
        try:
            with transaction.atomic():
                current = (
                    Payment.objects
                    .select_for_update()
                    .get(pk=payment.pk)
                )
                if not current.provider_order_id:
                    _clear_payment_initialization_lease(current)
        except Exception:
            logger.exception(
                "Failed to clear Razorpay order-creation lease after gateway error",
                extra={
                    "order_number": order.order_number,
                    "payment_id": payment.id,
                },
            )

        raise PaymentGatewayError(
            "Unable to initialize online payment."
        ) from exc

    if not isinstance(gateway_order, dict):
        raise PaymentGatewayError(
            "Payment gateway returned an invalid response."
        )

    razorpay_order_id = str(
        gateway_order.get("id") or ""
    ).strip()

    if not razorpay_order_id:
        logger.error(
            "Razorpay returned no order ID",
            extra={
                "order_number": order.order_number,
                "payment_id": payment.id,
            },
        )
        raise PaymentGatewayError(
            "Payment gateway returned an invalid response."
        )

    returned_amount = gateway_order.get("amount")
    returned_currency = str(
        gateway_order.get("currency") or ""
    ).upper()

    if returned_amount != amount_paise:
        logger.error(
            "Razorpay amount mismatch",
            extra={
                "order_number": order.order_number,
                "expected_amount": amount_paise,
                "gateway_amount": returned_amount,
            },
        )
        raise PaymentGatewayError(
            "Payment gateway amount mismatch."
        )

    if returned_currency != str(order.currency or "").upper():
        logger.error(
            "Razorpay currency mismatch",
            extra={
                "order_number": order.order_number,
                "expected_currency": order.currency,
                "gateway_currency": returned_currency,
            },
        )
        raise PaymentGatewayError(
            "Payment gateway currency mismatch."
        )

    # ------------------------------------------------------------------
    # Phase 3: persist the gateway result under a fresh DB transaction.
    # Re-check all authoritative state because another request may have
    # completed while the network call was in flight.
    # ------------------------------------------------------------------
    with transaction.atomic():
        current_order = _get_owned_order(
            order_number=order_number,
            customer=customer,
        )
        _assert_online_payment_allowed(current_order)

        current_payment = (
            Payment.objects
            .select_for_update()
            .select_related("order")
            .get(
                pk=payment.pk,
                order_id=current_order.pk,
                provider="razorpay",
            )
        )

        if current_payment.provider_order_id:
            return _build_razorpay_payment_response(
                order=current_order,
                payment=current_payment,
            )

        if current_payment.amount != current_order.grand_total:
            raise PaymentValidationError(
                "Payment amount does not match the order."
            )

        if str(current_payment.currency or "").upper() != str(current_order.currency or "").upper():
            raise PaymentValidationError(
                "Payment currency does not match the order."
            )

        current_payment.provider_order_id = razorpay_order_id
        current_payment.status = Payment.Status.PENDING

        metadata = (
            current_payment.raw_metadata
            if isinstance(current_payment.raw_metadata, dict)
            else {}
        )
        updated_metadata = dict(metadata)
        updated_metadata["gateway_order_status"] = gateway_order.get("status")
        updated_metadata["gateway_order_amount"] = returned_amount
        updated_metadata["gateway_order_currency"] = returned_currency
        updated_metadata.pop("gateway_order_creation", None)
        current_payment.raw_metadata = updated_metadata

        current_payment.save(
            update_fields=[
                "provider_order_id",
                "status",
                "raw_metadata",
                "updated_at",
            ]
        )

        return _build_razorpay_payment_response(
            order=current_order,
            payment=current_payment,
        )


# ============================================================================
# CHECKOUT PAYMENT SIGNATURE
# ============================================================================


def verify_checkout_signature(
    *,
    razorpay_order_id: str,
    razorpay_payment_id: str,
    razorpay_signature: str,
) -> None:
    """
    Verify the signature returned by Razorpay Checkout.

    Razorpay documents the HMAC-SHA256 construction as:

        order_id|payment_id

    using the API key secret.

    The caller MUST still verify that the gateway order ID belongs to the
    expected BlazeLine Payment record before changing any state.
    """
    if not razorpay_order_id:
        raise PaymentSignatureError(
            "Missing Razorpay order ID."
        )

    if not razorpay_payment_id:
        raise PaymentSignatureError(
            "Missing Razorpay payment ID."
        )

    if not razorpay_signature:
        raise PaymentSignatureError(
            "Missing Razorpay payment signature."
        )

    if not RAZORPAY_KEY_SECRET:
        raise PaymentConfigurationError(
            "Razorpay secret is not configured."
        )

    message = (
        f"{razorpay_order_id}|{razorpay_payment_id}"
    ).encode(
        "utf-8"
    )

    expected = hmac.new(
        RAZORPAY_KEY_SECRET.encode(
            "utf-8"
        ),
        message,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(
        expected,
        razorpay_signature,
    ):
        raise PaymentSignatureError(
            "Invalid payment signature."
        )


# ============================================================================
# VERIFY + CAPTURE CLIENT-SIDE SUCCESS
# ============================================================================


def confirm_checkout_payment(
    *,
    customer,
    order_number: str,
    razorpay_order_id: str,
    razorpay_payment_id: str,
    razorpay_signature: str,
) -> Payment:
    """Verify Checkout, persist authorization, then reconcile with Razorpay."""
    verify_checkout_signature(
        razorpay_order_id=razorpay_order_id,
        razorpay_payment_id=razorpay_payment_id,
        razorpay_signature=razorpay_signature,
    )

    with transaction.atomic():
        order = _get_owned_order(
            order_number=order_number,
            customer=customer,
        )

        payment = (
            Payment.objects
            .select_for_update()
            .select_related("order")
            .filter(order=order, provider="razorpay")
            .order_by("-created_at")
            .first()
        )

        if not payment:
            raise PaymentValidationError("Payment record not found.")

        if payment.provider_order_id != razorpay_order_id:
            raise PaymentValidationError("Payment gateway order mismatch.")

        if payment.currency != order.currency:
            raise PaymentValidationError("Payment currency mismatch.")

        if payment.amount != order.grand_total:
            raise PaymentValidationError("Payment amount mismatch.")

        if payment.status == Payment.Status.CAPTURED:
            if (
                payment.provider_payment_id
                and payment.provider_payment_id != razorpay_payment_id
            ):
                raise PaymentValidationError("Payment record conflict.")
            return payment

        if (
            payment.provider_payment_id
            and payment.provider_payment_id != razorpay_payment_id
        ):
            raise PaymentValidationError("Payment record conflict.")

        payment.provider_payment_id = razorpay_payment_id
        payment.provider_signature = razorpay_signature
        payment.status = Payment.Status.AUTHORIZED
        payment.save(
            update_fields=[
                "provider_payment_id",
                "provider_signature",
                "status",
                "updated_at",
            ]
        )

    # Never hold database locks during gateway network I/O.
    gateway_payment = _fetch_gateway_payment(
        provider_payment_id=razorpay_payment_id,
    )

    gateway_payment_id = _extract_gateway_payment_id(gateway_payment)
    gateway_order_id = _extract_gateway_order_id(gateway_payment)
    gateway_amount = _extract_gateway_amount(gateway_payment)
    gateway_currency = _extract_gateway_currency(gateway_payment)
    gateway_status = str(gateway_payment.get("status") or "").strip().lower()

    if gateway_payment_id != razorpay_payment_id:
        raise PaymentValidationError("Gateway payment ID mismatch.")
    if gateway_order_id != razorpay_order_id:
        raise PaymentValidationError("Gateway order ID mismatch.")
    if gateway_amount is None:
        raise PaymentValidationError("Gateway payment amount is missing.")
    if gateway_amount != _money_to_paise(payment.amount):
        raise PaymentValidationError("Gateway payment amount mismatch.")
    if gateway_currency != payment.currency:
        raise PaymentValidationError("Gateway payment currency mismatch.")

    if gateway_status == "captured":
        return _settle_payment_success(
            payment=payment,
            gateway_payment_id=razorpay_payment_id,
            gateway_amount=gateway_amount,
            gateway_currency=gateway_currency,
            source_event="checkout.reconciliation",
        )

    if gateway_status == "failed":
        with transaction.atomic():
            current = (
                Payment.objects
                .select_for_update()
                .select_related("order")
                .get(pk=payment.pk)
            )
            if current.status == Payment.Status.CAPTURED:
                return current

            current.status = Payment.Status.FAILED
            current.failed_at = timezone.now()
            current.failure_message = "Gateway reported the payment as failed."
            current.save(
                update_fields=[
                    "status",
                    "failed_at",
                    "failure_message",
                    "updated_at",
                ]
            )

            current_order = (
                Order.objects
                .select_for_update()
                .get(pk=current.order_id)
            )
            current_order.payment_status = Order.PaymentStatus.FAILED
            current_order.save(update_fields=["payment_status", "updated_at"])
            return current

    if gateway_status not in {"created", "authorized", "captured", "failed", "refunded"}:
        raise PaymentValidationError(
            "Gateway returned an unsupported payment status."
        )

    return payment

def reconcile_checkout_payment(
    *,
    order_id: int,
    payment_id: int,
    expected_razorpay_payment_id: str,
) -> dict:
    """
    Reconcile a checkout payment against Razorpay's server-side state.

    This is intentionally separate from client-side signature verification.
    The signature proves the payment parameters were signed correctly;
    the gateway fetch confirms the actual payment state on Razorpay.
    """
    client = _get_client()

    expected_razorpay_payment_id = (
        str(expected_razorpay_payment_id or "").strip()
    )

    if not expected_razorpay_payment_id:
        raise ValueError("Razorpay payment id is required.")

    try:
        gateway_payment = client.payment.fetch(expected_razorpay_payment_id)
    except Exception:
        logger.exception(
            "Failed to fetch Razorpay payment during reconciliation "
            "for order_id=%s payment_id=%s razorpay_payment_id=%s",
            order_id,
            payment_id,
            expected_razorpay_payment_id,
        )
        raise

    if not isinstance(gateway_payment, dict):
        raise ValueError("Invalid Razorpay payment response.")

    gateway_payment_id = str(
        gateway_payment.get("id") or ""
    ).strip()

    if gateway_payment_id != expected_razorpay_payment_id:
        raise ValueError("Razorpay payment id mismatch.")

    gateway_order_id = str(
        gateway_payment.get("order_id") or ""
    ).strip()

    gateway_amount = gateway_payment.get("amount")
    gateway_currency = str(
        gateway_payment.get("currency") or ""
    ).upper()

    gateway_status = str(
        gateway_payment.get("status") or ""
    ).lower()

    with transaction.atomic():
        payment = (
            Payment.objects
            .select_for_update()
            .select_related("order")
            .get(
                id=payment_id,
                order_id=order_id,
                provider="razorpay",
            )
        )

        order = (
            Order.objects
            .select_for_update()
            .get(id=order_id)
        )

        if (
            payment.provider_payment_id
            and payment.provider_payment_id != expected_razorpay_payment_id
        ):
            raise ValueError("Payment is already linked to another Razorpay payment.")

        if (
            gateway_order_id
            and payment.provider_order_id
            and gateway_order_id != payment.provider_order_id
        ):
            raise ValueError("Razorpay order id mismatch.")

        if gateway_amount is None:
            raise ValueError("Razorpay payment amount is missing.")

        if int(gateway_amount) != _money_to_paise(payment.amount):
            raise ValueError("Razorpay payment amount mismatch.")

        expected_currency = str(payment.currency or "").upper()
        if gateway_currency != expected_currency:
            raise ValueError("Razorpay payment currency mismatch.")

        payment.provider_payment_id = expected_razorpay_payment_id
        payment.raw_metadata = {
            **(payment.raw_metadata or {}),
            "reconciliation": gateway_payment,
        }

        if gateway_status == "captured":
            payment.status = Payment.Status.CAPTURED
            payment.paid_at = timezone.now()

            order.payment_status = Order.PaymentStatus.PAID
            order.status = Order.Status.CONFIRMED
            order.paid_at = payment.paid_at

        elif gateway_status == "authorized":
            payment.status = Payment.Status.AUTHORIZED

        elif gateway_status in {"failed", "refunded"}:
            payment.status = (
                Payment.Status.FAILED
                if gateway_status == "failed"
                else Payment.Status.REFUNDED
            )

            if gateway_status == "failed":
                order.payment_status = Order.PaymentStatus.FAILED

        else:
            payment.status = Payment.Status.PENDING

        payment.save(
            update_fields=[
                "provider_payment_id",
                "status",
                "paid_at",
                "raw_metadata",
                "updated_at",
            ]
        )

        order.save(
            update_fields=[
                "status",
                "payment_status",
                "paid_at",
                "updated_at",
            ]
        )

        return {
            "status": gateway_status,
            "razorpay_payment_id": expected_razorpay_payment_id,
            "amount": gateway_amount,
            "currency": gateway_currency,
        }
# ============================================================================
# WEBHOOK SIGNATURE
# ============================================================================


def verify_webhook_signature(
    *,
    raw_body: bytes,
    signature: str,
) -> None:
    """
    Validate Razorpay webhook authenticity.

    CRITICAL:
    `raw_body` MUST be request.body bytes exactly as received.

    Do NOT:
        json.loads(...)
        json.dumps(...)

    before calculating this signature.
    """
    if not raw_body:
        raise PaymentWebhookError(
            "Empty webhook payload."
        )

    if not signature:
        raise PaymentWebhookError(
            "Missing webhook signature."
        )

    if not RAZORPAY_WEBHOOK_SECRET:
        raise PaymentConfigurationError(
            "Razorpay webhook secret is not configured."
        )

    expected = hmac.new(
        RAZORPAY_WEBHOOK_SECRET.encode(
            "utf-8"
        ),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(
        expected,
        signature,
    ):
        raise PaymentWebhookError(
            "Invalid webhook signature."
        )


# ============================================================================
# WEBHOOK EVENT LEDGER
# ============================================================================


@transaction.atomic
def record_webhook_event(
    *,
    provider_event_id: str,
    event_type: str,
    payload: dict,
) -> tuple[PaymentEvent, bool]:
    """
    Persist a verified webhook event exactly once.

    Returns:
        (event, created)

    created=False means the provider already delivered this event.
    """

    provider_event_id = str(
        provider_event_id or ""
    ).strip()

    event_type = str(
        event_type or ""
    ).strip()

    if not provider_event_id:
        raise PaymentWebhookError(
            "Webhook event ID is missing."
        )

    if not event_type:
        raise PaymentWebhookError(
            "Webhook event type is missing."
        )

    try:
        # Use a nested savepoint so a uniqueness race does not poison
        # the surrounding transaction.
        with transaction.atomic():
            event = PaymentEvent.objects.create(
                provider="razorpay",
                provider_event_id=provider_event_id,
                event_type=event_type,
                payload=payload,
                processing_status=(
                    PaymentEvent.ProcessingStatus.RECEIVED
                ),
            )

        return event, True

    except IntegrityError:
        # The inner savepoint has already rolled back, so the outer
        # transaction is still usable.
        existing = (
            PaymentEvent.objects
            .select_for_update()
            .filter(
                provider="razorpay",
                provider_event_id=provider_event_id,
            )
            .first()
        )

        if not existing:
            raise PaymentWebhookError(
                "Webhook event could not be recorded."
            )

        return existing, False




# ============================================================================
# WEBHOOK PAYMENT EXTRACTION
# ============================================================================


def _extract_payment_payload(
    payload: dict,
) -> dict:
    """
    Extract common Razorpay payment entity data without trusting it as
    commercial truth.

    The order/payment records in our DB remain authoritative for amount and
    ownership.
    """
    event_payload = payload.get(
        "payload"
    )

    if not isinstance(
        event_payload,
        dict,
    ):
        return {}

    payment_container = (
        event_payload.get(
            "payment"
        )
    )

    if not isinstance(
        payment_container,
        dict,
    ):
        return {}

    entity = payment_container.get(
        "entity"
    )

    if not isinstance(
        entity,
        dict,
    ):
        return {}

    return entity


def _extract_gateway_order_id(
    payment_entity: dict,
) -> str:
    return str(
        payment_entity.get(
            "order_id"
        )
        or ""
    ).strip()


def _extract_gateway_payment_id(
    payment_entity: dict,
) -> str:
    return str(
        payment_entity.get(
            "id"
        )
        or ""
    ).strip()


def _extract_gateway_amount(
    payment_entity: dict,
) -> int | None:
    value = payment_entity.get(
        "amount"
    )

    if value is None:
        return None

    try:
        return int(value)
    except (
        TypeError,
        ValueError,
    ):
        return None


def _extract_gateway_currency(
    payment_entity: dict,
) -> str:

    return str(
        payment_entity.get(
            "currency"
        )
        or ""
    ).strip().upper()
def _extract_order_paid_payment_payload(
    payload: dict,
) -> dict:
    """
    Extract the payment entity from a Razorpay `order.paid` webhook.

    Razorpay's order.paid payload contains the payment entity under:
        payload.payment.entity

    The extracted gateway values are still untrusted until validated
    against the stored BlazeLine Payment record.
    """
    return _extract_payment_payload(payload)
@transaction.atomic
def _settle_payment_success(
    *,
    payment: Payment,
    gateway_payment_id: str,
    gateway_amount: int,
    gateway_currency: str,
    source_event: str,
) -> Payment:
    """
    Idempotent successful-payment settlement.

    This is the single settlement path used by client-side reconciliation
    and successful webhooks.

    Preconditions:
    - gateway authenticity has already been verified
    - payment belongs to the expected BlazeLine order
    - gateway order/payment identity is already resolved
    """

    payment = (
        Payment.objects
        .select_for_update()
        .select_related("order")
        .get(pk=payment.pk)
    )

    order = (
        Order.objects
        .select_for_update()
        .get(pk=payment.order_id)
    )

    expected_amount = _money_to_paise(
        payment.amount
    )

    if gateway_amount != expected_amount:
        raise PaymentWebhookError(
            "Gateway payment amount does not match the order."
        )

    if gateway_currency != payment.currency:
        raise PaymentWebhookError(
            "Gateway payment currency does not match the order."
        )

    if not gateway_payment_id:
        raise PaymentWebhookError(
            "Gateway payment ID is missing."
        )

    # --------------------------------------------------------------
    # Already captured = idempotent replay.
    # --------------------------------------------------------------

    if payment.status == Payment.Status.CAPTURED:
        if (
            payment.provider_payment_id
            and payment.provider_payment_id
            != gateway_payment_id
        ):
            raise PaymentValidationError(
                "Payment record conflict."
            )

        return payment

    # --------------------------------------------------------------
    # Protect against a different payment ID trying to settle the
    # same payment row.
    # --------------------------------------------------------------

    if (
        payment.provider_payment_id
        and payment.provider_payment_id
        != gateway_payment_id
    ):
        raise PaymentValidationError(
            "Payment record conflict."
        )

    # --------------------------------------------------------------
    # DB-level uniqueness gives us an additional global backstop:
    # the same provider payment ID cannot belong to another row.
    # --------------------------------------------------------------

    conflicting_payment = (
        Payment.objects
        .select_for_update()
        .filter(
            provider=payment.provider,
            provider_payment_id=gateway_payment_id,
        )
        .exclude(pk=payment.pk)
        .first()
    )

    if conflicting_payment:
        raise PaymentValidationError(
            "Gateway payment is already linked to another payment."
        )

    # --------------------------------------------------------------
    # If another payment for this order is already captured,
    # do not create a second settlement.
    # --------------------------------------------------------------

    another_captured = (
        Payment.objects
        .select_for_update()
        .filter(
            order=order,
            status=Payment.Status.CAPTURED,
        )
        .exclude(pk=payment.pk)
        .first()
    )

    if another_captured:
        raise PaymentStateError(
            "Another payment has already settled this order."
        )

    # --------------------------------------------------------------
    # Capture payment.
    # --------------------------------------------------------------

    payment.provider_payment_id = gateway_payment_id
    payment.status = Payment.Status.CAPTURED
    payment.paid_at = timezone.now()

    metadata = (
        payment.raw_metadata
        if isinstance(payment.raw_metadata, dict)
        else {}
    )

    payment.raw_metadata = {
        **metadata,
        "settled_from": source_event,
    }

    payment.save(
        update_fields=[
            "provider_payment_id",
            "status",
            "paid_at",
            "raw_metadata",
            "updated_at",
        ]
    )

    # --------------------------------------------------------------
    # Mark order paid.
    # --------------------------------------------------------------

    order.payment_status = (
        Order.PaymentStatus.PAID
    )

    order.status = (
        Order.Status.CONFIRMED
    )

    order.paid_at = timezone.now()

    order.save(
        update_fields=[
            "payment_status",
            "status",
            "paid_at",
            "updated_at",
        ]
    )

    # --------------------------------------------------------------
    # Consume inventory exactly once.
    #
    # ACTIVE -> CONSUMED
    #
    # Released/expired reservations are deliberately not resurrected.
    # --------------------------------------------------------------

    active_reservations = list(
        InventoryReservation.objects
        .select_for_update()
        .filter(
            order=order,
            status=InventoryReservation.Status.ACTIVE,
        )
    )

    if active_reservations:
        consumed_at = timezone.now()
        for reservation in active_reservations:
            reservation.status = InventoryReservation.Status.CONSUMED
            reservation.consumed_at = consumed_at
            reservation.save(update_fields=["status", "consumed_at"])
    else:
        metadata = payment.raw_metadata if isinstance(payment.raw_metadata, dict) else {}
        payment.raw_metadata = {
            **metadata,
            "stock_reconciliation_required": True,
            "stock_reconciliation_reason": (
                "Payment captured after the inventory reservation was no longer ACTIVE."
            ),
        }
        payment.save(update_fields=["raw_metadata", "updated_at"])
        logger.critical(
            "Late payment capture requires stock reconciliation",
            extra={
                "order_number": order.order_number,
                "payment_id": payment.id,
                "provider_payment_id": gateway_payment_id,
            },
        )

    return payment
# ============================================================================
# CAPTURE WEBHOOK
# ============================================================================


@transaction.atomic
def process_payment_captured_webhook(
    *,
    event: PaymentEvent,
) -> Payment:
    """
    Process an authenticated Razorpay payment.captured webhook.

    All successful payment settlement is delegated to the single
    idempotent `_settle_payment_success()` path.
    """

    payment_entity = _extract_payment_payload(
        event.payload
    )

    gateway_order_id = _extract_gateway_order_id(
        payment_entity
    )

    gateway_payment_id = _extract_gateway_payment_id(
        payment_entity
    )

    gateway_amount = _extract_gateway_amount(
        payment_entity
    )

    gateway_currency = _extract_gateway_currency(
        payment_entity
    )

    if not gateway_order_id:
        raise PaymentWebhookError(
            "Webhook payment does not contain an order ID."
        )

    if not gateway_payment_id:
        raise PaymentWebhookError(
            "Webhook payment does not contain a payment ID."
        )

    if gateway_amount is None:
        raise PaymentWebhookError(
            "Webhook payment does not contain an amount."
        )

    if not gateway_currency:
        raise PaymentWebhookError(
            "Webhook payment does not contain a currency."
        )

    payment = (
        Payment.objects
        .select_for_update()
        .filter(
            provider="razorpay",
            provider_order_id=gateway_order_id,
        )
        .first()
    )

    if not payment:
        raise PaymentWebhookError(
            "No matching BlazeLine payment found."
        )

    payment = _settle_payment_success(
        payment=payment,
        gateway_payment_id=gateway_payment_id,
        gateway_amount=gateway_amount,
        gateway_currency=gateway_currency,
        source_event=event.event_type,
    )

    event.payment = payment
    event.processing_status = (
        PaymentEvent.ProcessingStatus.PROCESSED
    )
    event.processed_at = timezone.now()
    event.error_message = ""

    event.save(
        update_fields=[
            "payment",
            "processing_status",
            "processed_at",
            "error_message",
        ]
    )

    return payment

# ============================================================================
# ORDER.PAID WEBHOOK
# ============================================================================


@transaction.atomic
def process_order_paid_webhook(
    *,
    event: PaymentEvent,
) -> Payment:
    """
    Process an authenticated Razorpay `order.paid` webhook.

    `order.paid` is another successful-payment signal. It must converge
    on the exact same settlement path as `payment.captured`.
    """

    payment_entity = _extract_payment_payload(
        event.payload
    )

    gateway_order_id = _extract_gateway_order_id(
        payment_entity
    )

    gateway_payment_id = _extract_gateway_payment_id(
        payment_entity
    )

    gateway_amount = _extract_gateway_amount(
        payment_entity
    )

    gateway_currency = _extract_gateway_currency(
        payment_entity
    )

    if not gateway_order_id:
        raise PaymentWebhookError(
            "Webhook payment does not contain an order ID."
        )

    if not gateway_payment_id:
        raise PaymentWebhookError(
            "Webhook payment does not contain a payment ID."
        )

    if gateway_amount is None:
        raise PaymentWebhookError(
            "Webhook payment does not contain an amount."
        )

    if not gateway_currency:
        raise PaymentWebhookError(
            "Webhook payment does not contain a currency."
        )

    payment = (
        Payment.objects
        .select_for_update()
        .filter(
            provider="razorpay",
            provider_order_id=gateway_order_id,
        )
        .first()
    )

    if not payment:
        raise PaymentWebhookError(
            "No matching BlazeLine payment found."
        )

    payment = _settle_payment_success(
        payment=payment,
        gateway_payment_id=gateway_payment_id,
        gateway_amount=gateway_amount,
        gateway_currency=gateway_currency,
        source_event=event.event_type,
    )

    event.payment = payment
    event.processing_status = (
        PaymentEvent.ProcessingStatus.PROCESSED
    )
    event.processed_at = timezone.now()
    event.error_message = ""

    event.save(
        update_fields=[
            "payment",
            "processing_status",
            "processed_at",
            "error_message",
        ]
    )


    return payment
# ============================================================================
# WEBHOOK FAILURE
# ============================================================================


@transaction.atomic
def process_payment_failed_webhook(
    *,
    event: PaymentEvent,
) -> Payment:
    """
    Process an already-authenticated `payment.failed` webhook.

    We deliberately keep this separate from the captured handler so a later
    captured event can transition the same payment safely when the gateway
    reports a successful outcome after an earlier failure.
    """
    payload = event.payload

    payment_entity = _extract_payment_payload(
        payload
    )

    gateway_order_id = _extract_gateway_order_id(
        payment_entity
    )

    if not gateway_order_id:
        raise PaymentWebhookError(
            "Webhook payment does not contain an order ID."
        )

    payment = (
        Payment.objects
        .select_for_update()
        .select_related("order")
        .filter(
            provider="razorpay",
            provider_order_id=gateway_order_id,
        )
        .order_by("-created_at")
        .first()
    )

    if not payment:
        raise PaymentWebhookError(
            "No matching BlazeLine payment found."
        )

    if payment.status == Payment.Status.CAPTURED:
        # Never downgrade a successful payment because of a late/stale
        # failure webhook.
        event.payment = payment
        event.processing_status = (
            PaymentEvent.ProcessingStatus.PROCESSED
        )
        event.processed_at = timezone.now()
        event.error_message = ""
        event.save(
            update_fields=[
                "payment",
                "processing_status",
                "processed_at",
                "error_message",
            ]
        )
        return payment

    payment.status = Payment.Status.FAILED
    payment.failed_at = timezone.now()

    error_data = payment_entity.get(
        "error"
    )

    if isinstance(
        error_data,
        dict,
    ):
        payment.failure_code = str(
            error_data.get(
                "code"
            )
            or ""
        )[:120]

        payment.failure_message = str(
            error_data.get(
                "description"
            )
            or error_data.get(
                "reason"
            )
            or ""
        )

    payment.save(
        update_fields=[
            "status",
            "failed_at",
            "failure_code",
            "failure_message",
            "updated_at",
        ]
    )

    order = (
        Order.objects
        .select_for_update()
        .get(
            pk=payment.order_id
        )
    )

    # Do not mark an order as permanently failed if another payment attempt
    # may still be created later. The payment record is failed; the order stays
    # pending_payment until the order workflow explicitly expires/cancels it.
    order.payment_status = (
        Order.PaymentStatus.FAILED
    )

    order.save(
        update_fields=[
            "payment_status",
            "updated_at",
        ]
    )

    event.payment = payment
    event.processing_status = (
        PaymentEvent.ProcessingStatus.PROCESSED
    )
    event.processed_at = timezone.now()
    event.error_message = ""

    event.save(
        update_fields=[
            "payment",
            "processing_status",
            "processed_at",
            "error_message",
        ]
    )

    return payment
