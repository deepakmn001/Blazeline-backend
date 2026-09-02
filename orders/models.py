from decimal import Decimal
from uuid import uuid4

from django.core.validators import MinValueValidator
from django.db import models

from accounts.models import Customer
from catalog.models import ProductVariant


# ==========================================================
# ORDER
# ==========================================================


class Order(models.Model):
    """
    Immutable commercial snapshot + mutable fulfillment/payment state.

    Design principles:
    - Customer owns the order.
    - Prices/taxes/delivery are snapshotted at order time.
    - Current ProductVariant remains linked for operational reference only.
    - Human-facing order number is separate from DB primary key.
    - Idempotency key protects client retries from creating duplicate orders.
    """

    class Status(models.TextChoices):
        PENDING_PAYMENT = "pending_payment", "Pending Payment"
        CONFIRMED = "confirmed", "Confirmed"
        PROCESSING = "processing", "Processing"
        PACKED = "packed", "Packed"
        DISPATCHED = "dispatched", "Dispatched"
        DELIVERED = "delivered", "Delivered"
        CANCELLED = "cancelled", "Cancelled"
        FAILED = "failed", "Failed"

    class PaymentStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        PAID = "paid", "Paid"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"
        REFUND_PENDING = "refund_pending", "Refund Pending"
        PARTIALLY_REFUNDED = "partially_refunded", "Partially Refunded"
        REFUNDED = "refunded", "Refunded"

    class PaymentMethod(models.TextChoices):
        UPI = "upi", "UPI"
        CARD = "card", "Card"
        NETBANKING = "netbanking", "Net Banking"
        COD = "cod", "Cash on Delivery"
        CREDIT = "credit", "Credit Terms"

    # ------------------------------------------------------
    # Identity
    # ------------------------------------------------------

    order_number = models.CharField(
        max_length=40,
        unique=True,
        editable=False,
        db_index=True,
        help_text="Public/customer-facing order identifier.",
    )

    customer = models.ForeignKey(
        Customer,
        on_delete=models.PROTECT,
        related_name="orders",
    )

    # One unique key per order-creation attempt from a client/session.
    # The actual enforcement will be strengthened by the service layer.
    idempotency_key = models.CharField(
        max_length=128,
        unique=True,
        editable=False,
        db_index=True,
        help_text="Client-supplied idempotency token for safe retries.",
    )

    # ------------------------------------------------------
    # State
    # ------------------------------------------------------

    status = models.CharField(
        max_length=30,
        choices=Status.choices,
        default=Status.PENDING_PAYMENT,
        db_index=True,
    )

    payment_status = models.CharField(
        max_length=30,
        choices=PaymentStatus.choices,
        default=PaymentStatus.PENDING,
        db_index=True,
    )

    payment_method = models.CharField(
        max_length=20,
        choices=PaymentMethod.choices,
    )

    # ------------------------------------------------------
    # Money
    # ------------------------------------------------------

    currency = models.CharField(
        max_length=3,
        default="INR",
    )

    subtotal = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
    )

    discount_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )

    delivery_charge = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )

    tax_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )

    cod_fee = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )

    grand_total = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
    )

    # ------------------------------------------------------
    # Shipping snapshot
    # ------------------------------------------------------

    shipping_full_name = models.CharField(
        max_length=150,
    )

    shipping_phone = models.CharField(
        max_length=15,
    )

    shipping_email = models.EmailField()

    shipping_company = models.CharField(
        max_length=200,
        blank=True,
    )

    shipping_gstin = models.CharField(
        max_length=15,
        blank=True,
    )

    shipping_address_line1 = models.CharField(
        max_length=255,
    )

    shipping_address_line2 = models.CharField(
        max_length=255,
        blank=True,
    )

    shipping_landmark = models.CharField(
        max_length=255,
        blank=True,
    )

    shipping_city = models.CharField(
        max_length=100,
    )

    shipping_state = models.CharField(
        max_length=100,
    )

    shipping_pincode = models.CharField(
        max_length=6,
    )

    # ------------------------------------------------------
    # Delivery snapshot
    # ------------------------------------------------------

    delivery_zone_id = models.PositiveBigIntegerField(
        null=True,
        blank=True,
        help_text="Snapshot of delivery zone ID used at order time.",
    )

    delivery_zone_name = models.CharField(
        max_length=100,
        blank=True,
    )

    delivery_breakdown = models.JSONField(
        default=list,
        blank=True,
        help_text="Customer-visible delivery charge calculation snapshot.",
    )

    # ------------------------------------------------------
    # Auditing / timestamps
    # ------------------------------------------------------

    notes = models.TextField(
        blank=True,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    paid_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    cancelled_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    delivered_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["customer", "-created_at"],
                name="order_customer_created_idx",
            ),
            models.Index(
                fields=["status", "-created_at"],
                name="order_status_created_idx",
            ),
            models.Index(
                fields=["payment_status", "-created_at"],
                name="order_payment_created_idx",
            ),
        ]
        verbose_name = "Order"
        verbose_name_plural = "Orders"

    def __str__(self):
        return self.order_number

    def save(self, *args, **kwargs):
        if not self.order_number:
            self.order_number = self._generate_order_number()

        if not self.idempotency_key:
            self.idempotency_key = uuid4().hex

        super().save(*args, **kwargs)

    @staticmethod
    def _generate_order_number():
        """
        Human-friendly public identifier.

        UUID randomness keeps IDs difficult to enumerate and avoids exposing
        database sequence information.
        """
        return f"BL-{uuid4().hex[:12].upper()}"


