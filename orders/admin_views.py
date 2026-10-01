from __future__ import annotations

import logging

from django.db.models import Q
from rest_framework import permissions, status
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from .admin_direct_serializers import DirectOrderCreateSerializer
from .direct_order_service import (
    DirectOrderError,
    DirectOrderIdempotencyConflict,
    DirectOrderValidationError,
    create_direct_order,
)
from .models import Order, PaymentLink
from .payment_link_service import (
    PaymentLinkServiceError,
    PaymentLinkStateError,
    create_razorpay_payment_link,
)
from .serializers import OrderSerializer


logger = logging.getLogger(__name__)


# ============================================================================
# SECURITY
# ============================================================================


class IsAdminStaff(permissions.BasePermission):
    """
    Staff/superuser access only.

    The Admin frontend authenticates with the staff/admin auth flow and sends
    the resulting access token as a Bearer token.
    """

    message = "Administrator access is required."

    def has_permission(self, request, view) -> bool:
        user = request.user

        return bool(
            user
            and user.is_authenticated
            and (
                getattr(user, "is_staff", False)
                or getattr(user, "is_superuser", False)
            )
        )


# ============================================================================
# PAGINATION
# ============================================================================


class DirectOrderPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100
    page_query_param = "page"


# ============================================================================
# HELPERS
# ============================================================================


def _serialize_payment_link(
    payment_link: PaymentLink | None,
) -> dict | None:
    if payment_link is None:
        return None

    return {
        "id": str(payment_link.id),
        "provider": payment_link.provider,
        "provider_link_id": payment_link.provider_link_id,
        "reference_id": payment_link.reference_id,
        "short_url": payment_link.short_url,
        "amount": str(payment_link.amount),
        "currency": payment_link.currency,
        "status": payment_link.status,
        "expires_at": (
            payment_link.expires_at.isoformat()
            if payment_link.expires_at
            else None
        ),
        "paid_at": (
            payment_link.paid_at.isoformat()
            if payment_link.paid_at
            else None
        ),
        "cancelled_at": (
            payment_link.cancelled_at.isoformat()
            if payment_link.cancelled_at
            else None
        ),
        "created_at": payment_link.created_at.isoformat(),
        "updated_at": payment_link.updated_at.isoformat(),
    }


def _direct_order_detail(order: Order) -> dict:
    latest_payment_link = (
        PaymentLink.objects
        .filter(
            payment__order=order,
        )
        .order_by("-created_at")
        .first()
    )

    return {
        "order": OrderSerializer(order).data,
        "payment_link": _serialize_payment_link(
            latest_payment_link
        ),
    }


