"""
publish_service.py

Generic Variant Engine importer.

DESIGN NOTE ON GENERICITY:
This service reads two OPTIONAL generic fields off ParsedProduct if the
parser provides them:

    parsed.attributes       -> dict[str, str], e.g. {"Size": "15mm",
                                "Voltage": "220V", "Pressure": "12 Bar"}
                                Static, non-fanning-out option axes.

    parsed.variant_prices   -> dict[str, Decimal|str|int], e.g.
                                {"GD": 3000, "RGD": 3400, "MB": 3800}
                                The fan-out axis: one ParsedProduct row
                                can spawn multiple ProductVariants, one
                                per key here.

    parsed.variant_axis_name -> str, e.g. "Finish". Name of the
                                ProductOption the variant_prices keys
                                belong to. Defaults to "Finish" if not
                                supplied.

No axis name or axis value (Size, Finish, GD, RGD, MB, Voltage, ...) is
ever hardcoded as a branch in this file — a client adding "Pressure" or
"Handle Type" tomorrow requires zero changes here, only a parser change
that populates `attributes` / `variant_prices` differently.

BACKWARD COMPAT: if `parsed.attributes` / `parsed.variant_prices` are
not present on the ParsedProduct instance (e.g. parser hasn't been
upgraded yet), this service falls back to the legacy
size/color/material/gd_price/rgd_price/mb_price/standard_price fields
so nothing breaks mid-migration. Once the parser is upgraded to emit
`attributes`/`variant_prices`, the legacy fallback path simply stops
being hit — no code deletion required, but it can be removed later.

SKU NOTE: ProductVariant.sku is unique=True at the DB level in the
models.py this service was written against. If that constraint has
since been dropped, `_sku_taken()` / `_build_variant_sku()` become
unnecessary and should be simplified — confirm the live schema.
"""

from decimal import Decimal

from django.db import transaction
from django.db.models import Count, Q
from django.db.models.functions import Length
from django.utils.text import slugify

from catalog.models import (
    Product,
    ProductOption,
    ProductOptionValue,
    ProductVariant,
    ProductVariantOption,
    ProductImage,
    ProductSpecification,
    SubCategory,
)

from catalog_import.models import (
    CatalogImport,
    ParsedProduct,
)


class PublishError(Exception):
    """
    Raised whenever a ParsedProduct cannot be published.
    """
    pass


# ==========================================================
# GENERIC HELPERS
# ==========================================================

def _decimal(value):
    """
    Safely convert nullable values to Decimal.
    """

    if value in (None, ""):
        return Decimal("0")

    return Decimal(str(value))


def _unique_slug(name: str) -> str:
    """
    Generate unique product slug.
    """

    base = slugify(name)

    if not base:
        base = "product"

    slug = base
    counter = 2

    while Product.objects.filter(slug=slug).exists():
        slug = f"{base}-{counter}"
        counter += 1

    return slug


def _find_subcategory(parsed: ParsedProduct):
    """
    Match ParsedProduct.subcategory with DB.

    Category always comes from parsed.catalog.category (never OCR).
    SubCategory is resolved by name (case-insensitive), falling back
    to slug, then to a partial/contains match. Subcategories are
    never auto-created.
    """

    category = parsed.catalog.category

    if not parsed.subcategory:
        raise PublishError(
            f"No subcategory found for SKU '{parsed.sku}'."
        )

    subcategory = (
        SubCategory.objects.filter(
            category=category,
            name__iexact=parsed.subcategory.strip(),
            active=True,
        ).first()
    )

    if subcategory:
        return subcategory

    subcategory = (
        SubCategory.objects.filter(
            category=category,
            slug__iexact=slugify(parsed.subcategory),
            active=True,
        ).first()
    )

    if subcategory:
        return subcategory

    subcategory = (
        SubCategory.objects.filter(
            category=category,
            name__icontains=parsed.subcategory.strip(),
            active=True,
        )
        .annotate(name_len=Length("name"))
        .order_by("name_len")
        .first()
    )

    if subcategory:
        return subcategory

    raise PublishError(
        f"Subcategory '{parsed.subcategory}' not found "
        f"inside '{category.name}'."
    )


