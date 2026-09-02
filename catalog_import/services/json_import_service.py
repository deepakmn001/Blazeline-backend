"""
catalog_import/services/json_import_service.py

Production JSON catalog import service.

This module implements a parallel import pipeline for JSON catalog files,
running alongside (and never touching) the existing PDF / OCR import flow.

Pipeline:

    JSON -> CatalogImport -> ParsedProduct -> Catalog Review -> Publish

The Catalog Review and Publish flows are untouched by this module: it only
produces CatalogImport + ParsedProduct rows in the same shape that the
existing PDF pipeline produces them, so downstream code needs no changes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from numbers import Number
from typing import Any, Dict, List, Optional, Tuple

from django.core.files.uploadedfile import UploadedFile
from django.db import transaction

from catalog_import.models import CatalogImport, ParsedProduct

# `CatalogImport.category` is a ForeignKey(Category), not a plain string.
# Adjust this import path to wherever Category actually lives in your app
# layout (e.g. `catalog.models` vs `catalog_import.models`).
from catalog.models import Category


# --------------------------------------------------------------------------
# Exceptions
# --------------------------------------------------------------------------

class JsonImportError(Exception):
    """Base exception for all JSON import failures.

    Any exception raised during ``import_json`` should be (or inherit from)
    this class so callers / views can catch a single, predictable error
    type and translate it into an appropriate API response.
    """


class JsonParseError(JsonImportError):
    """Raised when the uploaded file is not valid JSON."""


class JsonSchemaError(JsonImportError):
    """Raised when the JSON is valid but does not match the expected schema."""


class ProductValidationError(JsonImportError):
    """Raised when a single product entry fails field-level validation."""

    def __init__(self, index: int, message: str):
        self.index = index
        self.message = message
        super().__init__(f"Product at index {index}: {message}")


class DuplicateSkuError(JsonImportError):
    """Raised when the same SKU appears more than once within one JSON file."""

    def __init__(self, sku: str, first_index: int, duplicate_index: int):
        self.sku = sku
        self.first_index = first_index
        self.duplicate_index = duplicate_index
        super().__init__(
            f"Duplicate sku '{sku}' at index {duplicate_index} "
            f"(already used at index {first_index})."
        )


# --------------------------------------------------------------------------
# Internal data structures
# --------------------------------------------------------------------------

@dataclass
class _ValidatedProduct:
    """A single product entry after validation, ready to become a
    ParsedProduct row."""

    sku: str
    product_name: str
    variant: str
    standard_price: Optional[Decimal]
    gd_price: Optional[Decimal]
    rgd_price: Optional[Decimal]
    mb_price: Optional[Decimal]
    finish: str
    finishes: List[str]
    category: str
    subcategory: str
    page_number: int
    raw_text: str
    collection: str
    series: str

    attributes: Dict[str, Any]
    specifications: Dict[str, Any]
    variant_axis_name: str
    variant_prices: Dict[str, Decimal]
    variants: List[Dict[str, Any]]
    variant_result_attributes: Dict[str, Any]

    ocr_confidence: float
    ai_confidence: float
    sku_confidence: float
    name_confidence: float
    price_confidence: float
    layout_confidence: float


@dataclass
class _ImportSummary:
    total_products: int = 0
    imported_products: int = 0
    invalid_products: int = 0
    brand: str = ""
    category: str = ""
    errors: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "total_products": self.total_products,
            "imported_products": self.imported_products,
            "invalid_products": self.invalid_products,
            "brand": self.brand,
            "category": self.category,
        }


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------

@transaction.atomic
def import_json(
    *,
    json_file: UploadedFile,
    brand: str,
    category: Optional[Category],
    default_subcategory: str = "",
) -> Tuple[CatalogImport, Dict[str, Any]]:
    """Import a JSON catalog file into the CatalogImport / ParsedProduct
    pipeline.

    Args:
        json_file: The uploaded JSON file (e.g. ``request.FILES['file']``).
        brand: Brand supplied by the request. Takes priority over any brand
            declared inside the JSON payload.
        category: ``Category`` instance supplied by the request (``CatalogImport.category``
            is a ForeignKey, so this must be a model instance, not a string).
            Takes priority over the root-level ``category`` name in the JSON
            payload, which is resolved to a ``Category`` by lookup. Product-level
            ``category`` (a plain string on ``ParsedProduct``) still overrides
            both, see ``_resolve_product_field``.
        default_subcategory: Subcategory supplied by the request, used as the
            fallback when the JSON payload has no root-level ``subcategory``.
            Product-level ``subcategory`` still overrides both, see
            ``_resolve_product_field``.

    Returns:
        A tuple of ``(catalog_import, summary)`` where ``catalog_import`` is
        the created ``CatalogImport`` instance and ``summary`` is a plain
        dict describing the outcome of the import.

    Raises:
        JsonParseError: If the file content is not valid JSON.
        JsonSchemaError: If the JSON does not match the expected schema
            (e.g. missing/invalid ``products`` list, or an unresolvable category).
        ProductValidationError: If a product entry fails validation.
        DuplicateSkuError: If the same SKU appears more than once in the file.

    The whole operation runs inside a single database transaction: if any
    step fails, nothing is persisted (including the CatalogImport row).
    """
    raw_bytes = _read_file(json_file)
    payload = _parse_json(raw_bytes)
    _validate_payload_schema(payload)

    resolved_brand = _first_non_empty(brand, payload.get("brand"))
    resolved_category = _resolve_catalog_category(category, payload.get("category"))
    root_collection = _clean_str(payload.get("collection"))
    root_series = _clean_str(payload.get("series"))
    # Product-level `category`/`subcategory` on ParsedProduct are plain strings,
    # so the root-level fallback for those stays string-based (independent of
    # the FK resolution above used for CatalogImport.category).
    root_category_name = _clean_str(payload.get("category")) or resolved_category.name
    root_subcategory = (
        _clean_str(payload.get("subcategory"))
        or default_subcategory
    )

    products_raw = payload["products"]

    validated_products: List[_ValidatedProduct] = []
    seen_products: Dict[tuple[str, str, str], int] = {}
    summary = _ImportSummary(
        total_products=len(products_raw),
        brand=resolved_brand,
        category=resolved_category.name,
    )

    for index, raw_product in enumerate(products_raw):
        try:
            validated = _validate_product(
                raw_product,
                index=index,
                root_category=root_category_name,
                root_subcategory=root_subcategory,
                root_collection=root_collection,
                root_series=root_series,
            )
            
            
            
            variant_identity = (
                validated.variant.strip()
    if validated.variant
    else str(
        validated.attributes.get(
            validated.variant_axis_name,
            ""
        )
    ).strip()
)

            product_key = (
                validated.sku.strip().upper(),
                validated.product_name.strip().upper(),
                variant_identity.strip().upper(),
            )
            

            if product_key in seen_products:
                raise DuplicateSkuError(
                    sku=validated.sku,
                    first_index=seen_products[product_key],
                    duplicate_index=index,
                )
            seen_products[product_key] = index
        except (ProductValidationError, DuplicateSkuError):
            # Validation failures abort the whole import (transaction.atomic
            # rolls everything back), per the "raise meaningful exceptions /
            # rollback entire import" requirement. We still track the count
            # for completeness before re-raising.
            summary.invalid_products += 1
            raise
        validated_products.append(validated)

    catalog_import = _create_catalog_import(
        json_file=json_file,
        brand=resolved_brand,
        category=resolved_category,
    )

    _bulk_create_parsed_products(
        catalog_import=catalog_import,
        products=validated_products,
    )

    summary.imported_products = len(validated_products)

    return catalog_import, summary.as_dict()


# --------------------------------------------------------------------------
# File / JSON parsing helpers
# --------------------------------------------------------------------------

def _read_file(json_file: UploadedFile) -> bytes:
    """Read the full content of the uploaded file, resetting its pointer
    afterwards so it can still be saved to the CatalogImport model."""
    try:
        json_file.seek(0)
        content = json_file.read()
    except Exception as exc:  # noqa: BLE001 - surface as a domain error
        raise JsonParseError(f"Could not read uploaded file: {exc}") from exc
    finally:
        try:
            json_file.seek(0)
        except Exception:  # noqa: BLE001 - best effort, not fatal
            pass

    if not content:
        raise JsonParseError("Uploaded JSON file is empty.")

    return content


def _parse_json(raw_bytes: bytes) -> Dict[str, Any]:
    """Parse raw bytes as JSON, supporting both object and array roots."""
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise JsonParseError(
            f"Uploaded file is not valid UTF-8: {exc}"
        ) from exc

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise JsonParseError(
            f"Uploaded file is not valid JSON: {exc}"
        ) from exc

    # Support JSON files whose root is an array
    if isinstance(data, list):
        return {
            "products": data
        }

    if not isinstance(data, dict):
        raise JsonSchemaError(
            "Root JSON must be an object or an array of products."
        )

    return data


# --------------------------------------------------------------------------
# Schema / field validation helpers
# --------------------------------------------------------------------------

def _validate_payload_schema(payload: Dict[str, Any]) -> None:
    """Validate the top-level structure of the payload.

    Ensures ``products`` exists and is a non-empty list. Individual product
    validation happens separately in ``_validate_product``.
    """
    if "products" not in payload:
        raise JsonSchemaError("JSON payload is missing required key 'products'.")

    products = payload["products"]

    if not isinstance(products, list):
        raise JsonSchemaError("'products' must be a list.")

    if len(products) == 0:
        raise JsonSchemaError("'products' list must not be empty.")


def _validate_product(
    raw_product: Any,
    *,
    index: int,
    root_category: str,
    root_subcategory: str,
    root_collection: str,
    root_series: str,
) -> _ValidatedProduct:
    """Validate a single product dict and convert it into a
    ``_ValidatedProduct``.

    Raises ``ProductValidationError`` with the offending index/reason on
    any failure.
    """
    if not isinstance(raw_product, dict):
        raise ProductValidationError(index, "product entry must be an object.")

    sku = _require_str(raw_product, "sku", index)
    product_name = _require_str(raw_product, "product_name", index)

    variant = _validate_optional_str(raw_product, "variant", index)
    finish = _validate_optional_str(raw_product, "finish", index)
    raw_text = _validate_optional_str(raw_product, "raw_text", index)

    finishes = _validate_finishes(raw_product.get("finishes"), index)

    standard_price = _validate_numeric(raw_product.get("standard_price"), "standard_price", index)

    variant_prices = _validate_variant_prices(raw_product.get("variant_prices"), index)
    variants = raw_product.get("variants") or []

    if not isinstance(variants, list):
       raise ProductValidationError(
        index,
        "'variants' must be a list."
    )

    # Backward compatibility: if the payload provides the newer
    # `variant_prices` mapping (e.g. {"GD": 3300, "RGD": 3060, "MB": 2600}),
    # derive the legacy gd_price/rgd_price/mb_price fields from it so both
    # old and new JSON producers keep working against the same
    # ParsedProduct columns. If `variant_prices` is absent/empty, fall back
    # to reading the legacy flat fields directly, exactly as before.
    if variant_prices:
        gd_price = variant_prices.get("GD")
        rgd_price = variant_prices.get("RGD")
        mb_price = variant_prices.get("MB")
    else:
        gd_price = _validate_numeric(raw_product.get("gd_price"), "gd_price", index)
        rgd_price = _validate_numeric(raw_product.get("rgd_price"), "rgd_price", index)
        mb_price = _validate_numeric(raw_product.get("mb_price"), "mb_price", index)

    page_number = _validate_page_number(raw_product.get("page_number"), index)

    ocr_confidence = _validate_confidence(raw_product.get("ocr_confidence"), "ocr_confidence", index)
    ai_confidence = _validate_confidence(raw_product.get("ai_confidence"), "ai_confidence", index)
    sku_confidence = _validate_confidence(raw_product.get("sku_confidence"), "sku_confidence", index)
    name_confidence = _validate_confidence(raw_product.get("name_confidence"), "name_confidence", index)
    price_confidence = _validate_confidence(raw_product.get("price_confidence"), "price_confidence", index)
    layout_confidence = _validate_confidence(raw_product.get("layout_confidence"), "layout_confidence", index)

    category = _resolve_product_field(raw_product, "category", root_category)
    subcategory = _resolve_product_field(raw_product, "subcategory", root_subcategory)
    collection = _resolve_product_field(raw_product, "collection", root_collection)
    series = _resolve_product_field(raw_product, "series", root_series)

    attributes = _validate_attributes(
        raw_product.get("attributes"),
        index,
    )

    specifications = _validate_attributes(
        raw_product.get("specifications"),
        index,
    )

    variant_axis_name = _validate_optional_str(
        raw_product,
        "variant_axis_name",
        index,
    )
    variant_result_attributes = _validate_attributes(
    raw_product.get("variant_result_attributes"),
    index,
)

    return _ValidatedProduct(
        sku=sku,
        product_name=product_name,
        variant=variant,
        standard_price=standard_price,
        gd_price=gd_price,
        rgd_price=rgd_price,
        mb_price=mb_price,
        finish=finish,
        finishes=finishes,
        category=category,
        subcategory=subcategory,
        page_number=page_number,
        raw_text=raw_text,
        collection=collection,
        series=series,
        attributes=attributes,
        specifications=specifications,
        variant_axis_name=variant_axis_name,
        variant_prices=variant_prices,
        variants=variants,
        variant_result_attributes=variant_result_attributes,
        ocr_confidence=ocr_confidence,
        ai_confidence=ai_confidence,
        sku_confidence=sku_confidence,
        name_confidence=name_confidence,
        price_confidence=price_confidence,
        layout_confidence=layout_confidence,
    )


def _require_str(data: Dict[str, Any], key: str, index: int) -> str:
    """Require a non-empty string field on a product entry."""
    value = data.get(key)
    if value is None or not isinstance(value, str) or value.strip() == "":
        raise ProductValidationError(index, f"'{key}' is required and must be a non-empty string.")
    return value.strip()


def _validate_optional_str(data: Dict[str, Any], key: str, index: int) -> str:
    """Validate an optional string field, defaulting to ''."""
    value = data.get(key, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ProductValidationError(index, f"'{key}' must be a string.")
    return value


def _validate_finishes(value: Any, index: int) -> List[str]:
    """Validate the 'finishes' list field."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ProductValidationError(index, "'finishes' must be a list.")
    for item in value:
        if not isinstance(item, str):
            raise ProductValidationError(index, "'finishes' must be a list of strings.")
    return value


