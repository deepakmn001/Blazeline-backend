# ==========================================================
# catalog/views.py
# (existing prefetch already covers every relation the
#  serializer output needs: options/options__values,
#  variants/variants__images, and
#  variants__variant_options__option_value__option)
# ==========================================================
import logging

from django.core.exceptions import ValidationError
from rest_framework.exceptions import ValidationError as DRFValidationError  # aliased
# to avoid colliding with django.core.exceptions.ValidationError above.
# DRF's exception handler only auto-converts THIS ValidationError into a 400 —
# raising the django.core one instead falls through as an unhandled 500.

from django.db import transaction
from django.db.models import Count
from django.db.models.functions import TruncMonth
from django.utils import timezone
from django.shortcuts import get_object_or_404

from rest_framework import viewsets, filters, parsers, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from django_filters.rest_framework import DjangoFilterBackend

from .pagination import StandardResultsPagination
from .filters import ProductFilter, DeliveryRuleFilter
from .services import LocationService
from .delivery.services import (
    calculate_delivery,
    NotServiceableError,
    get_serviceable_location,
    CartValidationError,
)
from .delivery.rule_scope import compute_rule_status, status_query
from .facet_service import build_product_facets

from .models import (
    Category,
    HomepageCategory,
    SubCategory,
    Product,
    ProductImage,
    ProductVariant,
    ProductSpecification,
    ServiceablePincode,
    QuoteRequest,
    DeliveryZone,
    DeliveryRule,
    DeliveryRuleAction,
)

from .serializers import (
    CategorySerializer,
    HomepageCategorySerializer,
    SubCategorySerializer,
    ProductSerializer,
    ProductListSerializer,
    ProductImageSerializer,
    ProductVariantSerializer,
    ProductSpecificationSerializer,
    DeliveryCheckSerializer,
    DeliveryQuoteRequestSerializer,
    DeliveryQuoteResponseSerializer,
    QuoteRequestSerializer,
    BulkDeleteSerializer,
    BulkMoveProductsSerializer,
    DeliveryZoneSerializer,
    ServiceablePincodeSerializer,
    DeliveryRuleSerializer,
    DeliveryRuleListSerializer,
)

logger = logging.getLogger(__name__)


# ==========================================================
# CATEGORY
# ==========================================================

class CategoryViewSet(viewsets.ModelViewSet):
    queryset = Category.objects.all()
    serializer_class = CategorySerializer

    lookup_field = "slug"
    lookup_url_kwarg = "slug"

    def get_permissions(self):
        if self.action in ["list", "retrieve"]:
            return [AllowAny()]
        return [IsAuthenticated()]

    filter_backends = [
        filters.SearchFilter,
        DjangoFilterBackend,
        filters.OrderingFilter,
    ]
    filterset_fields = ["active"]
    search_fields = ["name", "group"]
    ordering = ["name"]

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["request"] = self.request
        return context


class AdminCategoryViewSet(CategoryViewSet):
    lookup_field = "pk"
    lookup_url_kwarg = "pk"

    def get_object(self):
        return get_object_or_404(
            self.get_queryset(),
            pk=self.kwargs["pk"],
        )


# ==========================================================
# HOMEPAGE CATEGORY
# ==========================================================

class HomepageCategoryViewSet(viewsets.ModelViewSet):
    queryset = (
        HomepageCategory.objects
        .select_related("category")
        .order_by("sort_order", "id")
    )

    serializer_class = HomepageCategorySerializer

    def get_permissions(self):
        if self.action in ["list", "retrieve"]:
            return [AllowAny()]
        return [IsAuthenticated()]


# ==========================================================
# SUB CATEGORY
# ==========================================================

class SubCategoryViewSet(viewsets.ModelViewSet):
    queryset = (
        SubCategory.objects
        .select_related("category")
    )

    serializer_class = SubCategorySerializer
    lookup_field = "slug"
    lookup_url_kwarg = "slug"

    def get_permissions(self):
        if self.action in ["list", "retrieve"]:
            return [AllowAny()]
        return [IsAuthenticated()]

    filter_backends = [
        DjangoFilterBackend,
        filters.SearchFilter,
        filters.OrderingFilter,
    ]

    filterset_fields = ["category", "active"]
    search_fields = ["name"]
    ordering = ["name"]

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["request"] = self.request
        return context


