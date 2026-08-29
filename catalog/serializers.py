# ==========================================================
# catalog/serializers.py
# ==========================================================

from decimal import Decimal, InvalidOperation

from cloudinary.utils import cloudinary_url
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from rest_framework import serializers

from .models import (
    Category,
    HomepageCategory,
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
    DeliveryZone,
    ServiceablePincode,
    DeliveryRule,
    DeliveryRuleCondition,
    DeliveryRuleAction,
)
from .delivery.validators import normalize_pincode
from .delivery.rule_scope import compute_rule_status, resolve_rule_scope

def _normalize(text):
    return (text or "").strip().lower()


# ==========================================================
# CATEGORY
# ==========================================================

class CategorySerializer(serializers.ModelSerializer):

    class Meta:
        model = Category
        fields = "__all__"
        
class CategoryMiniSerializer(serializers.ModelSerializer):

    class Meta:
        model = Category
        fields = [
            "id",
            "name",
            "slug",
        ]
        
class HomepageCategorySerializer(serializers.ModelSerializer):
    category = CategoryMiniSerializer(read_only=True)

    category_id = serializers.PrimaryKeyRelatedField(
        source="category",
        queryset=Category.objects.all(),
        write_only=True,
    )

    class Meta:
        model = HomepageCategory
        fields = [
            "id",
            "category",
            "category_id",
            "is_active",
            "sort_order",
        ]



# ==========================================================
# SUB CATEGORY
# ==========================================================

class SubCategorySerializer(serializers.ModelSerializer):

    category_name = serializers.CharField(
        source="category.name",
        read_only=True,
    )

    product_count = serializers.SerializerMethodField()

    class Meta:
        model = SubCategory
        fields = [
            "id",
            "category",
            "category_name",
            "name",
            "slug",
            "description",
            "image",
             "featured",
            "active",
            "sort_order",
            "created_at",
            "updated_at",
            "product_count",
        ]
        read_only_fields = [
            "id",
            "category_name",
            "created_at",
            "updated_at",
            "product_count",
        ]

    def get_product_count(self, obj):
        return obj.products.count()


class SubCategoryMiniSerializer(serializers.ModelSerializer):

    class Meta:
        model = SubCategory
        fields = [
            "id",
            "name",
            "slug",
        ]


# ==========================================================
# PRODUCT OPTION VALUE
# ==========================================================

class ProductOptionValueSerializer(serializers.ModelSerializer):

    id = serializers.IntegerField(required=False)

    class Meta:
        model = ProductOptionValue
        fields = [
            "id",
            "value",
            "hex_color",
            "image",
            "sort_order",
        ]


# ==========================================================
# PRODUCT OPTION
# ==========================================================

class ProductOptionSerializer(serializers.ModelSerializer):

    id = serializers.IntegerField(required=False)

    values = ProductOptionValueSerializer(
        many=True,
        required=False,
    )

    class Meta:
        model = ProductOption
        fields = [
            "id",
            "name",
            "display_type",
            "sort_order",
            "values",
        ]


# ==========================================================
# PRODUCT VARIANT OPTION
#
# This serializer is used ONLY as a write-only nested field
# ("option_values", source="variant_options") on
# ProductVariantSerializer. It is never used to produce output
# (the read-side "selections" shape is built separately via
# ProductVariantSerializer.get_selections()), so every field here
# is a write-side input field.
#
# It now accepts BOTH payload shapes:
#
#   New / current frontend contract:
#     { "option": "Finish", "value": "GD", "hex_color": null, "image": null }
#
#   Legacy contract (still supported):
#     { "option_name_ref": "Finish", "value_ref": "GD" }
#
#   Direct reference (still supported):
#     { "option_value_id": 123 }
# ==========================================================

class ProductVariantOptionSerializer(serializers.ModelSerializer):

    id = serializers.IntegerField(required=False)

    # ---- direct reference (unchanged) ----
    option_value_id = serializers.PrimaryKeyRelatedField(
        source="option_value",
        queryset=ProductOptionValue.objects.all(),
        write_only=True,
        required=False,
    )

    # ---- legacy resolve-by-name fields (unchanged) ----
    option_name_ref = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True,
    )
    value_ref = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True,
    )

    # ---- current frontend contract fields ----
    option = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True,
    )
    value = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True,
    )
    hex_color = serializers.CharField(
        write_only=True,
        required=False,
        allow_null=True,
        allow_blank=True,
    )
    image = serializers.CharField(
        write_only=True,
        required=False,
        allow_null=True,
        allow_blank=True,
    )

    class Meta:
        model = ProductVariantOption
        fields = [
            "id",
            "option_value_id",
            "option_name_ref",
            "value_ref",
            "option",
            "value",
            "hex_color",
            "image",
        ]


# ==========================================================
# PRODUCT IMAGE
# ==========================================================

class ProductImageSerializer(serializers.ModelSerializer):

    id = serializers.IntegerField(required=False)

    variant = serializers.PrimaryKeyRelatedField(
        queryset=ProductVariant.objects.all()
    )

    image_url = serializers.SerializerMethodField()
    image_url_large = serializers.SerializerMethodField()

    class Meta:
        model = ProductImage
        fields = [
            "id",
            "variant",
            "image",
            "image_url",
            "image_url_large",
            "featured",
            "sort_order",
        ]

    def get_image_url(self, obj):
        if not obj.image:
            return None

        try:
            url, _ = cloudinary_url(
                obj.image.name,
                width=600,
                quality="auto",
                fetch_format="auto",
            )
            return url
        except Exception:
            return obj.image.url

    def get_image_url_large(self, obj):
        if not obj.image:
            return None

        try:
            url, _ = cloudinary_url(
                obj.image.name,
                width=1920,
                quality="auto:best",
                fetch_format="auto",
            )
            return url
        except Exception:
            return obj.image.url


