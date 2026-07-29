from django.contrib import admin
from django.db.models import Count, Sum, Min, Max
from django.utils.html import format_html

from .models import (
    Category,
    SubCategory,
    Product,
    ProductOption,
    ProductOptionValue,
    ProductVariant,
    ProductVariantOption,
    ProductImage,
    ProductSpecification,
    QuoteRequest,
    QuoteAttachment,
)

from .models import ServiceablePincode
# ==========================================================
# CATEGORY / SUBCATEGORY
# ==========================================================

@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "group", "product_count", "featured", "active", "created_at")
    list_filter = ("group", "featured", "active")
    search_fields = ("name", "slug", "group")
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ("created_at", "updated_at")
    ordering = ("name",)
    list_per_page = 50

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            _product_count=Count("products", distinct=True)
        )

    def product_count(self, obj):
        return obj._product_count
    product_count.short_description = "Products"
    product_count.admin_order_field = "_product_count"


@admin.register(SubCategory)
class SubCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "category", "product_count", "active", "created_at")
    list_filter = ("category", "active")
    search_fields = ("name", "slug", "category__name")
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ("created_at",)
    ordering = ("category", "name")
    autocomplete_fields = ("category",)
    list_select_related = ("category",)
    list_per_page = 50

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            _product_count=Count("products", distinct=True)
        )

    def product_count(self, obj):
        return obj._product_count
    product_count.short_description = "Products"
    product_count.admin_order_field = "_product_count"


# ==========================================================
# INLINES — Product level
# ==========================================================

class ProductSpecificationInline(admin.TabularInline):
    model = ProductSpecification
    extra = 1
    fields = ("key", "value")


class ProductOptionInline(admin.TabularInline):
    model = ProductOption
    extra = 1
    fields = ("name", "display_type", "sort_order")
    show_change_link = True


class ProductVariantInline(admin.TabularInline):
    model = ProductVariant
    extra = 0
    fields = (
        "sku",
        "mrp",
        "selling_price",
        "stock",
        "minimum_order_quantity",
        "lead_time_days",
        "weight",
        "is_default",
        "active",
    )
    show_change_link = True


# ==========================================================
# INLINES — ProductOption level
# ==========================================================

class ProductOptionValueInline(admin.TabularInline):
    model = ProductOptionValue
    extra = 1

    def get_fields(self, request, obj=None):
        """
        obj here is the parent ProductOption instance.
        Dynamically show only the fields relevant to its display_type
        so a 'Size' option doesn't show hex_color/image, and a 'Color'
        option doesn't show an irrelevant image field, etc.
        """
        if obj is None:
            # Brand-new ProductOption — display_type not chosen/saved yet,
            # so show everything to be safe.
            return ["value", "hex_color", "image", "sort_order"]

        if obj.display_type == "color":
            return ["value", "hex_color", "sort_order"]

        if obj.display_type == "image":
            return ["value", "image", "sort_order"]

        # dropdown / buttons — plain text values only
        return ["value", "sort_order"]


# ==========================================================
# INLINES — ProductVariant level
# ==========================================================

class ProductImageInline(admin.TabularInline):
    model = ProductImage
    extra = 1
    fields = ("image", "image_preview", "featured", "sort_order")
    readonly_fields = ("image_preview",)

    def image_preview(self, obj):
        if obj and obj.image:
            return format_html(
                '<img src="{}" style="height:55px;border-radius:4px;object-fit:cover;" />',
                obj.image.url,
            )
        return "—"
    image_preview.short_description = "Preview"


class ProductVariantOptionInline(admin.TabularInline):
    model = ProductVariantOption
    extra = 1
    fields = ("option_value",)
    autocomplete_fields = ("option_value",)

    def get_formset(self, request, obj=None, **kwargs):
        """
        Restrict option_value choices to values belonging to this
        variant's own product — prevents mismatched assignments.
        """
        formset = super().get_formset(request, obj, **kwargs)
        if obj is not None:
            formset.form.base_fields["option_value"].queryset = (
                ProductOptionValue.objects
                .filter(option__product=obj.product)
                .select_related("option")
            )
        return formset


# ==========================================================
# PRODUCT
# ==========================================================