# ==========================================================
# SKU HELPERS
# (sku is unique=True on ProductVariant in the models.py this was
# written against — see module docstring)
# ==========================================================

def _sku_taken(sku: str) -> bool:
    return ProductVariant.objects.filter(sku=sku).exists()


def _build_variant_sku(parsed: ParsedProduct, axis_value: str, fan_out: bool) -> str:
    """
    A single ParsedProduct row fanning out into multiple variants
    (Layout A) cannot reuse the same raw sku for each of them under a
    unique sku constraint, so the fan-out axis value is suffixed on.
    When only one variant comes from this row, the original sku is
    kept exactly as parsed.
    """

    if not fan_out:
        return parsed.sku

    suffix = slugify(str(axis_value)).upper().replace("-", "")

    return f"{parsed.sku}-{suffix}"


# ==========================================================
# PRODUCT (Layout B grouping happens here)
# ==========================================================

def _normalize_name(name: str) -> str:
    return " ".join(name.strip().split()).lower()


def _find_or_create_product(parsed: ParsedProduct):
    """
    Layout B support.

    Reuses an existing Product under the same category + subcategory
    if the name matches, so VAU1001 / VAU1002 / VAU1003 (different
    ParsedProduct rows = different sizes/finishes of the same item)
    merge into a single Product across successive publish calls.

    Matching order:
      1. exact case-insensitive name match (fast path, indexed-ish)
      2. whitespace-normalized, case-insensitive match across
         candidates in the same category+subcategory (handles
         "Angle  Cock" vs "Angle Cock" / stray double spaces)

    Returns (product, created: bool).
    """

    category = parsed.catalog.category
    subcategory = _find_subcategory(parsed)

    product = Product.objects.filter(
        category=category,
        subcategory=subcategory,
        name__iexact=parsed.product_name.strip(),
    ).first()

    if product:
        return product, False

    target = _normalize_name(parsed.product_name)

    candidates = Product.objects.filter(
        category=category,
        subcategory=subcategory,
    )

    for candidate in candidates:
        if _normalize_name(candidate.name) == target:
            return candidate, False

    product = Product.objects.create(
        category=category,
        subcategory=subcategory,
        name=parsed.product_name,
        slug=_unique_slug(parsed.product_name),
        short_description="",
        description=parsed.raw_text or "",
        featured=False,
        active=True,
        status="published",
    )

    return product, True


def _attach_specifications(product, parsed):
    """
    Store parser metadata as Product-level specifications. Variant
    axes (Size/Finish/...) are never duplicated here — they live in
    ProductOption/ProductOptionValue/ProductVariantOption.
    """

    specs = []

    if parsed.collection:
        specs.append(("Collection", parsed.collection))

    if parsed.series:
        specs.append(("Series", parsed.series))

    if not specs:
        return

    existing = set(
        ProductSpecification.objects.filter(product=product)
        .values_list("key", "value")
    )

    existing_lower = {(k.lower(), v.lower()) for k, v in existing}

    to_create = [
        ProductSpecification(product=product, key=key, value=value)
        for key, value in specs
        if (key.lower(), value.lower()) not in existing_lower
    ]

    if to_create:
        ProductSpecification.objects.bulk_create(to_create)


# ==========================================================
# OPTIONS / OPTION VALUES (fully dynamic — no hardcoded axis names)
# ==========================================================

