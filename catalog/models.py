import uuid
from django.db import models
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator


# ==========================================================
# CATEGORY
# ==========================================================

class Category(models.Model):
    name = models.CharField(max_length=120)
    slug = models.SlugField(unique=True)

    group = models.CharField(
        max_length=100,
        blank=True,
        help_text="Example: Building Materials, Interior, Electrical",
    )

    description = models.TextField(blank=True)

    image = models.ImageField(
        upload_to="categories/",
        blank=True,
        null=True,
    )
    icon = models.CharField(
        max_length=100,
        blank=True,
    )
    sort_order = models.PositiveIntegerField(default=0)
    featured = models.BooleanField(default=False)
    active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Category"
        verbose_name_plural = "Categories"

    def __str__(self):
        return self.name


# ==========================================================
# SUB CATEGORY
# ==========================================================

class SubCategory(models.Model):
    category = models.ForeignKey(
        Category,
        on_delete=models.CASCADE,
        related_name="subcategories",
    )

    name = models.CharField(max_length=120)
    slug = models.SlugField()

    description = models.TextField(blank=True)

    image = models.ImageField(
        upload_to="subcategories/",
        blank=True,
        null=True,
    )
    featured = models.BooleanField(default=False)

    sort_order = models.PositiveIntegerField(default=0)
    active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "name"]
        unique_together = ("category", "slug")
        verbose_name = "Sub Category"
        verbose_name_plural = "Sub Categories"

    def __str__(self):
        return f"{self.category.name} → {self.name}"


# ==========================================================
# HOMEPAGE CATEGORY
# ==========================================================

class HomepageCategory(models.Model):
    category = models.OneToOneField(
        Category,
        on_delete=models.CASCADE,
        related_name="homepage_config",
    )

    is_active = models.BooleanField(default=True)

    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "id"]
        verbose_name = "Homepage Category"
        verbose_name_plural = "Homepage Categories"

    def __str__(self):
        return f"{self.sort_order} · {self.category.name}"


# ==========================================================
# PRODUCT
# ==========================================================

class Product(models.Model):

    STATUS_CHOICES = [
        ("draft", "Draft"),
        ("published", "Published"),
        ("hidden", "Hidden"),
    ]

    category = models.ForeignKey(
        Category,
        on_delete=models.CASCADE,
        related_name="products",
    )

    subcategory = models.ForeignKey(
        SubCategory,
        on_delete=models.CASCADE,
        related_name="products",
    )

    name = models.CharField(max_length=255)
    brand = models.CharField(
        max_length=120,
        blank=True,
        default="",
        db_index=True,
    )

    slug = models.SlugField(unique=True)

    short_description = models.CharField(
        max_length=300,
        blank=True,
    )

    description = models.TextField(blank=True)

    featured = models.BooleanField(default=False)

    active = models.BooleanField(default=True)

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="draft",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Product"
        verbose_name_plural = "Products"

    def __str__(self):
        return self.name


# ==========================================================
# PRODUCT OPTION  (e.g. "Size", "Finish", "Voltage")
# ==========================================================

class ProductOption(models.Model):
    """
    Defines a variant-defining attribute for a specific product.
    Fully dynamic — no hardcoded 'size'/'finish'/'color' fields anywhere.
    """

    DISPLAY_TYPE_CHOICES = [
        ("dropdown", "Dropdown"),
        ("buttons", "Buttons"),
        ("color", "Color"),
        ("image", "Image"),
    ]

    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="options",
    )

    name = models.CharField(
        max_length=100,
        help_text="Example: Size, Finish, Voltage, Pack Size",
    )

    display_type = models.CharField(
        max_length=20,
        choices=DISPLAY_TYPE_CHOICES,
        default="dropdown",
    )

    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "id"]
        verbose_name = "Product Option"
        verbose_name_plural = "Product Options"
        constraints = [
            models.UniqueConstraint(
                fields=["product", "name"],
                name="uniq_product_option_name",
            ),
        ]
        indexes = [
            models.Index(fields=["product", "sort_order"]),
        ]

    def __str__(self):
        return f"{self.product.name} · {self.name}"


# ==========================================================
# PRODUCT OPTION VALUE  (e.g. "15mm", "20mm", "GD", "MB")
# ==========================================================