# ==========================================================
# PRODUCT VARIANT
# Reshaped to match the frontend variant contract:
#   { id, sku, selections, mrp, selling_price, currency, stock,
#     in_stock, gst_included, gst_rate, estimated_dispatch_days, images }
#
# `option_values` stays as the write-only nested input (same source
# mapping as before -> validated_data["variant_options"]) so
# `_sync_variants` / `_sync_variant_option_values` are untouched.
# ==========================================================

class ProductVariantSerializer(serializers.ModelSerializer):

    # Delegates to the model's own computed property (ProductVariant.
    # display_name), which already joins selected option values in the
    # correct option__sort_order. Removes the previous duplicate,
    # unordered re-derivation that lived in this serializer.
    name = serializers.SerializerMethodField()

    id = serializers.IntegerField(required=False)

    # Automatic UniqueValidator removed — replaced with a custom
    # id-aware uniqueness check in validate() below. This is required
    # because nested variants are validated without `self.instance`
    # bound (updates are matched manually in _sync_variants), so the
    # default UniqueValidator would always treat an existing SKU as a
    # collision, even when it belongs to the same variant being edited.
    sku = serializers.CharField(validators=[])

    # write-only input path (unchanged sync logic keys off
    # "variant_options" via source=)
    option_values = ProductVariantOptionSerializer(
        source="variant_options",
        many=True,
        required=False,
        write_only=True,
    )

    # read-only reshaped output
    selections = serializers.SerializerMethodField()

    # Read-only: images are uploaded via the separate image endpoint
    # only, never nested on create/update.
    images = ProductImageSerializer(
        many=True,
        required=False,
        read_only=True,
    )

    currency = serializers.SerializerMethodField()
    in_stock = serializers.SerializerMethodField()
    gst_included = serializers.SerializerMethodField()
    gst_rate = serializers.SerializerMethodField()
    estimated_dispatch_days = serializers.SerializerMethodField()

    class Meta:
        model = ProductVariant
        fields = [
            "id",
            "name",
            "sku",
            "selections",
            "mrp",
            "selling_price",
            "currency",
            "stock",
            "in_stock",
            "gst_included",
            "gst_rate",
            "estimated_dispatch_days",
            "images",
            "option_values",
        ]

    def validate(self, attrs):
        mrp = attrs.get("mrp", getattr(self.instance, "mrp", None))
        selling_price = attrs.get(
            "selling_price", getattr(self.instance, "selling_price", None)
        )

        if mrp is not None and selling_price is not None and selling_price > mrp:
            raise serializers.ValidationError(
                {"selling_price": "Selling price cannot be greater than MRP."}
            )

        # --- custom SKU uniqueness check ---
        # `self.instance` is not reliably bound here (nested variants are
        # matched to existing rows manually in _sync_variants), so fall
        # back to the "id" carried in the payload itself to identify
        # which variant, if any, is allowed to keep its own SKU.
        sku = attrs.get("sku", getattr(self.instance, "sku", None))

        if sku:
            variant_id = attrs.get("id", getattr(self.instance, "id", None))

            existing = ProductVariant.objects.filter(sku=sku)
            if variant_id:
                existing = existing.exclude(pk=variant_id)

            if existing.exists():
                raise serializers.ValidationError(
                    {"sku": "Product Variant with this sku already exists."}
                )

        return attrs

    def get_selections(self, obj):
        selections = []
        for vo in obj.variant_options.all():
            ov = vo.option_value
            request = self.context.get("request")
            image_url = None
            if ov.image:
                image_url = (
                    request.build_absolute_uri(ov.image.url)
                    if request
                    else ov.image.url
                )
            selections.append({
                "option": ov.option.name,
                "value": ov.value,
                "hex_color": ov.hex_color,
                "image": image_url,
            })
        return selections

    def get_name(self, obj):
        return obj.display_name

    def get_currency(self, obj):
        return getattr(obj, "currency", "INR")

    def get_in_stock(self, obj):
        return (obj.stock or 0) > 0

    def get_gst_included(self, obj):
        return getattr(obj, "gst_included", True)

    def get_gst_rate(self, obj):
        return getattr(obj, "gst_rate", None)

    def get_estimated_dispatch_days(self, obj):
        return getattr(obj, "lead_time_days", None)


# ==========================================================
# PRODUCT SPECIFICATION
# ==========================================================

class ProductSpecificationSerializer(serializers.ModelSerializer):

    id = serializers.IntegerField(required=False)

    class Meta:
        model = ProductSpecification
        fields = [
            "id",
            "key",
            "value",
        ]