class AdminSubCategoryViewSet(SubCategoryViewSet):
    lookup_field = "pk"
    lookup_url_kwarg = "pk"
    permission_classes = [IsAuthenticated]

    ordering = ["sort_order", "name"]

    ordering_fields = [
        "sort_order",
        "name",
        "created_at",
        "updated_at",
    ]

    def get_object(self):
        return get_object_or_404(
            self.get_queryset(),
            pk=self.kwargs["pk"],
        )


# ==========================================================
# PRODUCT
# ==========================================================

class ProductViewSet(viewsets.ModelViewSet):
    lookup_field = "slug"
    lookup_url_kwarg = "slug"
    pagination_class = StandardResultsPagination

    queryset = (
        Product.objects
        .select_related("category", "subcategory")
        .prefetch_related(
            "specifications",
            "options",
            "options__values",
            "variants",
            "variants__images",
            "variants__variant_options",
            "variants__variant_options__option_value",
            "variants__variant_options__option_value__option",
        )
    )

    def get_serializer_class(self):
        if self.action == "list":
            return ProductListSerializer
        return ProductSerializer

    def get_permissions(self):
        if self.action in ["list", "retrieve", "facets"]:
            return [AllowAny()]
        return [IsAuthenticated()]

    parser_classes = [
        parsers.MultiPartParser,
        parsers.FormParser,
        parsers.JSONParser,
    ]

    filter_backends = [
        DjangoFilterBackend,
        filters.SearchFilter,
        filters.OrderingFilter,
    ]

    filterset_class = ProductFilter

    search_fields = ["name", "description", "short_description"]
    ordering_fields = ["name", "created_at", "updated_at"]
    ordering = ["-created_at"]

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["request"] = self.request
        return context

    @action(detail=False, methods=["get"], url_path="facets")
    def facets(self, request):
        """
        Read-only customer-facing filter metadata.
        Existing /products/ list/retrieve behavior is untouched.
        """

        def parse_id(value):
            if value in (None, ""):
                return None
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                return None
            return parsed if parsed > 0 else None

        category_id = parse_id(request.query_params.get("category"))
        subcategory_id = parse_id(request.query_params.get("subcategory"))

        payload = build_product_facets(
            category_id=category_id,
            subcategory_id=subcategory_id,
        )

        return Response(payload, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="bulk-delete")
    def bulk_delete(self, request):
        serializer = BulkDeleteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        product_ids = serializer.validated_data["product_ids"]

        existing_ids = set(
            Product.objects.filter(id__in=product_ids).values_list("id", flat=True)
        )
        missing_ids = set(product_ids) - existing_ids

        with transaction.atomic():
            deleted_count, _ = Product.objects.filter(id__in=existing_ids).delete()

        return Response(
            {
                "deleted": deleted_count,
                "not_found": list(missing_ids),
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["post"], url_path="bulk-move")
    def bulk_move(self, request):
        serializer = BulkMoveProductsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        product_ids = serializer.validated_data["product_ids"]
        category = serializer.validated_data["category"]
        subcategory = serializer.validated_data["subcategory"]

        if subcategory.category_id != category.id:
            raise DRFValidationError(
                {"subcategory": "Subcategory does not belong to the selected category."}
            )

        with transaction.atomic():
            updated_count = (
                Product.objects.filter(id__in=product_ids)
                .update(category=category, subcategory=subcategory)
            )

        return Response(
            {
                "updated": updated_count,
                "category": category.id,
                "subcategory": subcategory.id,
            },
            status=status.HTTP_200_OK,
        )


# ==========================================================
# PRODUCT IMAGE
# ==========================================================

class ProductImageViewSet(viewsets.ModelViewSet):
    queryset = ProductImage.objects.all()
    serializer_class = ProductImageSerializer
    permission_classes = [IsAuthenticated]

    parser_classes = [
        parsers.MultiPartParser,
        parsers.FormParser,
        parsers.JSONParser,
    ]

    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ["variant", "featured"]
    ordering = ["sort_order"]

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["request"] = self.request
        return context


# ==========================================================
# PRODUCT VARIANT
# ==========================================================

class ProductVariantViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]

    queryset = (
        ProductVariant.objects
        .select_related("product")
        .prefetch_related(
            "images",
            "variant_options",
            "variant_options__option_value",
            "variant_options__option_value__option",
        )
    )

    serializer_class = ProductVariantSerializer

    filter_backends = [
        DjangoFilterBackend,
        filters.SearchFilter,
        filters.OrderingFilter,
    ]

    filterset_fields = ["product"]
    search_fields = ["sku", "barcode", "product__name"]
    ordering = ["id"]

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["request"] = self.request
        return context


