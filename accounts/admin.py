from django.contrib import admin

from .models import OTP, Address, Customer


class AddressInline(admin.TabularInline):
    model = Address
    extra = 0


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ["id", "email", "phone", "full_name", "is_active", "date_joined"]
    list_filter = ["is_active", "is_email_verified", "is_phone_verified"]
    search_fields = ["email", "phone", "full_name"]
    ordering = ["-date_joined"]
    inlines = [AddressInline]


@admin.register(OTP)
class OTPAdmin(admin.ModelAdmin):
    list_display = ["identifier", "channel", "is_used", "attempts", "expires_at", "created_at"]
    list_filter = ["channel", "is_used"]
    search_fields = ["identifier"]
    readonly_fields = [f.name for f in OTP._meta.fields]