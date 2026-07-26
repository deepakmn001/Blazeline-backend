from .spatial_parser import SKU_REGEX
from .types import ParsedProduct


def convert_regions_to_parsed_products(
    regions,
    brand="",
    category="",
    subcategory="",
):
    """
    Convert ProductRegion objects into ParsedProduct objects.

    This is a pure transformation step.
    No validation.
    No database access.

    PHASE: parser-integration update.

    Every new field added to ProductRegion in the spatial_parser
    Phase 1/2 passes (collection, series, mb_price, finishes, variant,
    ocr_confidence, ai_confidence, and the four confidence sub-scores)
    is now copied across onto ParsedProduct here.

    PHASE 5 UPDATE: spatial_parser's Phase 5 change added a dedicated
    `price` field on ProductRegion for "Standard Products" - a product
    with a single unlabeled price (no GD/RGD/MB label found anywhere
    near the SKU). That value used to be folded into gd_price and so
    was already covered here; now that it lives in its own field, it
    is copied across the same way (getattr with a "" default, so this
    still works unchanged against any ProductRegion built before this
    field existed).

    Backward compatibility notes:

    - `category` / `subcategory` PARAMETERS (passed in by the caller,
      e.g. from CatalogImport.category at upload time) are left as the
      authoritative source and take priority, exactly as before - this
      function's existing contract with its callers doesn't change.
      region.category / region.subcategory (OCR-derived, best-effort)
      are only used to FILL IN when the caller didn't supply a value,
      via `or`. This avoids a regression where a low-confidence OCR
      guess silently overrides a category the user explicitly chose
      during upload.
    - Every region attribute is read with getattr(..., default) rather
      than direct attribute access, so this function keeps working
      unchanged even if it's ever called with ProductRegion instances
      constructed before these fields existed (e.g. in an old test
      fixture or a cached/pickled object), rather than raising
      AttributeError.
    """

    parsed_products = []

    for region in regions:

        region_category = getattr(region, "category", "") or ""
        region_subcategory = getattr(region, "subcategory", "") or ""

        parsed_products.append(
            ParsedProduct(
                sku=region.sku.text,
                name=region.name,
                gd_price=region.gd_price,
                rgd_price=region.rgd_price,
                finish=region.finish,
                page=region.page_number,
                brand=brand,

                # category/subcategory: explicit caller value wins;
                # OCR-derived region value only fills gaps.
                category=category or region_category,
                subcategory=subcategory or region_subcategory,
                image_path=getattr(region, "image_path", "") or "",

                # ---- newly propagated fields ----
                # PHASE 5: Standard-Product unlabeled price. Populated
                # only when no GD/RGD/MB label was found near the SKU
                # at all - see spatial_parser.extract_product_data().
                price=getattr(region, "price", "") or "",
                mb_price=getattr(region, "mb_price", "") or "",
                finishes=list(getattr(region, "finishes", []) or []),
                variant=getattr(region, "variant", "") or "",
                collection=getattr(region, "collection", "") or "",
                series=getattr(region, "series", "") or "",

                ocr_confidence=getattr(region, "ocr_confidence", 0.0) or 0.0,
                ai_confidence=getattr(region, "ai_confidence", 0.0) or 0.0,
                sku_confidence=getattr(region, "sku_confidence", 0.0) or 0.0,
                name_confidence=getattr(region, "name_confidence", 0.0) or 0.0,
                price_confidence=getattr(region, "price_confidence", 0.0) or 0.0,
                layout_confidence=getattr(region, "layout_confidence", 0.0) or 0.0,

                # PHASE 4: OCR merge-artifact / page-outlier review flags.
                # Purely informational - never used to change status below.
                flagged_for_review=getattr(region, "flagged_for_review", False) or False,
                review_reasons=list(getattr(region, "review_reasons", []) or []),

                status="pending",
                error_message="",
            )
        )

    return parsed_products


