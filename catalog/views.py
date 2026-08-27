# ==========================================================
# catalog/views.py
# (unchanged — existing prefetch already covers every relation
#  the new serializer output needs: options/options__values,
#  variants/variants__images, and
#  variants__variant_options__option_value__option)
# ==========================================================
from .pagination import StandardResultsPagination 
from .filters import ProductFilter
from .services import LocationService
from .delivery.services import (
    calculate_delivery,
    NotServiceableError,
    get_serviceable_location,
    CartValidationError,
)
from .facet_service import build_product_facets
from django.core.exceptions import ValidationError
from rest_framework import viewsets, filters, parsers
from rest_framework.permissions import IsAuthenticated, AllowAny
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.decorators import action
from django.db import transaction
from rest_framework import status

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
    BulkMoveProductsSerializer
)
from rest_framework.views import APIView
from rest_framework.response import Response

from django.db.models import Count
from django.db.models.functions import TruncMonth
from django.shortcuts import get_object_or_404

# ==========================================================
# CATEGORY
# ==========================================================

class CategoryViewSet(viewsets.ModelViewSet):
    queryset = Category.objects.all()

    serializer_class = CategorySerializer

    lookup_field = "slug"
    lookup_url_kwarg = "slug"

    serializer_class = CategorySerializer

    def get_permissions(self):
        if self.action in ["list", "retrieve"]:
            return [AllowAny()]
        return [IsAuthenticated()]

    filter_backends = [
        filters.SearchFilter,
        DjangoFilterBackend,
        filters.OrderingFilter,
    ]
    filterset_fields = [
    "active",
]
    search_fields = [
        "name",
        "group",
    ]

    ordering = [
        "name",
    ]

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

    filterset_fields = [
        "category",
        "active",
    ]

    search_fields = [
        "name",
    ]

    ordering = [
        "name",
    ]

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["request"] = self.request
        return context
class AdminSubCategoryViewSet(SubCategoryViewSet):
    lookup_field = "pk"
    lookup_url_kwarg = "pk"
    permission_classes = [IsAuthenticated]

    ordering = [
        "sort_order",
        "name",
    ]

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
        .select_related(
            "category",
            "subcategory",
        )
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

    search_fields = [
        "name",
        "description",
        "short_description",
    ]

    ordering_fields = [
        "name",
        "created_at",
        "updated_at",
    ]

    ordering = [
        "-created_at",
    ]

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

        category_id = parse_id(
            request.query_params.get("category")
        )

        subcategory_id = parse_id(
            request.query_params.get("subcategory")
        )

        payload = build_product_facets(
            category_id=category_id,
            subcategory_id=subcategory_id,
        )

        return Response(
            payload,
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["post"], url_path="bulk-delete")
    def bulk_delete(self, request):
        serializer = BulkDeleteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        product_ids = serializer.validated_data["product_ids"]

        with transaction.atomic():
            deleted_count, _ = (
                Product.objects.filter(id__in=product_ids).delete()
            )

        return Response(
            {"deleted": deleted_count},
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["post"], url_path="bulk-move")
    def bulk_move(self, request):
        serializer = BulkMoveProductsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        product_ids = serializer.validated_data["product_ids"]
        category = serializer.validated_data["category"]
        subcategory = serializer.validated_data["subcategory"]

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

    filter_backends = [
        DjangoFilterBackend,
        filters.OrderingFilter,
    ]

    filterset_fields = [
        "variant",
        "featured",
    ]

    ordering = [
        "sort_order",
    ]

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

    filterset_fields = [
        "product",
    ]

    search_fields = [
        "sku",
        "barcode",
        "product__name",
    ]

    ordering = [
        "id",
    ]

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["request"] = self.request
        return context


# ==========================================================
# PRODUCT SPECIFICATION
# ==========================================================