def _find_or_create_option(product, name: str, option_cache: dict):
    """
    Reuse an existing ProductOption (case-insensitive) or create it.
    `option_cache` is keyed per publish call to avoid re-querying the
    same option name repeatedly when a row touches it more than once.
    """

    name = name.strip()
    key = name.lower()

    if key in option_cache:
        return option_cache[key]

    option = ProductOption.objects.filter(
        product=product,
        name__iexact=name,
    ).first()

    if not option:
        next_sort = ProductOption.objects.filter(product=product).count()
        option = ProductOption.objects.create(
            product=product,
            name=name,
            sort_order=next_sort,
        )

    option_cache[key] = option
    return option


def _find_or_create_option_value(option, value: str, value_cache: dict):
    """
    Reuse an existing ProductOptionValue (case-insensitive) or create it.
    """

    value = str(value).strip()
    key = (option.id, value.lower())

    if key in value_cache:
        return value_cache[key]

    option_value = ProductOptionValue.objects.filter(
        option=option,
        value__iexact=value,
    ).first()

    if not option_value:
        next_sort = ProductOptionValue.objects.filter(option=option).count()
        option_value = ProductOptionValue.objects.create(
            option=option,
            value=value,
            sort_order=next_sort,
        )

    value_cache[key] = option_value
    return option_value


def _resolve_static_axes(product, parsed, option_cache, value_cache):
    """
    Fully generic static-axis resolution.

    Preferred path: parsed.attributes (dict[str, str]) — the parser
    decides what axes exist (Size, Voltage, Pressure, Handle Type,
    ...); this function has no knowledge of axis names.

    Fallback path (only used if parsed.attributes is absent): legacy
    size/color/material getattr, kept for backward compatibility
    during parser migration only.
    """

    attributes = getattr(parsed, "attributes", None)

    if attributes:
        pairs = list(attributes.items())
    else:
        pairs = [
            ("Size", getattr(parsed, "size", None)),
            ("Color", getattr(parsed, "color", None)),
            ("Material", getattr(parsed, "material", None)),
        ]

    option_values = []

    for axis_name, raw_value in pairs:

        if not raw_value:
            continue

        axis_name = str(axis_name).strip()

        if not axis_name:
            continue

        option = _find_or_create_option(product, axis_name, option_cache)
        option_value = _find_or_create_option_value(option, raw_value, value_cache)

        option_values.append(option_value)

    return option_values


def _resolve_fanout_axis(parsed):
    """
    Fully generic fan-out resolution.

    Preferred path: parsed.variant_prices (dict[str, price]) +
    parsed.variant_axis_name (str, defaults to "Finish"). Neither the
    axis name nor its values are hardcoded here.

    Fallback path (only used if parsed.variant_prices is absent):
    legacy gd_price/rgd_price/mb_price/standard_price fields, axis
    fixed at "Finish", kept for backward compatibility only.

    Returns (axis_name: str, [(value, price), ...]).
    """

    variant_prices = getattr(parsed, "variant_prices", None)

    if variant_prices:
        axis_name = getattr(parsed, "variant_axis_name", None) or "Finish"
        pairs = [
            (str(code), price)
            for code, price in variant_prices.items()
            if price not in (None, "")
        ]
        return axis_name, pairs

    legacy_fields = (
        ("GD", "gd_price"),
        ("RGD", "rgd_price"),
        ("MB", "mb_price"),
        ("Standard", "standard_price"),
    )

    pairs = [
        (code, getattr(parsed, field, None))
        for code, field in legacy_fields
        if getattr(parsed, field, None)
    ]

    return "Finish", pairs


# ==========================================================
# EXISTING-COMBINATION LOOKUP (avoids N+1 per candidate variant)
# ==========================================================

def _load_existing_combinations(product):
    """
    One query for the whole product: builds {variant_id: {option_value_id, ...}}
    then reduces to a set of frozensets, so checking "does this exact
    Size+Finish combination already exist" is an in-memory set lookup
    instead of a query per candidate variant.
    """

    rows = ProductVariantOption.objects.filter(
        variant__product=product,
    ).values_list("variant_id", "option_value_id")

    by_variant = {}

    for variant_id, option_value_id in rows:
        by_variant.setdefault(variant_id, set()).add(option_value_id)

    return {frozenset(values) for values in by_variant.values()}


