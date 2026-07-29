# ==========================================================
# catalog/views.py
# (unchanged — existing prefetch already covers every relation
#  the new serializer output needs: options/options__values,
#  variants/variants__images, and
#  variants__variant_options__option_value__option)
# ==========================================================

from rest_framework import viewsets, filters, parsers
from django_filters.rest_framework import DjangoFilterBackend

from .models import (
    Category,
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
    SubCategorySerializer,
    ProductSerializer,
    ProductImageSerializer,
    ProductVariantSerializer,
    ProductSpecificationSerializer,
    DeliveryCheckSerializer,
    QuoteRequestSerializer,
)
from rest_framework.views import APIView
from rest_framework.response import Response

from django.db.models import Count
from django.db.models.functions import TruncMonth

# ==========================================================
# CATEGORY
# ==========================================================

class CategoryViewSet(viewsets.ModelViewSet):

    queryset = Category.objects.all()

    serializer_class = CategorySerializer

    filter_backends = [
        filters.SearchFilter,
        filters.OrderingFilter,
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


# ==========================================================
# SUB CATEGORY
# ==========================================================

class SubCategoryViewSet(viewsets.ModelViewSet):

    queryset = (
        SubCategory.objects
        .select_related("category")
    )

    serializer_class = SubCategorySerializer

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


# ==========================================================
# PRODUCT
# ==========================================================

class ProductViewSet(viewsets.ModelViewSet):
    lookup_field = "slug"
    lookup_url_kwarg = "slug"
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

    serializer_class = ProductSerializer

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

    filterset_fields = [
        "category",
        "subcategory",
        "featured",
        "active",
        "status",
    ]

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


# ==========================================================
# PRODUCT IMAGE
# ==========================================================

class ProductImageViewSet(viewsets.ModelViewSet):

    queryset = ProductImage.objects.all()

    serializer_class = ProductImageSerializer

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

    # ==========================================================
# DELIVERY CHECK API
# ==========================================================

class DeliveryCheckAPIView(APIView):

    def post(self, request):

        serializer = DeliveryCheckSerializer(
            data=request.data
        )

        serializer.is_valid(
            raise_exception=True
        )

        pincode = serializer.validated_data["pincode"]

        location = ServiceablePincode.objects.filter(
            pincode=pincode,
            is_active=True
        ).first()

        if location:

            return Response({

                "deliverable": True,

                "pincode": location.pincode,

                "area": location.area_name,

                "city": location.city,

                "message": "Delivery Available"

            })

        return Response({

            "deliverable": False,

            "message": "Currently we deliver only within Kolkata."

        })







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