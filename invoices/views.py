from __future__ import annotations

"""
Production-grade read-only API for the custom BlazeLine Admin Invoice UI.

Endpoints expected by the custom Next.js admin:

    GET /api/admin/invoices/
    GET /api/admin/invoices/overview/
    GET /api/admin/invoices/<uuid:pk>/

Design goals
------------
- Admin-only access; invoice data is never public.
- Read-only: this module does not mutate invoice or invoice-item snapshots.
- Queryset optimized with select_related/prefetch_related.
- Stable, explicit ordering whitelist; no arbitrary ORM ordering from clients.
- Server-side pagination with a bounded page size.
- Search across invoice, order, customer and payment identifiers.
- Response shapes match the custom Next.js invoice admin service contract.
- Monetary values are returned as strings to preserve Decimal precision.
- Datetimes are returned as ISO-8601 strings.
- Overview counters are computed from the full invoice table, independent of
  the currently selected list filters.

The canonical commercial/customer snapshot lives on Invoice / InvoiceItem and
is intentionally surfaced as stored. This API never rebuilds invoice amounts
from a live cart, product or customer record.
"""

from decimal import Decimal
from typing import Any
from uuid import UUID

from django.db.models import Count, Prefetch, Q, QuerySet
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Invoice, InvoiceItem


# ============================================================================
# SECURITY
# ============================================================================


