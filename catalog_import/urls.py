from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    CatalogImportViewSet,
    ParsedProductViewSet,
    CatalogUploadAPIView,
    JsonCatalogImportAPIView
)

router = DefaultRouter()

router.register(
    "catalog-imports",
    CatalogImportViewSet,
)

router.register(
    "parsed-products",
    ParsedProductViewSet,
)

urlpatterns = [
    path(
        "catalog-import/upload/",
        CatalogUploadAPIView.as_view(),
        name="catalog-upload",
    ),
    path(
    "catalog-import/import-json/",
    JsonCatalogImportAPIView.as_view(),
    name="catalog-import-json",
),
]

urlpatterns += router.urls