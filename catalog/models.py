import uuid
from django.db import models
from django.core.exceptions import ValidationError


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
    