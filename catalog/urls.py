from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    CategoryViewSet,
    SubCategoryViewSet,
    ProductViewSet,
    ProductImageViewSet,
    ProductVariantViewSet,
    ProductSpecificationViewSet,
    DashboardAPIView,
    DeliveryCheckAPIView,
)

router = DefaultRouter()

# ==========================================================
# CATEGORY
# ==========================================================

router.register(
    "categories",
    CategoryViewSet,
)

# ==========================================================
# SUB CATEGORY
# ==========================================================

router.register(
    "subcategories",
    SubCategoryViewSet,
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
    "delivery/check/",
    DeliveryCheckAPIView.as_view(),
    name="delivery-check",
),
]

# ==========================================================
# DRF ROUTER URLS
# ==========================================================

urlpatterns += router.urls