class ProductListSerializer(serializers.ModelSerializer):

    category = CategoryMiniSerializer(read_only=True)
    subcategory = SubCategoryMiniSerializer(read_only=True)

    image = serializers.SerializerMethodField()
    price = serializers.SerializerMethodField()
    in_stock = serializers.SerializerMethodField()
    collection = serializers.SerializerMethodField()
    series = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = [
            "id",
            "name",
            "slug",
            "category",
            "subcategory",
            "image",
            "price",
            "in_stock",
            "featured",
            "status",
            "collection",
            "series",
        ]

    def _get_default_variant(self, obj):
        variants = list(obj.variants.all())
        if not variants:
            return None
        for v in variants:
            if v.is_default:
                return v
        return variants[0]

    def get_image(self, obj):
        variant = self._get_default_variant(obj)
        if not variant:
            return None

        image = variant.images.filter(featured=True).first() or variant.images.first()
        if not image:
            return None

        return ProductImageSerializer(
            image,
            context=self.context,
        ).data

    def get_price(self, obj):
        variant = self._get_default_variant(obj)
        if not variant:
            return None

        return variant.selling_price

    def get_in_stock(self, obj):
        variant = self._get_default_variant(obj)
        if not variant:
            return False
        return (variant.stock or 0) > 0
    def _get_spec_value(self, obj, key):
        for spec in obj.specifications.all():
            if (
                 isinstance(spec.key, str)
                 and spec.key.strip().lower() == key.lower()
        ):
                value = (spec.value or "").strip()
                return value or None

        return None


    def get_collection(self, obj):
        return self._get_spec_value(obj, "collection")


    def get_series(self, obj):
        return self._get_spec_value(obj, "series")

# ==========================================================
# PRODUCT
# ==========================================================

