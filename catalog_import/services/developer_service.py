from __future__ import annotations

from catalog_import.models import CatalogImport

from .parser import parse_catalog
from .importer import (
    convert_regions_to_parsed_products,
    validate_products,
    summarize_import,
)


def parse_existing_catalog(
    *,
    catalog_id: int,
    start_page: int | None = None,
    end_page: int | None = None,
):
    """
    Developer mode.

    Parse an existing uploaded catalog without
    creating a new CatalogImport or writing anything
    to the database.

    Returns

        catalog
        parsed_products
        summary
    """

    catalog = CatalogImport.objects.get(
        pk=catalog_id
    )

    # ------------------------------------
    # Parse PDF
    # ------------------------------------

    product_regions = parse_catalog(
        catalog.pdf,
        start_page=start_page,
        end_page=end_page,
    )

    # ------------------------------------
    # Convert
    # ------------------------------------

    parsed_products = convert_regions_to_parsed_products(
        regions=product_regions,
        brand=catalog.brand,
        category=str(catalog.category),
        subcategory="",
    )

    # ------------------------------------
    # Validate
    # ------------------------------------

    validate_products(parsed_products)

    # ------------------------------------
    # Summary
    # ------------------------------------

    summary = summarize_import(
        parsed_products
    )

    return (
        catalog,
        parsed_products,
        summary,
    )