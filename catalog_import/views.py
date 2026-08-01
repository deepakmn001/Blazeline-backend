import csv
import traceback
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.filters import SearchFilter, OrderingFilter

from django_filters.rest_framework import DjangoFilterBackend
from django.db.models import Count, Q
from django.http import StreamingHttpResponse

from catalog.models import Category, SubCategory

from .models import (
    CatalogImport,
    ParsedProduct,
)

from .serializers import (
    CatalogImportSerializer,
    ParsedProductSerializer,
    JsonCatalogImportSerializer,
)

from .services.catalog_import_service import import_catalog
from .services.json_import_service import import_json
from .services.publish_service import (
    publish_parsed_product,
    PublishError,
)
from .pagination import StandardResultsPagination

# ==========================================================
# CATALOG IMPORT
# ==========================================================

class CatalogImportViewSet(viewsets.ModelViewSet):

    permission_classes = [IsAuthenticated]

    queryset = CatalogImport.objects.all().order_by("-created_at")

    serializer_class = CatalogImportSerializer


# ==========================================================
# CSV STREAMING HELPER
# ==========================================================

class _Echo:
    """A file-like object that just returns what it's given — lets csv.writer
    stream rows through StreamingHttpResponse without buffering the whole
    file in memory."""

    def write(self, value):
        return value


# ==========================================================
# PARSED PRODUCTS
# ==========================================================