# ==========================================================
# VARIANT CREATION (bulk)
# ==========================================================

def _create_variants(product, parsed):
    """
    Creates every variant implied by this ParsedProduct row, using
    bulk_create for ProductVariant, ProductVariantOption and
    ProductImage to minimize queries.
    """

    option_cache = {}
    value_cache = {}

    static_option_values = _resolve_static_axes(
        product, parsed, option_cache, value_cache,
    )

    fanout_axis_name, fanout_pairs = _resolve_fanout_axis(parsed)

    if not fanout_pairs:
        raise PublishError(
            f"No publishable price found for SKU '{parsed.sku}'."
        )

    fan_out = len(fanout_pairs) > 1

    existing_combinations = _load_existing_combinations(product)

    # ---- Pass 1: resolve options/values + build candidate specs ----

    candidates = []  # list of dicts: sku, price, option_values

    for axis_value, price in fanout_pairs:

        fanout_option = _find_or_create_option(
            product, fanout_axis_name, option_cache,
        )
        fanout_value = _find_or_create_option_value(
            fanout_option, axis_value, value_cache,
        )

        combination = static_option_values + [fanout_value]
        combo_key = frozenset(ov.id for ov in combination)

        if combo_key in existing_combinations:
            continue

        existing_combinations.add(combo_key)  # guard against dup within this row

        sku = _build_variant_sku(parsed, axis_value, fan_out)

        candidates.append({
            "sku": sku,
            "price": _decimal(price),
            "option_values": combination,
        })

    if not candidates:
        return []

    # ---- SKU pre-check (fail fast, all-or-nothing for this row) ----

    candidate_skus = [c["sku"] for c in candidates]

    taken = set(
        ProductVariant.objects.filter(
            sku__in=candidate_skus,
        ).values_list("sku", flat=True)
    )

    if taken:
        raise PublishError(
            f"SKU(s) already in use and cannot be reused: "
            f"{', '.join(sorted(taken))} (sku is unique across the catalog)."
        )

    # ---- Pass 2: bulk create variants ----

    variant_objs = [
        ProductVariant(
            product=product,
            sku=c["sku"],
            mrp=c["price"],
            selling_price=c["price"],
            stock=0,
            active=True,
        )
        for c in candidates
    ]

    created_variants = ProductVariant.objects.bulk_create(variant_objs)

    # Defensive: some backends/config combos don't return pks from
    # bulk_create. Refetch by sku if that happens so downstream FK
    # writes never get a variant with pk=None.
    if any(v.pk is None for v in created_variants):
        by_sku = {
            v.sku: v
            for v in ProductVariant.objects.filter(sku__in=candidate_skus)
        }
        created_variants = [by_sku[c["sku"]] for c in candidates]

    # ---- Pass 3: bulk create variant options ----

    variant_option_objs = []

    for variant, candidate in zip(created_variants, candidates):
        for option_value in candidate["option_values"]:
            variant_option_objs.append(
                ProductVariantOption(
                    variant=variant,
                    option_value=option_value,
                )
            )

    if variant_option_objs:
        ProductVariantOption.objects.bulk_create(variant_option_objs)

    # ---- Pass 4: image (see module docstring for the business rule) ----

    _attach_images(product, created_variants, parsed)

    return created_variants


# ==========================================================
# IMAGES
# ==========================================================

