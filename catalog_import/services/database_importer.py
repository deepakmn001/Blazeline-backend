from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, List, Optional

from django.core.files.base import File
from django.db import transaction

from catalog_import.models import CatalogImport
from catalog_import.models import ParsedProduct as ParsedProductModel
from catalog_import.services.types import ParsedProduct as ParsedProductData


def _to_decimal(value: str) -> Optional[Decimal]:
    """
    Convert a parsed price string (e.g. "1200.00", or "" when missing)
    into a Decimal suitable for a DecimalField, or None if it can't be
    parsed / is empty.
    """

    if not value:
        return None

    try:
        return Decimal(value)
    except (InvalidOperation, TypeError):
        return None


def _to_float(value: Any) -> float:
    """
    Convert a confidence score into a plain float suitable for a
    FloatField. Confidence fields on ParsedProduct (dataclass) already
    default to 0.0 and are always numeric, but this guards against None
    or an unexpected type reaching bulk_create and raising there instead
    of failing loudly here.
    """

    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def create_import(pdf_file: File, brand: str, category: Any) -> CatalogImport:
    """
    Create and persist a new CatalogImport for this upload.

    `category` is a catalog.Category instance (or its pk), matching
    CatalogImport.category's existing foreign key.
    """

    return CatalogImport.objects.create(
        pdf=pdf_file,
        brand=brand,
        category=category,
        status=CatalogImport.Status.UPLOADED,
    )


def build_parsed_product_models(
    catalog_import: CatalogImport,
    parsed_products: List[ParsedProductData],
) -> List[ParsedProductModel]:
    """
    Convert ParsedProduct dataclass instances (from the importer pipeline)
    into unsaved ParsedProduct model instances linked to catalog_import.

    Does not hit the database.

    UPDATED: now maps every field added to the ParsedProduct dataclass /
    model in the spatial_parser Phase 1/2 + models.py migration work:

        mb_price, collection, series, finishes,
        ocr_confidence, ai_confidence, sku_confidence,
        name_confidence, price_confidence, layout_confidence

    Also fixes a pre-existing gap (not introduced by the recent changes):
    `variant` existed on both the dataclass and the model already, but
    was never copied across here - it's included now too.

    PHASE 5 UPDATE (Standard Product price + review flags): maps three
    more fields that were added to the dataclass/model but were still
    missing here:

      - dataclass `product.price` -> model `standard_price`. NOT a
        same-name mapping on purpose: the dataclass field is named
        `price` because there's no legacy field to collide with there,
        but on the model side `price` is already the pre-existing
        DEPRECATED flat-price column (see models.py). Writing into
        model.price here would silently mix new "Standard Product, no
        label found" data into a column that's on a documented removal
        path, and that data would be lost once that column is finally
        dropped. `standard_price` is the correct, distinct destination
        column - see models.py for the full rationale.
      - dataclass `product.flagged_for_review` -> model
        `flagged_for_review` (same name, no collision - straight copy).
      - dataclass `product.review_reasons` -> model `review_reasons`
        (same name, no collision - straight copy, defensively coerced
        to a list the same way `finishes` already is below).

    getattr(..., default) is used throughout (not direct attribute
    access) so this function doesn't raise AttributeError if it's ever
    called with an older ParsedProductData instance that predates these
    fields (e.g. a stale cached object).
    """

    model_instances: List[ParsedProductModel] = []

    for product in parsed_products:

        model_instances.append(
            ParsedProductModel(
                catalog=catalog_import,
                page_number=product.page,
                sku=product.sku,
                product_name=product.name,
                gd_price=_to_decimal(product.gd_price),
                rgd_price=_to_decimal(product.rgd_price),
                finish=product.finish,
                category=product.category,
                subcategory=product.subcategory,
                status=product.status,
                error_message=product.error_message,

                # ---- previously-existing model field, never mapped ----
                variant=getattr(product, "variant", "") or "",

                # ---- newly propagated fields ----
                mb_price=_to_decimal(getattr(product, "mb_price", "")),
                collection=getattr(product, "collection", "") or "",
                series=getattr(product, "series", "") or "",
                finishes=list(getattr(product, "finishes", []) or []),
                attributes=dict(
    getattr(product, "attributes", {}) or {}
),

variant_axis_name=getattr(
    product,
    "variant_axis_name",
    "",
) or "",

variant_prices=dict(
    getattr(product, "variant_prices", {}) or {}
),

                ocr_confidence=_to_float(getattr(product, "ocr_confidence", 0.0)),
                ai_confidence=_to_float(getattr(product, "ai_confidence", 0.0)),
                sku_confidence=_to_float(getattr(product, "sku_confidence", 0.0)),
                name_confidence=_to_float(getattr(product, "name_confidence", 0.0)),
                price_confidence=_to_float(getattr(product, "price_confidence", 0.0)),
                layout_confidence=_to_float(getattr(product, "layout_confidence", 0.0)),
                image=getattr(product, "image_path", "") or "",

                # ---- PHASE 5: Standard Product price + review flags ----
                # dataclass.price -> model.standard_price (deliberately
                # NOT model.price - see docstring above / models.py).
                standard_price=_to_decimal(getattr(product, "price", "")),

                flagged_for_review=bool(
                    getattr(product, "flagged_for_review", False)
                ),
                review_reasons=list(
                    getattr(product, "review_reasons", []) or []
                ),
            )
        )

    return model_instances


def bulk_save_products(model_instances: List[ParsedProductModel]) -> int:
    """
    Bulk-insert ParsedProduct model instances inside a single transaction.

    Returns the number of records created.
    """

    with transaction.atomic():
        created = ParsedProductModel.objects.bulk_create(model_instances)

    return len(created)


def update_catalog_statistics(
    catalog_import: CatalogImport,
    parsed_products: List[ParsedProductData],
) -> CatalogImport:
    """
    Update CatalogImport after an import run and persist the change.

    NOTE: CatalogImport does not currently define total_products /
    valid_products / invalid_products fields, and this function does not
    modify models.py, so those counts are not persisted here. Only
    `status` is updated. Counts can be computed on demand, e.g.:

        catalog_import.parsed_products.count()
        catalog_import.parsed_products.filter(status="valid").count()

    If persisted rollup fields are needed later, they should be added to
    CatalogImport via a dedicated migration.
    """

    total = len(parsed_products)

    if total == 0:
        catalog_import.status = CatalogImport.Status.FAILED
    else:
        catalog_import.status = CatalogImport.Status.IMPORTED

    catalog_import.save(update_fields=["status", "updated_at"])

    return catalog_import


def run_database_import(
    pdf_file: File,
    brand: str,
    category: Any,
    parsed_products: List[ParsedProductData],
) -> CatalogImport:
    """
    Full database import workflow:

    create_import()
        -> build_parsed_product_models()
        -> bulk_save_products()
        -> update_catalog_statistics()
        -> CatalogImport
    """

    catalog_import = create_import(pdf_file, brand, category)

    model_instances = build_parsed_product_models(
        catalog_import,
        parsed_products,
    )

    bulk_save_products(model_instances)

    update_catalog_statistics(catalog_import, parsed_products)

    return catalog_import