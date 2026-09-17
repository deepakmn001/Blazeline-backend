from django.contrib import admin

from .models import Promotion, PromotionRule


class PromotionRuleInline(admin.TabularInline):
    model = PromotionRule
    extra = 1
    autocomplete_fields = (
        "category",
        "subcategory",
        "product",
        "variant",
    )
    fields = (
        "target_type",
        "category",
        "subcategory",
        "brand",
        "product",
        "variant",
    )


@admin.register(Promotion)
class PromotionAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "discount_type",
        "discount_value",
        "minimum_cart_value",
        "max_discount_amount",
        "priority",
        "stackable",
        "is_active",
        "start_at",
        "end_at",
        "created_at",
    )

    list_filter = (
        "is_active",
        "discount_type",
        "stackable",
        "start_at",
        "end_at",
    )

    search_fields = (
        "name",
        "description",
    )

    ordering = (
        "-priority",
        "-created_at",
        "-id",
    )

    readonly_fields = (
        "created_at",
        "updated_at",
    )

    fieldsets = (
        (
            "Promotion",
            {
                "fields": (
                    "name",
                    "description",
                    "discount_type",
                    "discount_value",
                    "max_discount_amount",
                    "minimum_cart_value",
                )
            },
        ),
        (
            "Scheduling & Behavior",
            {
                "fields": (
                    "is_active",
                    "start_at",
                    "end_at",
                    "priority",
                    "stackable",
                )
            },
        ),
        (
            "Audit",
            {
                "fields": (
                    "created_at",
                    "updated_at",
                )
            },
        ),
    )

    inlines = (
        PromotionRuleInline,
    )

    def save_model(self, request, obj, form, change):
        obj.full_clean()
        super().save_model(request, obj, form, change)


@admin.register(PromotionRule)
class PromotionRuleAdmin(admin.ModelAdmin):
    list_display = (
        "promotion",
        "target_type",
        "category",
        "subcategory",
        "brand",
        "product",
        "variant",
        "created_at",
    )

    list_filter = (
        "target_type",
    )

    search_fields = (
        "promotion__name",
        "brand",
        "category__name",
        "subcategory__name",
        "product__name",
        "variant__sku",
    )

    autocomplete_fields = (
        "promotion",
        "category",
        "subcategory",
        "product",
        "variant",
    )

    readonly_fields = (
        "brand_normalized",
        "created_at",
        "updated_at",
    )

    def save_model(self, request, obj, form, change):
        obj.full_clean()
        super().save_model(request, obj, form, change)