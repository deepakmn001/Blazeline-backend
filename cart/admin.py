from django.contrib import admin

from .models import Cart, CartItem


class CartItemInline(admin.TabularInline):
    model = CartItem
    extra = 0
    readonly_fields = ["added_at", "updated_at"]


@admin.register(Cart)
class CartAdmin(admin.ModelAdmin):
    list_display = ["id", "customer", "guest_id", "updated_at"]
    search_fields = ["customer__email", "customer__phone", "guest_id"]
    inlines = [CartItemInline]