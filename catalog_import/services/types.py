from dataclasses import dataclass, field


@dataclass
class ParsedProduct:
    sku: str
    name: str
    gd_price: str
    rgd_price: str
    finish: str
    page: int
    brand: str
    category: str
    subcategory: str
    status: str = "pending"
    error_message: str = ""

    # ------------------------------------------------------------
    # NEW (additive only) — propagated from ProductRegion via
    # importer.convert_regions_to_parsed_products().
    #
    # All defaulted, and appended AFTER the existing defaulted fields
    # (status, error_message) rather than inserted earlier, so any
    # existing positional instantiation of ParsedProduct(...) anywhere
    # in the codebase keeps working unchanged.
    # ------------------------------------------------------------

    # PHASE 5 ADDITION: "Standard Product" price - populated when a
    # product had a single unlabeled price nearby with no GD/RGD/MB
    # label found anywhere near its SKU (see spatial_parser Phase 5 /
    # ProductRegion.price). Kept as its own field rather than folded
    # into gd_price so that a populated gd_price always means an
    # actual "GD" label was read off the source page.
    price: str = ""

    mb_price: str = ""
    finishes: list = field(default_factory=list)
    variant: str = ""
    collection: str = ""
    series: str = ""
    image_path: str = ""
    attributes: dict = field(default_factory=dict)

    variant_axis_name: str = ""

    variant_prices: dict = field(default_factory=dict)
    ocr_confidence: float = 0.0
    ai_confidence: float = 0.0
    sku_confidence: float = 0.0
    name_confidence: float = 0.0
    price_confidence: float = 0.0
    layout_confidence: float = 0.0

    # PHASE 4 ADDITION: propagated from ProductRegion.flagged_for_review /
    # .review_reasons (spatial_parser's OCR-merge-artifact and page-local
    # statistical-outlier checks). Never affects `status`/`error_message`
    # (validate_products() leaves this alone) - a flagged product can
    # still be "valid"; this is purely a human-review signal for the
    # import UI to surface separately.
    flagged_for_review: bool = False
    review_reasons: list = field(default_factory=list)