from django.contrib import admin
from django.db.models import Count, Sum, Min, Max
from django.utils.html import format_html
import csv
import io

from django.contrib import messages
from django.db import transaction
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.urls import path

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
from .models import DeliveryZone, DeliveryRule, DeliveryRuleCondition, DeliveryRuleAction
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
        "state",
         "zone",
        "is_active",
    )

    search_fields = (
        "pincode",
        "area_name",
        "city",
        "state",
    )

    list_filter = (
        "city",
        "state",
        "zone",
        "is_active",
    )
    autocomplete_fields = ("zone",)
    ordering = ("pincode",)
    list_per_page = 100

    change_list_template = "admin/serviceable_pincode/change_list.html"

    def get_urls(self):
        urls = super().get_urls()

        custom_urls = [
            path(
                "import-csv/",
                self.admin_site.admin_view(self.import_csv_view),
                name="serviceablepincode_import_csv",
            ),
        ]

        return custom_urls + urls

    def import_csv_view(self, request):
        if request.method == "POST":
            uploaded_file = request.FILES.get("csv_file")

            if not uploaded_file:
                messages.error(request, "Please select a CSV file.")
                return HttpResponseRedirect(request.path)

            if not uploaded_file.name.lower().endswith(".csv"):
                messages.error(request, "Only CSV files are supported.")
                return HttpResponseRedirect(request.path)

            try:
                raw = uploaded_file.read().decode("utf-8-sig")
            except UnicodeDecodeError:
                messages.error(
                    request,
                    "CSV must be UTF-8 encoded. Please save the file as UTF-8 CSV.",
                )
                return HttpResponseRedirect(request.path)

            reader = csv.DictReader(io.StringIO(raw))

            required_columns = {
                "pincode",
                "area_name",
                "city",
                "state",
                "is_active",
            }

            headers = {
                (header or "").strip().lower()
                for header in (reader.fieldnames or [])
            }

            missing_columns = required_columns - headers

            if missing_columns:
                messages.error(
                    request,
                    "Missing columns: "
                    + ", ".join(sorted(missing_columns)),
                )
                return HttpResponseRedirect(request.path)

            # "zone" is optional — if the column is absent, every row is
            # simply imported without a zone assignment (legacy behavior).
            has_zone_column = "zone" in headers

            zone_lookup = {
                (z.code or "").strip().lower(): z
                for z in DeliveryZone.objects.all()
            }

            rows = []
            seen_pincodes = set()
            invalid_rows = []
            duplicate_rows = []

            for line_number, row in enumerate(reader, start=2):
                pincode = (row.get("pincode") or "").strip()
                area_name = (row.get("area_name") or "").strip()
                city = (row.get("city") or "").strip()
                state = (row.get("state") or "").strip()
                is_active_raw = (row.get("is_active") or "").strip().lower()

                if not pincode.isdigit() or len(pincode) != 6:
                    invalid_rows.append(
                        f"Row {line_number}: invalid pincode '{pincode}'."
                    )
                    continue

                if not area_name:
                    invalid_rows.append(
                        f"Row {line_number}: area_name is required."
                    )
                    continue

                if not city:
                    invalid_rows.append(
                        f"Row {line_number}: city is required."
                    )
                    continue

                if not state:
                    invalid_rows.append(
                        f"Row {line_number}: state is required."
                    )
                    continue

                zone_obj = None
                if has_zone_column:
                    zone_raw = (row.get("zone") or "").strip()
                    if zone_raw:
                        zone_obj = zone_lookup.get(zone_raw.lower())
                        if zone_obj is None:
                            invalid_rows.append(
                                f"Row {line_number}: unknown zone code '{zone_raw}'."
                            )
                            continue

                if is_active_raw in {"true", "1", "yes", "y"}:
                    is_active = True
                elif is_active_raw in {"false", "0", "no", "n"}:
                    is_active = False
                else:
                    invalid_rows.append(
                        f"Row {line_number}: invalid is_active value '{is_active_raw}'."
                    )
                    continue

                if pincode in seen_pincodes:
                    duplicate_rows.append(
                        f"Row {line_number}: duplicate pincode {pincode}."
                    )
                    continue

                seen_pincodes.add(pincode)

                rows.append(
                    {
                        "pincode": pincode,
                        "area_name": area_name,
                        "city": city,
                        "state": state,
                        "is_active": is_active,
                        "zone": zone_obj,
                    }
                )

            existing = {
                obj.pincode: obj
                for obj in ServiceablePincode.objects.filter(
                    pincode__in=seen_pincodes
                )
            }

            to_create = []
            to_update = []

            for row in rows:
                existing_obj = existing.get(row["pincode"])

                if existing_obj:
                    existing_obj.area_name = row["area_name"]
                    existing_obj.city = row["city"]
                    existing_obj.state = row["state"]
                    existing_obj.is_active = row["is_active"]
                    if has_zone_column:
                        existing_obj.zone = row["zone"]
                    to_update.append(existing_obj)
                else:
                    to_create.append(
                        ServiceablePincode(**row)
                    )

            update_fields = ["area_name", "city", "state", "is_active"]
            if has_zone_column:
                update_fields.append("zone")

            try:
                with transaction.atomic():
                    if to_create:
                        ServiceablePincode.objects.bulk_create(
                            to_create,
                            batch_size=500,
                        )

                    if to_update:
                        ServiceablePincode.objects.bulk_update(
                            to_update,
                            update_fields,
                            batch_size=500,
                        )

            except Exception as exc:
                messages.error(
                    request,
                    f"Import failed: {exc}",
                )
                return HttpResponseRedirect(request.path)

            messages.success(
                request,
                (
                    f"Import complete — "
                    f"{len(to_create)} created, "
                    f"{len(to_update)} updated, "
                    f"{len(duplicate_rows)} duplicate rows skipped, "
                    f"{len(invalid_rows)} invalid rows skipped."
                ),
            )

            return HttpResponseRedirect(
                "../"
            )

        context = {
            **self.admin_site.each_context(request),
            "title": "Import Serviceable Pincodes",
        }

        return render(
            request,
            "admin/serviceable_pincode/import.html",
            context,
        )





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