def _validate_attributes(value: Any, index: int) -> Dict[str, Any]:
    """Validate the optional 'attributes' object field.

    Defaults to {} when absent, matching ParsedProduct.attributes'
    default=dict so the service and model default stay consistent.
    """
    if value is None:
        return {}

    if not isinstance(value, dict):
        raise ProductValidationError(
            index,
            "'attributes' must be an object."
        )

    return value


def _validate_variant_prices(value: Any, index: int) -> Dict[str, Decimal]:
    """Validate the optional 'variant_prices' object field.

    Each value in the mapping is validated the same way as any other
    price-like field (via ``_validate_numeric``), so keys like "GD",
    "RGD", "MB" resolve to ``Decimal`` (or raise on a malformed entry).
    Defaults to {} when absent, matching ParsedProduct.variant_prices'
    default=dict.
    """
    if value is None:
        return {}

    if not isinstance(value, dict):
        raise ProductValidationError(
            index,
            "'variant_prices' must be an object."
        )

    validated: Dict[str, Decimal] = {}

    for key, price in value.items():
        validated[key] = _validate_numeric(
            price,
            f"variant_prices.{key}",
            index,
        )

    return validated


def _validate_numeric(value: Any, key: str, index: int) -> Optional[Decimal]:
    """Validate a price-like field: null, int, or float are accepted.

    Converted via `Decimal(str(value))` rather than `Decimal(value)` to avoid
    inheriting binary-float rounding artifacts (e.g. `Decimal(4800.1)` ->
    `4800.099999999999...`), and to match the model's `DecimalField`.
    """
    if value is None:
        return None
    if isinstance(value, bool):  # bool is a Number subclass in Python - reject explicitly
        raise ProductValidationError(index, f"'{key}' must be numeric, not boolean.")
    if not isinstance(value, Number):
        raise ProductValidationError(index, f"'{key}' must be numeric or null.")
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise ProductValidationError(index, f"'{key}' is not a valid decimal number.") from exc