class IsAdminStaff(permissions.BasePermission):
    """
    Allow authenticated staff/superuser accounts only.

    This deliberately uses Django's standard `is_staff` capability because the
    custom frontend authenticates against the admin auth flow and sends the
    resulting access token as a Bearer token.
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


class AdminInvoicePagination(PageNumberPagination):
    """Bounded pagination for the admin invoice table."""

    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100
    page_query_param = "page"


# ============================================================================
# QUERY / SORT CONTRACT
# ============================================================================


INVOICE_STATUS_VALUES = {
    value for value, _label in Invoice.Status.choices
}

DELIVERY_STATUS_VALUES = {
    value for value, _label in Invoice.DeliveryStatus.choices
}

# Only these ORM fields can be requested by the browser.
ORDERING_FIELDS = {
    "invoice_number": "invoice_number",
    "issued_at": "issued_at",
    "updated_at": "updated_at",
    "grand_total": "grand_total",
    "customer_name": "customer_name",
    "payment_status": "payment_status",
    "payment_method": "payment_method",
    "status": "status",
    "email_status": "email_status",
    "whatsapp_status": "whatsapp_status",
}

DEFAULT_ORDERING = "-issued_at"
MAX_SEARCH_LENGTH = 200


# ============================================================================
# SERIALIZATION HELPERS
# ============================================================================


def _decimal(value: Decimal | None) -> str:
    """Serialize Decimal without losing precision."""

    return str(value if value is not None else Decimal("0.00"))


def _iso(value) -> str | None:
    """Serialize a Django datetime to ISO-8601."""

    return value.isoformat() if value else None


def _order_number(invoice: Invoice) -> str | None:
    order = getattr(invoice, "order", None)
    return getattr(order, "order_number", None) if order else None


def _serialize_item(item: InvoiceItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "product_name": item.product_name,
        "sku": item.sku,
        "variant_name": item.variant_name,
        "quantity": item.quantity,
        "unit_price": _decimal(item.unit_price),
        "tax_rate": _decimal(item.tax_rate),
        "tax_amount": _decimal(item.tax_amount),
        "discount_amount": _decimal(item.discount_amount),
        "line_total": _decimal(item.line_total),
        "currency": item.currency,
    }


def _serialize_invoice(
    invoice: Invoice,
    *,
    include_items: bool = True,
) -> dict[str, Any]:
    """
    Serialize the canonical stored invoice snapshot.

    `include_items=False` is available for callers that want a lightweight
    representation. The current Next.js admin expects the full `items` array,
    so the list endpoint uses the default `True` behaviour.
    """

    items = []
    if include_items:
        prefetched_items = getattr(invoice, "_prefetched_items", None)
        if prefetched_items is None:
            prefetched_items = invoice.items.all()

        items = [
            _serialize_item(item)
            for item in prefetched_items
        ]

    return {
        "id": str(invoice.pk),
        "invoice_number": invoice.invoice_number,
        "order_id": invoice.order_id,
        "order_number": _order_number(invoice),
        "customer_id": invoice.customer_id,
        "customer_name": invoice.customer_name,
        "customer_email": invoice.customer_email,
        "customer_phone": invoice.customer_phone,
        "company_name": invoice.company_name,
        "customer_gstin": invoice.customer_gstin,
        "billing_address_line1": invoice.billing_address_line1,
        "billing_address_line2": invoice.billing_address_line2,
        "billing_landmark": invoice.billing_landmark,
        "billing_city": invoice.billing_city,
        "billing_state": invoice.billing_state,
        "billing_pincode": invoice.billing_pincode,
        "currency": invoice.currency,
        "subtotal": _decimal(invoice.subtotal),
        "discount_amount": _decimal(invoice.discount_amount),
        "delivery_charge": _decimal(invoice.delivery_charge),
        "tax_amount": _decimal(invoice.tax_amount),
        "cod_fee": _decimal(invoice.cod_fee),
        "grand_total": _decimal(invoice.grand_total),
        "payment_method": invoice.payment_method,
        "payment_status": invoice.payment_status,
        "razorpay_payment_id": invoice.razorpay_payment_id,
        "pdf_url": invoice.pdf_url,
        "pdf_public_id": invoice.pdf_public_id,
        "status": invoice.status,
        "email_status": invoice.email_status,
        "email_sent_at": _iso(invoice.email_sent_at),
        "email_error": invoice.email_error,
        "whatsapp_status": invoice.whatsapp_status,
        "whatsapp_sent_at": _iso(invoice.whatsapp_sent_at),
        "whatsapp_error": invoice.whatsapp_error,
        "delivery_attempts": invoice.delivery_attempts,
        "last_error": invoice.last_error,
        "issued_at": _iso(invoice.issued_at),
        "updated_at": _iso(invoice.updated_at),
        "items": items,
    }


# ============================================================================
# BASE QUERYSET
# ============================================================================


def _base_invoice_queryset() -> QuerySet[Invoice]:
    """
    Queryset used by the admin endpoints.

    `order` is a OneToOne relation and `customer` is a foreign key, so
    select_related avoids per-row queries for the order/customer identity.

    Invoice items are prefetched once and stored on `_prefetched_items` so the
    serializer can use the same in-memory list instead of querying repeatedly.
    """

    items = InvoiceItem.objects.only(
        "id",
        "invoice_id",
        "product_name",
        "sku",
        "variant_name",
        "quantity",
        "unit_price",
        "tax_rate",
        "tax_amount",
        "discount_amount",
        "line_total",
        "currency",
    ).order_by("id")

    return (
        Invoice.objects
        .select_related("order", "customer")
        .prefetch_related(
            # Use a lightweight child queryset and expose it under a private
            # attribute used by the serializer.
            Prefetch("items", queryset=items, to_attr="_prefetched_items")
        )
        .all()
    )


# ============================================================================
# FILTERING / ORDERING
# ============================================================================


def _clean_search(raw: Any) -> str:
    value = str(raw or "").strip()
    return value[:MAX_SEARCH_LENGTH]


def _validate_choice(
    value: Any,
    *,
    allowed: set[str],
    parameter: str,
) -> tuple[str | None, Response | None]:
    if value in (None, ""):
        return None, None

    cleaned = str(value).strip().lower()
    if cleaned not in allowed:
        return None, Response(
            {
                "detail": f"Invalid {parameter}.",
                "code": f"invalid_{parameter}",
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    return cleaned, None


def _apply_filters(
    queryset: QuerySet[Invoice],
    request,
) -> tuple[QuerySet[Invoice], Response | None]:
    """Apply only documented, server-controlled invoice filters."""

    params = request.query_params

    status_value, error = _validate_choice(
        params.get("status"),
        allowed=INVOICE_STATUS_VALUES,
        parameter="status",
    )
    if error:
        return queryset, error

    email_status, error = _validate_choice(
        params.get("email_status"),
        allowed=DELIVERY_STATUS_VALUES,
        parameter="email_status",
    )
    if error:
        return queryset, error

    whatsapp_status, error = _validate_choice(
        params.get("whatsapp_status"),
        allowed=DELIVERY_STATUS_VALUES,
        parameter="whatsapp_status",
    )
    if error:
        return queryset, error

    payment_status = str(params.get("payment_status") or "").strip().lower()
    if payment_status:
        # PaymentStatus is stored as a plain CharField in Invoice rather than
        # a constrained TextChoices field, so support the current UI values
        # without inventing a closed backend enum.
        if len(payment_status) > 50:
            return queryset, Response(
                {
                    "detail": "payment_status is too long.",
                    "code": "invalid_payment_status",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

    search = _clean_search(params.get("q"))

    if status_value:
        queryset = queryset.filter(status=status_value)

    if email_status:
        queryset = queryset.filter(email_status=email_status)

    if whatsapp_status:
        queryset = queryset.filter(whatsapp_status=whatsapp_status)

    if payment_status:
        queryset = queryset.filter(payment_status__iexact=payment_status)

    if search:
        queryset = queryset.filter(
            Q(invoice_number__icontains=search)
            | Q(order__order_number__icontains=search)
            | Q(customer_name__icontains=search)
            | Q(customer_email__icontains=search)
            | Q(customer_phone__icontains=search)
            | Q(company_name__icontains=search)
            | Q(customer_gstin__icontains=search)
            | Q(razorpay_payment_id__icontains=search)
        )

    return queryset, None


def _safe_ordering(raw: Any) -> str:
    """
    Convert the browser's requested ordering to one of the explicit allowlist
    fields. Invalid values safely fall back to newest-first.
    """

    value = str(raw or "").strip()
    if not value:
        return DEFAULT_ORDERING

    descending = value.startswith("-")
    field = value[1:] if descending else value
    mapped = ORDERING_FIELDS.get(field)

    if mapped is None:
        return DEFAULT_ORDERING

    return f"-{mapped}" if descending else mapped


# ============================================================================
# API VIEWS
# ============================================================================


class InvoiceAdminListAPIView(APIView):
    """
    GET /admin/invoices/

    Paginated invoice list for the custom Next.js admin panel.
    """

    permission_classes = [IsAdminStaff]
    http_method_names = ["get", "head", "options"]

    def get(self, request):
        queryset = _base_invoice_queryset()
        queryset, error = _apply_filters(queryset, request)
        if error:
            return error

        queryset = queryset.order_by(
            _safe_ordering(request.query_params.get("ordering"))
        )

        paginator = AdminInvoicePagination()
        page = paginator.paginate_queryset(queryset, request, view=self)

        if page is None:
            return Response(
                {
                    "count": 0,
                    "next": None,
                    "previous": None,
                    "results": [],
                }
            )

        results = [
            _serialize_invoice(invoice, include_items=True)
            for invoice in page
        ]

        return paginator.get_paginated_response(results)


class InvoiceAdminOverviewAPIView(APIView):
    """
    GET /admin/invoices/overview/

    Returns dashboard counters across the complete invoice dataset.
    """

    permission_classes = [IsAdminStaff]
    http_method_names = ["get", "head", "options"]

    def get(self, request):
        queryset = Invoice.objects.all()

        counts = queryset.aggregate(
            total=Count("pk"),
            pending=Count("pk", filter=Q(status=Invoice.Status.PENDING)),
            generated=Count("pk", filter=Q(status=Invoice.Status.GENERATED)),
            sent=Count("pk", filter=Q(status=Invoice.Status.SENT)),
            failed=Count("pk", filter=Q(status=Invoice.Status.FAILED)),
            email_pending=Count(
                "pk",
                filter=Q(email_status=Invoice.DeliveryStatus.PENDING),
            ),
            email_sent=Count(
                "pk",
                filter=Q(email_status=Invoice.DeliveryStatus.SENT),
            ),
            email_failed=Count(
                "pk",
                filter=Q(email_status=Invoice.DeliveryStatus.FAILED),
            ),
            whatsapp_pending=Count(
                "pk",
                filter=Q(whatsapp_status=Invoice.DeliveryStatus.PENDING),
            ),
            whatsapp_sent=Count(
                "pk",
                filter=Q(whatsapp_status=Invoice.DeliveryStatus.SENT),
            ),
            whatsapp_failed=Count(
                "pk",
                filter=Q(whatsapp_status=Invoice.DeliveryStatus.FAILED),
            ),
        )

        return Response(
            {
                **counts,
                "generated_at": timezone.now().isoformat(),
            }
        )


class InvoiceAdminDetailAPIView(APIView):
    """
    GET /admin/invoices/<uuid>/

    Returns the complete stored invoice snapshot and all immutable line items.
    """

    permission_classes = [IsAdminStaff]
    http_method_names = ["get", "head", "options"]

    def get(self, request, pk: UUID):
        try:
            invoice = (
                _base_invoice_queryset()
                .filter(pk=pk)
                .first()
            )
        except (TypeError, ValueError):
            invoice = None

        if invoice is None:
            return Response(
                {
                    "detail": "Invoice not found.",
                    "code": "invoice_not_found",
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response(
            _serialize_invoice(invoice, include_items=True)
        )
