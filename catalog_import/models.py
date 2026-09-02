from django.db import models

from .storage import CatalogImportStorage
class CatalogImport(models.Model):

    class Status(models.TextChoices):
        UPLOADED = "uploaded", "Uploaded"
        PARSING = "parsing", "Parsing"
        PARSED = "parsed", "Parsed"
        IMPORTED = "imported", "Imported"
        FAILED = "failed", "Failed"

    class SourceType(models.TextChoices):
        PDF = "pdf", "PDF"
        JSON = "json", "JSON"

    pdf = models.FileField(
    storage=CatalogImportStorage(),
    upload_to="",
    blank=True,
    null=True,
)
    brand = models.CharField(
        max_length=120,
        blank=True,
    )

    category = models.ForeignKey(
        "catalog.Category",
        on_delete=models.CASCADE,
        related_name="catalog_imports",
    )

    source_type = models.CharField(
        max_length=10,
        choices=SourceType.choices,
        default=SourceType.PDF,
    )

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.UPLOADED,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.brand} - {self.pdf.name}"


class ParsedProduct(models.Model):

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        VALID = "valid", "Valid"
        INVALID = "invalid", "Invalid"

    catalog = models.ForeignKey(
        CatalogImport,
        on_delete=models.CASCADE,
        related_name="parsed_products",
    )

    page_number = models.PositiveIntegerField(
        default=1,
    )

    sku = models.CharField(
        max_length=120,
        blank=True,
    )

    product_name = models.CharField(
        max_length=255,
    )

    # DEPRECATED: kept for backward compatibility with existing rows and
    # any code/reports still reading it. The spatial parser now produces
    # two distinct price points (GD / RGD) instead of one flat price, so
    # new rows should populate gd_price / rgd_price below.
    #
    # Migration strategy (do NOT do this in this change):
    #   1. Ship this migration adding gd_price/rgd_price as nullable,
    #      alongside the untouched `price` field (this step).
    #   2. Backfill: data migration copies `price` -> `gd_price` for all
    #      existing rows where gd_price is null, so historical data
    #      isn't lost and reads against gd_price stay correct.
    #   3. Update all remaining read/write call sites (admin, serializers,
    #      importer service) to use gd_price/rgd_price exclusively.
    #   4. Once nothing references `price` (verified via a deprecation
    #      period + search/monitoring), ship a final migration that
    #      removes the `price` field.
    #
    # NOTE (kept deliberately untouched by the Phase 5 change below): do
    # NOT repurpose this field for ParsedProduct(dataclass).price / the
    # spatial_parser "Standard Product" unlabeled price. That is a
    # DIFFERENT concept (a product legitimately parsed with no GD/RGD/MB
    # label at all) that happens to share the same attribute name as
    # this deprecated legacy field on the dataclass side. Writing it here
    # would (a) violate step 4 of the migration plan above - a final
    # migration dropping this column would silently delete real,
    # non-legacy data - and (b) make legacy-vs-new rows indistinguishable
    # to anything still reading this field as "the old flat price". See
    # `standard_price` below instead.
    price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        blank=True,
        null=True,
    )

    gd_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        blank=True,
        null=True,
    )

    rgd_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        blank=True,
        null=True,
    )

    # NEW: third price tier from spatial_parser Phase 1. Deliberately
    # nullable/blank with NO default-zero — an empty MB price must mean
    # "not present on this catalog page", never "priced at 0", matching
    # the "never invent MB" rule upstream.
    mb_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        blank=True,
        null=True,
    )

    # NEW (spatial_parser Phase 5): the "Standard Product" price - set
    # when a product had exactly one unlabeled price nearby and NO
    # GD/RGD/MB label was found anywhere near its SKU at all. Kept as a
    # field distinct from both the deprecated `price` above and from
    # `gd_price`, so:
    #   - a populated `gd_price` always means an actual "GD" label was
    #     read off the source page (never inferred), and
    #   - this new concept doesn't collide with, or get swept up in,
    #     the existing `price` deprecation/removal migration plan.
    # Same nullable/no-default-zero convention as the other price tiers.
    standard_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        blank=True,
        null=True,
    )

    finish = models.CharField(
        max_length=50,
        blank=True,
    )

    # NEW: full finish/price-code list (e.g. ["GD", "RGD", "MB", "CP"]),
    # distinct from the single `finish` material-descriptor field above
    # (e.g. "ROSE GOLD"). Stored as JSON since it's a variable-length
    # list, not a scalar - default=list (not []) so every row gets its
    # own list instance, not a shared mutable default.
    finishes = models.JSONField(
        default=list,
        blank=True,
    )

    # Raw/parsed category & subcategory text as read off the catalog page.
    # Distinct from CatalogImport.category, which is the structured FK
    # used for the import job itself - these are free-text values pulled
    # straight from OCR and may later be reconciled against catalog.Category
    # during review, without touching CatalogImport.category.
    category = models.CharField(
        max_length=120,
        blank=True,
    )

    subcategory = models.CharField(
        max_length=120,
        blank=True,
    )

    variant = models.CharField(
        max_length=120,
        blank=True,
    )

    # NEW: page-level metadata from spatial_parser's page-metadata
    # extraction (masthead/title detection) - the brand/collection line
    # (e.g. "Optra") and the series line (e.g. "Single Lever") that a
    # product's SKU fell under on its source page.
    collection = models.CharField(
        max_length=120,
        blank=True,
    )

    series = models.CharField(
        max_length=120,
        blank=True,
    )

    raw_text = models.TextField(
        blank=True,
    )

    image = models.ImageField(
        upload_to="catalog_products/",
        blank=True,
        null=True,
    )

    is_imported = models.BooleanField(
        default=False,
    )

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )

    error_message = models.TextField(
        blank=True,
        default="",
    )

    # ------------------------------------------------------------
    # NEW: confidence engine fields (spatial_parser Phase 2).
    #
    # All FloatFields, default=0.0 (never null) since "no confidence
    # computed yet" and "zero confidence" are the same actionable state
    # for review tooling (both mean: show this row for manual check).
    # Kept as plain floats, not DecimalField, since these are heuristic
    # scores for sorting/filtering in the review UI, not financial or
    # otherwise precision-critical values.
    # ------------------------------------------------------------

    ocr_confidence = models.FloatField(
        default=0.0,
    )

    ai_confidence = models.FloatField(
        default=0.0,
    )

    sku_confidence = models.FloatField(
        default=0.0,
    )

    name_confidence = models.FloatField(
        default=0.0,
    )

    price_confidence = models.FloatField(
        default=0.0,
    )

    layout_confidence = models.FloatField(
        default=0.0,
    )

    # ------------------------------------------------------------
    # NEW: OCR merge-artifact / page-outlier review flags
    # (spatial_parser Phase 4).
    #
    # Never used to set status=INVALID - a flagged row can still be
    # "valid"; this is purely a human-review signal so the review UI
    # can surface/filter/sort rows that need a second look, separate
    # from the pass/fail validation status above.
    # ------------------------------------------------------------

    flagged_for_review = models.BooleanField(
        default=False,
    )

    review_reasons = models.JSONField(
        default=list,
        blank=True,
    )
        # ------------------------------------------------------------
    # TECHNICAL SPECIFICATIONS
    #
    # Dedicated payload for customer-facing technical specifications.
    # IMPORTANT:
    # - `attributes` is reserved for selector / variant-driving data.
    # - `specifications` stores non-selectable factual product details.
    # - Existing rows remain safe because this field defaults to {}.
    # ------------------------------------------------------------
    attributes = models.JSONField(
        default=dict,
        blank=True,
    )

    specifications = models.JSONField(
        default=dict,
        blank=True,
    )

    variant_axis_name = models.CharField(
        max_length=100,
        blank=True,
    )
    variant_prices = models.JSONField(
        default=dict,
        blank=True,
    )
    variants = models.JSONField(
    default=list,
    blank=True,
)
# while these values belong to the exact selected variant.
    variant_result_attributes = models.JSONField(
        default=dict,
        blank=True,
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    class Meta:
        ordering = ["catalog", "page_number", "sku"]

    def __str__(self):
        return f"{self.sku} - {self.product_name}"