def _validate_page_number(value: Any, index: int) -> int:
    """Validate the 'page_number' field.

    Falls back to 1 when absent, matching the model's
    `page_number = PositiveIntegerField(default=1)` so the service and the
    model default stay consistent (avoids passing NULL into a non-nullable
    field with a default).
    """
    if value is None:
        return 1
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProductValidationError(index, "'page_number' must be an integer.")
    if value < 1:
        raise ProductValidationError(index, "'page_number' must be a positive integer.")
    return value


def _validate_confidence(value: Any, key: str, index: int) -> float:
    """Validate an optional confidence score.

    Trusted JSON sources have no confidence data, so this defaults to 1.0
    (full confidence). If a future JSON source starts emitting real
    confidence scores (e.g. from an AI-assisted JSON generation step),
    they're picked up here automatically instead of being silently
    overwritten with 1.0.
    """
    if value is None:
        return 1.0
    if isinstance(value, bool) or not isinstance(value, Number):
        raise ProductValidationError(index, f"'{key}' must be numeric.")
    score = float(value)
    if not (0.0 <= score <= 1.0):
        raise ProductValidationError(index, f"'{key}' must be between 0.0 and 1.0.")
    return score


def _resolve_catalog_category(
    request_category: Optional[Category],
    json_category_name: Optional[str],
) -> Category:
    """Resolve the ``Category`` instance to store on ``CatalogImport.category``.

    ``CatalogImport.category`` is a ForeignKey, so the request-supplied
    ``Category`` instance always wins when present. Otherwise the root-level
    ``category`` name from the JSON payload is looked up by name.

    Raises:
        JsonSchemaError: If no request category was given and the JSON's
            category name doesn't resolve to an existing ``Category``.
    """
    if request_category is not None:
        return request_category

    json_category_name = _clean_str(json_category_name)
    if not json_category_name:
        raise JsonSchemaError(
            "No category was supplied on the request and the JSON payload "
            "has no root-level 'category' to fall back to."
        )

    try:
        return Category.objects.get(name__iexact=json_category_name)
    except Category.DoesNotExist as exc:
        raise JsonSchemaError(
            f"JSON category '{json_category_name}' does not match any existing Category."
        ) from exc
    except Category.MultipleObjectsReturned as exc:
        raise JsonSchemaError(
            f"JSON category '{json_category_name}' matches more than one Category; "
            "supply an explicit category on the request instead."
        ) from exc


