from django.contrib import admin

from .models import (
    CatalogImport,
    ParsedProduct,
)


@admin.register(CatalogImport)
class CatalogImportAdmin(admin.ModelAdmin):

    list_display = (
        "id",
        "brand",
        "category",
        "status",
        "created_at",
    )

    list_filter = (
        "status",
        "category",
    )

    search_fields = (
        "brand",
    )

    ordering = (
        "-created_at",
    )


@admin.register(ParsedProduct)
class ParsedProductAdmin(admin.ModelAdmin):

    list_display = (
        "id",
        "product_name",
        "sku",
        "price",
        "variant",
        "catalog",
        "is_imported",
    )

    list_filter = (
        "is_imported",
    )

    search_fields = (
        "product_name",
        "sku",
    )

    ordering = (
        "-created_at",
    )