class ProductSpecificationViewSet(viewsets.ModelViewSet):

    permission_classes = [IsAuthenticated]

    queryset = (
        ProductSpecification.objects
        .select_related("product")
    )

    serializer_class = ProductSpecificationSerializer

    filter_backends = [
        DjangoFilterBackend,
        filters.SearchFilter,
        filters.OrderingFilter,
    ]

    filterset_fields = [
        "product",
    ]

    search_fields = [
        "key",
        "value",
    ]

    ordering = [
        "id",
    ]

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
            "published_products": Product.objects.filter(
                status="published"
            ).count(),
            "draft_products": Product.objects.filter(
                status="draft"
            ).count(),
            "categories": Category.objects.count(),
            "subcategories": SubCategory.objects.count(),
        }

        recent_products = (
            Product.objects
            .select_related(
                "category",
                "subcategory",
            )
            .prefetch_related(
                "variants",
                "variants__images",
            )
            .order_by("-created_at")[:10]
        )

        recent_products_data = []

        for product in recent_products:

            image = None

            variant = (
                product.variants
                .filter(is_default=True)
                .first()
                or product.variants.first()
            )

            if variant:
                featured = variant.images.filter(
                    featured=True
                ).first()

                if featured and featured.image:
                    try:
                        image = request.build_absolute_uri(
                            featured.image.url
                        )
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

            Category.objects.annotate(
                total=Count("products")
            ).values(
                "name",
                "total",
            )

        )

        product_growth = list(

            Product.objects.annotate(
                month=TruncMonth("created_at")
            ).values(
                "month",
            ).annotate(
                total=Count("id")
            ).order_by("month")

        )

        return Response({

            "stats": stats,

            "recent_products": recent_products_data,

            "category_distribution": category_distribution,

            "product_growth": product_growth,

        })


class ReverseLocationAPIView(APIView):

    def post(self, request):

        latitude = request.data.get("latitude")
        longitude = request.data.get("longitude")

        if latitude is None or longitude is None:
            return Response(
                {
                    "message": "Latitude and longitude are required."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:

            location = LocationService.reverse_geocode(
                float(latitude),
                float(longitude),
            )

            if not location:
                return Response(
                    {
                        "message": "Unable to determine your location."
                    },
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

        except Exception as e:
            import traceback

            traceback.print_exc()

            return Response(
                {
                    "message": str(e),
                },
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

# ==========================================================
# DELIVERY CHECK API
# ==========================================================


class DeliveryCheckAPIView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = DeliveryCheckSerializer(
            data=request.data
        )

        serializer.is_valid(
            raise_exception=True
        )

        pincode = serializer.validated_data["pincode"]

        try:
            location = get_serviceable_location(
                pincode
            )

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
                    {
                        "id": location.zone.id,
                        "name": location.zone.name,
                    }
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

    def post(self, request):
        request_serializer = DeliveryQuoteRequestSerializer(
            data=request.data
        )

        request_serializer.is_valid(
            raise_exception=True
        )

        pincode = request_serializer.validated_data["pincode"]
        items = request_serializer.validated_data["items"]

        try:
            result = calculate_delivery(
                pincode=pincode,
                cart_items=items,
            )

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
                {
                    "message": str(exc),
                    "errors": exc.errors,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        response_payload = {
            "deliverable": True,
            "zone": (
                {
                    "id": result["zone"].id,
                    "name": result["zone"].name,
                }
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

        response_serializer = DeliveryQuoteResponseSerializer(
            response_payload
        )

        return Response(
            response_serializer.data,
            status=status.HTTP_200_OK,
        )
    # ==========================================================
# REQUEST A QUOTE API
# ==========================================================

class QuoteRequestAPIView(APIView):

    parser_classes = [
        parsers.MultiPartParser,
        parsers.FormParser,
    ]

    def post(self, request):

        serializer = QuoteRequestSerializer(
            data=request.data
        )

        serializer.is_valid(
            raise_exception=True
        )

        quote = serializer.save()

        return Response(
            {
                "success": True,
                "message": "Quote request submitted successfully.",
                "quote_id": str(quote.quote_id),
            },
            status=201,
        )