class ProductSerializer(serializers.ModelSerializer):

    # --- category / subcategory ---
    # Read: nested object (frontend contract).
    # Write: still resolved to the model's "category"/"subcategory" FK
    # via source=, so validated_data keeps the same keys the existing
    # create()/update()/_sync_* methods already rely on.
    category = CategoryMiniSerializer(read_only=True)
    category_id = serializers.PrimaryKeyRelatedField(
        source="category",
        queryset=Category.objects.all(),
        write_only=True,
        required=False,
    )
    subcategory = SubCategoryMiniSerializer(read_only=True)
    subcategory_id = serializers.PrimaryKeyRelatedField(
        source="subcategory",
        queryset=SubCategory.objects.all(),
        write_only=True,
        required=False,
    )

    # --- options / option_groups ---
    # Write: "options" (unchanged, feeds _sync_options via validated_data["options"])
    options = ProductOptionSerializer(
        many=True,
        required=False,
        write_only=True,
    )
    # Read: "option_groups" (frontend contract), same shape, sourced from "options"
    option_groups = ProductOptionSerializer(
        many=True,
        read_only=True,
        source="options",
    )

    variants = ProductVariantSerializer(
        many=True,
        required=False,
    )

    specifications = ProductSpecificationSerializer(
        many=True,
        required=False,
    )

    images = serializers.SerializerMethodField()
    default_variant_id = serializers.SerializerMethodField()

    brand = serializers.SerializerMethodField()
    applications = serializers.SerializerMethodField()
    downloads = serializers.SerializerMethodField()
    related_products = serializers.SerializerMethodField()
    delivery = serializers.SerializerMethodField()
    meta_title = serializers.SerializerMethodField()
    meta_description = serializers.SerializerMethodField()
    is_verified_supplier = serializers.SerializerMethodField()
    gst_invoice_available = serializers.SerializerMethodField()
    warranty_text = serializers.SerializerMethodField()
    unit_label = serializers.SerializerMethodField()
    min_order_quantity = serializers.SerializerMethodField()
    max_order_quantity = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = [
            "id",

            "category",
            "category_id",
            "subcategory",
            "subcategory_id",

            "name",
            "slug",
            "short_description",
            "description",
            "featured",
            "active",
            "status",

            "brand",
            "images",
            "option_groups",
            "options",
            "variants",
            "default_variant_id",
            "specifications",

            "applications",
            "downloads",
            "related_products",
            "delivery",

            "meta_title",
            "meta_description",
            "is_verified_supplier",
            "gst_invoice_available",
            "warranty_text",
            "unit_label",
            "min_order_quantity",
            "max_order_quantity",

            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "created_at",
            "updated_at",
        ]

    # ======================================================
    # HELPERS
    # ======================================================

    def _get_default_variant(self, obj):
        variants = list(obj.variants.all())
        if not variants:
            return None
        for v in variants:
            if v.is_default:
                return v
        return variants[0]

    # ---- computed / passthrough-with-defaults fields ----

    def get_images(self, obj):
        variant = self._get_default_variant(obj)
        if not variant:
            return []
        return ProductImageSerializer(
            variant.images.all(), many=True, context=self.context
        ).data

    def get_default_variant_id(self, obj):
        variant = self._get_default_variant(obj)
        return variant.id if variant else None

    def get_brand(self, obj):
        return getattr(obj, "brand", None)

    def get_applications(self, obj):
        return list(getattr(obj, "applications", []) or [])

    def get_downloads(self, obj):
        return list(getattr(obj, "downloads", []) or [])

    def get_related_products(self, obj):
        return list(getattr(obj, "related_products", []) or [])

    def get_delivery(self, obj):
        delivery_data = getattr(obj, "delivery", None)
        return delivery_data if delivery_data else {}

    def get_meta_title(self, obj):
        return getattr(obj, "meta_title", None) or obj.name

    def get_meta_description(self, obj):
        return getattr(obj, "meta_description", None) or obj.short_description

    def get_is_verified_supplier(self, obj):
        return getattr(obj, "is_verified_supplier", False)

    def get_gst_invoice_available(self, obj):
        return getattr(obj, "gst_invoice_available", True)

    def get_warranty_text(self, obj):
        return getattr(obj, "warranty_text", None)

    def get_unit_label(self, obj):
        return getattr(obj, "unit_label", "piece")

    def get_min_order_quantity(self, obj):
        variant = self._get_default_variant(obj)
        return variant.minimum_order_quantity if variant else None

    def get_max_order_quantity(self, obj):
        return getattr(obj, "max_order_quantity", None)

    # ======================================================
    # CREATE
    # ======================================================

    @transaction.atomic
    def create(self, validated_data):
        options_data = validated_data.pop("options", [])
        variants_data = validated_data.pop("variants", [])
        specifications_data = validated_data.pop("specifications", [])

        product = Product.objects.create(**validated_data)

        option_value_lookup = self._sync_options(product, options_data)

        self._sync_simple_children(
            product, "specifications", specifications_data, ProductSpecification,
        )

        self._sync_variants(product, variants_data, option_value_lookup)

        return product

    # ======================================================
    # UPDATE
    # ======================================================

    @transaction.atomic
    def update(self, instance, validated_data):
        options_data = validated_data.pop("options", None)
        variants_data = validated_data.pop("variants", None)
        specifications_data = validated_data.pop("specifications", None)

        changed_fields = []

        for field, value in validated_data.items():
            if getattr(instance, field) != value:
                setattr(instance, field, value)
                changed_fields.append(field)

        if changed_fields:
            instance.save(update_fields=changed_fields)

        if options_data is not None:
            option_value_lookup = self._sync_options(instance, options_data)
        else:
            option_value_lookup = {
                (_normalize(ov.option.name), _normalize(ov.value)): ov
                for ov in ProductOptionValue.objects.filter(
                    option__product=instance
                ).select_related("option")
            }

        if specifications_data is not None:
            self._sync_simple_children(
                instance, "specifications", specifications_data, ProductSpecification,
            )

        if variants_data is not None:
            self._sync_variants(instance, variants_data, option_value_lookup)

        return instance

    # ======================================================
    # INTERNAL SYNC HELPERS
    # ======================================================

    def _sync_simple_children(self, parent_obj, related_name, children_data, model):
        manager = getattr(parent_obj, related_name)
        parent_field_name = manager.field.name

        existing = {obj.id: obj for obj in manager.all()}
        seen_ids = set()

        for child_data in children_data:
            child_data = dict(child_data)
            child_id = child_data.pop("id", None)

            if child_id and child_id in existing:
                obj = existing[child_id]

                changed_fields = []

                for field, value in child_data.items():
                    if getattr(obj, field) != value:
                        setattr(obj, field, value)
                        changed_fields.append(field)

                if changed_fields:
                    obj.save(update_fields=changed_fields)
            else:
                obj = model.objects.create(
                    **{parent_field_name: parent_obj}, **child_data
                )

            seen_ids.add(obj.id)

        for old_id, old_obj in existing.items():
            if old_id not in seen_ids:
                old_obj.delete()

    def _sync_variant_images(self, variant, images_data):
        existing = {img.id: img for img in variant.images.all()}

        for image_data in images_data:
            image_data = dict(image_data)
            should_delete = image_data.pop("delete", False)
            image_id = image_data.pop("id", None)

            if image_id and image_id in existing:
                if should_delete:
                    existing[image_id].delete()
                else:
                    obj = existing[image_id]
                    for field, value in image_data.items():
                        setattr(obj, field, value)
                    obj.save()
            elif not should_delete:
                ProductImage.objects.create(variant=variant, **image_data)

    def _sync_options(self, product, options_data):
        existing_options = {opt.id: opt for opt in product.options.all()}
        seen_option_ids = set()
        lookup = {}

        for option_data in options_data:
            option_data = dict(option_data)
            values_data = option_data.pop("values", [])
            option_id = option_data.pop("id", None)

            if option_id and option_id in existing_options:
                option_obj = existing_options[option_id]

                changed_fields = []

                for field, value in option_data.items():
                    if getattr(option_obj, field) != value:
                        setattr(option_obj, field, value)
                        changed_fields.append(field)

                if changed_fields:
                    option_obj.save(update_fields=changed_fields)
            else:
                option_obj = ProductOption.objects.create(
                    product=product, **option_data
                )

            seen_option_ids.add(option_obj.id)

            existing_values = {v.id: v for v in option_obj.values.all()}
            seen_value_ids = set()

            for value_data in values_data:
                value_data = dict(value_data)
                value_id = value_data.pop("id", None)

                if value_id and value_id in existing_values:
                    value_obj = existing_values[value_id]

                    changed_fields = []

                    for field, value in value_data.items():
                        if getattr(value_obj, field) != value:
                            setattr(value_obj, field, value)
                            changed_fields.append(field)

                    if changed_fields:
                        value_obj.save(update_fields=changed_fields)
                else:
                    value_obj = ProductOptionValue.objects.create(
                        option=option_obj, **value_data
                    )

                seen_value_ids.add(value_obj.id)
                lookup[(_normalize(option_obj.name), _normalize(value_obj.value))] = value_obj

            for old_id, old_val in existing_values.items():
                if old_id not in seen_value_ids:
                    old_val.delete()

        for old_id, old_opt in existing_options.items():
            if old_id not in seen_option_ids:
                old_opt.delete()

        return lookup

    def _sync_variants(self, product, variants_data, option_value_lookup):
        existing_variants = {v.id: v for v in product.variants.all()}
        seen_variant_ids = set()

        for variant_data in variants_data:
            variant_data = dict(variant_data)
            images_data = variant_data.pop("images", [])
            option_values_data = variant_data.pop("variant_options", [])
            variant_id = variant_data.pop("id", None)

            if variant_id and variant_id in existing_variants:
                variant_obj = existing_variants[variant_id]

                changed_fields = []

                for field, value in variant_data.items():
                    if getattr(variant_obj, field) != value:
                        setattr(variant_obj, field, value)
                        changed_fields.append(field)

                if changed_fields:
                    variant_obj.save(update_fields=changed_fields)
            else:
                variant_obj = ProductVariant.objects.create(
                    product=product, **variant_data
                )

            seen_variant_ids.add(variant_obj.id)

            self._sync_variant_option_values(
                variant_obj, option_values_data, option_value_lookup
            )
            self._sync_variant_images(variant_obj, images_data)

        for old_id, old_variant in existing_variants.items():
            if old_id not in seen_variant_ids:
                old_variant.delete()

    def _sync_variant_option_values(self, variant, option_values_data, option_value_lookup):
        existing = {
            ov.option_value_id: ov
            for ov in variant.variant_options.all()
        }
        desired_ids = set()

        for entry in option_values_data:
            option_value_obj = entry.get("option_value")

            if option_value_obj is None:
                # Prefer the current frontend contract ("option" / "value"),
                # falling back to the legacy ("option_name_ref" / "value_ref")
                # field names so both payload shapes keep working.
                option_name = entry.get("option") or entry.get("option_name_ref")
                option_value_str = entry.get("value") or entry.get("value_ref")

                key = (_normalize(option_name), _normalize(option_value_str))
                option_value_obj = option_value_lookup.get(key)

            if option_value_obj is None:
                raise serializers.ValidationError(
                    f"Could not resolve an option value for variant "
                    f"'{variant.sku}'. Provide either 'option_value_id', an "
                    f"'option' + 'value' pair, or a matching "
                    f"'option_name_ref' + 'value_ref' pair defined in this "
                    f"product's 'options'."
                )

            desired_ids.add(option_value_obj.id)

            if option_value_obj.id not in existing:
                variant_option = ProductVariantOption(
                    variant=variant,
                    option_value=option_value_obj,
                )
                try:
                    variant_option.full_clean()
                except DjangoValidationError as exc:
                    raise serializers.ValidationError(exc.message_dict or exc.messages)
                variant_option.save()

        for old_value_id, old_ov in existing.items():
            if old_value_id not in desired_ids:
                old_ov.delete()