def _attach_images(product, created_variants, parsed):
    """
    Business rule (confirm this matches intent — see accompanying
    message): one ParsedProduct row carries at most one image. If that
    row fanned out into multiple variants (e.g. GD/RGD/MB sharing one
    photo), the image is attached ONCE, to the first variant created
    from this row — not duplicated across every fanned-out variant.

    Across rows (Layout B — 15mm row, 20mm row, ...), each row's own
    image still gets attached to that row's own first variant, so
    different sizes with different photos each keep their photo.

    `featured=True` is given to the very first ProductImage the
    product has received so far (product-level "hero" image);
    everything after that is featured=False.
    """

    if not parsed.image or not parsed.image.name or not created_variants:
        return

    target_variant = created_variants[0]

    already_attached = ProductImage.objects.filter(
        variant__product=product,
        image=parsed.image.name,
    ).exists()

    if already_attached:
        return

    is_first_image_for_product = not ProductImage.objects.filter(
        variant__product=product,
    ).exists()

    next_sort = ProductImage.objects.filter(variant=target_variant).count()

    ProductImage.objects.create(
        variant=target_variant,
        image=parsed.image,
        featured=is_first_image_for_product,
        sort_order=next_sort,
    )


# ==========================================================
# CATALOG STATUS
# ==========================================================

def _update_catalog_status(parsed):
    """
    Mark ParsedProduct as imported, then close out the parent
    CatalogImport once nothing is left pending.
    """

    parsed.is_imported = True
    parsed.status = ParsedProduct.Status.VALID
    parsed.error_message = ""

    parsed.save(
        update_fields=[
            "is_imported",
            "status",
            "error_message",
        ]
    )

    catalog = parsed.catalog

    remaining = catalog.parsed_products.filter(
        is_imported=False,
    ).exists()

    if not remaining:
        catalog.status = CatalogImport.Status.IMPORTED
        catalog.save(update_fields=["status"])


# ==========================================================
# MAIN PUBLISH FUNCTION
# ==========================================================

@transaction.atomic
def publish_parsed_product(parsed: ParsedProduct):
    """
    Publish one ParsedProduct into the production catalog using the
    Generic Variant Engine.

    Pipeline:

        ParsedProduct
              |
      Product (found or created — Layout B merge point)
              |
    ProductOption / ProductOptionValue (fully dynamic axes)
              |
       ProductVariant(s)          -- bulk_create
              |
      ProductVariantOption(s)     -- bulk_create
              |
      ProductSpecification(s)     -- bulk_create
              |
        ProductImage
              |
    ParsedProduct.is_imported = True

    Atomic: if any step fails, nothing is committed and the
    ParsedProduct is left untouched.
    """

    # ------------------------------------------------------
    # Already Imported
    # ------------------------------------------------------

    if parsed.is_imported:
        raise PublishError(
            f"SKU '{parsed.sku}' has already been imported."
        )

    # ------------------------------------------------------
    # Validation
    # ------------------------------------------------------

    if parsed.status == ParsedProduct.Status.INVALID:
        raise PublishError(
            "Invalid products cannot be published."
        )

    if not parsed.product_name:
        raise PublishError(
            "Product name is missing."
        )

    if not parsed.catalog:
        raise PublishError(
            "Catalog information is missing."
        )

    if not parsed.catalog.category:
        raise PublishError(
            "Category is missing."
        )

    # ------------------------------------------------------
    # Product (find-or-create -> Layout B grouping)
    # ------------------------------------------------------

    product, product_created = _find_or_create_product(parsed)

    # ------------------------------------------------------
    # Specifications
    # ------------------------------------------------------

    _attach_specifications(product, parsed)

    # ------------------------------------------------------
    # Variants (Options -> Option Values -> Variants -> VariantOptions -> Images)
    # ------------------------------------------------------

    variants = _create_variants(product, parsed)

    if not variants and product_created:
        raise PublishError(
            f"No new variants could be created for SKU '{parsed.sku}' "
            f"(all combinations already exist)."
        )

    # ------------------------------------------------------
    # Mark ParsedProduct Imported + Update CatalogImport
    # ------------------------------------------------------

    _update_catalog_status(parsed)

    # ------------------------------------------------------
    # Success
    # ------------------------------------------------------

    return {
        "success": True,
        "product": product,
        "product_created": product_created,
        "variants": variants,
    }