def _resolve_product_field(raw_product: Dict[str, Any], key: str, root_value: str) -> str:
    """Resolve a field that can be overridden at the product level, falling
    back to the root-level value.

    Product-level value wins only if it is a non-empty string; otherwise
    the root-level default is used.
    """
    product_value = raw_product.get(key)
    if isinstance(product_value, str) and product_value.strip() != "":
        return product_value.strip()
    return root_value or ""


def _first_non_empty(*values: Optional[str]) -> str:
    """Return the first non-empty string among the given values."""
    for value in values:
        if value is not None and isinstance(value, str) and value.strip() != "":
            return value.strip()
    return ""


def _clean_str(value: Any) -> str:
    """Normalize a possibly-missing string field to ''."""
    if isinstance(value, str):
        return value.strip()
    return ""


# --------------------------------------------------------------------------
# Persistence helpers
# --------------------------------------------------------------------------

def _create_catalog_import(
    *,
    json_file: UploadedFile,
    brand: str,
    category: Category,
) -> CatalogImport:
    """Create the CatalogImport row for this JSON import.

    Mirrors the PDF pipeline's CatalogImport creation, but sets
    ``source_type=JSON`` and stores the uploaded JSON file instead of a PDF.

    NOTE: the model has no dedicated field for a JSON source file, and no
    `error_message` field on CatalogImport itself. Since `pdf` is already
    blank=True/null=True and unused for JSON imports, the JSON file is
    stored there as generic upload storage. If/when the model gains a
    dedicated `source_file`/`json_file` field, swap the assignment below
    to use it instead - no other part of this function needs to change.
    """
    catalog_import = CatalogImport(
        source_type=CatalogImport.SourceType.JSON,
        status=CatalogImport.Status.PARSED,
        brand=brand,
        category=category,
        pdf=json_file,
    )
    catalog_import.save()
    return catalog_import


