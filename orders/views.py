from __future__ import annotations

import json
import logging

from django.db import transaction
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import Customer

from .models import Order, Payment, PaymentEvent
from .payment_service import (
    PaymentConfigurationError,
    PaymentGatewayError,
    PaymentOwnershipError,
    PaymentServiceError,
    PaymentSignatureError,
    PaymentStateError,
    PaymentValidationError,
    PaymentWebhookError,
    confirm_checkout_payment,
    create_razorpay_order_for_customer,
    process_order_paid_webhook,
    process_payment_captured_webhook,
    process_payment_failed_webhook,

    record_webhook_event,
    verify_webhook_signature,
)
from .serializers import (
    CreateOrderSerializer,
    OrderListSerializer,
    OrderSerializer,
)
from .services import (
    DeliveryQuote,
    DeliveryValidationError,
    EmptyCartError,
    IdempotencyConflictError,
    InsufficientStockError,
    InvalidOrderError,
    MinimumOrderQuantityError,
    VariantUnavailableError,
    create_order_safely,
)

from catalog.delivery.services import (
    CartValidationError,
    NotServiceableError,
    calculate_delivery,
)


logger = logging.getLogger(__name__)


# ============================================================================
# CUSTOMER BASE
# ============================================================================


class CustomerAuthenticatedView(APIView):
    permission_classes = [permissions.IsAuthenticated]


def _is_customer(request) -> bool:
    return isinstance(request.user, Customer)


# ============================================================================
# DELIVERY + TAX
# ============================================================================


def _server_tax_resolver(variant, line_subtotal):
    """
    Temporary server-side tax policy.

    The frontend never supplies tax.

    IMPORTANT:
    ProductVariant currently has no authoritative tax-rate field in the
    model contract we inspected, so 18% is the current server policy until
    a real tax configuration is introduced.
    """
    return 18


def _build_delivery_quote(
    *,
    shipping: dict,
    cart_items: list[dict],
) -> DeliveryQuote:
    try:
        result = calculate_delivery(
            pincode=shipping["pincode"],
            cart_items=cart_items,
        )
    except CartValidationError as exc:
        raise DeliveryValidationError(
            str(exc)
        ) from exc
    except NotServiceableError as exc:
        raise DeliveryValidationError(
            str(exc)
        ) from exc

    zone = result.get("zone")

    breakdown = []
    for item in result.get("breakdown", []):
        breakdown.append(
            {
                "rule": str(item.get("rule", "")),
                "label": str(item.get("label", "")),
                "amount": str(item.get("amount", "0.00")),
            }
        )

    return DeliveryQuote(
        charge=result["total"],
        free_delivery=result["total"] == 0,
        zone_id=zone.id if zone else None,
        zone_name=zone.name if zone else "",
        breakdown=breakdown,
    )


# ============================================================================
# ORDER ERROR MAPPING
# ============================================================================


