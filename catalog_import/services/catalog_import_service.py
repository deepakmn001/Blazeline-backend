from __future__ import annotations

from typing import Any

from django.core.files.base import File

from catalog_import.models import CatalogImport

from .database_importer import run_database_import
from .importer import (
    convert_regions_to_parsed_products,
    summarize_import,
    validate_products,
)
from .parser import parse_catalog


def import_catalog(
    *,
    pdf_file: File,
    category: Any,
    brand: str = "",
    subcategory: str = "",
    start_page: int | None = None,
    end_page: int | None = None,
) -> tuple[CatalogImport, dict]:
    """
    Complete catalog import workflow.

    Flow

        PDF
            ↓
        OCR
            ↓
        Spatial Parser
            ↓
        ParsedProduct
            ↓
        Validation
            ↓
        Database Import

    Developer Mode

        start_page / end_page are optional.

        If omitted:
            Entire catalog is parsed.

        If provided:
            Only the requested page range is parsed.

    """

    # --------------------------------------------------
    # Parse PDF
    # --------------------------------------------------

    product_regions = parse_catalog(
        pdf_file,
        start_page=start_page,
        end_page=end_page,
    )

    # --------------------------------------------------
    # Convert parser output
    # --------------------------------------------------

    parsed_products = convert_regions_to_parsed_products(
        regions=product_regions,
        brand=brand,
        category=str(category),
        subcategory=subcategory,
    )

    # --------------------------------------------------
    # Validation
    # --------------------------------------------------

    validate_products(parsed_products)

    # --------------------------------------------------
    # Summary
    # --------------------------------------------------

    summary = summarize_import(parsed_products)

    # --------------------------------------------------
    # Save into database
    # --------------------------------------------------

    catalog_import = run_database_import(
        pdf_file=pdf_file,
        brand=brand,
        category=category,
        parsed_products=parsed_products,
    )

    return catalog_import, summary