class ParsedProductViewSet(viewsets.ModelViewSet):
    """
    List supports:
      ?page=&page_size=&search=&status=&category=&finish=&is_imported=&ordering=&catalog=

    Extra actions:
      GET  /parsed-products/dashboard/    -> products + count + stats + facets in one call
      GET  /parsed-products/stats/        -> live counters (pending/valid/invalid/imported/total)
      GET  /parsed-products/facets/       -> distinct category & finish values for filter dropdowns
      POST /parsed-products/{id}/publish/ -> publish a single product
      POST /parsed-products/bulk_action/  -> bulk publish/delete/mark_valid/mark_invalid
      POST /parsed-products/export_csv/   -> streamed CSV, either of `ids` (selected rows)
                                              or the current filtered queryset if `ids` omitted
    """

    permission_classes = [IsAuthenticated]

    serializer_class = ParsedProductSerializer
    pagination_class = StandardResultsPagination

    queryset = (
        ParsedProduct.objects
        .select_related("catalog", "catalog__category")
        .all()
    )
    filter_backends = [
        DjangoFilterBackend,
        SearchFilter,
        OrderingFilter,
    ]

    # category / finish are handled manually in get_queryset() below (case
    # insensitive match against free-text OCR values), so they're
    # deliberately left out of filterset_fields to avoid a redundant /
    # conflicting exact-match filter running on top of it.
    filterset_fields = [
        "status",
        "is_imported",
        "catalog",
    ]

    search_fields = [
        "sku",
        "product_name",
        "finish",
        "category",
        "subcategory",
        "variant",
        "raw_text",
    ]

    ordering_fields = [
        "created_at",
        "sku",
        "product_name",
        "gd_price",
        "rgd_price",
        "page_number",
    ]

    ordering = [
        "-created_at",
    ]

    def get_queryset(self):
        qs = super().get_queryset()

        category = self.request.query_params.get("category")
        finish = self.request.query_params.get("finish")

        if category:
            qs = qs.filter(category__iexact=category)

        if finish:
            qs = qs.filter(finish__iexact=finish)

        return qs

    # ------------------------------------------------------
    # Combined payload for the review page: products + count +
    # stats + facets in a single round trip, instead of the client
    # firing three separate requests on every render.
    # ------------------------------------------------------
    @action(detail=False, methods=["get"], url_path="dashboard")
    def dashboard(self, request):
        base_qs = self.filter_queryset(self.get_queryset())

        page = self.paginate_queryset(base_qs)
        serializer = self.get_serializer(
            page if page is not None else base_qs, many=True
        )

        if page is not None:
            products_data = self.get_paginated_response(serializer.data).data
        else:
            products_data = {
                "count": base_qs.count(),
                "next": None,
                "previous": None,
                "results": serializer.data,
            }

        # Reuse the existing stats/facets actions rather than
        # re-implementing the aggregation logic here.
        stats_data = self.stats(request).data
        facets_data = self.facets(request).data

        return Response(
            {
                "products": products_data.get("results", []),
                "count": products_data.get("count", 0),
                "next": products_data.get("next"),
                "previous": products_data.get("previous"),
                "stats": stats_data,
                "facets": facets_data,
            },
            status=status.HTTP_200_OK,
        )

    # ------------------------------------------------------
    # Live counters for the toolbar
    # ------------------------------------------------------
    @action(detail=False, methods=["get"], url_path="stats")
    def stats(self, request):
        # Respect search/status/category/finish/is_imported filters so the
        # counters reflect what's currently visible, but ignore
        # ordering/page/page_size which don't affect counts.
        base_qs = self.filter_queryset(self.get_queryset())

        counts = base_qs.aggregate(
            pending=Count("id", filter=Q(status=ParsedProduct.Status.PENDING)),
            valid=Count("id", filter=Q(status=ParsedProduct.Status.VALID)),
            invalid=Count("id", filter=Q(status=ParsedProduct.Status.INVALID)),
            imported=Count("id", filter=Q(is_imported=True)),
            total=Count("id"),
        )

        return Response(counts, status=status.HTTP_200_OK)

    # ------------------------------------------------------
    # Distinct category / finish values for filter dropdowns
    # ------------------------------------------------------
    @action(detail=False, methods=["get"], url_path="facets")
    def facets(self, request):
        categories = (
            ParsedProduct.objects
            .exclude(category="")
            .order_by()
            .values_list("category", flat=True)
            .distinct()
        )

        finishes = (
            ParsedProduct.objects
            .exclude(finish="")
            .order_by()
            .values_list("finish", flat=True)
            .distinct()
        )

        return Response(
            {
                "categories": sorted(set(categories), key=str.lower),
                "finishes": sorted(set(finishes), key=str.lower),
            },
            status=status.HTTP_200_OK,
        )

    # ------------------------------------------------------
    # Publish a single product
    # ------------------------------------------------------
    @action(detail=True, methods=["post"], url_path="publish")
    def publish(self, request, pk=None):

        parsed = self.get_object()

        try:

            result = publish_parsed_product(parsed)

            return Response(
                {
                    "success": True,
                    "message": "Product published successfully.",
                    "product_id": result["product"].id,
                    "variants": len(result["variants"]),
                },
                status=status.HTTP_200_OK,
            )

        except PublishError as exc:

            return Response(
                {
                    "success": False,
                    "error": str(exc),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        except Exception as exc:

            return Response(
                {
                    "success": False,
                    "error": str(exc),
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    # ------------------------------------------------------
    # Bulk actions (publish / delete / mark_valid / mark_invalid)
    # CSV export is handled separately by export_csv below since it
    # returns a file stream rather than JSON.
    # ------------------------------------------------------
    @action(detail=False, methods=["post"], url_path="bulk_action")
    def bulk_action(self, request):
        action_type = request.data.get("action")
        ids = request.data.get("ids", [])

        if not action_type:
            return Response(
                {"success": False, "error": "`action` is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not isinstance(ids, list) or not ids:
            return Response(
                {"success": False, "error": "`ids` must be a non-empty list."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        queryset = ParsedProduct.objects.filter(id__in=ids)
        found_ids = list(queryset.values_list("id", flat=True))

        if action_type == "publish":

            success = 0
            failed = []

            for parsed in queryset.exclude(is_imported=True):

                try:

                    publish_parsed_product(parsed)

                    success += 1

                except Exception as exc:

                    failed.append(
                        {
                            "id": parsed.id,
                            "sku": parsed.sku,
                            "error": str(exc),
                        }
                    )

            return Response(
                {
                    "success": True,
                    "published": success,
                    "failed": failed,
                },
                status=status.HTTP_200_OK,
            )

        if action_type == "delete":
            deleted_count, _ = queryset.delete()
            return Response(
                {"success": True, "action": action_type, "deleted": deleted_count, "ids": found_ids},
                status=status.HTTP_200_OK,
            )

        if action_type == "mark_valid":
            updated = queryset.update(status=ParsedProduct.Status.VALID)
            return Response(
                {"success": True, "action": action_type, "updated": updated, "ids": found_ids},
                status=status.HTTP_200_OK,
            )

        if action_type == "mark_invalid":
            updated = queryset.update(status=ParsedProduct.Status.INVALID)
            return Response(
                {"success": True, "action": action_type, "updated": updated, "ids": found_ids},
                status=status.HTTP_200_OK,
            )

        return Response(
            {"success": False, "error": f"Unsupported action '{action_type}'."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # ------------------------------------------------------
    # Streamed CSV export
    # ------------------------------------------------------
    @action(detail=False, methods=["post"], url_path="export_csv")
    def export_csv(self, request):
        ids = request.data.get("ids")

        if ids is not None and not isinstance(ids, list):
            return Response(
                {"success": False, "error": "`ids` must be a list when provided."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # If ids are given (bulk "Export Selected"), export exactly those
        # rows. Otherwise export everything matching the current filters
        # (status/search/category/finish/is_imported/catalog from query
        # params), which lets the same endpoint power a future "Export All"
        # button with no extra backend work.
        if ids:
            queryset = ParsedProduct.objects.filter(id__in=ids)
        else:
            queryset = self.filter_queryset(self.get_queryset())

        header = [
            "id",
            "sku",
            "product_name",
            "category",
            "subcategory",
            "variant",
            "finish",
            "gd_price",
            "rgd_price",
            "status",
            "is_imported",
            "created_at",
        ]

        def row_iterator():
            yield header
            for product in queryset.iterator():
                yield [
                    product.id,
                    product.sku,
                    product.product_name,
                    product.category,
                    product.subcategory,
                    product.variant,
                    product.finish,
                    product.gd_price,
                    product.rgd_price,
                    product.status,
                    product.is_imported,
                    product.created_at.isoformat() if product.created_at else "",
                ]

        writer = csv.writer(_Echo())
        streaming_content = (writer.writerow(row) for row in row_iterator())

        response = StreamingHttpResponse(streaming_content, content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="parsed_products_export.csv"'
        return response


# ==========================================================
# UPLOAD PDF
# ==========================================================

class CatalogUploadAPIView(APIView):

    permission_classes = [IsAuthenticated]

    def post(self, request):
        pdf = request.FILES.get("pdf")
        brand = request.data.get("brand", "").strip()
        category_id = request.data.get("category")

        # --------------------------------------------------
        # Validation
        # --------------------------------------------------

        if not pdf:

            return Response(
                {
                    "success": False,
                    "error": "PDF file is required.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not category_id:

            return Response(
                {
                    "success": False,
                    "error": "Category is required.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:

            category = Category.objects.get(pk=category_id)

        except Category.DoesNotExist:

            return Response(
                {
                    "success": False,
                    "error": "Invalid category.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # --------------------------------------------------
        # Import Pipeline
        # --------------------------------------------------

        try:

            catalog_import, summary = import_catalog(
                pdf_file=pdf,
                brand=brand,
                category=category,
            )

            return Response(
                {
                    "success": True,
                    "message": "Catalog imported successfully.",
                    "catalog_import_id": catalog_import.id,
                    "status": catalog_import.status,
                    "summary": summary,
                },
                status=status.HTTP_201_CREATED,
            )

        except Exception as exc:

            return Response(
                {
                    "success": False,
                    "error": str(exc),
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


# ==========================================================
# UPLOAD JSON
# ==========================================================

class JsonCatalogImportAPIView(APIView):

    permission_classes = [IsAuthenticated]

    def post(self, request):

        serializer = JsonCatalogImportSerializer(data=request.data)

        serializer.is_valid(raise_exception=True)

        json_file = serializer.validated_data["json_file"]

        brand = request.data.get("brand", "").strip()

        category_id = request.data.get("category")

        subcategory_id = request.data.get("subcategory")

        # --------------------------------------------------
        # Validation
        # --------------------------------------------------

        if not category_id:

            return Response(
                {
                    "success": False,
                    "error": "Category is required.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not subcategory_id:

            return Response(
                {
                    "success": False,
                    "error": "Subcategory is required.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:

            category = Category.objects.get(pk=category_id)

        except Category.DoesNotExist:

            return Response(
                {
                    "success": False,
                    "error": "Invalid category.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            subcategory = SubCategory.objects.get(
                pk=subcategory_id,
                category=category,
                active=True,
            )
        except SubCategory.DoesNotExist:
            return Response(
                {
                    "success": False,
                    "error": "Invalid subcategory.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # --------------------------------------------------
        # Import Pipeline
        # --------------------------------------------------

        try:

            catalog_import, summary = import_json(
                json_file=json_file,
                brand=brand,
                category=category,
                default_subcategory=subcategory.name,
            )

            return Response(
                {
                    "success": True,
                    "message": "JSON imported successfully.",
                    "catalog_import_id": catalog_import.id,
                    "status": catalog_import.status,
                    "summary": summary,
                },
                status=status.HTTP_201_CREATED,
            )

        except Exception as exc:
            traceback.print_exc()

            return Response(
        {
            "success": False,
            "type": exc.__class__.__name__,
            "error": str(exc),
        },
        status=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )