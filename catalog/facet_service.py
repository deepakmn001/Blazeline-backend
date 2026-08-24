from __future__ import annotations

import logging
from typing import Any, TypedDict

from django.core.cache import cache
from django.db.models import Count, Min, Max, Q, QuerySet

from .models import (
    Product,
    ProductOption,
    ProductOptionValue,
)

logger = logging.getLogger(__name__)


# ==========================================================
# CUSTOMER-FACING FILTER SAFETY
# ==========================================================

# These are technical/internal attributes that should NOT become
# customer-facing filters even if they happen to exist as options.
HIDDEN_OPTION_NAMES: frozenset[str] = frozenset(
    {
        "sku",
        "barcode",
        "design number",
        "design no",
        "finish code",
        "code",
        "product code",
        "internal code",
    }
)

CACHE_TTL_SECONDS = 120
CACHE_KEY_PREFIX = "product_facets"


# ==========================================================
# TYPES
# ==========================================================


class OptionFacetValue(TypedDict):
    value: str
    label: str
    count: int
    hex_color: str | None
    image_url: str | None


class OptionFacet(TypedDict):
    key: str
    label: str
    type: str
    values: list[OptionFacetValue]


class PriceFacet(TypedDict):
    min: float
    max: float


class AvailabilityFacet(TypedDict):
    in_stock: int
    out_of_stock: int


class ProductFacets(TypedDict):
    filters: list[OptionFacet]
    price: PriceFacet
    availability: AvailabilityFacet


# ==========================================================
# HELPERS
# ==========================================================


def _normalize_name(value: str) -> str:
    return " ".join((value or "").strip().lower().split())


def _is_customer_filter(option_name: str) -> bool:
    normalized = _normalize_name(option_name)

    if not normalized:
        return False

    if normalized in HIDDEN_OPTION_NAMES:
        return False

    return True


def _filter_scope(
    *,
    category_id: int | None = None,
    subcategory_id: int | None = None,
) -> QuerySet[Product]:
    """
    Returns the base Product queryset used for facet calculation.

    IMPORTANT:
    This does not modify anything in the database.
    It only builds a read-only queryset.
    """

    qs = Product.objects.filter(
        active=True,
        status="published",
    )

    if category_id:
        qs = qs.filter(category_id=category_id)

    if subcategory_id:
        qs = qs.filter(subcategory_id=subcategory_id)

    return qs


def _resolve_image_url(storage_field: Any, *, context: str) -> str | None:
    """
    Safely resolves a FileField/ImageField's URL. Never raises — a
    missing/broken file must degrade to "no image", not break the
    entire facet payload for every customer on the storefront.

    Deliberately broad except: different storage backends (local,
    Cloudinary, S3, ...) raise different exception types here, and
    this is a hard external boundary — any failure should degrade
    gracefully rather than propagate.
    """

    if not storage_field:
        return None

    try:
        return storage_field.url
    except Exception as exc:  # noqa: BLE001 - intentional boundary catch, see docstring
        logger.warning("Could not resolve image URL for %s: %s", context, exc)
        return None


# ==========================================================
# OPTION FACETS
# ==========================================================


def _resolve_option_metadata(
    scoped_products: QuerySet[Product],
) -> dict[str, dict[str, Any]]:
    """
    Fetches display metadata (canonical label, display_type, sort
    position) for every option in scope in a SINGLE query, grouped by
    normalized name. This both avoids an N+1 (one query per facet)
    and fixes a case-sensitivity bug where "Finish" and "finish"
    would previously be emitted as two separate duplicate facets.
    """

    rows = (
        ProductOption.objects.filter(product__in=scoped_products)
        .values("name", "display_type", "sort_order")
        .order_by("sort_order", "id")
    )

    metadata: dict[str, dict[str, Any]] = {}

    for row in rows:
        key = _normalize_name(row["name"])

        if not key or key in metadata:
            continue

        raw_display_type = row["display_type"]

        if raw_display_type == "color":
            display_type = "color"
        elif raw_display_type == "image":
            display_type = "image"
        else:
            # "buttons" / "dropdown" / anything else -> checkbox
            display_type = "checkbox"

        metadata[key] = {
            "label": row["name"],
            "display_type": display_type,
            "sort_order": row["sort_order"],
        }

    return metadata


