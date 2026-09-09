from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import DatabaseError
from django.db.models import Count, IntegerField, Q, Sum, Value
from django.db.models.functions import Cast, Coalesce
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from rest_framework import permissions, serializers, status
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import Customer
from catalog.models import Product, ProductVariant

from .models import AnalyticsEvent, AnalyticsSession

from .services import get_or_create_session, track_event
from .permissions import IsAnalyticsAdmin

# =============================================================================
# Analytics request safety limits
# =============================================================================

MAX_EVENT_PAYLOAD_BYTES = 32 * 1024
MAX_METADATA_BYTES = 16 * 1024

MAX_REPORT_DAYS = 365
DEFAULT_REPORT_DAYS = 30

PRODUCT_ANALYTICS_VIEW_EVENT = "product_viewed"
PRODUCT_ANALYTICS_ADD_EVENT = "cart_item_added"
PRODUCT_ANALYTICS_REMOVE_EVENT = "cart_item_removed"
PRODUCT_ANALYTICS_ORDER_EVENT = "checkout_order_created"


# =============================================================================
# Serializer
# =============================================================================


class AnalyticsEventSerializer(serializers.Serializer):
    """
    Strict validation for browser analytics events.

    Analytics is observational data only.
    It must never become a source of commercial truth
    or a mechanism for modifying application state.
    """

    event_name = serializers.CharField(
        max_length=100,
        trim_whitespace=True,
    )

    session_id = serializers.UUIDField(
        required=False,
        allow_null=True,
    )

    guest_id = serializers.UUIDField(
        required=False,
        allow_null=True,
    )

    product_id = serializers.IntegerField(
        required=False,
        allow_null=True,
        min_value=1,
    )

    variant_id = serializers.IntegerField(
        required=False,
        allow_null=True,
        min_value=1,
    )

    path = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=500,
    )

    page_title = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=255,
    )

    metadata = serializers.DictField(
        required=False,
        default=dict,
    )

    def validate_event_name(
        self,
        value: str,
    ) -> str:
        value = value.strip()

        if not value:
            raise serializers.ValidationError(
                "Event name is required."
            )

        return value

    def validate_metadata(
        self,
        value: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Prevent oversized analytics metadata from becoming
        a cheap memory/database abuse vector.
        """

        try:
            encoded = json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        except (TypeError, ValueError):
            raise serializers.ValidationError(
                "Metadata contains unsupported values."
            )

        if len(encoded.encode("utf-8")) > MAX_METADATA_BYTES:
            raise serializers.ValidationError(
                f"Metadata cannot exceed "
                f"{MAX_METADATA_BYTES // 1024} KB."
            )

        return value


# =============================================================================
# Product / Variant Resolution
# =============================================================================


def _resolve_product_and_variant(
    *,
    product_id: int | None,
    variant_id: int | None,
    metadata: dict[str, Any],
) -> tuple[Product | None, ProductVariant | None]:
    """
    Resolve browser-supplied product/variant identifiers into real
    catalog objects.

    Compatibility:
    - New payloads use top-level product_id / variant_id.
    - Older payloads may still contain productId / variantId
      inside metadata.

    Safety:
    - Product and variant IDs are resolved against the real catalog.
    - A supplied variant must exist.
    - A supplied variant must belong to the supplied product.
    - Invalid identifiers never break analytics ingestion.
    """

    # -------------------------------------------------------------------------
    # Resolve product ID
    # -------------------------------------------------------------------------

    resolved_product_id = product_id

    if resolved_product_id is None:
        legacy_product_id = metadata.get("productId")

        if isinstance(
            legacy_product_id,
            int,
        ):
            if legacy_product_id > 0:
                resolved_product_id = legacy_product_id

        elif isinstance(
            legacy_product_id,
            str,
        ):
            try:
                parsed_product_id = int(
                    legacy_product_id.strip()
                )
            except (TypeError, ValueError):
                parsed_product_id = 0

            if parsed_product_id > 0:
                resolved_product_id = parsed_product_id

    # -------------------------------------------------------------------------
    # Resolve variant ID
    # -------------------------------------------------------------------------

    resolved_variant_id = variant_id

    if resolved_variant_id is None:
        legacy_variant_id = metadata.get("variantId")

        if isinstance(
            legacy_variant_id,
            int,
        ):
            if legacy_variant_id > 0:
                resolved_variant_id = legacy_variant_id

        elif isinstance(
            legacy_variant_id,
            str,
        ):
            try:
                parsed_variant_id = int(
                    legacy_variant_id.strip()
                )
            except (TypeError, ValueError):
                parsed_variant_id = 0

            if parsed_variant_id > 0:
                resolved_variant_id = parsed_variant_id

    # -------------------------------------------------------------------------
    # Resolve variant first.
    #
    # IMPORTANT:
    # Product does not expose an `is_active` field in the current catalog
    # model, so do not use product__is_active here.
    # -------------------------------------------------------------------------

    variant: ProductVariant | None = None

    if resolved_variant_id is not None:
        variant = (
            ProductVariant.objects
            .select_related("product")
            .filter(
                id=resolved_variant_id,
            )
            .first()
        )

        if variant is None:
            return None, None

    # -------------------------------------------------------------------------
    # Resolve product.
    #
    # IMPORTANT:
    # Do not filter by `is_active` because the current Product model
    # does not define that field.
    # -------------------------------------------------------------------------

    product: Product | None = None

    if resolved_product_id is not None:
        product = (
            Product.objects
            .filter(
                id=resolved_product_id,
            )
            .first()
        )

        if product is None:
            return None, None

    # -------------------------------------------------------------------------
    # Variant-only request:
    # safely derive product from variant.
    # -------------------------------------------------------------------------

    if (
        product is None
        and variant is not None
    ):
        product = variant.product

    # -------------------------------------------------------------------------
    # Both supplied:
    # ensure the variant belongs to the product.
    # -------------------------------------------------------------------------

    if (
        product is not None
        and variant is not None
        and variant.product_id != product.id
    ):
        return None, None

    return product, variant


# =============================================================================
# Analytics Event API
# =============================================================================


class AnalyticsEventAPIView(APIView):
    """
    POST /api/analytics/events/

    Receives browser-side behavioral analytics.

    Security principles:
    - Anonymous access is required for guest analytics.
    - A valid customer JWT may identify the request.
    - Client analytics never controls commercial data.
    - Product/variant identifiers are resolved server-side.
    - Analytics failures never propagate into the customer experience.
    """

    permission_classes = [
        permissions.AllowAny,
    ]

    http_method_names = [
        "post",
        "options",
    ]

    def post(
        self,
        request,
    ):
        # =====================================================================
        # Request-size guard
        # =====================================================================

        content_length = request.META.get(
            "CONTENT_LENGTH"
        )

        if content_length:
            try:
                request_size = int(
                    content_length
                )
            except (TypeError, ValueError):
                request_size = 0

            if request_size > MAX_EVENT_PAYLOAD_BYTES:
                return Response(
                    {
                        "success": False,
                        "detail": "Analytics payload is too large.",
                    },
                    status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                )

        # =====================================================================
        # Validate request payload
        # =====================================================================

        serializer = AnalyticsEventSerializer(
            data=request.data,
        )

        serializer.is_valid(
            raise_exception=True,
        )

        data = serializer.validated_data

        # =====================================================================
        # Resolve authenticated customer
        # =====================================================================

        customer: Customer | None = None

        request_user = getattr(
            request,
            "user",
            None,
        )

        if (
            getattr(
                request_user,
                "is_authenticated",
                False,
            )
            and isinstance(
                request_user,
                Customer,
            )
        ):
            customer = request_user

        # =====================================================================
        # Browser page context
        #
        # IMPORTANT:
        # The browser page path is analytics context.
        # It is not the analytics API endpoint path.
        # =====================================================================

        page_path = str(
            data.get(
                "path",
                "",
            )
            or ""
        ).strip()[:500]

        page_title = str(
            data.get(
                "page_title",
                "",
            )
            or ""
        ).strip()[:255]

        guest_id = data.get(
            "guest_id"
        )

        metadata: dict[str, Any] = {
            **data.get(
                "metadata",
                {},
            ),
            "page_title": page_title,
        }

        # =====================================================================
        # Resolve catalog objects
        # =====================================================================

        product: Product | None = None
        variant: ProductVariant | None = None

        try:
            product, variant = _resolve_product_and_variant(
                product_id=data.get(
                    "product_id"
                ),
                variant_id=data.get(
                    "variant_id"
                ),
                metadata=metadata,
            )
        except Exception:
            # Product analytics must never break browsing.
            product = None
            variant = None

        # =====================================================================
        # Resolve analytics session
        # =====================================================================

        try:
            session = get_or_create_session(
                session_id=data.get(
                    "session_id"
                ),
                customer=customer,
                guest_id=guest_id,
                request=request,
                landing_page=page_path,
            )
        except (
            DatabaseError,
            ValidationError,
            ValueError,
        ):
            return Response(
                {
                    "success": False,
                    "detail": "Analytics temporarily unavailable.",
                },
                status=status.HTTP_202_ACCEPTED,
            )
        except Exception:
            return Response(
                {
                    "success": False,
                    "detail": "Analytics temporarily unavailable.",
                },
                status=status.HTTP_202_ACCEPTED,
            )

        # =====================================================================
        # Persist event
        # =====================================================================

        try:
            event = track_event(
                event_name=data["event_name"],
                request=request,
                session=session,
                customer=customer,
                guest_id=guest_id,
                product=product,
                variant=variant,
                page_path=page_path,
                metadata=metadata,
            )
        except Exception:
            # Absolute analytics safety boundary:
            # never break the customer-facing application.
            event = None

        # =====================================================================
        # Response
        # =====================================================================

        return Response(
            {
                "success": True,
                "accepted": event is not None,
                "session_id": str(
                    session.id
                ),
            },
            status=status.HTTP_202_ACCEPTED,
        )


# =============================================================================
# Product Analytics
# =============================================================================


class ProductAnalyticsPagination(PageNumberPagination):
    """
    Controlled pagination for admin product analytics.

    Keeps response sizes bounded while remaining easy for an admin
    dashboard to consume.
    """

    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100


def _parse_report_datetime(
    value: str | None,
    *,
    is_end: bool = False,
) -> datetime | None:
    """
    Parse ISO datetime/date query parameters.

    Date-only values are interpreted in the server timezone:
    - start => beginning of that date
    - end   => beginning of the following date
    """

    if not value:
        return None

    value = value.strip()

    parsed_datetime = parse_datetime(value)

    if parsed_datetime is not None:
        if timezone.is_naive(parsed_datetime):
            return timezone.make_aware(
                parsed_datetime,
                timezone.get_current_timezone(),
            )

        return parsed_datetime

    parsed_date = parse_date(value)

    if parsed_date is None:
        raise serializers.ValidationError(
            f"Invalid date/datetime: {value}"
        )

    start_of_day = timezone.make_aware(
        datetime.combine(
            parsed_date,
            datetime.min.time(),
        ),
        timezone.get_current_timezone(),
    )

    if is_end:
        return start_of_day + timedelta(days=1)

    return start_of_day


def _resolve_report_window(
    request,
) -> tuple[datetime, datetime]:
    """
    Resolve reporting window.

    Supported:
      ?days=30
      ?start=2026-09-01
      ?end=2026-09-06

    Explicit start/end takes precedence over days.
    """

    now = timezone.now()

    start_raw = request.query_params.get("start")
    end_raw = request.query_params.get("end")

    if start_raw or end_raw:
        start = _parse_report_datetime(
            start_raw,
            is_end=False,
        )
        end = _parse_report_datetime(
            end_raw,
            is_end=True,
        )

        if start is None:
            raise serializers.ValidationError(
                "Both start and end are required when using a custom range."
            )

        if end is None:
            end = now

    else:
        raw_days = request.query_params.get(
            "days",
            str(DEFAULT_REPORT_DAYS),
        )

        try:
            days = int(raw_days)
        except (TypeError, ValueError):
            raise serializers.ValidationError(
                "days must be an integer."
            )

        if days < 1:
            raise serializers.ValidationError(
                "days must be at least 1."
            )

        if days > MAX_REPORT_DAYS:
            raise serializers.ValidationError(
                f"days cannot exceed {MAX_REPORT_DAYS}."
            )

        end = now
        start = end - timedelta(days=days)

    if start >= end:
        raise serializers.ValidationError(
            "start must be earlier than end."
        )

    # Hard safety cap for custom date ranges too.
    if end - start > timedelta(days=MAX_REPORT_DAYS):
        raise serializers.ValidationError(
            f"Report range cannot exceed {MAX_REPORT_DAYS} days."
        )

    return start, end


# =============================================================================
# Live Activity / Event Explorer
# =============================================================================


class AnalyticsActivityPagination(PageNumberPagination):
    """
    Controlled pagination for the admin activity stream.

    The activity feed is intentionally bounded because it can become a
    high-volume read path in production.
    """

    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 100


def _safe_activity_value(
    value: Any,
    *,
    max_length: int,
) -> str:
    """
    Normalize observational strings before they are returned to the admin UI.
    """

    if value is None:
        return ""

    return str(value).strip()[:max_length]


def _activity_client_context(
    *,
    user_agent: str,
    device: str,
    browser: str,
    os_name: str,
) -> tuple[str, str, str]:
    """
    Best-effort fallback for older sessions that predate UA classification.

    The persisted analytics session remains the source of truth when a value
    is present; this only fills presentation gaps from the observed UA.
    """
    ua = str(user_agent or "")
    low = ua.lower()

    resolved_device = str(device or "").strip()
    resolved_browser = str(browser or "").strip()
    resolved_os = str(os_name or "").strip()

    if not resolved_device:
        if "ipad" in low or "tablet" in low or ("android" in low and "mobile" not in low):
            resolved_device = "Tablet"
        elif "mobile" in low or "iphone" in low or "ipod" in low or "windows phone" in low:
            resolved_device = "Mobile"
        elif ua:
            resolved_device = "Desktop"

    if not resolved_browser:
        if "edg/" in low:
            resolved_browser = "Edge"
        elif "opr/" in low or "opera" in low:
            resolved_browser = "Opera"
        elif "samsungbrowser/" in low:
            resolved_browser = "Samsung Internet"
        elif "firefox/" in low:
            resolved_browser = "Firefox"
        elif "crios/" in low or "chrome/" in low:
            resolved_browser = "Chrome"
        elif "safari/" in low and "chrome/" not in low and "crios/" not in low:
            resolved_browser = "Safari"
        elif ua:
            resolved_browser = "Other"

    if not resolved_os:
        if "windows nt" in low:
            resolved_os = "Windows"
        elif "iphone os" in low or "ipad; cpu os" in low or "cpu os" in low:
            resolved_os = "iOS"
        elif "mac os x" in low:
            resolved_os = "macOS"
        elif "android" in low:
            resolved_os = "Android"
        elif "linux" in low:
            resolved_os = "Linux"
        elif ua:
            resolved_os = "Other"

    return resolved_device[:100], resolved_browser[:100], resolved_os[:100]


def _activity_session_context(session: AnalyticsSession | None) -> dict[str, Any] | None:
    """
    Serialize only observational session metadata useful to an administrator.

    Attribute fallbacks make this response tolerant of the current analytics
    model naming without changing persisted data.
    """

    if session is None:
        return None

    ip_address = getattr(
        session,
        "ip_address",
        None,
    )
    if not ip_address:
        ip_address = getattr(
            session,
            "ip",
            None,
        )

    user_agent = getattr(
        session,
        "user_agent",
        None,
    )
    if not user_agent:
        user_agent = getattr(
            session,
            "ua",
            None,
        )

    resolved_device, resolved_browser, resolved_os = _activity_client_context(
        user_agent=str(user_agent or ""),
        device=str(getattr(session, "device", "") or ""),
        browser=str(getattr(session, "browser", "") or ""),
        os_name=str(getattr(session, "os", "") or ""),
    )

    return {
        "id": str(session.id),
        "device": _safe_activity_value(
            resolved_device,
            max_length=100,
        ),
        "browser": _safe_activity_value(
            resolved_browser,
            max_length=100,
        ),
        "os": _safe_activity_value(
            resolved_os,
            max_length=100,
        ),
        "country": _safe_activity_value(
            getattr(session, "country", ""),
            max_length=100,
        ),
        "source": _safe_activity_value(
            getattr(session, "source", ""),
            max_length=255,
        ),
        "landing_page": _safe_activity_value(
            getattr(session, "landing_page", ""),
            max_length=500,
        ),
        "last_path": _safe_activity_value(
            getattr(session, "last_path", ""),
            max_length=500,
        ),
        "ip_address": _safe_activity_value(
            ip_address,
            max_length=64,
        ),
        "user_agent": _safe_activity_value(
            user_agent,
            max_length=1000,
        ),
        "utm_source": _safe_activity_value(
            getattr(session, "utm_source", ""),
            max_length=255,
        ),
        "utm_medium": _safe_activity_value(
            getattr(session, "utm_medium", ""),
            max_length=255,
        ),
        "utm_campaign": _safe_activity_value(
            getattr(session, "utm_campaign", ""),
            max_length=255,
        ),
        "utm_term": _safe_activity_value(
            getattr(session, "utm_term", ""),
            max_length=255,
        ),
        "utm_content": _safe_activity_value(
            getattr(session, "utm_content", ""),
            max_length=255,
        ),
        "first_seen_at": (
            session.first_seen_at.isoformat()
            if getattr(session, "first_seen_at", None)
            else None
        ),
        "last_seen_at": (
            session.last_seen_at.isoformat()
            if getattr(session, "last_seen_at", None)
            else None
        ),
        "is_active": bool(
            getattr(
                session,
                "is_active",
                False,
            )
        ),
    }


def _serialize_activity_event(
    event: AnalyticsEvent,
) -> dict[str, Any]:
    """
    Convert one AnalyticsEvent into a bounded, admin-safe representation.

    This function is read-only and never trusts browser-supplied fields as
    commercial truth.
    """

    customer = getattr(
        event,
        "customer",
        None,
    )
    product = getattr(
        event,
        "product",
        None,
    )
    variant = getattr(
        event,
        "variant",
        None,
    )
    category = getattr(
        event,
        "category",
        None,
    )
    subcategory = getattr(
        event,
        "subcategory",
        None,
    )
    order = getattr(
        event,
        "order",
        None,
    )
    session = getattr(
        event,
        "session",
        None,
    )

    return {
        "id": str(event.id),
        "event_name": event.event_name,
        "occurred_at": (
            event.occurred_at.isoformat()
            if event.occurred_at
            else None
        ),
        "path": _safe_activity_value(
            getattr(
                event,
                "path",
                "",
            ),
            max_length=500,
        ),
        "customer": (
            {
                "id": customer.id,
                "name": _safe_activity_value(
                    getattr(
                        customer,
                        "full_name",
                        "",
                    ),
                    max_length=150,
                ),
                "email": _safe_activity_value(
                    getattr(
                        customer,
                        "email",
                        "",
                    ),
                    max_length=255,
                ),
            }
            if customer is not None
            else None
        ),
        "guest_id": (
            str(event.guest_id)
            if event.guest_id
            else None
        ),
        "identity_type": (
            "customer"
            if customer is not None
            else "guest"
        ),
        "product": (
            {
                "id": product.id,
                "name": _safe_activity_value(
                    getattr(
                        product,
                        "name",
                        "",
                    ),
                    max_length=255,
                ),
                "brand": _safe_activity_value(
                    getattr(
                        product,
                        "brand",
                        "",
                    ),
                    max_length=150,
                ),
            }
            if product is not None
            else None
        ),
        "variant": (
            {
                "id": variant.id,
                "sku": _safe_activity_value(
                    getattr(
                        variant,
                        "sku",
                        "",
                    ),
                    max_length=120,
                ),
            }
            if variant is not None
            else None
        ),
        "category": (
            getattr(
                category,
                "name",
                "",
            )
            if category is not None
            else ""
        ),
        "subcategory": (
            getattr(
                subcategory,
                "name",
                "",
            )
            if subcategory is not None
            else ""
        ),
        "order": (
            {
                "id": order.id,
                "order_number": _safe_activity_value(
                    getattr(
                        order,
                        "order_number",
                        "",
                    ),
                    max_length=100,
                ),
                "status": _safe_activity_value(
                    getattr(
                        order,
                        "status",
                        "",
                    ),
                    max_length=50,
                ),
                "payment_status": _safe_activity_value(
                    getattr(
                        order,
                        "payment_status",
                        "",
                    ),
                    max_length=50,
                ),
            }
            if order is not None
            else None
        ),
        "session": _activity_session_context(
            session
        ),
        "metadata": (
            event.metadata
            if isinstance(
                getattr(
                    event,
                    "metadata",
                    None,
                ),
                dict,
            )
            else {}
        ),
    }


class AnalyticsActivityAPIView(APIView):
    """
    GET /api/analytics/activity/

    Admin-only read model for the live activity / event explorer.

    Supported filters:
      ?days=1
      ?start=2026-09-09
      ?end=2026-09-10
      ?event=product_viewed
      ?search=chrome
      ?customer_id=123
      ?session_id=<uuid>
      ?ordering=-occurred_at
      ?page=1
      ?page_size=50

    Security:
      - Analytics administrator permission is required.
      - Read-only endpoint.
      - No browser-supplied analytics identity is trusted for authorization.
      - Pagination and date bounds keep response sizes predictable.
    """

    permission_classes = [
        IsAnalyticsAdmin,
    ]

    http_method_names = [
        "get",
        "options",
    ]

    pagination_class = AnalyticsActivityPagination

    ORDERING_MAP = {
        "occurred_at": "occurred_at",
        "-occurred_at": "-occurred_at",
        "event_name": "event_name",
        "-event_name": "-event_name",
    }

    def get(
        self,
        request,
    ):
        # =====================================================================
        # Date window
        # =====================================================================

        try:
            start, end = _resolve_report_window(
                request
            )
        except serializers.ValidationError as exc:
            return Response(
                {
                    "success": False,
                    "detail": exc.detail,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # =====================================================================
        # Filters
        # =====================================================================

        event_name = str(
            request.query_params.get(
                "event",
                "",
            )
            or ""
        ).strip()[:100]

        search_query = str(
            request.query_params.get(
                "search",
                "",
            )
            or ""
        ).strip()[:255]

        customer_id_raw = str(
            request.query_params.get(
                "customer_id",
                "",
            )
            or ""
        ).strip()

        session_id_raw = str(
            request.query_params.get(
                "session_id",
                "",
            )
            or ""
        ).strip()

        # =====================================================================
        # Base read-only queryset
        # =====================================================================

        events = (
            AnalyticsEvent.objects
            .select_related(
                "session",
                "customer",
                "product",
                "variant",
                "category",
                "subcategory",
                "order",
            )
            .filter(
                occurred_at__gte=start,
                occurred_at__lt=end,
            )
        )

        if event_name:
            events = events.filter(
                event_name=event_name,
            )

        if customer_id_raw:
            try:
                customer_id = int(
                    customer_id_raw
                )
            except (
                TypeError,
                ValueError,
            ):
                return Response(
                    {
                        "success": False,
                        "detail": "customer_id must be an integer.",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if customer_id < 1:
                return Response(
                    {
                        "success": False,
                        "detail": "customer_id must be at least 1.",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            events = events.filter(
                customer_id=customer_id,
            )

        if session_id_raw:
            try:
                session_uuid = UUID(
                    session_id_raw
                )
            except (
                TypeError,
                ValueError,
            ):
                return Response(
                    {
                        "success": False,
                        "detail": "session_id must be a valid UUID.",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            events = events.filter(
                session_id=session_uuid,
            )

        if search_query:
            events = events.filter(
                Q(event_name__icontains=search_query)
                | Q(path__icontains=search_query)
                | Q(customer__email__icontains=search_query)
                | Q(customer__full_name__icontains=search_query)
                | Q(product__name__icontains=search_query)
                | Q(product__brand__icontains=search_query)
                | Q(order__order_number__icontains=search_query)
            )

        # =====================================================================
        # Stable ordering
        # =====================================================================

        requested_ordering = str(
            request.query_params.get(
                "ordering",
                "-occurred_at",
            )
            or "-occurred_at"
        ).strip()

        ordering = self.ORDERING_MAP.get(
            requested_ordering
        )

        if ordering is None:
            ordering = "-occurred_at"

        events = events.order_by(
            ordering,
            "id",
        )

        # =====================================================================
        # Summary for the filtered feed
        # =====================================================================

        summary = events.aggregate(
            total_events=Count("id"),
            unique_sessions=Count(
                "session",
                distinct=True,
            ),
            unique_customers=Count(
                "customer",
                distinct=True,
            ),
            authenticated_events=Count(
                "id",
                filter=Q(
                    customer__isnull=False
                ),
            ),
            guest_events=Count(
                "id",
                filter=Q(
                    customer__isnull=True
                ),
            ),
        )

        # =====================================================================
        # Event breakdown
        # =====================================================================

        breakdown_rows = (
            events
            .values("event_name")
            .annotate(
                count=Count("id")
            )
            .order_by(
                "-count",
                "event_name",
            )[:10]
        )

        event_breakdown = [
            {
                "event_name": row["event_name"],
                "count": int(
                    row["count"] or 0
                ),
            }
            for row in breakdown_rows
        ]

        # =====================================================================
        # Pagination
        # =====================================================================

        paginator = self.pagination_class()

        page = paginator.paginate_queryset(
            events,
            request,
            view=self,
        )

        results = [
            _serialize_activity_event(
                event
            )
            for event in page
        ]

        response_data = {
            "success": True,
            "filters": {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "days": round(
                    (
                        end - start
                    ).total_seconds()
                    / 86400,
                    2,
                ),
                "event": event_name,
                "search": search_query,
                "customer_id": (
                    int(customer_id_raw)
                    if customer_id_raw
                    else None
                ),
                "session_id": session_id_raw or None,
                "ordering": (
                    requested_ordering
                    if requested_ordering
                    in self.ORDERING_MAP
                    else "-occurred_at"
                ),
            },
            "summary": {
                "total_events": int(
                    summary["total_events"] or 0
                ),
                "unique_sessions": int(
                    summary["unique_sessions"] or 0
                ),
                "unique_customers": int(
                    summary["unique_customers"] or 0
                ),
                "authenticated_events": int(
                    summary["authenticated_events"] or 0
                ),
                "guest_events": int(
                    summary["guest_events"] or 0
                ),
            },
            "event_breakdown": event_breakdown,
            "results": results,
        }

        return Response(
            {
                "count": paginator.page.paginator.count,
                "next": paginator.get_next_link(),
                "previous": paginator.get_previous_link(),
                **response_data,
            },
            status=status.HTTP_200_OK,
        )


class ProductAnalyticsAPIView(APIView):
    """
    GET /api/analytics/products/

    Admin-only product intelligence report.

    Reads immutable AnalyticsEvent records and never modifies
    transactional/catalog state.

    Supported filters:
      ?days=30
      ?start=2026-09-01
      ?end=2026-09-06
      ?search=hafele
      ?ordering=-orders_created
      ?page=1
      ?page_size=25
    """

    permission_classes = [
    IsAnalyticsAdmin,
]

    http_method_names = [
        "get",
        "options",
    ]

    pagination_class = ProductAnalyticsPagination

    ORDERING_MAP = {
        "views": "views",
        "-views": "-views",
        "unique_sessions": "unique_sessions",
        "-unique_sessions": "-unique_sessions",
        "unique_customers": "unique_customers",
        "-unique_customers": "-unique_customers",
        "add_to_cart": "add_to_cart",
        "-add_to_cart": "-add_to_cart",
        "remove_from_cart": "remove_from_cart",
        "-remove_from_cart": "-remove_from_cart",
        "quantity_added": "quantity_added",
        "-quantity_added": "-quantity_added",
        "orders_created": "orders_created",
        "-orders_created": "-orders_created",
    }

    def get(
        self,
        request,
    ):
        # =====================================================================
        # Date window
        # =====================================================================

        try:
            start, end = _resolve_report_window(
                request
            )
        except serializers.ValidationError as exc:
            return Response(
                {
                    "success": False,
                    "detail": exc.detail,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # =====================================================================
        # Product search
        # =====================================================================

        search_query = str(
            request.query_params.get(
                "search",
                "",
            )
            or ""
        ).strip()

        # =====================================================================
        # Event time window
        # =====================================================================

        event_window = Q(
            analytics_events__occurred_at__gte=start,
            analytics_events__occurred_at__lt=end,
        )

        # =====================================================================
        # Build product queryset
        #
        # Only products having at least one analytics event inside the
        # requested window are returned.
        # =====================================================================

        products = (
            Product.objects
            .filter(event_window)
            .select_related(
                "category",
                "subcategory",
            )
            .distinct()
        )

        if search_query:
            products = products.filter(
                Q(name__icontains=search_query)
                | Q(brand__icontains=search_query)
                | Q(short_description__icontains=search_query)
                | Q(description__icontains=search_query)
                | Q(category__name__icontains=search_query)
                | Q(subcategory__name__icontains=search_query)
            )

        # =====================================================================
        # Metric annotations
        # =====================================================================

        view_filter = (
            event_window
            & Q(
                analytics_events__event_name=PRODUCT_ANALYTICS_VIEW_EVENT,
            )
        )

        add_filter = (
            event_window
            & Q(
                analytics_events__event_name=PRODUCT_ANALYTICS_ADD_EVENT,
            )
        )

        remove_filter = (
            event_window
            & Q(
                analytics_events__event_name=PRODUCT_ANALYTICS_REMOVE_EVENT,
            )
        )

        order_filter = (
            event_window
            & Q(
                analytics_events__event_name=PRODUCT_ANALYTICS_ORDER_EVENT,
            )
        )

        products = products.annotate(
            views=Count(
                "analytics_events",
                filter=view_filter,
            ),
            unique_sessions=Count(
                "analytics_events__session",
                filter=view_filter,
                distinct=True,
            ),
            unique_customers=Count(
                "analytics_events__customer",
                filter=view_filter,
                distinct=True,
            ),
            add_to_cart=Count(
                "analytics_events",
                filter=add_filter,
            ),
            remove_from_cart=Count(
                "analytics_events",
                filter=remove_filter,
            ),
            quantity_added=Coalesce(
                Sum(
                    Cast(
                        "analytics_events__metadata__added_quantity",
                        IntegerField(),
                    ),
                    filter=add_filter,
                ),
                Value(0),
            ),
            orders_created=Count(
                "analytics_events__order",
                filter=order_filter,
                distinct=True,
            ),
        )

        # =====================================================================
        # Ordering
        # =====================================================================

        requested_ordering = str(
            request.query_params.get(
                "ordering",
                "-orders_created",
            )
            or "-orders_created"
        ).strip()

        ordering = self.ORDERING_MAP.get(
            requested_ordering
        )

        if ordering is None:
            ordering = "-orders_created"

        products = products.order_by(
            ordering,
            "id",
        )

        # =====================================================================
        # Summary metrics across the filtered product set
        # =====================================================================

        summary = products.aggregate(
            total_views=Coalesce(
                Sum("views"),
                Value(0),
            ),
            total_unique_sessions=Coalesce(
                Sum("unique_sessions"),
                Value(0),
            ),
            total_unique_customers=Coalesce(
                Sum("unique_customers"),
                Value(0),
            ),
            total_add_to_cart=Coalesce(
                Sum("add_to_cart"),
                Value(0),
            ),
            total_remove_from_cart=Coalesce(
                Sum("remove_from_cart"),
                Value(0),
            ),
            total_quantity_added=Coalesce(
                Sum("quantity_added"),
                Value(0),
            ),
            total_orders_created=Coalesce(
                Sum("orders_created"),
                Value(0),
            ),
        )

        # =====================================================================
        # Pagination
        # =====================================================================

        paginator = self.pagination_class()

        page = paginator.paginate_queryset(
            products,
            request,
            view=self,
        )

        # =====================================================================
        # Serialize report rows
        # =====================================================================

        results = []

        for product in page:
            views = int(
                product.views or 0
            )
            unique_sessions = int(
                product.unique_sessions or 0
            )
            unique_customers = int(
                product.unique_customers or 0
            )
            add_to_cart = int(
                product.add_to_cart or 0
            )
            remove_from_cart = int(
                product.remove_from_cart or 0
            )
            quantity_added = int(
                product.quantity_added or 0
            )
            orders_created = int(
                product.orders_created or 0
            )

            view_to_cart_percent = (
                round(
                    (add_to_cart / views) * 100,
                    2,
                )
                if views
                else 0.0
            )

            view_to_order_percent = (
                round(
                    (orders_created / views) * 100,
                    2,
                )
                if views
                else 0.0
            )

            cart_to_order_percent = (
                round(
                    (orders_created / add_to_cart) * 100,
                    2,
                )
                if add_to_cart
                else 0.0
            )

            results.append(
                {
                    "product_id": product.id,
                    "product": {
                        "name": product.name,
                        "brand": getattr(
                            product,
                            "brand",
                            "",
                        ),
                        "category": (
                            product.category.name
                            if getattr(
                                product,
                                "category",
                                None,
                            )
                            else ""
                        ),
                        "subcategory": (
                            product.subcategory.name
                            if getattr(
                                product,
                                "subcategory",
                                None,
                            )
                            else ""
                        ),
                    },
                    "metrics": {
                        "views": views,
                        "unique_sessions": unique_sessions,
                        "unique_customers": unique_customers,
                        "add_to_cart": add_to_cart,
                        "remove_from_cart": remove_from_cart,
                        "quantity_added": quantity_added,
                        "orders_created": orders_created,
                    },
                    "conversion": {
                        "view_to_cart_percent": view_to_cart_percent,
                        "cart_to_order_percent": cart_to_order_percent,
                        "view_to_order_percent": view_to_order_percent,
                    },
                }
            )

        # =====================================================================
        # Final production-clean response
        #
        # IMPORTANT:
        # Do not pass the complete response object through
        # paginator.get_paginated_response(), because that creates
        # an unnecessary nested `results` wrapper.
        # =====================================================================

        response_data = {
            "success": True,
            "filters": {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "days": round(
                    (end - start).total_seconds() / 86400,
                    2,
                ),
                "search": search_query,
                "ordering": (
                    requested_ordering
                    if requested_ordering in self.ORDERING_MAP
                    else "-orders_created"
                ),
            },
            "summary": {
                "total_products": products.count(),
                "total_views": int(
                    summary["total_views"] or 0
                ),
                "total_unique_sessions": int(
                    summary["total_unique_sessions"] or 0
                ),
                "total_unique_customers": int(
                    summary["total_unique_customers"] or 0
                ),
                "total_add_to_cart": int(
                    summary["total_add_to_cart"] or 0
                ),
                "total_remove_from_cart": int(
                    summary["total_remove_from_cart"] or 0
                ),
                "total_quantity_added": int(
                    summary["total_quantity_added"] or 0
                ),
                "total_orders_created": int(
                    summary["total_orders_created"] or 0
                ),
            },
            "results": results,
        }

        return Response(
            {
                "count": paginator.page.paginator.count,
                "next": paginator.get_next_link(),
                "previous": paginator.get_previous_link(),
                **response_data,
            },
            status=status.HTTP_200_OK,
        )