# ==========================================================
# PRODUCT SPECIFICATION
# ==========================================================

class ProductSpecificationViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]

    queryset = ProductSpecification.objects.select_related("product")
    serializer_class = ProductSpecificationSerializer

    filter_backends = [
        DjangoFilterBackend,
        filters.SearchFilter,
        filters.OrderingFilter,
    ]

    filterset_fields = ["product"]
    search_fields = ["key", "value"]
    ordering = ["id"]

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["request"] = self.request
        return context


# ==========================================================
# DASHBOARD API
# ==========================================================

class DashboardAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        stats = {
            "total_products": Product.objects.count(),
            "published_products": Product.objects.filter(status="published").count(),
            "draft_products": Product.objects.filter(status="draft").count(),
            "categories": Category.objects.count(),
            "subcategories": SubCategory.objects.count(),
        }

        recent_products = (
            Product.objects
            .select_related("category", "subcategory")
            .prefetch_related("variants", "variants__images")
            .order_by("-created_at")[:10]
        )

        recent_products_data = []

        for product in recent_products:
            image = None

            # Use the prefetch cache (product.variants.all()) instead of
            # product.variants.filter(...), which bypasses prefetch_related
            # and fires one extra query per product (N+1).
            variants = list(product.variants.all())
            variant = next((v for v in variants if v.is_default), None) or (
                variants[0] if variants else None
            )

            if variant:
                images = list(variant.images.all())  # also served from prefetch cache
                featured = next((img for img in images if img.featured), None)

                if featured and featured.image:
                    try:
                        image = request.build_absolute_uri(featured.image.url)
                    except Exception:
                        image = None

            recent_products_data.append({
                "id": product.id,
                "name": product.name,
                "category": product.category.name if product.category else None,
                "subcategory": product.subcategory.name if product.subcategory else None,
                "status": product.status,
                "created_at": product.created_at,
                "image": image,
            })

        category_distribution = list(
            Category.objects.annotate(total=Count("products")).values("name", "total")
        )

        product_growth = list(
            Product.objects.annotate(month=TruncMonth("created_at"))
            .values("month")
            .annotate(total=Count("id"))
            .order_by("month")
        )

        return Response({
            "stats": stats,
            "recent_products": recent_products_data,
            "category_distribution": category_distribution,
            "product_growth": product_growth,
        })