class ProductOptionValue(models.Model):
    """
    A concrete value belonging to a ProductOption.
    """

    option = models.ForeignKey(
        ProductOption,
        on_delete=models.CASCADE,
        related_name="values",
    )

    value = models.CharField(
        max_length=120,
        help_text="Example: 15mm, GD, RGD, 220V",
    )

    hex_color = models.CharField(
        max_length=7,
        blank=True,
        help_text="Example: #FFFFFF (used when the parent option's display_type is 'color')",
    )

    image = models.ImageField(
        upload_to="option_values/",
        blank=True,
        null=True,
        help_text="Swatch/thumbnail image (used when the parent option's display_type is 'image')",
    )

    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "id"]
        verbose_name = "Product Option Value"
        verbose_name_plural = "Product Option Values"
        constraints = [
            models.UniqueConstraint(
                fields=["option", "value"],
                name="uniq_option_value_per_option",
            ),
        ]
        indexes = [
            models.Index(fields=["option", "sort_order"]),
        ]

    def __str__(self):
        return f"{self.option.name}: {self.value}"


# ==========================================================
# PRODUCT VARIANT  (pure commerce record — SKU / price / stock only)
# ==========================================================

class ProductVariant(models.Model):
    """
    A sellable unit of a Product. Carries NO descriptive attributes —
    those live entirely in ProductOptionValue via ProductVariantOption.
    The display name (e.g. "15mm / GD") is generated dynamically.
    """

    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="variants",
    )

    sku = models.CharField(
        max_length=120,
        unique=True,
    )

    barcode = models.CharField(
        max_length=120,
        blank=True,
    )

    mrp = models.DecimalField(
        max_digits=10,
        decimal_places=2,
    )

    selling_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
    )

    stock = models.PositiveIntegerField(default=0)

    weight = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
    )

    minimum_order_quantity = models.PositiveIntegerField(default=1)

    lead_time_days = models.PositiveIntegerField(default=1)

    is_default = models.BooleanField(default=False)

    active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["id"]
        verbose_name = "Product Variant"
        verbose_name_plural = "Product Variants"
        indexes = [
            models.Index(fields=["product", "active"]),
        ]

    def __str__(self):
        label = self.display_name
        return f"{self.product.name} ({label})" if label else f"{self.product.name} [{self.sku}]"

    @property
    def display_name(self):
        """
        Dynamically builds e.g. '15mm / GD' from mapped option values,
        ordered by the parent ProductOption.sort_order.
        """
        values = (
            self.variant_options
            .select_related("option_value", "option_value__option")
            .order_by("option_value__option__sort_order", "option_value__sort_order")
        )
        return " / ".join(vo.option_value.value for vo in values)


# ==========================================================
# PRODUCT VARIANT OPTION  (maps a Variant → its selected OptionValues)
# ==========================================================

class ProductVariantOption(models.Model):
    """
    Join table: pins a specific ProductOptionValue onto a ProductVariant.
    One row per (variant, option) pair — e.g. Variant → Size:15mm,
    Variant → Finish:GD.
    """

    variant = models.ForeignKey(
        ProductVariant,
        on_delete=models.CASCADE,
        related_name="variant_options",
    )

    option_value = models.ForeignKey(
        ProductOptionValue,
        on_delete=models.CASCADE,
        related_name="variant_options",
    )

    class Meta:
        ordering = ["id"]
        verbose_name = "Product Variant Option"
        verbose_name_plural = "Product Variant Options"
        constraints = [
            # No duplicate mapping of the same option value to the same variant
            models.UniqueConstraint(
                fields=["variant", "option_value"],
                name="uniq_variant_option_value",
            ),
        ]
        indexes = [
            models.Index(fields=["variant"]),
            models.Index(fields=["option_value"]),
        ]

    def __str__(self):
        return f"{self.variant} → {self.option_value}"

    def clean(self):
        """
        Enforces: a variant can only have ONE value per ProductOption
        (e.g. can't have both Size:15mm AND Size:20mm on the same variant).
        Also enforces the option_value's option belongs to the same product
        as the variant.
        """
        if self.variant_id and self.option_value_id:
            same_product = (
                self.option_value.option.product_id == self.variant.product_id
            )
            if not same_product:
                raise ValidationError(
                    "This option value does not belong to the variant's product."
                )

            conflict = (
                ProductVariantOption.objects
                .filter(
                    variant=self.variant,
                    option_value__option=self.option_value.option,
                )
                .exclude(pk=self.pk)
                .exists()
            )
            if conflict:
                raise ValidationError(
                    f"Variant already has a value assigned for option "
                    f"'{self.option_value.option.name}'."
                )