@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "category",
        "subcategory",
        "status",
        "variant_count",
        "total_stock",
        "price_range",
        "featured",
        "active",
        "created_at",
        "updated_at",
    )

    list_filter = (
        "status",
        "category",
        "subcategory",
        "featured",
        "active",
    )

    search_fields = (
        "name",
        "slug",
        "short_description",
        "category__name",
        "subcategory__name",
    )

    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ("created_at", "updated_at")
    ordering = ("-created_at",)

    autocomplete_fields = ("category", "subcategory")
    list_select_related = ("category", "subcategory")

    list_per_page = 50
    save_on_top = True

    inlines = [
        ProductSpecificationInline,
        ProductOptionInline,
        ProductVariantInline,
    ]

    fieldsets = (
        ("General", {
            "fields": ("name", "slug", "category", "subcategory", "status")
        }),
        ("Content", {
            "fields": ("short_description", "description"),
        }),
        ("Visibility", {
            "fields": ("featured", "active"),
        }),
        ("Timestamps", {
            "fields": ("created_at", "updated_at"),
            "classes": ("collapse",),
        }),
    )

    def get_queryset(self, request):
        # Single annotated query for count + stock + price range —
        # all pulled from the same "variants" relation, so no fan-out
        # duplication between the aggregates.
        return super().get_queryset(request).select_related(
            "category", "subcategory"
        ).annotate(
            _variant_count=Count("variants", distinct=True),
            _total_stock=Sum("variants__stock"),
            _min_price=Min("variants__selling_price"),
            _max_price=Max("variants__selling_price"),
        )

    def variant_count(self, obj):
        return obj._variant_count
    variant_count.short_description = "Variants"
    variant_count.admin_order_field = "_variant_count"

    def total_stock(self, obj):
        return obj._total_stock or 0
    total_stock.short_description = "Total Stock"
    total_stock.admin_order_field = "_total_stock"

    def price_range(self, obj):
        if obj._min_price is None:
            return "—"
        if obj._min_price == obj._max_price:
            return f"₹{obj._min_price}"
        return f"₹{obj._min_price} - ₹{obj._max_price}"
    price_range.short_description = "Price Range"
    price_range.admin_order_field = "_min_price"


# ==========================================================
# PRODUCT OPTION / VALUE
# ==========================================================

@admin.register(ProductOption)
class ProductOptionAdmin(admin.ModelAdmin):
    list_display = ("name", "product", "display_type", "sort_order")
    list_filter = ("display_type", "product__category")
    search_fields = ("name", "product__name")
    ordering = ("product", "sort_order")
    autocomplete_fields = ("product",)
    list_select_related = ("product",)
    inlines = [ProductOptionValueInline]
    list_per_page = 50


@admin.register(ProductOptionValue)
class ProductOptionValueAdmin(admin.ModelAdmin):
    list_display = ("value", "option", "swatch", "sort_order")
    list_filter = ("option__display_type", "option__product__category")
    search_fields = ("value", "option__name", "option__product__name")
    ordering = ("option", "sort_order")
    autocomplete_fields = ("option",)
    list_select_related = ("option", "option__product")
    list_per_page = 50

    def swatch(self, obj):
        if obj.hex_color:
            return format_html(
                '<span style="display:inline-block;width:22px;height:22px;'
                'border-radius:50%;border:1px solid #ccc;background:{};"></span>',
                obj.hex_color,
            )
        if obj.image:
            return format_html(
                '<img src="{}" style="height:28px;width:28px;border-radius:4px;object-fit:cover;" />',
                obj.image.url,
            )
        return "—"
    swatch.short_description = "Swatch"


# ==========================================================
# PRODUCT VARIANT / VARIANT OPTION
# ==========================================================

class StockLevelFilter(admin.SimpleListFilter):
    """
    Quick stock-health filter for warehouse/ops teams.
    """
    title = "stock level"
    parameter_name = "stock_level"

    def lookups(self, request, model_admin):
        return (
            ("out", "Out of stock (0)"),
            ("low", "Low stock (< 10)"),
            ("in", "In stock (>= 10)"),
        )

    def queryset(self, request, queryset):
        if self.value() == "out":
            return queryset.filter(stock=0)
        if self.value() == "low":
            return queryset.filter(stock__gt=0, stock__lt=10)
        if self.value() == "in":
            return queryset.filter(stock__gte=10)
        return queryset