def _order_error_response(exc: Exception):
    if isinstance(exc, EmptyCartError):
        return Response(
            {
                "code": "empty_cart",
                "detail": "Your cart is empty.",
            },
            status=status.HTTP_409_CONFLICT,
        )

    if isinstance(exc, InsufficientStockError):
        return Response(
            {
                "code": "insufficient_stock",
                "detail": str(exc),
            },
            status=status.HTTP_409_CONFLICT,
        )

    if isinstance(exc, MinimumOrderQuantityError):
        return Response(
            {
                "code": "minimum_order_quantity",
                "detail": str(exc),
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    if isinstance(exc, VariantUnavailableError):
        return Response(
            {
                "code": "variant_unavailable",
                "detail": str(exc),
            },
            status=status.HTTP_409_CONFLICT,
        )

    if isinstance(exc, DeliveryValidationError):
        return Response(
            {
                "code": "delivery_unavailable",
                "detail": str(exc),
            },
            status=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    if isinstance(exc, IdempotencyConflictError):
        return Response(
            {
                "code": "idempotency_conflict",
                "detail": str(exc),
            },
            status=status.HTTP_409_CONFLICT,
        )

    if isinstance(exc, InvalidOrderError):
        return Response(
            {
                "code": "invalid_order",
                "detail": str(exc),
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    return None


# ============================================================================
# CREATE ORDER
# ============================================================================


class OrderCreateAPIView(CustomerAuthenticatedView):
    """
    POST /api/orders/create/

    Creates an order from the authenticated customer's current cart.

    Client does NOT control:
    - product price
    - subtotal
    - GST
    - delivery charge
    - grand total
    - stock

    Idempotency-Key is mandatory.
    """

    http_method_names = [
        "post",
        "options",
    ]

    def post(self, request):
        if not _is_customer(request):
            return Response(
                {
                    "code": "customer_required",
                    "detail": "Customer account required.",
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = CreateOrderSerializer(
            data=request.data
        )
        serializer.is_valid(
            raise_exception=True
        )

        idempotency_key = str(
            request.headers.get(
                "Idempotency-Key"
            )
            or ""
        ).strip()

        if not idempotency_key:
            return Response(
                {
                    "code": "missing_idempotency_key",
                    "detail": (
                        "Idempotency-Key header is required."
                    ),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        shipping = serializer.validated_data["shipping"]

        try:
            # Read current cart only to construct delivery input.
            # The actual order service acquires DB locks again.
            from cart.models import CartItem

            cart_items = list(
                CartItem.objects
                .filter(
                    cart__customer=request.user
                )
                .values(
                    "variant_id",
                    "quantity",
                )
                .order_by("variant_id")
            )

            if not cart_items:
                return Response(
                    {
                        "code": "empty_cart",
                        "detail": "Your cart is empty.",
                    },
                    status=status.HTTP_409_CONFLICT,
                )

            delivery_quote = _build_delivery_quote(
                shipping=shipping,
                cart_items=[
                    {
                        "variant_id": int(
                            item["variant_id"]
                        ),
                        "quantity": int(
                            item["quantity"]
                        ),
                    }
                    for item in cart_items
                ],
            )

            order = create_order_safely(
                customer=request.user,
                idempotency_key=idempotency_key,
                payment_method=serializer.validated_data[
                    "payment_method"
                ],
                shipping=shipping,
                delivery_quote=delivery_quote,
                tax_resolver=_server_tax_resolver,
                currency=serializer.validated_data[
                    "currency"
                ],
                notes=serializer.validated_data.get(
                    "notes",
                    "",
                ),
            )

        except (
            EmptyCartError,
            InsufficientStockError,
            MinimumOrderQuantityError,
            VariantUnavailableError,
            DeliveryValidationError,
            IdempotencyConflictError,
            InvalidOrderError,
        ) as exc:
            return _order_error_response(exc)

        except Exception:
            logger.exception(
                "Unexpected order creation failure",
                extra={
                    "customer_id": getattr(
                        request.user,
                        "pk",
                        None,
                    )
                },
            )

            return Response(
                {
                    "code": "order_creation_failed",
                    "detail": (
                        "We couldn't create your order right now. "
                        "Please try again."
                    ),
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(
            {
                "success": True,
                "order": OrderSerializer(
                    order
                ).data,
            },
            status=status.HTTP_201_CREATED,
        )


# ============================================================================
# CUSTOMER ORDERS
# ============================================================================


class CustomerOrderListAPIView(
    CustomerAuthenticatedView
):
    http_method_names = [
        "get",
        "head",
        "options",
    ]

    def get(self, request):
        if not _is_customer(request):
            return Response(
                {
                    "code": "customer_required",
                    "detail": "Customer account required.",
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        orders = (
            Order.objects
            .filter(
                customer=request.user
            )
            .order_by("-created_at")
        )

        return Response(
            {
                "results": OrderListSerializer(
                    orders,
                    many=True,
                ).data
            },
            status=status.HTTP_200_OK,
        )


class CustomerOrderDetailAPIView(
    CustomerAuthenticatedView
):
    http_method_names = [
        "get",
        "head",
        "options",
    ]

    def get(
        self,
        request,
        order_number,
    ):
        if not _is_customer(request):
            return Response(
                {
                    "code": "customer_required",
                    "detail": "Customer account required.",
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        order = (
            Order.objects
            .filter(
                customer=request.user,
                order_number=order_number,
            )
            .prefetch_related(
                "items",
                "payments",
            )
            .first()
        )

        if not order:
            return Response(
                {
                    "code": "order_not_found",
                    "detail": "Order not found.",
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response(
            OrderSerializer(order).data,
            status=status.HTTP_200_OK,
        )


# ============================================================================
# RAZORPAY CREATE
# ============================================================================


class RazorpayPaymentCreateAPIView(
    CustomerAuthenticatedView
):
    http_method_names = [
        "post",
        "options",
    ]

    def post(
        self,
        request,
        order_number,
    ):
        if not _is_customer(request):
            return Response(
                {
                    "code": "customer_required",
                    "detail": "Customer account required.",
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            data = (
                create_razorpay_order_for_customer(
                    customer=request.user,
                    order_number=order_number,
                )
            )

        except Exception as exc:
            response = _payment_error_response(
                exc
            )

            if response is not None:
                return response

            logger.exception(
                "Unexpected Razorpay order creation error",
                extra={
                    "customer_id": request.user.pk,
                    "order_number": order_number,
                },
            )

            return Response(
                {
                    "code": "payment_initialization_failed",
                    "detail": (
                        "Unable to initialize payment."
                    ),
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(
            {
                "success": True,
                **data,
            },
            status=status.HTTP_200_OK,
        )


# ============================================================================
# RAZORPAY VERIFY
# ============================================================================


class RazorpayPaymentVerifyAPIView(
    CustomerAuthenticatedView
):
    http_method_names = [
        "post",
        "options",
    ]

    def post(
        self,
        request,
        order_number,
    ):
        if not _is_customer(request):
            return Response(
                {
                    "code": "customer_required",
                    "detail": "Customer account required.",
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        razorpay_order_id = str(
            request.data.get(
                "razorpay_order_id"
            )
            or ""
        ).strip()

        razorpay_payment_id = str(
            request.data.get(
                "razorpay_payment_id"
            )
            or ""
        ).strip()

        razorpay_signature = str(
            request.data.get(
                "razorpay_signature"
            )
            or ""
        ).strip()

        if not all(
            [
                razorpay_order_id,
                razorpay_payment_id,
                razorpay_signature,
            ]
        ):
            return Response(
                {
                    "code": "missing_payment_fields",
                    "detail": (
                        "Payment verification fields are required."
                    ),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            payment = confirm_checkout_payment(
                customer=request.user,
                order_number=order_number,
                razorpay_order_id=razorpay_order_id,
                razorpay_payment_id=razorpay_payment_id,
                razorpay_signature=razorpay_signature,
            )



        except Exception as exc:
            response = _payment_error_response(
                exc
            )

            if response is not None:
                return response

            logger.exception(
                "Unexpected payment verification error",
                extra={
                    "customer_id": request.user.pk,
                    "order_number": order_number,
                },
            )

            return Response(
                {
                    "code": "payment_verification_failed",
                    "detail": (
                        "Unable to verify payment."
                    ),
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(
            {
                "success": True,
                "payment": {
                    "id": payment.id,
                    "status": payment.status,
                    "provider_order_id": payment.provider_order_id,
                    "provider_payment_id": payment.provider_payment_id,
                    "amount": str(payment.amount),
                    "currency": payment.currency,
                },
            },
            status=status.HTTP_200_OK,
        )


# ============================================================================
# RAZORPAY WEBHOOK
# ============================================================================


class RazorpayWebhookAPIView(APIView):
    """
    POST /api/payments/webhook/

    No JWT.

    Authenticity comes exclusively from the Razorpay webhook HMAC signature.
    """

    authentication_classes = []
    permission_classes = [
        permissions.AllowAny
    ]

    http_method_names = [
        "post",
        "options",
    ]

    def post(self, request):
        raw_body = request.body

        signature = (
            request.headers.get(
                "X-Razorpay-Signature"
            )
            or ""
        ).strip()

        try:
            verify_webhook_signature(
                raw_body=raw_body,
                signature=signature,
            )
        except Exception as exc:
            response = _payment_error_response(
                exc
            )

            if response is not None:
                return response

            return Response(
                {
                    "code": "invalid_webhook",
                    "detail": "Invalid webhook.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            payload = json.loads(
                raw_body.decode("utf-8")
            )
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
        ):
            return Response(
                {
                    "code": "invalid_webhook_payload",
                    "detail": "Invalid webhook payload.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not isinstance(payload, dict):
            return Response(
                {
                    "code": "invalid_webhook_payload",
                    "detail": "Invalid webhook payload.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        event_type = str(
            payload.get("event")
            or ""
        ).strip()

        if not event_type:
            return Response(
                {
                    "code": "missing_webhook_event",
                    "detail": "Webhook event is missing.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        provider_event_id = str(
            request.headers.get(
                "X-Razorpay-Event-Id"
            )
            or payload.get("id")
            or ""
        ).strip()

        if not provider_event_id:
            return Response(
                {
                    "code": "missing_webhook_event_id",
                    "detail": (
                        "Webhook event ID is missing."
                    ),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            event, created = record_webhook_event(
                provider_event_id=provider_event_id,
                event_type=event_type,
                payload=payload,
            )

            if not created:
                if event.processing_status in {
                    PaymentEvent.ProcessingStatus.PROCESSED,
                    PaymentEvent.ProcessingStatus.IGNORED,
                }:
                    return Response(
                        {
                            "success": True,
                            "duplicate": True,
                        },
                        status=status.HTTP_200_OK,
                    )

            if event_type == "payment.captured":
                process_payment_captured_webhook(
                    event=event
                )

            elif event_type == "order.paid":
                process_order_paid_webhook(
                    event=event
                )

            elif event_type == "payment.failed":
                process_payment_failed_webhook(
                    event=event
                )
            else:
                with transaction.atomic():
                    event = (
                        PaymentEvent.objects
                        .select_for_update()
                        .get(pk=event.pk)
                    )

                    event.processing_status = (
                        PaymentEvent.ProcessingStatus.IGNORED
                    )
                    event.processed_at = timezone.now()
                    event.error_message = ""

                    event.save(
                        update_fields=[
                            "processing_status",
                            "processed_at",
                            "error_message",
                        ]
                    )

            return Response(
                {
                    "success": True
                },
                status=status.HTTP_200_OK,
            )

        except Exception:
            logger.exception(
                "Razorpay webhook processing failed",
                extra={
                    "event_type": event_type,
                    "provider_event_id": provider_event_id,
                },
            )

            return Response(
                {
                    "code": "webhook_processing_failed",
                    "detail": (
                        "Webhook could not be processed."
                    ),
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


def _payment_error_response(exc: Exception):
    if isinstance(exc, PaymentOwnershipError):
        return Response(
            {
                "code": "payment_not_found",
                "detail": "Order or payment not found.",
            },
            status=status.HTTP_404_NOT_FOUND,
        )

    if isinstance(exc, PaymentStateError):
        return Response(
            {
                "code": "invalid_payment_state",
                "detail": str(exc),
            },
            status=status.HTTP_409_CONFLICT,
        )

    if isinstance(exc, PaymentValidationError):
        return Response(
            {
                "code": "invalid_payment",
                "detail": str(exc),
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    if isinstance(exc, PaymentSignatureError):
        return Response(
            {
                "code": "invalid_payment_signature",
                "detail": "Payment verification failed.",
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    if isinstance(exc, PaymentGatewayError):
        return Response(
            {
                "code": "payment_gateway_error",
                "detail": "Unable to initialize payment.",
            },
            status=status.HTTP_502_BAD_GATEWAY,
        )

    if isinstance(exc, PaymentConfigurationError):
        return Response(
            {
                "code": "payment_unavailable",
                "detail": (
                    "Online payment is temporarily unavailable."
                ),
            },
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    if isinstance(exc, PaymentWebhookError):
        return Response(
            {
                "code": "invalid_webhook",
                "detail": "Invalid payment webhook.",
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    if isinstance(exc, PaymentServiceError):
        return Response(
            {
                "code": "payment_error",
                "detail": str(exc),
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    return None