# ==========================================================
# PRODUCT IMAGES
# ==========================================================

class ProductImage(models.Model):

    variant = models.ForeignKey(
        ProductVariant,
        on_delete=models.CASCADE,
        related_name="images",
    )

    image = models.ImageField(
        upload_to="products/",
    )

    featured = models.BooleanField(default=False)

    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order"]
        verbose_name = "Product Image"
        verbose_name_plural = "Product Images"

    def __str__(self):
        return f"{self.variant.product.name} - {self.variant.display_name or self.variant.sku}"


# ==========================================================
# PRODUCT SPECIFICATIONS
# ==========================================================

class ProductSpecification(models.Model):

    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="specifications",
    )

    key = models.CharField(max_length=120)

    value = models.TextField()

    class Meta:
        ordering = ["id"]
        verbose_name = "Product Specification"
        verbose_name_plural = "Product Specifications"

    def __str__(self):
        return f"{self.key}: {self.value}"


class ServiceablePincode(models.Model):
    pincode = models.CharField(max_length=6, unique=True)
    area_name = models.CharField(max_length=100)
    city = models.CharField(max_length=100, default="Kolkata")
    state = models.CharField(max_length=100, default="West Bengal")
    is_active = models.BooleanField(default=True)

    zone = models.ForeignKey(
        "DeliveryZone",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pincodes",
        help_text="Optional — assign for zone-based delivery rules. Leave blank to keep legacy Kolkata-only behavior.",
    )

    class Meta:
        ordering = ["pincode"]
        verbose_name = "Serviceable Pincode"
        verbose_name_plural = "Serviceable Pincodes"

    def __str__(self):
        return f"{self.pincode} - {self.area_name}"


# ==========================================================
# REQUEST A QUOTE
# ==========================================================

class QuoteRequest(models.Model):

    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("reviewing", "Reviewing"),
        ("quoted", "Quoted"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
    ]

    quote_id = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        unique=True,
    )

    full_name = models.CharField(max_length=150)

    phone = models.CharField(max_length=20)

    email = models.EmailField()

    company = models.CharField(
        max_length=200,
        blank=True,
    )

    project_location = models.CharField(max_length=255)

    delivery_pincode = models.CharField(max_length=6)

    project_type = models.CharField(max_length=80)

    materials = models.JSONField(default=list)

    requirements = models.TextField(blank=True)

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="pending",
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Quote Request"
        verbose_name_plural = "Quote Requests"

    def __str__(self):
        return f"{self.full_name} ({self.quote_id})"


# ==========================================================
# QUOTE ATTACHMENTS
# ==========================================================

class QuoteAttachment(models.Model):

    quote = models.ForeignKey(
        QuoteRequest,
        on_delete=models.CASCADE,
        related_name="attachments",
    )

    file = models.FileField(
        upload_to="quote_requests/",
    )

    uploaded_at = models.DateTimeField(
        auto_now_add=True,
    )

    class Meta:
        ordering = ["id"]
        verbose_name = "Quote Attachment"
        verbose_name_plural = "Quote Attachments"

    def __str__(self):
        return f"{self.quote.full_name} - Attachment"


# ==========================================================
# DELIVERY ENGINE — ZONES
# ==========================================================