def _bulk_create_parsed_products(
    *,
    catalog_import: CatalogImport,
    products: List[_ValidatedProduct],
) -> None:
    """Build ParsedProduct instances for every validated product and persist
    them in a single ``bulk_create`` call.

    Every field on ParsedProduct is populated explicitly (rather than
    relying on model defaults) so behavior is identical regardless of how
    the model's defaults are defined.

    Does not return the created rows: ``bulk_create``'s return value does
    not reliably have PKs populated on every database backend, so callers
    should not depend on it. The caller already knows the count from the
    validated-products list it passed in.
    """
    parsed_products = [
        ParsedProduct(
            catalog=catalog_import,
            page_number=product.page_number,
            sku=product.sku,
            product_name=product.product_name,
            standard_price=product.standard_price,
            gd_price=product.gd_price,
            rgd_price=product.rgd_price,
            mb_price=product.mb_price,
            finish=product.finish,
            finishes=product.finishes,
            category=product.category,
            subcategory=product.subcategory,
            variant=product.variant,
            collection=product.collection,
            series=product.series,
            attributes=product.attributes,
            specifications=product.specifications,
            variant_axis_name=product.variant_axis_name,
            variant_prices={
                key: str(price) for key, price in product.variant_prices.items()
            },
            variants=product.variants,
            variant_result_attributes=product.variant_result_attributes,

            raw_text=product.raw_text,
            status=ParsedProduct.Status.PENDING,
            is_imported=False,
            error_message="",
            ocr_confidence=product.ocr_confidence,
            ai_confidence=product.ai_confidence,
            sku_confidence=product.sku_confidence,
            name_confidence=product.name_confidence,
            price_confidence=product.price_confidence,
            layout_confidence=product.layout_confidence,
            flagged_for_review=False,
            review_reasons=[],
        )
        for product in products
    ]

    ParsedProduct.objects.bulk_create(parsed_products)