class ReverseLocationAPIView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        latitude = request.data.get("latitude")
        longitude = request.data.get("longitude")

        if latitude is None or longitude is None:
            return Response(
                {"message": "Latitude and longitude are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            location = LocationService.reverse_geocode(float(latitude), float(longitude))

            if not location:
                return Response(
                    {"message": "Unable to determine your location."},
                    status=status.HTTP_404_NOT_FOUND,
                )

            serviceable = ServiceablePincode.objects.filter(
                pincode=location["postcode"],
                is_active=True,
            ).first()

            return Response({
                "postcode": location["postcode"],
                "city": location["city"],
                "area": location["area"],
                "deliverable": bool(serviceable),
                "message": (
                    "Delivery Available"
                    if serviceable
                    else "Currently we deliver only within Kolkata."
                ),
            })

        except (TypeError, ValueError):
            return Response(
                {"message": "Invalid latitude/longitude."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception:
            # Never leak raw exception text / stack traces to the client.
            logger.exception("Reverse geocode failed")
            return Response(
                {"message": "Something went wrong. Please try again."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


# ==========================================================
# SUB CATEGORY STATS API
# ==========================================================

class SubCategoryStatsAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({
            "total": SubCategory.objects.count(),
            "featured": SubCategory.objects.filter(featured=True).count(),
            "active": SubCategory.objects.filter(active=True).count(),
            "inactive": SubCategory.objects.filter(active=False).count(),
            "products": Product.objects.count(),
        })


# ==========================================================
# DELIVERY CHECK API
# ==========================================================

class DeliveryCheckAPIView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = DeliveryCheckSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        pincode = serializer.validated_data["pincode"]

        try:
            location = get_serviceable_location(pincode)

        except CartValidationError as exc:
            return Response(
                {
                    "deliverable": False,
                    "pincode": pincode,
                    "zone": None,
                    "message": str(exc),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        except NotServiceableError as exc:
            return Response(
                {
                    "deliverable": False,
                    "pincode": pincode,
                    "zone": None,
                    "message": str(exc),
                },
                status=status.HTTP_200_OK,
            )

        return Response(
            {
                "deliverable": True,
                "pincode": location.pincode,
                "area": location.area_name,
                "city": location.city,
                "zone": (
                    {"id": location.zone.id, "name": location.zone.name}
                    if location.zone
                    else None
                ),
                "message": "Delivery Available",
            },
            status=status.HTTP_200_OK,
        )


# ==========================================================
# DELIVERY QUOTE API
# ==========================================================

class DeliveryQuoteAPIView(APIView):
    """
    POST /api/delivery/quote/

    Client sends only:
        - pincode
        - variant_id
        - quantity

    All pricing, weight, product, variant, zone and
    serviceability decisions are resolved server-side.
    """

    permission_classes = [AllowAny]

    def post(self, request):
        request_serializer = DeliveryQuoteRequestSerializer(data=request.data)
        request_serializer.is_valid(raise_exception=True)

        pincode = request_serializer.validated_data["pincode"]
        items = request_serializer.validated_data["items"]

        try:
            result = calculate_delivery(pincode=pincode, cart_items=items)

        except NotServiceableError as exc:
            return Response(
                {
                    "deliverable": False,
                    "zone": None,
                    "subtotal": "0.00",
                    "weight": "0.000",
                    "delivery_charge": None,
                    "free_delivery": False,
                    "breakdown": [],
                    "message": str(exc),
                },
                status=status.HTTP_200_OK,
            )

        except CartValidationError as exc:
            return Response(
                {"message": str(exc), "errors": exc.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        response_payload = {
            "deliverable": True,
            "zone": (
                {"id": result["zone"].id, "name": result["zone"].name}
                if result.get("zone")
                else None
            ),
            "subtotal": result["subtotal"],
            "weight": result["weight"],
            "delivery_charge": result["total"],
            "free_delivery": result["total"] == 0,
            "breakdown": result["breakdown"],
            "message": "Delivery Available",
        }

        response_serializer = DeliveryQuoteResponseSerializer(response_payload)

        return Response(response_serializer.data, status=status.HTTP_200_OK)


# ==========================================================
# REQUEST A QUOTE API
# ==========================================================

class QuoteRequestAPIView(APIView):
    permission_classes = [AllowAny]

    parser_classes = [
        parsers.MultiPartParser,
        parsers.FormParser,
    ]

    def post(self, request):
        serializer = QuoteRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        quote = serializer.save()

        return Response(
            {
                "success": True,
                "message": "Quote request submitted successfully.",
                "quote_id": str(quote.quote_id),
            },
            status=201,
        )


# ==========================================================
# DELIVERY ZONE
# ==========================================================

class DeliveryZoneViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    pagination_class = StandardResultsPagination

    queryset = DeliveryZone.objects.annotate(
        _pincode_count=Count("pincodes", distinct=True),
        _rule_count=Count("rules", distinct=True),
    )
    serializer_class = DeliveryZoneSerializer

    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["active"]
    search_fields = ["name", "code"]
    ordering_fields = ["name", "priority", "created_at", "updated_at"]
    ordering = ["-priority", "name"]

    def perform_destroy(self, instance):
        rule_count = instance.rules.count()
        if rule_count:
            # Must be DRF's ValidationError (aliased as DRFValidationError above),
            # not django.core.exceptions.ValidationError — only the DRF one is
            # auto-converted into a 400 by the exception handler.
            raise DRFValidationError({
                "detail": (
                    f"Cannot delete a zone that {rule_count} rule(s) still target. "
                    f"Reassign or delete those rules first."
                )
            })
        instance.delete()

    @action(detail=True, methods=["post"], url_path="toggle-active")
    def toggle_active(self, request, pk=None):
        zone = self.get_object()
        zone.active = not zone.active
        zone.save(update_fields=["active"])
        return Response(self.get_serializer(zone).data)


# ==========================================================
# SERVICEABLE PINCODE
# ==========================================================

class ServiceablePincodeViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    pagination_class = StandardResultsPagination

    queryset = ServiceablePincode.objects.select_related("zone")
    serializer_class = ServiceablePincodeSerializer

    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["is_active", "zone", "city", "state"]
    search_fields = ["pincode", "area_name", "city"]
    ordering_fields = ["pincode", "city"]
    ordering = ["pincode"]

    @action(detail=True, methods=["post"], url_path="toggle-active")
    def toggle_active(self, request, pk=None):
        pincode = self.get_object()
        pincode.is_active = not pincode.is_active
        pincode.save(update_fields=["is_active"])
        return Response(self.get_serializer(pincode).data)


# ==========================================================
# DELIVERY RULE
# ==========================================================

class DeliveryRuleViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    pagination_class = StandardResultsPagination

    queryset = (
        DeliveryRule.objects
        .select_related("zone", "category", "subcategory", "product", "variant")
        .prefetch_related("conditions", "actions")
        .annotate(
            _condition_count=Count("conditions", distinct=True),
            _action_count=Count("actions", distinct=True),
        )
    )

    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = DeliveryRuleFilter
    search_fields = ["name", "code"]
    ordering_fields = ["priority", "name", "created_at", "updated_at"]
    ordering = ["-priority", "id"]

    def get_serializer_class(self):
        if self.action == "list":
            return DeliveryRuleListSerializer
        return DeliveryRuleSerializer

    @action(detail=True, methods=["post"], url_path="toggle-active")
    def toggle_active(self, request, pk=None):
        rule = self.get_object()
        rule.active = not rule.active
        rule.save(update_fields=["active"])
        # deliveryApi.ts types rulesApi.toggleActive() as
        # Promise<DeliveryRuleListItem> — list serializer here is
        # intentional, matching the existing frontend contract exactly.
        return Response(DeliveryRuleListSerializer(rule).data)


# ==========================================================
# DELIVERY OVERVIEW
# ==========================================================

class DeliveryOverviewAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        zone_active = DeliveryZone.objects.filter(active=True).count()
        zone_inactive = DeliveryZone.objects.filter(active=False).count()
        pincode_serviceable = ServiceablePincode.objects.filter(is_active=True).count()
        pincode_total = ServiceablePincode.objects.count()

                # Status counts pushed to SQL via status_query() so they stay
        # consistent with DeliveryRuleFilter and calculate_delivery().
        now = timezone.now()

        status_counts = DeliveryRule.objects.aggregate(
    active_count=Count("id", filter=status_query("active", now)),
    scheduled_count=Count("id", filter=status_query("scheduled", now)),
    expired_count=Count("id", filter=status_query("expired", now)),
    inactive_count=Count("id", filter=status_query("inactive", now)),
    total_count=Count("id"),
)

        free_delivery_count = (
            DeliveryRule.objects
            .filter(actions__action_type=DeliveryRuleAction.ACTION_FREE_DELIVERY)
            .distinct()
            .count()
        )

        recent_changes = list(
            DeliveryRule.objects.order_by("-updated_at")[:5]
            .values("id", "name", "active", "updated_at")
        )

        return Response({
            "zones": {"active": zone_active, "inactive": zone_inactive},
            "pincodes": {"serviceable": pincode_serviceable, "total": pincode_total},
                        "rules": {
    "active": status_counts["active_count"],
    "scheduled": status_counts["scheduled_count"],
    "expired": status_counts["expired_count"],
    "inactive": status_counts["inactive_count"],
    "free_delivery": free_delivery_count,
    "total": status_counts["total_count"],
},
            "recent_changes": recent_changes,
        })