@admin.register(ProductVariant)
class ProductVariantAdmin(admin.ModelAdmin):
    list_display = (
        "sku",
        "product",
        "variant_label",
        "mrp",
        "selling_price",
        "stock",
        "is_default",
        "active",
    )

    list_filter = ("active", "is_default", StockLevelFilter, "product__category")
    search_fields = ("sku", "barcode", "product__name")
    ordering = ("product", "id")

    autocomplete_fields = ("product",)
    list_select_related = ("product",)
    readonly_fields = ("display_name_display", "created_at", "updated_at")

    list_per_page = 50
    save_on_top = True

    inlines = [
        ProductImageInline,
        ProductVariantOptionInline,
    ]

    fieldsets = (
        ("Identity", {
            "fields": ("product", "sku", "barcode", "display_name_display", "is_default", "active")
        }),
        ("Pricing & Stock", {
            "fields": ("mrp", "selling_price", "stock", "minimum_order_quantity", "lead_time_days")
        }),
        ("Logistics", {
            "fields": ("weight",),
            "classes": ("collapse",),
        }),
        ("Timestamps", {
            "fields": ("created_at", "updated_at"),
            "classes": ("collapse",),
        }),
    )

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related("product")
            .prefetch_related("variant_options__option_value__option")
        )

    def variant_label(self, obj):
        return obj.display_name or "—"
    variant_label.short_description = "Variant"

    def display_name_display(self, obj):
        return obj.display_name or "—"
    display_name_display.short_description = "Display Name"


@admin.register(ProductVariantOption)
class ProductVariantOptionAdmin(admin.ModelAdmin):
    list_display = ("variant", "option_value")
    list_filter = ("option_value__option", "option_value__option__product__category")
    search_fields = ("variant__sku", "variant__product__name", "option_value__value")
    autocomplete_fields = ("variant", "option_value")
    list_select_related = ("variant", "option_value", "option_value__option")
    list_per_page = 50


# ==========================================================
# PRODUCT IMAGE
# ==========================================================

@admin.register(ProductImage)
class ProductImageAdmin(admin.ModelAdmin):
    list_display = ("variant", "image_preview", "featured", "sort_order")
    list_filter = ("featured", "variant__product__category")
    search_fields = ("variant__sku", "variant__product__name")
    ordering = ("variant", "sort_order")
    autocomplete_fields = ("variant",)
    list_select_related = ("variant", "variant__product")
    readonly_fields = ("image_preview",)
    list_per_page = 50

    def image_preview(self, obj):
        if obj.image:
            return format_html(
                '<img src="{}" style="height:55px;border-radius:4px;object-fit:cover;" />',
                obj.image.url,
            )
        return "—"
    image_preview.short_description = "Preview"


# ==========================================================
# PRODUCT SPECIFICATION
# ==========================================================

@admin.register(ProductSpecification)
class ProductSpecificationAdmin(admin.ModelAdmin):
    list_display = ("product", "key", "value")
    list_filter = ("product__category",)
    search_fields = ("product__name", "key", "value")
    autocomplete_fields = ("product",)
    list_select_related = ("product",)
    list_per_page = 50








@admin.register(ServiceablePincode)
class ServiceablePincodeAdmin(admin.ModelAdmin):
    list_display = (
        "pincode",
        "area_name",
        "city",
        "is_active",
    )

    search_fields = (
        "pincode",
        "area_name",
    )

    list_filter = (
        "city",
        "is_active",
    )

    ordering = ("pincode",)








    # ==========================================================
# REQUEST A QUOTE
# ==========================================================

class QuoteAttachmentInline(admin.TabularInline):
    model = QuoteAttachment
    extra = 0


@admin.register(QuoteRequest)
class QuoteRequestAdmin(admin.ModelAdmin):

    list_display = (
        "quote_id",
        "full_name",
        "phone",
        "project_type",
        "status",
        "created_at",
    )

    list_filter = (
        "status",
        "project_type",
        "created_at",
    )

    search_fields = (
        "quote_id",
        "full_name",
        "phone",
        "email",
    )

    readonly_fields = (
        "quote_id",
        "created_at",
        "updated_at",
    )

    ordering = ("-created_at",)

    list_per_page = 30

    inlines = [
        QuoteAttachmentInline,
    ]

    fieldsets = (
        (
            "Customer",
            {
                "fields": (
                    "quote_id",
                    "full_name",
                    "phone",
                    "email",
                    "company",
                )
            },
        ),
        (
            "Project",
            {
                "fields": (
                    "project_location",
                    "delivery_pincode",
                    "project_type",
                    "materials",
                    "requirements",
                )
            },
        ),
        (
            "Status",
            {
                "fields": (
                    "status",
                )
            },
        ),
        (
            "Timestamps",
            {
                "fields": (
                    "created_at",
                    "updated_at",
                ),
                "classes": ("collapse",),
            },
        ),
    )