# ==========================================================
# BULK PRODUCT ACTIONS
# ==========================================================
#
# Shared base handles what every bulk action needs identically:
#   - product_ids must be non-empty
#   - duplicates in product_ids are silently ignored (order preserved)
#   - every id must reference an existing Product, or the whole
#     request is rejected with a DRF ValidationError (400)
# ==========================================================

class BaseBulkProductActionSerializer(serializers.Serializer):

    product_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        allow_empty=False,
    )

    def validate_product_ids(self, value):
        return list(dict.fromkeys(value))

    def validate(self, attrs):
        ids = attrs["product_ids"]

        existing_ids = set(
            Product.objects.filter(id__in=ids).values_list("id", flat=True)
        )
        missing_ids = sorted(set(ids) - existing_ids)

        if missing_ids:
            raise serializers.ValidationError(
                {"product_ids": f"The following product ids do not exist: {missing_ids}"}
            )

        return attrs


class BulkDeleteSerializer(BaseBulkProductActionSerializer):
    """POST /products/bulk-delete/ — {"product_ids": [1,2,3]}"""
    pass


class BulkMoveProductsSerializer(BaseBulkProductActionSerializer):
    """
    POST /products/bulk-move/ — {"product_ids": [...], "category": 5, "subcategory": 12}

    This is the ONLY allowed way to bulk-change a product's category or
    subcategory. category and subcategory are always written together,
    inside one atomic transaction, so no product can ever end up with a
    subcategory that doesn't belong to its category.
    """

    category = serializers.PrimaryKeyRelatedField(
        queryset=Category.objects.all(),
    )
    subcategory = serializers.PrimaryKeyRelatedField(
        queryset=SubCategory.objects.select_related("category"),
    )

    def validate(self, attrs):
        attrs = super().validate(attrs)

        category = attrs["category"]
        subcategory = attrs["subcategory"]

        if subcategory.category_id != category.id:
            raise serializers.ValidationError(
                {
                    "subcategory": (
                        f"Subcategory '{subcategory.name}' belongs to category "
                        f"'{subcategory.category.name}', not '{category.name}'. "
                        "The selected subcategory must belong to the selected category."
                    )
                }
            )

        return attrs                


class DeliveryCheckSerializer(serializers.Serializer):

    pincode = serializers.RegexField(
        regex=r"^\d{6}$"
    )




    # ==========================================================
# REQUEST A QUOTE
# ==========================================================

class QuoteAttachmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = QuoteAttachment
        fields = (
            "id",
            "file",
            "uploaded_at",
        )
        read_only_fields = (
            "id",
            "uploaded_at",
        )


class QuoteRequestSerializer(serializers.ModelSerializer):

    attachments = serializers.ListField(
        child=serializers.FileField(),
        write_only=True,
        required=False,
    )

    class Meta:
        model = QuoteRequest
        fields = (
            "id",
            "quote_id",
            "full_name",
            "phone",
            "email",
            "company",
            "project_location",
            "delivery_pincode",
            "project_type",
            "materials",
            "requirements",
            "status",
            "attachments",
            "created_at",
        )

        read_only_fields = (
            "id",
            "quote_id",
            "status",
            "created_at",
        )

    @transaction.atomic
    def create(self, validated_data):
        files = validated_data.pop("attachments", [])

        quote = QuoteRequest.objects.create(**validated_data)

        for file in files:
            QuoteAttachment.objects.create(
                quote=quote,
                file=file,
            )

        return quote