# ==========================================================
# DELIVERY ENGINE
# ==========================================================

class DeliveryRuleConditionInline(admin.TabularInline):
    model = DeliveryRuleCondition
    extra = 1
    fields = ("field", "operator", "value", "sort_order")


class DeliveryRuleActionInline(admin.TabularInline):
    model = DeliveryRuleAction
    extra = 1
    fields = ("action_type", "pricing_mode", "amount", "label", "active", "sort_order")


@admin.register(DeliveryZone)
class DeliveryZoneAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "active", "priority")
    list_filter = ("active",)
    search_fields = ("name", "code")
    prepopulated_fields = {"code": ("name",)}
    ordering = ("-priority", "name")
    list_per_page = 50

@admin.register(DeliveryRule)
class DeliveryRuleAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "zone",
        "category",
        "subcategory",
        "product",
        "variant",
        "combine_mode",
        "stop_after",
        "priority",
        "active",
    )

    list_filter = (
        "active",
        "combine_mode",
        "zone",
    )

    search_fields = ("name", "code")
    prepopulated_fields = {"code": ("name",)}
    ordering = ("-priority", "name")

    autocomplete_fields = ("category", "subcategory", "product", "variant")
    list_select_related = ("zone", "category", "subcategory", "product", "variant")

    inlines = [
        DeliveryRuleConditionInline,
        DeliveryRuleActionInline,
    ]

    fieldsets = (
        ("Identity", {
            "fields": ("name", "code", "active", "priority"),
        }),
        ("Targeting (leave blank = wildcard/global)", {
            "fields": ("zone", "category", "subcategory", "product", "variant"),
        }),
        ("Combination behavior", {
            "fields": ("combine_mode", "stop_after"),
        }),
        ("Validity Window", {
            "fields": ("starts_at", "ends_at"),
            "classes": ("collapse",),
        }),
    )

    list_per_page = 50
    save_on_top = True