# ==========================================================
# ORDER ITEM
# ==========================================================


class OrderItem(models.Model):
    """
    Immutable commercial snapshot of one purchased variant.

    `variant` remains nullable because catalog objects can legitimately be
    deleted/retired in the future while historical orders must remain intact.
    """

    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name="items",
    )

    variant = models.ForeignKey(
        ProductVariant,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="order_items",
    )

    # Snapshot identity
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

    # Snapshot commercial values
    quantity = models.PositiveIntegerField(
        validators=[MinValueValidator(1)],
    )

    unit_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
    )

    tax_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )

    tax_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )

    discount_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )

    line_total = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
    )

    currency = models.CharField(
        max_length=3,
        default="INR",
    )

    # Useful operational snapshot
    weight = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    class Meta:
        ordering = ["id"]
        verbose_name = "Order Item"
        verbose_name_plural = "Order Items"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(quantity__gte=1),
                name="order_item_quantity_gte_1",
            ),
            models.CheckConstraint(
                condition=models.Q(unit_price__gte=0),
                name="order_item_unit_price_gte_0",
            ),
            models.CheckConstraint(
                condition=models.Q(line_total__gte=0),
                name="order_item_line_total_gte_0",
            ),
        ]
        indexes = [
            models.Index(
                fields=["order", "sku"],
                name="order_item_order_sku_idx",
            ),
            models.Index(
                fields=["variant"],
                name="order_item_variant_idx",
            ),
        ]

    def __str__(self):
        return f"{self.order.order_number} · {self.sku} × {self.quantity}"


# ==========================================================
# PAYMENT
# ==========================================================


class Payment(models.Model):
    """
    Gateway-independent payment record.

    Razorpay (or another provider) identifiers live here instead of directly
    on Order, allowing retries, refunds and future gateway changes without
    polluting the order domain.

    The actual payment state transition must be performed inside a service
    transaction after signature/webhook verification.
    """

    class Status(models.TextChoices):
        CREATED = "created", "Created"
        PENDING = "pending", "Pending"
        AUTHORIZED = "authorized", "Authorized"
        CAPTURED = "captured", "Captured"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"
        REFUND_PENDING = "refund_pending", "Refund Pending"
        PARTIALLY_REFUNDED = "partially_refunded", "Partially Refunded"
        REFUNDED = "refunded", "Refunded"

    order = models.ForeignKey(
        Order,
        on_delete=models.PROTECT,
        related_name="payments",
    )

    provider = models.CharField(
        max_length=50,
        default="razorpay",
        db_index=True,
    )

    status = models.CharField(
        max_length=30,
        choices=Status.choices,
        default=Status.CREATED,
        db_index=True,
    )

    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
    )

    currency = models.CharField(
        max_length=3,
        default="INR",
    )

    provider_order_id = models.CharField(
        max_length=255,
        blank=True,
        db_index=True,
    )

    provider_payment_id = models.CharField(
        max_length=255,
        blank=True,
        db_index=True,
    )

    provider_signature = models.CharField(
        max_length=512,
        blank=True,
    )

    failure_code = models.CharField(
        max_length=120,
        blank=True,
    )

    failure_message = models.TextField(
        blank=True,
    )

    raw_metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Non-secret gateway response metadata. Never store secrets.",
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    paid_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    failed_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    class Meta:
        ordering = ["-created_at"]

        indexes = [
            models.Index(
                fields=["order", "-created_at"],
                name="payment_order_created_idx",
            ),
            models.Index(
                fields=["provider", "provider_order_id"],
                name="payment_provider_order_idx",
            ),
            models.Index(
                fields=["provider", "provider_payment_id"],
                name="payment_provider_payment_idx",
            ),
        ]

        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gte=0),
                name="payment_amount_gte_0",
            ),

            # A real gateway order ID must identify only one payment
            # for a given provider.
            models.UniqueConstraint(
                fields=["provider", "provider_order_id"],
                condition=~models.Q(provider_order_id=""),
                name="payment_provider_order_uniq",
            ),

            # A real gateway payment ID must never belong to two payment rows.
            models.UniqueConstraint(
                fields=["provider", "provider_payment_id"],
                condition=~models.Q(provider_payment_id=""),
                name="payment_provider_payment_uniq",
            ),

            # One order can never have two independently captured payments.
            # NOTE: nested Meta class bodies cannot see names bound in the
            # enclosing Payment class body (Status), so we use the literal
            # value here rather than Status.CAPTURED.
            models.UniqueConstraint(
                fields=["order"],
                condition=models.Q(status="captured"),
                name="payment_order_captured_uniq",
            ),
        ]

        verbose_name = "Payment"
        verbose_name_plural = "Payments"

    def __str__(self):
        return f"{self.order.order_number} · {self.provider} · {self.status}"


