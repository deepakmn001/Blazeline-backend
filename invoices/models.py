from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models


class Invoice(models.Model):
    """
    Canonical commercial invoice for a BlazeLine order.

    Design goals:
    - Exactly one invoice per order.
    - Commercial/customer data is snapshotted at invoice creation time.
    - Historical invoice data must not silently change later.
    - Delivery state remains mutable for email/WhatsApp retries.
    - PDF storage metadata is kept on the invoice for retrieval/resend.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        GENERATED = "generated", "Generated"
        PARTIALLY_SENT = "partially_sent", "Partially Sent"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"

    class DeliveryStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"
        NOT_APPLICABLE = "not_applicable", "Not Applicable"

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    id = models.UUIDField(
        primary_key=True,
        default=uuid4,
        editable=False,
    )

    invoice_number = models.CharField(
        max_length=50,
        unique=True,
        editable=False,
        db_index=True,
        help_text="Unique customer-facing invoice identifier.",
    )

    order = models.OneToOneField(
        "orders.Order",
        on_delete=models.PROTECT,
        related_name="invoice",
    )

    customer = models.ForeignKey(
        "accounts.Customer",
        on_delete=models.PROTECT,
        related_name="invoices",
    )

    # ------------------------------------------------------------------
    # Invoice lifecycle
    # ------------------------------------------------------------------

    status = models.CharField(
        max_length=30,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )

    # ------------------------------------------------------------------
    # Customer snapshot
    # ------------------------------------------------------------------

    customer_name = models.CharField(
        max_length=150,
    )

    customer_email = models.EmailField(
        blank=True,
    )

    customer_phone = models.CharField(
        max_length=15,
        blank=True,
    )

    company_name = models.CharField(
        max_length=200,
        blank=True,
    )

    customer_gstin = models.CharField(
        max_length=15,
        blank=True,
    )

    # Current checkout stores one address snapshot on Order.
    # We treat that captured address as the invoice billing address.
    billing_address_line1 = models.CharField(
        max_length=255,
    )

    billing_address_line2 = models.CharField(
        max_length=255,
        blank=True,
    )

    billing_landmark = models.CharField(
        max_length=255,
        blank=True,
    )

    billing_city = models.CharField(
        max_length=100,
    )

    billing_state = models.CharField(
        max_length=100,
    )

    billing_pincode = models.CharField(
        max_length=6,
    )

    # ------------------------------------------------------------------
    # Commercial snapshot
    # ------------------------------------------------------------------

    currency = models.CharField(
        max_length=3,
        default="INR",
    )

    subtotal = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[
            MinValueValidator(Decimal("0.00")),
        ],
    )

    discount_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[
            MinValueValidator(Decimal("0.00")),
        ],
    )

    delivery_charge = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[
            MinValueValidator(Decimal("0.00")),
        ],
    )

    tax_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[
            MinValueValidator(Decimal("0.00")),
        ],
    )

    cod_fee = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[
            MinValueValidator(Decimal("0.00")),
        ],
    )

    grand_total = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[
            MinValueValidator(Decimal("0.00")),
        ],
    )

    # ------------------------------------------------------------------
    # Payment snapshot
    # ------------------------------------------------------------------

    payment_method = models.CharField(
        max_length=20,
    )

    payment_status = models.CharField(
        max_length=30,
    )

    razorpay_payment_id = models.CharField(
        max_length=255,
        blank=True,
    )

    # ------------------------------------------------------------------
    # PDF storage
    # ------------------------------------------------------------------

    pdf_url = models.URLField(
        blank=True,
        help_text="Persistent URL of the generated invoice PDF.",
    )

    pdf_public_id = models.CharField(
        max_length=500,
        blank=True,
        help_text="Persistent storage identifier for the invoice PDF.",
    )

    # ------------------------------------------------------------------
    # Email delivery
    # ------------------------------------------------------------------

    email_status = models.CharField(
        max_length=30,
        choices=DeliveryStatus.choices,
        default=DeliveryStatus.PENDING,
        db_index=True,
    )

    email_sent_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    email_error = models.TextField(
        blank=True,
    )

    # ------------------------------------------------------------------
    # WhatsApp delivery
    # ------------------------------------------------------------------

    whatsapp_status = models.CharField(
        max_length=30,
        choices=DeliveryStatus.choices,
        default=DeliveryStatus.PENDING,
        db_index=True,
    )

    whatsapp_sent_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    whatsapp_error = models.TextField(
        blank=True,
    )

    # ------------------------------------------------------------------
    # Delivery diagnostics
    # ------------------------------------------------------------------

    delivery_attempts = models.PositiveIntegerField(
        default=0,
    )

    last_error = models.TextField(
        blank=True,
    )

    # ------------------------------------------------------------------
    # Audit
    # ------------------------------------------------------------------

    issued_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        ordering = ["-issued_at"]
        indexes = [
            models.Index(
                fields=["customer", "-issued_at"],
                name="invoice_customer_issued_idx",
            ),
            models.Index(
                fields=["status", "-issued_at"],
                name="invoice_status_issued_idx",
            ),
            models.Index(
                fields=["email_status", "-issued_at"],
                name="invoice_email_status_idx",
            ),
            models.Index(
                fields=["whatsapp_status", "-issued_at"],
                name="invoice_whatsapp_status_idx",
            ),
            models.Index(
                fields=["order"],
                name="invoice_order_idx",
            ),
        ]

    def __str__(self) -> str:
        return self.invoice_number

    @staticmethod
    def generate_invoice_number() -> str:
        """
        Generate a non-guessable unique public invoice identifier.

        Example:
            INV-BL-7F2A9C31E4
        """
        return f"INV-BL-{uuid4().hex[:10].upper()}"

    def save(self, *args, **kwargs):
        """
        Protect the historical commercial/customer snapshot.

        Mutable fields such as delivery statuses, PDF metadata and errors
        are intentionally allowed to change after invoice creation.
        """

        if not self.invoice_number:
            self.invoice_number = self.generate_invoice_number()

        if not self._state.adding and self.pk:
            old = (
                type(self)
                .objects
                .filter(pk=self.pk)
                .values(
                    "order_id",
                    "customer_id",
                    "invoice_number",
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
                )
                .first()
            )

            if old:
                protected_fields = (
                    "order_id",
                    "customer_id",
                    "invoice_number",
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
                )

                for field_name in protected_fields:
                    if getattr(self, field_name) != old[field_name]:
                        raise ValidationError(
                            f"Invoice field '{field_name}' is immutable."
                        )

        super().save(*args, **kwargs)


class InvoiceItem(models.Model):
    """
    Immutable line-item snapshot belonging to one invoice.

    This deliberately duplicates commercial values from OrderItem so the
    historical invoice remains independent of future catalog/order changes.
    """

    id = models.BigAutoField(
        primary_key=True,
    )

    invoice = models.ForeignKey(
        Invoice,
        on_delete=models.CASCADE,
        related_name="items",
    )

    product_name = models.CharField(
        max_length=255,
    )

    sku = models.CharField(
        max_length=120,
    )

    variant_name = models.CharField(
        max_length=255,
        blank=True,
    )

    quantity = models.PositiveIntegerField(
        validators=[
            MinValueValidator(1),
        ],
    )

    unit_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[
            MinValueValidator(Decimal("0.00")),
        ],
    )

    tax_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[
            MinValueValidator(Decimal("0.00")),
        ],
    )

    tax_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[
            MinValueValidator(Decimal("0.00")),
        ],
    )

    discount_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[
            MinValueValidator(Decimal("0.00")),
        ],
    )

    line_total = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[
            MinValueValidator(Decimal("0.00")),
        ],
    )

    currency = models.CharField(
        max_length=3,
        default="INR",
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    class Meta:
        ordering = ["id"]
        indexes = [
            models.Index(
                fields=["invoice", "sku"],
                name="invoice_item_invoice_sku_idx",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"{self.invoice.invoice_number} · "
            f"{self.sku} × {self.quantity}"
        )

    def save(self, *args, **kwargs):
        """
        Invoice line items are historical snapshots and cannot be edited
        after creation.
        """
        if not self._state.adding and self.pk:
            old = (
                type(self)
                .objects
                .filter(pk=self.pk)
                .values(
                    "invoice_id",
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
                )
                .first()
            )

            if old:
                protected_fields = (
                    "invoice_id",
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
                )

                for field_name in protected_fields:
                    if getattr(self, field_name) != old[field_name]:
                        raise ValidationError(
                            f"InvoiceItem field '{field_name}' is immutable."
                        )

        super().save(*args, **kwargs)