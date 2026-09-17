from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone


class Promotion(models.Model):
    """
    Production-grade product promotion/campaign definition.

    IMPORTANT:
    - This model does NOT modify ProductVariant.selling_price.
    - Promotions are evaluated dynamically by the promotion service.
    - Orders should snapshot the final discount at checkout time.
    """

    class DiscountType(models.TextChoices):
        PERCENTAGE = "PERCENTAGE", "Percentage"
        FIXED_AMOUNT = "FIXED_AMOUNT", "Fixed amount"

    name = models.CharField(
        max_length=180,
        db_index=True,
        help_text="Internal/admin-facing name of the promotion.",
    )

    description = models.TextField(
        blank=True,
        default="",
    )

    discount_type = models.CharField(
        max_length=20,
        choices=DiscountType.choices,
        db_index=True,
    )

    discount_value = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        help_text=(
            "Percentage value when discount_type=PERCENTAGE, "
            "otherwise fixed discount amount."
        ),
    )

    max_discount_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text=(
            "Optional maximum total discount allowed from this promotion "
            "for a single cart/order."
        ),
    )

    minimum_cart_value = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text=(
            "Promotion applies only when the eligible cart subtotal "
            "meets or exceeds this amount."
        ),
    )

    is_active = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Master switch. Valid dates alone do not activate a promotion.",
    )

    start_at = models.DateTimeField(
        db_index=True,
        help_text="Promotion becomes eligible from this timestamp.",
    )

    end_at = models.DateTimeField(
        db_index=True,
        help_text="Promotion remains eligible until this timestamp.",
    )

    priority = models.PositiveIntegerField(
        default=0,
        db_index=True,
        help_text=(
            "Higher priority wins when multiple applicable promotions "
            "have the same targeting specificity."
        ),
    )

    stackable = models.BooleanField(
        default=False,
        db_index=True,
        help_text=(
            "Whether this promotion may be combined with another "
            "promotion by the promotion engine."
        ),
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        ordering = (
            "-priority",
            "-created_at",
            "-id",
        )
        indexes = [
            models.Index(
                fields=["is_active", "start_at", "end_at"],
                name="promo_active_window_idx",
            ),
            models.Index(
                fields=["is_active", "priority"],
                name="promo_active_priority_idx",
            ),
            models.Index(
                fields=["discount_type", "is_active"],
                name="promo_type_active_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(discount_value__gt=0),
                name="promo_discount_value_positive",
            ),
            models.CheckConstraint(
                condition=Q(minimum_cart_value__gte=0),
                name="promo_min_cart_value_non_negative",
            ),
            models.CheckConstraint(
                condition=(
                    Q(max_discount_amount__isnull=True)
                    | Q(max_discount_amount__gt=0)
                ),
                name="promo_max_discount_positive_or_null",
            ),
            models.CheckConstraint(
                condition=(
                    Q(discount_type="PERCENTAGE")
                    & Q(discount_value__gt=0)
                    & Q(discount_value__lte=100)
                )
                | (
                    Q(discount_type="FIXED_AMOUNT")
                    & Q(discount_value__gt=0)
                ),
                name="promo_discount_value_matches_type",
            ),
        ]

    def __str__(self) -> str:
        return self.name

    def clean(self) -> None:
        super().clean()

        if self.start_at and self.end_at:
            if self.start_at >= self.end_at:
                raise ValidationError(
                    {
                        "end_at": (
                            "Promotion end time must be later than start time."
                        )
                    }
                )

        if self.discount_type == self.DiscountType.PERCENTAGE:
            if self.discount_value > Decimal("100.00"):
                raise ValidationError(
                    {
                        "discount_value": (
                            "Percentage discount cannot be greater than 100%."
                        )
                    }
                )

        if self.discount_type == self.DiscountType.FIXED_AMOUNT:
            if self.discount_value <= Decimal("0.00"):
                raise ValidationError(
                    {
                        "discount_value": (
                            "Fixed discount amount must be greater than zero."
                        )
                    }
                )

        if self.minimum_cart_value < Decimal("0.00"):
            raise ValidationError(
                {
                    "minimum_cart_value": (
                        "Minimum cart value cannot be negative."
                    )
                }
            )

        if (
            self.max_discount_amount is not None
            and self.max_discount_amount <= Decimal("0.00")
        ):
            raise ValidationError(
                {
                    "max_discount_amount": (
                        "Maximum discount amount must be greater than zero."
                    )
                }
            )

    @property
    def is_currently_active(self) -> bool:
        """
        Runtime convenience property.

        The promotion engine should still perform the authoritative
        timezone-aware check itself rather than trusting this property.
        """
        if not self.is_active:
            return False

        now = timezone.now()

        return self.start_at <= now < self.end_at


class PromotionRule(models.Model):
    """
    Defines WHAT a promotion applies to.

    A promotion can have multiple rules, allowing for example:
        - Category: Electricals
        - Category: Lighting
        - Brand: Finolex
        - Product: Some Product
        - Variant: Some SKU

    Target fields are mutually exclusive according to target_type.
    """

    class TargetType(models.TextChoices):
        ALL = "ALL", "All products"
        CATEGORY = "CATEGORY", "Category"
        SUBCATEGORY = "SUBCATEGORY", "Subcategory"
        BRAND = "BRAND", "Brand"
        PRODUCT = "PRODUCT", "Product"
        VARIANT = "VARIANT", "Variant"

    promotion = models.ForeignKey(
        Promotion,
        on_delete=models.CASCADE,
        related_name="rules",
    )

    target_type = models.CharField(
        max_length=20,
        choices=TargetType.choices,
        db_index=True,
    )

    category = models.ForeignKey(
        "catalog.Category",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="promotion_rules",
    )

    subcategory = models.ForeignKey(
        "catalog.SubCategory",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="promotion_rules",
    )

    product = models.ForeignKey(
        "catalog.Product",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="promotion_rules",
    )

    variant = models.ForeignKey(
        "catalog.ProductVariant",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="promotion_rules",
    )

    # Existing catalog architecture stores Product.brand as a string.
    # We deliberately keep that architecture unchanged.
    brand = models.CharField(
        max_length=120,
        null=True,
        blank=True,
        db_index=True,
        help_text=(
            "Brand value exactly as represented in the catalog. "
            "Used only when target_type=BRAND."
        ),
    )

    # Normalized copy used by the promotion engine for deterministic,
    # case-insensitive brand matching.
    brand_normalized = models.CharField(
        max_length=120,
        null=True,
        blank=True,
        db_index=True,
        editable=False,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        ordering = (
            "promotion_id",
            "target_type",
            "id",
        )
        indexes = [
            models.Index(
                fields=["promotion", "target_type"],
                name="promo_rule_promo_type_idx",
            ),
            models.Index(
                fields=["target_type", "category"],
                name="promo_rule_category_idx",
            ),
            models.Index(
                fields=["target_type", "subcategory"],
                name="promo_rule_subcat_idx",
            ),
            models.Index(
                fields=["target_type", "product"],
                name="promo_rule_product_idx",
            ),
            models.Index(
                fields=["target_type", "variant"],
                name="promo_rule_variant_idx",
            ),
            models.Index(
                fields=["target_type", "brand_normalized"],
                name="promo_rule_brand_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    (
                        Q(target_type="ALL")
                        & Q(category__isnull=True)
                        & Q(subcategory__isnull=True)
                        & Q(product__isnull=True)
                        & Q(variant__isnull=True)
                        & Q(brand__isnull=True)
                        & Q(brand_normalized__isnull=True)
                    )
                    |
                    (
                        Q(target_type="CATEGORY")
                        & Q(category__isnull=False)
                        & Q(subcategory__isnull=True)
                        & Q(product__isnull=True)
                        & Q(variant__isnull=True)
                        & Q(brand__isnull=True)
                        & Q(brand_normalized__isnull=True)
                    )
                    |
                    (
                        Q(target_type="SUBCATEGORY")
                        & Q(category__isnull=True)
                        & Q(subcategory__isnull=False)
                        & Q(product__isnull=True)
                        & Q(variant__isnull=True)
                        & Q(brand__isnull=True)
                        & Q(brand_normalized__isnull=True)
                    )
                    |
                    (
                        Q(target_type="BRAND")
                        & Q(category__isnull=True)
                        & Q(subcategory__isnull=True)
                        & Q(product__isnull=True)
                        & Q(variant__isnull=True)
                        & Q(brand__isnull=False)
                        & Q(brand_normalized__isnull=False)
                    )
                    |
                    (
                        Q(target_type="PRODUCT")
                        & Q(category__isnull=True)
                        & Q(subcategory__isnull=True)
                        & Q(product__isnull=False)
                        & Q(variant__isnull=True)
                        & Q(brand__isnull=True)
                        & Q(brand_normalized__isnull=True)
                    )
                    |
                    (
                        Q(target_type="VARIANT")
                        & Q(category__isnull=True)
                        & Q(subcategory__isnull=True)
                        & Q(product__isnull=True)
                        & Q(variant__isnull=False)
                        & Q(brand__isnull=True)
                        & Q(brand_normalized__isnull=True)
                    )
                ),
                name="promo_rule_target_consistency",
            ),
            models.UniqueConstraint(
                fields=["promotion", "target_type"],
                condition=Q(target_type="ALL"),
                name="promo_rule_unique_all_target",
            ),
            models.UniqueConstraint(
                fields=["promotion", "category"],
                condition=Q(target_type="CATEGORY"),
                name="promo_rule_unique_category_target",
            ),
            models.UniqueConstraint(
                fields=["promotion", "subcategory"],
                condition=Q(target_type="SUBCATEGORY"),
                name="promo_rule_unique_subcategory_target",
            ),
            models.UniqueConstraint(
                fields=["promotion", "brand_normalized"],
                condition=Q(target_type="BRAND"),
                name="promo_rule_unique_brand_target",
            ),
            models.UniqueConstraint(
                fields=["promotion", "product"],
                condition=Q(target_type="PRODUCT"),
                name="promo_rule_unique_product_target",
            ),
            models.UniqueConstraint(
                fields=["promotion", "variant"],
                condition=Q(target_type="VARIANT"),
                name="promo_rule_unique_variant_target",
            ),
        ]

    def __str__(self) -> str:
        if self.target_type == self.TargetType.ALL:
            target = "All products"
        elif self.target_type == self.TargetType.CATEGORY:
            target = (
                self.category.name
                if self.category_id and self.category
                else "Category"
            )
        elif self.target_type == self.TargetType.SUBCATEGORY:
            target = (
                self.subcategory.name
                if self.subcategory_id and self.subcategory
                else "Subcategory"
            )
        elif self.target_type == self.TargetType.BRAND:
            target = self.brand or "Brand"
        elif self.target_type == self.TargetType.PRODUCT:
            target = (
                self.product.name
                if self.product_id and self.product
                else "Product"
            )
        elif self.target_type == self.TargetType.VARIANT:
            target = (
                self.variant.sku
                if self.variant_id and self.variant
                else "Variant"
            )
        else:
            target = "Unknown"

        return f"{self.promotion.name} → {target}"

    @staticmethod
    def normalize_brand(value: str | None) -> str | None:
        """
        Canonical normalization for case-insensitive brand matching.

        Example:
            '  Finolex  ' -> 'finolex'
        """
        if value is None:
            return None

        normalized = " ".join(value.strip().split()).casefold()

        return normalized or None

    def clean(self) -> None:
        super().clean()

        normalized_brand = self.normalize_brand(self.brand)

        if self.target_type == self.TargetType.ALL:
            if any(
                [
                    self.category_id,
                    self.subcategory_id,
                    self.product_id,
                    self.variant_id,
                    self.brand,
                ]
            ):
                raise ValidationError(
                    "ALL promotion rules cannot contain a specific target."
                )

        elif self.target_type == self.TargetType.CATEGORY:
            if not self.category_id:
                raise ValidationError(
                    {"category": "Category is required for CATEGORY rules."}
                )

        elif self.target_type == self.TargetType.SUBCATEGORY:
            if not self.subcategory_id:
                raise ValidationError(
                    {
                        "subcategory": (
                            "Subcategory is required for SUBCATEGORY rules."
                        )
                    }
                )

        elif self.target_type == self.TargetType.BRAND:
            if not normalized_brand:
                raise ValidationError(
                    {
                        "brand": (
                            "Brand is required for BRAND promotion rules."
                        )
                    }
                )

        elif self.target_type == self.TargetType.PRODUCT:
            if not self.product_id:
                raise ValidationError(
                    {"product": "Product is required for PRODUCT rules."}
                )

        elif self.target_type == self.TargetType.VARIANT:
            if not self.variant_id:
                raise ValidationError(
                    {"variant": "Variant is required for VARIANT rules."}
                )

        else:
            raise ValidationError(
                {"target_type": "Unsupported promotion target type."}
            )

        self.brand_normalized = (
            normalized_brand
            if self.target_type == self.TargetType.BRAND
            else None
        )
    def save(self, *args, **kwargs):
        """
        Keep brand_normalized synchronized on every persistence path.

        Django does not automatically call full_clean() before save(),
        so normalization must not depend exclusively on admin/form validation.
        """
        if self.target_type == self.TargetType.BRAND:
            self.brand_normalized = self.normalize_brand(self.brand)
        else:
            self.brand_normalized = None

        super().save(*args, **kwargs)