# ==========================================================
# DELIVERY QUOTE
# ==========================================================

# ==========================================================
# DELIVERY QUOTE
# ==========================================================


class DeliveryQuoteItemSerializer(serializers.Serializer):
    variant_id = serializers.IntegerField(
        min_value=1,
    )

    quantity = serializers.IntegerField(
        min_value=1,
        max_value=100000,
    )


class DeliveryQuoteRequestSerializer(serializers.Serializer):
    pincode = serializers.CharField(
        trim_whitespace=True,
        max_length=6,
    )

    items = DeliveryQuoteItemSerializer(
        many=True,
        allow_empty=False,
    )

    def validate_pincode(self, value):
        from .delivery.validators import normalize_pincode

        try:
            return normalize_pincode(value)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc))

    def validate_items(self, value):
        if len(value) > 100:
            raise serializers.ValidationError(
                "A delivery quote can contain at most 100 line items."
            )

        variant_ids = [
            item["variant_id"]
            for item in value
        ]

        if len(variant_ids) != len(set(variant_ids)):
            raise serializers.ValidationError(
                "Duplicate variant_id values are not allowed."
            )

        return value


class DeliveryQuoteBreakdownSerializer(serializers.Serializer):
    rule = serializers.CharField()
    label = serializers.CharField()
    amount = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
    )


class DeliveryQuoteZoneSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()


class DeliveryQuoteResponseSerializer(serializers.Serializer):
    deliverable = serializers.BooleanField()

    zone = DeliveryQuoteZoneSerializer(
        allow_null=True,
        required=False,
    )

    subtotal = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
    )

    weight = serializers.DecimalField(
        max_digits=12,
        decimal_places=3,
    )

    delivery_charge = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        allow_null=True,
    )

    free_delivery = serializers.BooleanField()

    breakdown = DeliveryQuoteBreakdownSerializer(
        many=True,
    )

    message = serializers.CharField()
    
    
    # ==========================================================
# DELIVERY ZONE
# ==========================================================

class DeliveryZoneMiniSerializer(serializers.ModelSerializer):
    class Meta:
        model = DeliveryZone
        fields = ["id", "name", "code"]


class DeliveryZoneSerializer(serializers.ModelSerializer):
    pincode_count = serializers.SerializerMethodField()
    rule_count = serializers.SerializerMethodField()

    class Meta:
        model = DeliveryZone
        fields = [
            "id", "name", "code", "active", "priority",
            "pincode_count", "rule_count", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def get_pincode_count(self, obj):
        annotated = getattr(obj, "_pincode_count", None)
        return annotated if annotated is not None else obj.pincodes.count()

    def get_rule_count(self, obj):
        annotated = getattr(obj, "_rule_count", None)
        return annotated if annotated is not None else obj.rules.count()


# ==========================================================
# SERVICEABLE PINCODE
# ==========================================================

class ServiceablePincodeSerializer(serializers.ModelSerializer):
    zone = DeliveryZoneMiniSerializer(read_only=True)
    zone_id = serializers.PrimaryKeyRelatedField(
        source="zone", queryset=DeliveryZone.objects.all(),
        write_only=True, required=False, allow_null=True,
    )

    pincode = serializers.CharField(max_length=6, validators=[])
    area_name = serializers.CharField(required=True, allow_blank=False)
    city = serializers.CharField(required=True, allow_blank=False)
    state = serializers.CharField(required=True, allow_blank=False)

    class Meta:
        model = ServiceablePincode
        fields = ["id", "pincode", "area_name", "city", "state", "is_active", "zone", "zone_id"]
        read_only_fields = ["id"]

    def validate_pincode(self, value):
        try:
            normalized = normalize_pincode(value)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc))

        existing = ServiceablePincode.objects.filter(pincode=normalized)
        if self.instance:
            existing = existing.exclude(pk=self.instance.pk)
        if existing.exists():
            raise serializers.ValidationError(
                "Serviceable pincode with this pincode already exists."
            )
        return normalized

    def validate_area_name(self, value):
        if not value.strip():
            raise serializers.ValidationError("This field may not be blank.")
        return value

    def validate_city(self, value):
        if not value.strip():
            raise serializers.ValidationError("This field may not be blank.")
        return value

    def validate_state(self, value):
        if not value.strip():
            raise serializers.ValidationError("This field may not be blank.")
        return value


# ==========================================================
# DELIVERY RULE CONDITION
# ==========================================================

class DeliveryRuleConditionSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(required=False)
    field_display = serializers.CharField(source="get_field_display", read_only=True)
    operator_display = serializers.CharField(source="get_operator_display", read_only=True)

    class Meta:
        model = DeliveryRuleCondition
        fields = ["id", "field", "field_display", "operator", "operator_display", "value", "sort_order"]

    _TEXT_FIELDS = {DeliveryRuleCondition.FIELD_CUSTOMER_TYPE, DeliveryRuleCondition.FIELD_SHIPPING_TYPE}
    _DECIMAL_FIELDS = {DeliveryRuleCondition.FIELD_CART_VALUE, DeliveryRuleCondition.FIELD_WEIGHT}
    _INT_FIELDS = {DeliveryRuleCondition.FIELD_QUANTITY, DeliveryRuleCondition.FIELD_TOTAL_QUANTITY}

    def validate(self, attrs):
        field = attrs.get("field", getattr(self.instance, "field", None))
        operator = attrs.get("operator", getattr(self.instance, "operator", None))
        value = attrs.get("value", getattr(self.instance, "value", None))

        if field in self._TEXT_FIELDS and operator in {
            DeliveryRuleCondition.OP_GT, DeliveryRuleCondition.OP_GTE,
            DeliveryRuleCondition.OP_LT, DeliveryRuleCondition.OP_LTE,
        }:
            raise serializers.ValidationError(
                "Comparison operators (>, >=, <, <=) don't apply to text "
                "fields — use 'equal to' or 'in'."
            )

        if operator == DeliveryRuleCondition.OP_IN:
            if not value or not value.strip():
                raise serializers.ValidationError(
                    {"value": "Provide at least one comma-separated value for 'in'."}
                )

            parts = [p.strip() for p in value.split(",")]

            if any(p == "" for p in parts):
                raise serializers.ValidationError(
                    {"value": "Comma-separated 'in' values cannot contain empty entries."}
                )

            if field in self._DECIMAL_FIELDS:
                for p in parts:
                    try:
                        parsed = Decimal(p)
                    except (InvalidOperation, TypeError):
                        raise serializers.ValidationError(
                            {"value": f"'{p}' is not a valid number."}
                        )
                    if parsed < 0:
                        raise serializers.ValidationError(
                            {"value": f"'{p}' must be non-negative."}
                        )

            elif field in self._INT_FIELDS:
                for p in parts:
                    try:
                        parsed_int = int(p)
                    except (TypeError, ValueError):
                        raise serializers.ValidationError(
                            {"value": f"'{p}' is not a valid whole number."}
                        )
                    if parsed_int < 0:
                        raise serializers.ValidationError(
                            {"value": f"'{p}' must be non-negative."}
                        )

            return attrs

        if field in self._DECIMAL_FIELDS:
            try:
                parsed = Decimal(value)
            except (InvalidOperation, TypeError):
                raise serializers.ValidationError({"value": "This field requires a numeric value."})
            if parsed < 0:
                raise serializers.ValidationError({"value": "Value must be non-negative."})

        elif field in self._INT_FIELDS:
            try:
                parsed_int = int(value)
            except (TypeError, ValueError):
                raise serializers.ValidationError({"value": "This field requires a whole number."})
            if parsed_int < 0:
                raise serializers.ValidationError({"value": "Value must be non-negative."})

        return attrs


# ==========================================================
# DELIVERY RULE ACTION
# ==========================================================

class DeliveryRuleActionSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(required=False)
    action_type_display = serializers.CharField(source="get_action_type_display", read_only=True)
    pricing_mode_display = serializers.CharField(source="get_pricing_mode_display", read_only=True)
    amount = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=0)

    class Meta:
        model = DeliveryRuleAction
        fields = [
            "id", "action_type", "action_type_display",
            "pricing_mode", "pricing_mode_display",
            "amount", "label", "metadata", "sort_order", "active",
        ]
    # NOTE: discount+override cross-check deliberately NOT here — it needs
    # the parent rule's combine_mode, which this child serializer can't
    # see. Enforced in DeliveryRuleSerializer.validate() below.


# ==========================================================
# DELIVERY RULE — LIST
# ==========================================================

class DeliveryRuleListSerializer(serializers.ModelSerializer):
    zone = DeliveryZoneMiniSerializer(read_only=True)
    category = CategoryMiniSerializer(read_only=True)
    subcategory = SubCategoryMiniSerializer(read_only=True)
    computed_status = serializers.SerializerMethodField()
    scope = serializers.SerializerMethodField()
    condition_count = serializers.SerializerMethodField()
    action_count = serializers.SerializerMethodField()

    class Meta:
        model = DeliveryRule
        fields = [
            "id", "name", "code", "active", "computed_status",
            "zone", "category", "subcategory", "scope",
            "priority", "combine_mode", "stop_after",
            "starts_at", "ends_at",
            "condition_count", "action_count", "updated_at",
        ]

    def get_computed_status(self, obj):
        return compute_rule_status(obj)

    def get_scope(self, obj):
        scope_type, label = resolve_rule_scope(obj)
        return {"type": scope_type, "label": label}

    def get_condition_count(self, obj):
        annotated = getattr(obj, "_condition_count", None)
        return annotated if annotated is not None else obj.conditions.count()

    def get_action_count(self, obj):
        annotated = getattr(obj, "_action_count", None)
        return annotated if annotated is not None else obj.actions.count()


# ==========================================================
# DELIVERY RULE — DETAIL (create/update/retrieve)
# ==========================================================