class DeliveryZone(models.Model):
    name = models.CharField(max_length=100, unique=True)
    code = models.SlugField(max_length=50, unique=True)
    active = models.BooleanField(default=True)
    priority = models.PositiveIntegerField(
        default=0,
        help_text=(
            "Higher number = higher priority. Currently NOT consumed by "
            "the evaluator (each pincode maps to exactly one zone via a "
            "ForeignKey, so there is no ambiguity to break). Reserved for "
            "future overlapping-zone support."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-priority", "name"]
        verbose_name = "Delivery Zone"
        verbose_name_plural = "Delivery Zones"

    def __str__(self):
        return self.name


# ==========================================================
# DELIVERY ENGINE — RULES
# ==========================================================

class DeliveryRule(models.Model):
    """
    Defines WHO/WHAT this rule applies to (targeting) and HOW its charge
    combines with other matched rules (combine_mode/stop_after).

    Pricing itself lives entirely in DeliveryRuleAction — this model is
    NOT a pricing source. A rule with no actions contributes ₹0.

    Targeting fields = identity/scope (which cart items this rule considers).
    Runtime facts (cart value, weight, quantity, customer type, shipping
    type) belong on DeliveryRuleCondition instead — see that model's
    docstring. Date/time validity is a rule attribute (starts_at/ends_at),
    not a condition, since every rule needs at most one validity window.
    """

    COMBINE_ADD = "add"
    COMBINE_OVERRIDE = "override"

    COMBINE_CHOICES = [
        (COMBINE_ADD, "Add — stack on top of the running total so far"),
        (COMBINE_OVERRIDE, "Override — discard the running total, restart from this rule"),
    ]

    name = models.CharField(max_length=150)
    code = models.SlugField(max_length=120, unique=True)
    active = models.BooleanField(default=True)

    priority = models.IntegerField(
        default=0,
        db_index=True,
        help_text=(
            "Tie-breaker within the same specificity level. Rules are "
            "evaluated from broadest to most specific, and within equal "
            "specificity, from lowest to highest priority — so a higher "
            "priority rule is evaluated later and takes precedence."
        ),
    )

    # ---- Targeting / scope (identity — all optional, combinable via AND) ----
    zone = models.ForeignKey(
        DeliveryZone, on_delete=models.CASCADE,
        null=True, blank=True, related_name="rules",
    )
    category = models.ForeignKey(
        Category, on_delete=models.CASCADE,
        null=True, blank=True, related_name="delivery_rules",
    )
    subcategory = models.ForeignKey(
        SubCategory, on_delete=models.CASCADE,
        null=True, blank=True, related_name="delivery_rules",
    )
    product = models.ForeignKey(
        Product, on_delete=models.CASCADE,
        null=True, blank=True, related_name="delivery_rules",
    )
    variant = models.ForeignKey(
        ProductVariant, on_delete=models.CASCADE,
        null=True, blank=True, related_name="delivery_rules",
    )

    # ---- Combination behavior (HOW this rule's actions combine) ----
    combine_mode = models.CharField(max_length=20, choices=COMBINE_CHOICES, default=COMBINE_ADD)
    stop_after = models.BooleanField(
        default=False,
        help_text="Stop evaluating any further (more specific / higher-priority) rules once this one is applied.",
    )

    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-priority", "id"]
        verbose_name = "Delivery Rule"
        verbose_name_plural = "Delivery Rules"
        indexes = [
            models.Index(fields=["active", "priority"]),
            models.Index(fields=["zone", "category", "subcategory", "product", "variant"]),
        ]

    def __str__(self):
        return f"{self.name} (priority {self.priority})"

    def clean(self):
        """
        Enforces that targeting FKs form a consistent hierarchy — prevents
        admin from accidentally saving impossible combinations like
        category=Building Materials + product=Fan (an electrical item).
        Also enforces starts_at <= ends_at.
        """
        errors = {}

        if self.subcategory_id and self.category_id and self.subcategory.category_id != self.category_id:
            errors["subcategory"] = (
                f"Subcategory '{self.subcategory.name}' belongs to category "
                f"'{self.subcategory.category.name}', not the selected category."
            )

        if self.product_id:
            if self.category_id and self.product.category_id != self.category_id:
                errors["product"] = (
                    f"Product '{self.product.name}' belongs to category "
                    f"'{self.product.category.name}', not the selected category."
                )
            if self.subcategory_id and self.product.subcategory_id != self.subcategory_id:
                errors["product"] = (
                    f"Product '{self.product.name}' belongs to subcategory "
                    f"'{self.product.subcategory.name}', not the selected subcategory."
                )

        if self.variant_id and self.product_id and self.variant.product_id != self.product_id:
            errors["variant"] = f"Variant '{self.variant.sku}' does not belong to the selected product."

        if self.starts_at and self.ends_at and self.starts_at > self.ends_at:
            errors["ends_at"] = "End date must be after start date."

        if errors:
            raise ValidationError(errors)

    @property
    def specificity(self):
        """Higher = more specific targeting. Used as the primary sort key."""
        return sum([
            self.variant_id is not None,
            self.product_id is not None,
            self.subcategory_id is not None,
            self.category_id is not None,
            self.zone_id is not None,
        ])


class DeliveryRuleCondition(models.Model):

    FIELD_CART_VALUE = "cart_value"
    FIELD_WEIGHT = "weight"
    FIELD_QUANTITY = "quantity"
    FIELD_TOTAL_QUANTITY = "total_quantity"
    FIELD_CUSTOMER_TYPE = "customer_type"
    FIELD_SHIPPING_TYPE = "shipping_type"

    FIELD_CHOICES = [
        (FIELD_CART_VALUE, "Cart Value"),
        (FIELD_WEIGHT, "Total Weight (kg)"),
        (FIELD_QUANTITY, "Item Quantity"),
        (FIELD_TOTAL_QUANTITY, "Total Cart Quantity"),
        (FIELD_CUSTOMER_TYPE, "Customer Type"),
        (FIELD_SHIPPING_TYPE, "Shipping Type"),
    ]

    OP_GT = "gt"
    OP_GTE = "gte"
    OP_LT = "lt"
    OP_LTE = "lte"
    OP_EQ = "eq"
    OP_IN = "in"

    OPERATOR_CHOICES = [
        (OP_GT, ">"), (OP_GTE, ">="), (OP_LT, "<"),
        (OP_LTE, "<="), (OP_EQ, "="), (OP_IN, "in"),
    ]

    rule = models.ForeignKey(DeliveryRule, on_delete=models.CASCADE, related_name="conditions")
    field = models.CharField(max_length=50, choices=FIELD_CHOICES)
    operator = models.CharField(max_length=10, choices=OPERATOR_CHOICES)
    value = models.CharField(
        max_length=255,
        help_text="Stored as string; cast to correct type by the evaluator at runtime.",
    )
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "id"]
        verbose_name = "Delivery Rule Condition"
        verbose_name_plural = "Delivery Rule Conditions"

    def __str__(self):
        return f"{self.rule.name}: {self.field} {self.operator} {self.value}"


class DeliveryRuleAction(models.Model):
    """
    The single authoritative source of pricing. A DeliveryRule has zero
    or more actions; each action independently defines its own charge
    calculation (pricing_mode + amount). A rule with no active actions
    contributes ₹0 to the delivery total.
    """

    ACTION_BASE_CHARGE = "base_charge"
    ACTION_SURCHARGE = "surcharge"
    ACTION_DISCOUNT = "discount"
    ACTION_FREE_DELIVERY = "free_delivery"

    ACTION_CHOICES = [
        (ACTION_BASE_CHARGE, "Base Charge"),
        (ACTION_SURCHARGE, "Surcharge"),
        (ACTION_DISCOUNT, "Discount"),
        (ACTION_FREE_DELIVERY, "Free Delivery"),
    ]

    PRICING_FIXED = "fixed"
    PRICING_PER_ITEM = "per_item"
    PRICING_PER_UNIT = "per_unit"
    PRICING_PER_KG = "per_kg"
    PRICING_PERCENTAGE = "percentage"

    PRICING_CHOICES = [
        (PRICING_FIXED, "Fixed Amount"),
        (PRICING_PER_ITEM, "Per Item"),
        (PRICING_PER_UNIT, "Per Unit"),
        (PRICING_PER_KG, "Per Kg"),
        (PRICING_PERCENTAGE, "Percentage (of this rule's scope subtotal)"),
    ]

    rule = models.ForeignKey(DeliveryRule, on_delete=models.CASCADE, related_name="actions")
    action_type = models.CharField(max_length=30, choices=ACTION_CHOICES)
    pricing_mode = models.CharField(
        max_length=20, choices=PRICING_CHOICES, default=PRICING_FIXED,
        help_text="Ignored when action_type = Free Delivery.",
    )
    amount = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
        validators=[MinValueValidator(0)],
        help_text="Amount, or percent value (e.g. 5 = 5%) when pricing_mode = Percentage. Must be non-negative — sign is inferred from action_type.",
    )
    label = models.CharField(
        max_length=120, blank=True,
        help_text="Customer-facing breakdown label, e.g. 'Heavy Shipment Fee'.",
    )
    metadata = models.JSONField(default=dict, blank=True)
    sort_order = models.PositiveIntegerField(default=0)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["sort_order", "id"]
        verbose_name = "Delivery Rule Action"
        verbose_name_plural = "Delivery Rule Actions"

    def __str__(self):
        return f"{self.rule.name} → {self.action_type} ({self.amount})"

    def clean(self):
        if self.amount is not None and self.amount < 0:
            raise ValidationError({"amount": "Amount must be non-negative. Sign is inferred from action_type."})

        if self.rule_id and self.action_type == DeliveryRuleAction.ACTION_DISCOUNT:
            if self.rule.combine_mode == DeliveryRule.COMBINE_OVERRIDE:
                raise ValidationError(
                    "A Discount action cannot be added to a rule with combine_mode=Override. "
                    "Discount only makes sense with combine_mode=Add (it subtracts from the running total)."
                )