def _build_values_for_option(
    *,
    canonical_name: str,
    scoped_products: QuerySet[Product],
) -> list[OptionFacetValue]:
    """
    Returns one facet-value entry per DISTINCT value string for the
    given option name.

    Different products often define their own ProductOption /
    ProductOptionValue rows that happen to share the same display
    value (e.g. two unrelated products both offering Finish = "GD").
    Those must collapse into a single facet-value entry with a
    combined count, not appear twice in the filter UI.
    """

    base_qs = ProductOptionValue.objects.filter(
        option__product__in=scoped_products,
        option__name__iexact=canonical_name,
    )

    # Pass 1 — authoritative count + sort position per distinct value,
    # computed with a single aggregate query. Only active variants
    # count, so a discontinued variant can't keep a filter option
    # alive (or inflate its count) after it's no longer purchasable.
    counts_by_value: dict[str, int] = {}
    sort_order_by_value: dict[str, int] = {}

    grouped = base_qs.values("value").annotate(
        product_count=Count(
            "variant_options__variant__product",
            filter=Q(
                variant_options__variant__product__in=scoped_products,
                variant_options__variant__active=True,
            ),
            distinct=True,
        ),
        min_sort_order=Min("sort_order"),
    )

    for row in grouped:
        counts_by_value[row["value"]] = int(row["product_count"] or 0)
        sort_order_by_value[row["value"]] = row["min_sort_order"] or 0

    # Pass 2 — one representative row per distinct value, used only to
    # source display metadata (hex_color / image) that pass 1 cannot
    # cleanly aggregate across a FileField.
    representative_by_value: dict[str, ProductOptionValue] = {}

    for option_value in base_qs.order_by("value", "sort_order", "id"):
        representative_by_value.setdefault(option_value.value, option_value)

    values: list[OptionFacetValue] = []
    sort_keys: dict[str, tuple[int, str]] = {}

    for value, count in counts_by_value.items():
        if count <= 0:
            continue

        representative = representative_by_value.get(value)
        hex_color = representative.hex_color or None if representative else None
        image_url = (
            _resolve_image_url(
                representative.image,
                context=f"option value '{value}' ({canonical_name})",
            )
            if representative
            else None
        )

        values.append(
            {
                "value": value,
                "label": value,
                "count": count,
                "hex_color": hex_color,
                "image_url": image_url,
            }
        )
        sort_keys[value] = (sort_order_by_value.get(value, 0), value)

    values.sort(key=lambda item: sort_keys[item["value"]])

    return values
def build_brand_facet(
    *,
    category_id: int | None = None,
    subcategory_id: int | None = None,
) -> OptionFacet:
    """
    Build the customer-facing Brand facet from the authoritative
    Product.brand field.

    Brand is product-level metadata, not a ProductOption.
    """

    scoped_products = _filter_scope(
        category_id=category_id,
        subcategory_id=subcategory_id,
    )

    rows = (
        scoped_products
        .exclude(brand="")
        .values("brand")
        .annotate(
            count=Count("id", distinct=True),
        )
        .order_by("brand")
    )

    return {
        "key": "brand",
        "label": "Brand",
        "type": "checkbox",
        "values": [
            {
                "value": row["brand"],
                "label": row["brand"],
                "count": int(row["count"]),
                "hex_color": None,
                "image_url": None,
            }
            for row in rows
            if row["brand"]
        ],
    }


def build_option_facets(
    *,
    category_id: int | None = None,
    subcategory_id: int | None = None,
) -> list[OptionFacet]:
    """
    Dynamically discovers ProductOption axes and their values within
    the requested product scope. No option names are hardcoded here.
    """

    scoped_products = _filter_scope(
        category_id=category_id,
        subcategory_id=subcategory_id,
    )

    option_metadata = _resolve_option_metadata(scoped_products)

    facets: list[OptionFacet] = []

    for normalized_key, meta in option_metadata.items():
        canonical_label = meta["label"]

        if not _is_customer_filter(canonical_label):
            continue
        if normalized_key == "brand":
            continue

        values = _build_values_for_option(
            canonical_name=canonical_label,
            scoped_products=scoped_products,
        )

        if not values:
            continue

        facets.append(
            {
                "key": normalized_key.replace(" ", "_"),
                "label": canonical_label,
                "type": meta["display_type"],
                "values": values,
            }
        )

    # Stable customer-facing order.
    facets.sort(key=lambda item: (item["label"].lower(), item["key"]))

    return facets


# ==========================================================
# PRICE FACET
# ==========================================================


def build_price_facet(
    *,
    category_id: int | None = None,
    subcategory_id: int | None = None,
) -> PriceFacet:
    """
    Calculates the actual selling-price range for products in scope,
    considering only active (purchasable) variants so a discontinued
    variant can't skew the slider range shown to customers.
    """

    scoped_products = _filter_scope(
        category_id=category_id,
        subcategory_id=subcategory_id,
    )

    aggregates = scoped_products.filter(variants__active=True).aggregate(
        minimum=Min("variants__selling_price"),
        maximum=Max("variants__selling_price"),
    )

    minimum = aggregates["minimum"]
    maximum = aggregates["maximum"]

    return {
        "min": float(minimum) if minimum is not None else 0,
        "max": float(maximum) if maximum is not None else 0,
    }


# ==========================================================
# AVAILABILITY FACET
# ==========================================================


def build_availability_facet(
    *,
    category_id: int | None = None,
    subcategory_id: int | None = None,
) -> AvailabilityFacet:
    """
    Counts products that have at least one active variant in stock
    versus products that currently have no stock available.
    """

    scoped_products = _filter_scope(
        category_id=category_id,
        subcategory_id=subcategory_id,
    )

    in_stock = (
        scoped_products.filter(
            variants__active=True,
            variants__stock__gt=0,
        )
        .distinct()
        .count()
    )

    total = scoped_products.distinct().count()

    out_of_stock = max(total - in_stock, 0)

    return {
        "in_stock": in_stock,
        "out_of_stock": out_of_stock,
    }


# ==========================================================
# COMPLETE FACET PAYLOAD
# ==========================================================


def _facets_cache_key(
    *,
    category_id: int | None,
    subcategory_id: int | None,
) -> str:
    return (
        f"{CACHE_KEY_PREFIX}:"
        f"cat={category_id or 'all'}:"
        f"subcat={subcategory_id or 'all'}"
    )


def build_product_facets(
    *,
    category_id: int | None = None,
    subcategory_id: int | None = None,
    use_cache: bool = False,
) -> ProductFacets:
    """
    Single read-only entry point used by the API.

    This deliberately returns only data required by the customer-facing
    filter UI.

    CACHING IS OFF BY DEFAULT. This project's default Django cache
    backend (LocMemCache) is per-process, so caching here would behave
    inconsistently across multiple Gunicorn/uWSGI workers and could
    also serve stale filters for up to CACHE_TTL_SECONDS after an
    admin publishes/edits a product, since there is no signal-based
    cache invalidation wired up yet.

    Before flipping use_cache=True in production:
      1. Configure a shared cache backend (e.g. Redis) in CACHES.
      2. Add post_save/post_delete signal handlers on Product /
         ProductVariant / ProductOption that call
         cache.delete(_facets_cache_key(...)) for the affected scope,
         so edits reflect immediately instead of waiting on TTL expiry.
      3. Load-test on staging first.

    Until then, this function's output is always computed live and
    behaves identically to before this refactor.
    """

    cache_key = _facets_cache_key(
        category_id=category_id,
        subcategory_id=subcategory_id,
    )

    if use_cache:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

    payload: ProductFacets = {
    "filters": [
        build_brand_facet(
            category_id=category_id,
            subcategory_id=subcategory_id,
        ),
        *build_option_facets(
            category_id=category_id,
            subcategory_id=subcategory_id,
        ),
    ],
        "price": build_price_facet(
            category_id=category_id,
            subcategory_id=subcategory_id,
        ),
        "availability": build_availability_facet(
            category_id=category_id,
            subcategory_id=subcategory_id,
        ),
    }

    if use_cache:
        cache.set(cache_key, payload, CACHE_TTL_SECONDS)

    return payload