class DeliveryRuleSerializer(serializers.ModelSerializer):
    zone = DeliveryZoneMiniSerializer(read_only=True)
    category = CategoryMiniSerializer(read_only=True)
    subcategory = SubCategoryMiniSerializer(read_only=True)
    product = serializers.SerializerMethodField()
    variant = serializers.SerializerMethodField()

    zone_id = serializers.PrimaryKeyRelatedField(
        source="zone", queryset=DeliveryZone.objects.all(), write_only=True, required=False, allow_null=True,
    )
    category_id = serializers.PrimaryKeyRelatedField(
        source="category", queryset=Category.objects.all(), write_only=True, required=False, allow_null=True,
    )
    subcategory_id = serializers.PrimaryKeyRelatedField(
        source="subcategory", queryset=SubCategory.objects.all(), write_only=True, required=False, allow_null=True,
    )
    product_id = serializers.PrimaryKeyRelatedField(
        source="product", queryset=Product.objects.all(), write_only=True, required=False, allow_null=True,
    )
    variant_id = serializers.PrimaryKeyRelatedField(
        source="variant", queryset=ProductVariant.objects.all(), write_only=True, required=False, allow_null=True,
    )

    computed_status = serializers.SerializerMethodField()
    specificity = serializers.IntegerField(read_only=True)

    conditions = DeliveryRuleConditionSerializer(many=True, required=False)
    actions = DeliveryRuleActionSerializer(many=True, required=False)

    class Meta:
        model = DeliveryRule
        fields = [
            "id", "name", "code", "active", "computed_status", "priority", "specificity",
            "zone", "zone_id", "category", "category_id",
            "subcategory", "subcategory_id", "product", "product_id",
            "variant", "variant_id",
            "combine_mode", "stop_after", "starts_at", "ends_at",
            "conditions", "actions", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def get_computed_status(self, obj):
        return compute_rule_status(obj)

    def get_product(self, obj):
        if not obj.product_id:
            return None
        return {"id": obj.product.id, "name": obj.product.name, "slug": obj.product.slug}

    def get_variant(self, obj):
        if not obj.variant_id:
            return None
        return {"id": obj.variant.id, "sku": obj.variant.sku, "name": obj.variant.display_name}

    def validate(self, attrs):
        instance = self.instance

        def eff(name):
            if name in attrs:
                return attrs[name]
            return getattr(instance, name) if instance is not None else None

        trial = DeliveryRule(
            zone=eff("zone"), category=eff("category"), subcategory=eff("subcategory"),
            product=eff("product"), variant=eff("variant"),
            starts_at=eff("starts_at"), ends_at=eff("ends_at"),
        )
        try:
            trial.clean()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(
                exc.message_dict if hasattr(exc, "message_dict") else exc.messages
            )

        combine_mode = attrs.get("combine_mode", getattr(instance, "combine_mode", None))

        if "actions" in attrs:
            has_discount = any(
                a.get("action_type") == DeliveryRuleAction.ACTION_DISCOUNT for a in attrs["actions"]
            )
        elif instance is not None:
            has_discount = instance.actions.filter(action_type=DeliveryRuleAction.ACTION_DISCOUNT).exists()
        else:
            has_discount = False

        if combine_mode == DeliveryRule.COMBINE_OVERRIDE and has_discount:
            raise serializers.ValidationError({
                "actions": [
                    "Discount actions require combine_mode = Add. Change combine "
                    "mode or remove the discount action."
                ]
            })

        return attrs

    @transaction.atomic
    def create(self, validated_data):
        conditions_data = validated_data.pop("conditions", [])
        actions_data = validated_data.pop("actions", [])
        rule = DeliveryRule.objects.create(**validated_data)
        self._sync_conditions(rule, conditions_data)
        self._sync_actions(rule, actions_data)
        return rule

    @transaction.atomic
    def update(self, instance, validated_data):
        conditions_data = validated_data.pop("conditions", None)
        actions_data = validated_data.pop("actions", None)

        changed = []
        for field, value in validated_data.items():
            if getattr(instance, field) != value:
                setattr(instance, field, value)
                changed.append(field)
        if changed:
            instance.save(update_fields=changed)

        if conditions_data is not None:
            self._sync_conditions(instance, conditions_data)
        if actions_data is not None:
            self._sync_actions(instance, actions_data)

        return instance

    # ------------------------------------------------------------
    # NESTED SYNC — hardened.
    #
    # An `id` in the payload MUST belong to this rule's existing
    # children. If it doesn't (foreign id, stale id, garbage id), we
    # reject the whole request with a 400 instead of silently creating
    # a new row under a mismatched/ignored id. Only a payload with NO
    # `id` is treated as "create new".
    # ------------------------------------------------------------

    def _sync_conditions(self, rule, conditions_data):
        existing = {c.id: c for c in rule.conditions.all()}
        seen = set()

        for data in conditions_data:
            data = dict(data)
            item_id = data.pop("id", None)

            if item_id is not None:
                if item_id not in existing:
                    raise serializers.ValidationError(
                        {"conditions": f"Condition id {item_id} does not belong to this rule."}
                    )
                obj = existing[item_id]
                fields_changed = [f for f, v in data.items() if getattr(obj, f) != v]
                for f, v in data.items():
                    setattr(obj, f, v)
                if fields_changed:
                    obj.save(update_fields=fields_changed)
            else:
                obj = DeliveryRuleCondition.objects.create(rule=rule, **data)

            seen.add(obj.id)

        for old_id, old_obj in existing.items():
            if old_id not in seen:
                old_obj.delete()

    def _sync_actions(self, rule, actions_data):
        existing = {a.id: a for a in rule.actions.all()}
        seen = set()

        for data in actions_data:
            data = dict(data)
            item_id = data.pop("id", None)

            if item_id is not None:
                if item_id not in existing:
                    raise serializers.ValidationError(
                        {"actions": f"Action id {item_id} does not belong to this rule."}
                    )
                obj = existing[item_id]
                fields_changed = [f for f, v in data.items() if getattr(obj, f) != v]
                for f, v in data.items():
                    setattr(obj, f, v)
                if fields_changed:
                    obj.save(update_fields=fields_changed)
            else:
                obj = DeliveryRuleAction.objects.create(rule=rule, **data)

            seen.add(obj.id)

        for old_id, old_obj in existing.items():
            if old_id not in seen:
                old_obj.delete()