def validate_products(products):
    """
    Validate ParsedProduct objects in-place.

    Sets:
        status
        error_message

    PHASE update: MB price is intentionally NOT part of the price
    validation check below. A product can be entirely valid with only
    a GD price and no MB/RGD - MB is opportunistic data, never
    required, consistent with the "never invent MB" rule upstream in
    spatial_parser. Confidence fields are informational (for review
    UI sorting/flagging) and deliberately do not affect valid/invalid
    status here - a low ai_confidence row should be surfaced for human
    review, not auto-rejected, since that would silently drop real
    products on noisy scans.

    PHASE 5 CHANGE: the price-presence check now also accepts
    `product.price` (the Standard-Product unlabeled price - see
    convert_regions_to_parsed_products / spatial_parser Phase 5).
    Before this change, a Standard Product's price lived in gd_price,
    so this check already covered it; now that it lives in its own
    field, the check has to look there too or every Standard Product
    would be wrongly flagged "Missing price" despite having a price.
    getattr(..., "") is used so this still works unchanged against any
    ParsedProduct built before the `price` field existed.
    """

    for product in products:

        errors = []

        # -----------------------------
        # SKU
        # -----------------------------

        if not product.sku:
            errors.append("Missing SKU")

        elif not SKU_REGEX.search(product.sku):
            errors.append("Invalid SKU format")

        # -----------------------------
        # Product Name
        # -----------------------------

        if not product.name:
            errors.append("Missing product name")

        # -----------------------------
        # Prices
        # -----------------------------

        has_price = (
            getattr(product, "price", "")
            or product.gd_price
            or product.rgd_price
        )

        if not has_price:
            errors.append("Missing price")

        # -----------------------------
        # Final Status
        # -----------------------------

        if errors:

            product.status = "invalid"
            product.error_message = "; ".join(errors)

        else:

            product.status = "valid"
            product.error_message = ""

    return products


def summarize_import(products):
    """
    Build an import summary.

    Returns a dictionary suitable for API responses
    and admin dashboards.

    PHASE update: added low_confidence_count and missing_collection as
    new, additive summary keys. Every existing key is untouched, so any
    caller reading e.g. summary["missing_price"] still works exactly as
    before - this only adds new keys a caller can opt into reading.

    PHASE 5 CHANGE: "missing_price" now matches the same has-a-price
    definition as validate_products() - product.price OR gd_price OR
    rgd_price - instead of only gd_price/rgd_price, so this count stays
    consistent with which products actually got flagged "invalid"
    above. Without this, "missing_price" could report Standard Products
    as missing a price even though validate_products() correctly
    treated them as valid.
    """

    total = len(products)

    valid_products = [
        p for p in products
        if p.status == "valid"
    ]

    invalid_products = [
        p for p in products
        if p.status == "invalid"
    ]

    def _missing_price(p):
        return not (
            getattr(p, "price", "")
            or p.gd_price
            or p.rgd_price
        )

    return {

        "total": total,

        "valid": len(valid_products),

        "invalid": len(invalid_products),

        "missing_name": sum(
            1
            for p in products
            if not p.name
        ),

        "missing_price": sum(
            1
            for p in products
            if _missing_price(p)
        ),

        "missing_sku": sum(
            1
            for p in products
            if not p.sku
        ),

        "invalid_sku_format": sum(
            1
            for p in products
            if p.sku and not SKU_REGEX.search(p.sku)
        ),

        "invalid_skus": [
            p.sku
            for p in invalid_products
        ],

        # ---- newly added, additive summary fields ----

        "missing_collection": sum(
            1
            for p in products
            if not getattr(p, "collection", "")
        ),

        "missing_mb_price": sum(
            1
            for p in products
            if not getattr(p, "mb_price", "")
        ),

        "low_confidence_count": sum(
            1
            for p in products
            if getattr(p, "ai_confidence", 1.0) < 0.5
        ),

        "flagged_for_review_count": sum(
            1
            for p in products
            if getattr(p, "flagged_for_review", False)
        ),
    }