def _direct_order_error_response(
    exc: Exception,
):
    if isinstance(
        exc,
        DirectOrderIdempotencyConflict,
    ):
        return Response(
            {
                "code": "idempotency_conflict",
                "detail": str(exc),
            },
            status=status.HTTP_409_CONFLICT,
        )

    if isinstance(
        exc,
        DirectOrderValidationError,
    ):
        return Response(
            {
                "code": "invalid_direct_order",
                "detail": str(exc),
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    if isinstance(
        exc,
        PaymentLinkStateError,
    ):
        return Response(
            {
                "code": "invalid_payment_link_state",
                "detail": str(exc),
            },
            status=status.HTTP_409_CONFLICT,
        )

    if isinstance(
        exc,
        PaymentLinkServiceError,
    ):
        return Response(
            {
                "code": "payment_link_error",
                "detail": str(exc),
            },
            status=status.HTTP_502_BAD_GATEWAY,
        )

    if isinstance(
        exc,
        DirectOrderError,
    ):
        return Response(
            {
                "code": "direct_order_error",
                "detail": str(exc),
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    return None


# ============================================================================
# CREATE DIRECT ORDER
# ============================================================================


class AdminDirectOrderCreateAPIView(APIView):
    """
    POST /api/admin/direct-orders/create/

    Creates a normal BlazeLine Order with source='direct'.

    The browser can submit product names, rates, quantities and discounts,
    but the final commercial values are recalculated server-side.
    """

    permission_classes = [
        permissions.IsAuthenticated,
        IsAdminStaff,
    ]

    http_method_names = [
        "post",
        "options",
    ]

    def post(self, request):
        serializer = DirectOrderCreateSerializer(
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

        try:
            order = create_direct_order(
                idempotency_key=idempotency_key,
                customer_data=serializer.validated_data[
                    "customer"
                ],
                shipping=serializer.validated_data[
                    "shipping"
                ],
                items=serializer.validated_data[
                    "items"
                ],
                delivery_charge=serializer.validated_data[
                    "delivery_charge"
                ],
                currency=serializer.validated_data[
                    "currency"
                ],
                notes=serializer.validated_data.get(
                    "notes",
                    "",
                ),
            )

        except Exception as exc:
            response = _direct_order_error_response(
                exc
            )

            if response is not None:
                return response

            logger.exception(
                "Unexpected Direct Order creation failure",
                extra={
                    "admin_user_id": getattr(
                        request.user,
                        "pk",
                        None,
                    )
                },
            )

            return Response(
                {
                    "code": "direct_order_creation_failed",
                    "detail": (
                        "Unable to create the Direct Order right now."
                    ),
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        order = (
            Order.objects
            .prefetch_related(
                "items",
                "payments",
            )
            .get(pk=order.pk)
        )

        return Response(
            {
                "success": True,
                **_direct_order_detail(order),
            },
            status=status.HTTP_201_CREATED,
        )


# ============================================================================
# CREATE PAYMENT LINK
# ============================================================================


class AdminDirectOrderPaymentLinkAPIView(
    APIView
):
    """
    POST /api/admin/direct-orders/<order_number>/payment-link/

    Creates or reuses the active Razorpay Payment Link for a Direct Order.
    """

    permission_classes = [
        permissions.IsAuthenticated,
        IsAdminStaff,
    ]

    http_method_names = [
        "post",
        "options",
    ]

    def post(
        self,
        request,
        order_number,
    ):
        order = (
            Order.objects
            .select_related("customer")
            .filter(
                order_number=order_number,
                source=Order.Source.DIRECT,
            )
            .first()
        )

        if not order:
            return Response(
                {
                    "code": "direct_order_not_found",
                    "detail": "Direct Order not found.",
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            payment_link = (
                create_razorpay_payment_link(
                    order=order
                )
            )

        except Exception as exc:
            response = _direct_order_error_response(
                exc
            )

            if response is not None:
                return response

            logger.exception(
                "Unexpected Payment Link creation failure",
                extra={
                    "admin_user_id": getattr(
                        request.user,
                        "pk",
                        None,
                    ),
                    "order_number": order.order_number,
                },
            )

            return Response(
                {
                    "code": "payment_link_creation_failed",
                    "detail": (
                        "Unable to create the Payment Link right now."
                    ),
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(
            {
                "success": True,
                "order_number": order.order_number,
                "payment_link": _serialize_payment_link(
                    payment_link
                ),
            },
            status=status.HTTP_200_OK,
        )


# ============================================================================
# DIRECT ORDER LIST
# ============================================================================


class AdminDirectOrderListAPIView(
    APIView
):
    """
    GET /api/admin/direct-orders/

    Admin-only list of Direct Orders.
    """

    permission_classes = [
        permissions.IsAuthenticated,
        IsAdminStaff,
    ]

    http_method_names = [
        "get",
        "head",
        "options",
    ]

    def get(self, request):
        queryset = (
            Order.objects
            .filter(
                source=Order.Source.DIRECT,
            )
            .select_related("customer")
            .prefetch_related(
                "items",
                "payments",
            )
            .order_by("-created_at")
        )

        search = str(
            request.query_params.get(
                "q",
                ""
            )
            or ""
        ).strip()

        if search:
            search = search[:200]

            queryset = queryset.filter(
                Q(order_number__icontains=search)
                | Q(
                    customer__full_name__icontains=search
                )
                | Q(
                    customer__email__icontains=search
                )
                | Q(
                    customer__phone__icontains=search
                )
                | Q(
                    shipping_full_name__icontains=search
                )
                | Q(
                    shipping_email__icontains=search
                )
                | Q(
                    shipping_phone__icontains=search
                )
            )

        paginator = DirectOrderPagination()

        page = paginator.paginate_queryset(
            queryset,
            request,
            view=self,
        )

        data = []

        for order in page:
            latest_payment_link = (
                PaymentLink.objects
                .filter(
                    payment__order=order,
                )
                .order_by("-created_at")
                .first()
            )

            data.append(
                {
                    "id": str(order.id),
                    "order_number": order.order_number,
                    "status": order.status,
                    "payment_status": order.payment_status,
                    "payment_method": order.payment_method,
                    "currency": order.currency,
                    "grand_total": str(
                        order.grand_total
                    ),
                    "customer": {
                        "full_name": (
                            order.customer.full_name
                        ),
                        "email": (
                            order.customer.email
                        ),
                        "phone": (
                            order.customer.phone
                        ),
                    },
                    "item_count": order.items.count(),
                    "payment_link": _serialize_payment_link(
                        latest_payment_link
                    ),
                    "created_at": (
                        order.created_at.isoformat()
                    ),
                    "paid_at": (
                        order.paid_at.isoformat()
                        if order.paid_at
                        else None
                    ),
                }
            )

        return paginator.get_paginated_response(
            data
        )


# ============================================================================
# DIRECT ORDER DETAIL
# ============================================================================


class AdminDirectOrderDetailAPIView(
    APIView
):
    """
    GET /api/admin/direct-orders/<order_number>/
    """

    permission_classes = [
        permissions.IsAuthenticated,
        IsAdminStaff,
    ]

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
        order = (
            Order.objects
            .filter(
                order_number=order_number,
                source=Order.Source.DIRECT,
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
                    "code": "direct_order_not_found",
                    "detail": "Direct Order not found.",
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response(
            _direct_order_detail(order),
            status=status.HTTP_200_OK,
        )