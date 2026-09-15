from django.contrib import admin

from .models import Invoice, InvoiceItem


class InvoiceItemInline(admin.TabularInline):
    model = InvoiceItem
    extra = 0
    readonly_fields = [
        "product_name",
        "sku",
        "variant_name",
        "quantity",
        "unit_price",
        "tax_rate",
        "tax_amount",
        "discount_amount",
        "line_total",
        "currency",
        "created_at",
    ]

    can_delete = False


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = [
        "invoice_number",
        "order",
        "customer_name",
        "grand_total",
        "status",
        "email_status",
        "whatsapp_status",
        "issued_at",
    ]

    list_filter = [
        "status",
        "email_status",
        "whatsapp_status",
        "payment_method",
        "issued_at",
    ]

    search_fields = [
        "invoice_number",
        "order__order_number",
        "customer_name",
        "customer_email",
        "customer_phone",
        "customer_gstin",
        "customer__email",
        "customer__phone",
    ]

    readonly_fields = [
        "invoice_number",
        "order",
        "customer",
        "customer_name",
        "customer_email",
        "customer_phone",
        "company_name",
        "customer_gstin",
        "billing_address_line1",
        "billing_address_line2",
        "billing_landmark",
        "billing_city",
        "billing_state",
        "billing_pincode",
        "currency",
        "subtotal",
        "discount_amount",
        "delivery_charge",
        "tax_amount",
        "cod_fee",
        "grand_total",
        "payment_method",
        "payment_status",
        "razorpay_payment_id",
        "pdf_url",
        "pdf_public_id",
        "email_status",
        "email_sent_at",
        "whatsapp_status",
        "whatsapp_sent_at",
        "delivery_attempts",
        "last_error",
        "issued_at",
        "updated_at",
    ]

    inlines = [InvoiceItemInline]

    ordering = ["-issued_at"]