# ==========================================================
# PAYMENT EVENT / WEBHOOK DEDUPLICATION
# ==========================================================


class PaymentEvent(models.Model):
    """
    Durable webhook/event ledger.

    `provider_event_id` is unique so the same webhook can safely be delivered
    multiple times without double-processing the order/payment.

    This model intentionally stores a compact payload snapshot for audit and
    debugging. Sensitive credentials/tokens must never be stored here.
    """

    class ProcessingStatus(models.TextChoices):
        RECEIVED = "received", "Received"
        PROCESSED = "processed", "Processed"
        IGNORED = "ignored", "Ignored"
        FAILED = "failed", "Failed"

    provider = models.CharField(
        max_length=50,
        default="razorpay",
        db_index=True,
    )

    provider_event_id = models.CharField(
        max_length=255,
        unique=True,
        help_text="Provider webhook/event ID; deduplication key.",
    )

    event_type = models.CharField(
        max_length=120,
        db_index=True,
    )

    processing_status = models.CharField(
        max_length=20,
        choices=ProcessingStatus.choices,
        default=ProcessingStatus.RECEIVED,
        db_index=True,
    )

    payment = models.ForeignKey(
        Payment,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="events",
    )

    payload = models.JSONField(
        default=dict,
        blank=True,
        help_text="Webhook payload snapshot after authenticity verification.",
    )

    error_message = models.TextField(
        blank=True,
    )

    received_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
    )

    processed_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    class Meta:
        ordering = ["-received_at"]
        indexes = [
            models.Index(
                fields=["provider", "event_type", "-received_at"],
                name="pay_event_provider_type_idx",
            ),
            models.Index(
                fields=["processing_status", "-received_at"],
                name="pay_event_status_received_idx",
            ),
        ]
        verbose_name = "Payment Event"
        verbose_name_plural = "Payment Events"

    def __str__(self):
        return f"{self.provider} · {self.event_type} · {self.provider_event_id}"


# ==========================================================
# INVENTORY RESERVATION
# ==========================================================


class InventoryReservation(models.Model):
    """
    Temporary stock reservation associated with an order.

    Reservation is separate from the catalog stock field so payment/order
    workflows can reserve stock without prematurely mutating final inventory.

    Actual stock decrement/release belongs in the order service under an
    atomic transaction with ProductVariant row locking.
    """

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        CONSUMED = "consumed", "Consumed"
        RELEASED = "released", "Released"
        EXPIRED = "expired", "Expired"

    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name="inventory_reservations",
    )

    variant = models.ForeignKey(
        ProductVariant,
        on_delete=models.PROTECT,
        related_name="inventory_reservations",
    )

    quantity = models.PositiveIntegerField(
        validators=[MinValueValidator(1)],
    )

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.ACTIVE,
        db_index=True,
    )

    expires_at = models.DateTimeField(
        db_index=True,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    consumed_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    released_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    class Meta:
        ordering = ["id"]
        indexes = [
            models.Index(
                fields=["variant", "status", "expires_at"],
                name="inv_variant_status_exp_idx",
            ),
            models.Index(
                fields=["order", "status"],
                name="inventory_order_status_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(quantity__gte=1),
                name="inventory_reservation_qty_gte_1",
            ),
        ]
        verbose_name = "Inventory Reservation"
        verbose_name_plural = "Inventory Reservations"

    def __str__(self):
        return (
            f"{self.order.order_number} · "
            f"{self.variant.sku} × {self.quantity} · {self.status}"
        )