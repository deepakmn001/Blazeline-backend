from __future__ import annotations

from django.core.validators import RegexValidator
from rest_framework import serializers

from .models import Order, OrderItem, Payment


PHONE_VALIDATOR = RegexValidator(
    regex=r"^\d{10}$",
    message="Enter a valid 10-digit phone number.",
)

PINCODE_VALIDATOR = RegexValidator(
    regex=r"^\d{6}$",
    message="Enter a valid 6-digit pincode.",
)

GSTIN_VALIDATOR = RegexValidator(
    regex=r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$",
    message="Enter a valid 15-character GSTIN.",
)


# ============================================================================
# CHECKOUT REQUEST
# ============================================================================


class ShippingAddressSerializer(serializers.Serializer):
    full_name = serializers.CharField(
        max_length=150,
        trim_whitespace=True,
    )

    phone = serializers.CharField(
        max_length=15,
        validators=[PHONE_VALIDATOR],
    )

    email = serializers.EmailField()

    company = serializers.CharField(
        max_length=200,
        required=False,
        allow_blank=True,
        default="",
    )

    gstin = serializers.CharField(
        max_length=15,
        required=False,
        allow_blank=True,
        default="",
    )

    address_line1 = serializers.CharField(
        max_length=255,
        trim_whitespace=True,
    )

    address_line2 = serializers.CharField(
        max_length=255,
        required=False,
        allow_blank=True,
        default="",
    )

    landmark = serializers.CharField(
        max_length=255,
        required=False,
        allow_blank=True,
        default="",
    )

    city = serializers.CharField(
        max_length=100,
        trim_whitespace=True,
    )

    state = serializers.CharField(
        max_length=100,
        trim_whitespace=True,
    )

    pincode = serializers.CharField(
        max_length=6,
        validators=[PINCODE_VALIDATOR],
    )

    def validate_phone(self, value: str) -> str:
        return "".join(
            character
            for character in value
            if character.isdigit()
        )

    def validate_gstin(self, value: str) -> str:
        value = value.strip().upper()

        if value and len(value) != 15:
            raise serializers.ValidationError(
                "GSTIN must contain exactly 15 characters."
            )

        if value:
            GSTIN_VALIDATOR(value)

        return value

    def validate_full_name(self, value: str) -> str:
        value = value.strip()

        if len(value) < 2:
            raise serializers.ValidationError(
                "Enter a valid full name."
            )

        return value

    def validate_city(self, value: str) -> str:
        value = value.strip()

        if not value:
            raise serializers.ValidationError(
                "City is required."
            )

        return value

    def validate_state(self, value: str) -> str:
        value = value.strip()

        if not value:
            raise serializers.ValidationError(
                "State is required."
            )

        return value


class CreateOrderSerializer(serializers.Serializer):
    """
    Public checkout request.

    SECURITY RULE:
    The client does NOT submit:
        subtotal
        delivery_charge
        tax_amount
        grand_total
        unit_price
        stock
        discount

    Those are server-authoritative values calculated inside the order service.
    """

    payment_method = serializers.ChoiceField(
        choices=Order.PaymentMethod.choices,
    )

    shipping = ShippingAddressSerializer()

    notes = serializers.CharField(
        max_length=2000,
        required=False,
        allow_blank=True,
        default="",
    )

    currency = serializers.CharField(
        max_length=3,
        required=False,
        default="INR",
    )

    def validate_currency(self, value: str) -> str:
        value = value.strip().upper()

        # BlazeLine currently operates in INR.
        # Keeping this explicit prevents a client from changing monetary
        # interpretation by submitting an arbitrary currency.
        if value != "INR":
            raise serializers.ValidationError(
                "Only INR orders are currently supported."
            )

        return value

    def validate(self, attrs):
        shipping = attrs["shipping"]
        payment_method = attrs["payment_method"]

        gstin = (
            shipping.get("gstin") or ""
        ).strip().upper()

        # A GSTIN is only meaningful when the customer actually supplies one.
        shipping["gstin"] = gstin

        if payment_method == Order.PaymentMethod.CREDIT:
            company = (
                shipping.get("company") or ""
            ).strip()

            if not company:
                raise serializers.ValidationError(
                    {
                        "shipping": {
                            "company": (
                                "Company name is required for credit terms."
                            )
                        }
                    }
                )

        return attrs


# ============================================================================
# ORDER READ SERIALIZERS
# ============================================================================


class OrderItemSerializer(
    serializers.ModelSerializer
):
    class Meta:
        model = OrderItem
        fields = [
            "id",
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
            "weight",
        ]
        read_only_fields = fields


class PaymentSerializer(
    serializers.ModelSerializer
):
    class Meta:
        model = Payment
        fields = [
            "id",
            "provider",
            "status",
            "amount",
            "currency",
            "provider_order_id",
            "provider_payment_id",
            "failure_code",
            "failure_message",
            "created_at",
            "updated_at",
            "paid_at",
            "failed_at",
        ]

        # Provider identifiers are server-owned.
        read_only_fields = fields


class OrderSerializer(
    serializers.ModelSerializer
):
    items = OrderItemSerializer(
        many=True,
        read_only=True,
    )

    payments = PaymentSerializer(
        many=True,
        read_only=True,
    )

    class Meta:
        model = Order

        fields = [
            "id",
            "order_number",
            "status",
            "payment_status",
            "payment_method",
            "currency",

            "subtotal",
            "discount_amount",
            "delivery_charge",
            "tax_amount",
            "cod_fee",
            "grand_total",

            "shipping_full_name",
            "shipping_phone",
            "shipping_email",
            "shipping_company",
            "shipping_gstin",
            "shipping_address_line1",
            "shipping_address_line2",
            "shipping_landmark",
            "shipping_city",
            "shipping_state",
            "shipping_pincode",

            "delivery_zone_id",
            "delivery_zone_name",
            "delivery_breakdown",

            "notes",

            "created_at",
            "updated_at",
            "paid_at",
            "cancelled_at",
            "delivered_at",

            "items",
            "payments",
        ]

        read_only_fields = fields


class OrderListSerializer(
    serializers.ModelSerializer
):
    item_count = serializers.IntegerField(
        source="items.count",
        read_only=True,
    )

    class Meta:
        model = Order

        fields = [
            "id",
            "order_number",
            "status",
            "payment_status",
            "payment_method",
            "currency",
            "grand_total",
            "item_count",
            "created_at",
            "paid_at",
        ]

        read_only_fields = fields