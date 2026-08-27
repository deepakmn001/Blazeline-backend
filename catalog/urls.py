from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    CategoryViewSet,
     HomepageCategoryViewSet,
    SubCategoryViewSet,
    ProductViewSet,
    ProductImageViewSet,
    ProductVariantViewSet,
    ProductSpecificationViewSet,
    DashboardAPIView,
    SubCategoryStatsAPIView,
     AdminSubCategoryViewSet,
    DeliveryCheckAPIView,
     DeliveryQuoteAPIView,
     QuoteRequestAPIView,
     ReverseLocationAPIView,
     AdminCategoryViewSet,
)

router = DefaultRouter()

# ==========================================================
# CATEGORY
# ==========================================================

router.register(
    "categories",
    CategoryViewSet,
)
router.register(
    "admin/categories",
    AdminCategoryViewSet,
    basename="admin-categories",
)
router.register(
    "homepage/categories",
    HomepageCategoryViewSet,
    basename="homepage-categories",
)
# ==========================================================
# SUB CATEGORY
# ==========================================================

router.register(
    "subcategories",
    SubCategoryViewSet,
)
router.register(
    "admin/subcategories",
    AdminSubCategoryViewSet,
    basename="admin-subcategories",
)

# ==========================================================
# PRODUCT
# ==========================================================

router.register(
    "products",
    ProductViewSet,
)

# ==========================================================
# PRODUCT IMAGES
# ==========================================================

router.register(
    "product-images",
    ProductImageViewSet,
)

# ==========================================================
# PRODUCT VARIANTS
# ==========================================================

router.register(
    "product-variants",
    ProductVariantViewSet,
)

# ==========================================================
# PRODUCT SPECIFICATIONS
# ==========================================================

router.register(
    "product-specifications",
    ProductSpecificationViewSet,
)


# ==========================================================
# DASHBOARD API
# ==========================================================

urlpatterns = [
    path(
        "dashboard/",
        DashboardAPIView.as_view(),
        name="dashboard",
    ),
    path(
    "subcategories/stats/",
    SubCategoryStatsAPIView.as_view(),
    name="subcategory-stats",
),
    path(
    "delivery/check/",
    DeliveryCheckAPIView.as_view(),
    name="delivery-check",
),

path(
    "delivery/quote/",
    DeliveryQuoteAPIView.as_view(),
    name="delivery-quote",
),
path(
    "quote-requests/",
    QuoteRequestAPIView.as_view(),
    name="quote-requests",
),
path(
    "location/reverse/",
    ReverseLocationAPIView.as_view(),
    name="reverse-location",
),
]

# ==========================================================
# DRF ROUTER URLS
# ==========================================================

urlpatterns += router.urls
