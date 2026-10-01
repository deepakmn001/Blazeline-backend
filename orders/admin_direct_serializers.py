from rest_framework import serializers


class DirectOrderCustomerSerializer(serializers.Serializer):
    full_name = serializers.CharField(
        max_length=150,
        allow_blank=False,
    )
    phone = serializers.CharField(
        max_length=15,
        required=False,
        allow_blank=True,
    )
    email = serializers.EmailField(
        required=False,
        allow_blank=True,
    )


class DirectOrderShippingSerializer(serializers.Serializer):
    full_name = serializers.CharField(
        max_length=150,
        allow_blank=False,
    )
    phone = serializers.CharField(
        max_length=15,
        allow_blank=False,
    )
    email = serializers.EmailField(
        allow_blank=False,
    )
    company = serializers.CharField(
        max_length=200,
        required=False,
        allow_blank=True,
    )
    gstin = serializers.CharField(
        max_length=15,
        required=False,
        allow_blank=True,
    )
    address_line1 = serializers.CharField(
        max_length=255,
        allow_blank=False,
    )
    address_line2 = serializers.CharField(
        max_length=255,
        required=False,
        allow_blank=True,
    )
    landmark = serializers.CharField(
        max_length=255,
        required=False,
        allow_blank=True,
    )
    city = serializers.CharField(
        max_length=100,
        allow_blank=False,
    )
    state = serializers.CharField(
        max_length=100,
        allow_blank=False,
    )
    pincode = serializers.CharField(
        max_length=6,
        min_length=6,
        allow_blank=False,
    )

    def validate_pincode(self, value):
        if not value.isdigit():
            raise serializers.ValidationError(
                "Pincode must contain exactly 6 digits."
            )
        return value


class DirectOrderItemSerializer(serializers.Serializer):
    product_name = serializers.CharField(
        max_length=255,
        allow_blank=False,
    )
    sku = serializers.CharField(
        max_length=120,
        required=False,
        allow_blank=True,
    )
    variant_name = serializers.CharField(
        max_length=255,
        required=False,
        allow_blank=True,
    )
    quantity = serializers.IntegerField(
        min_value=1,
    )
    rate = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        min_value=0,
    )
    discount_percent = serializers.DecimalField(
        max_digits=5,
        decimal_places=2,
        min_value=0,
        default=0,
    )
    tax_rate = serializers.DecimalField(
        max_digits=5,
        decimal_places=2,
        min_value=0,
        default=18,
    )

    def validate_discount_percent(self, value):
        if value > 100:
            raise serializers.ValidationError(
                "Discount cannot exceed 100%."
            )
        return value

    def validate_tax_rate(self, value):
        if value > 100:
            raise serializers.ValidationError(
                "Tax rate cannot exceed 100%."
            )
        return value


class DirectOrderCreateSerializer(serializers.Serializer):
    customer = DirectOrderCustomerSerializer()

    shipping = DirectOrderShippingSerializer()

    items = DirectOrderItemSerializer(
        many=True,
        allow_empty=False,
    )

    delivery_charge = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        min_value=0,
        required=False,
        default=0,
    )

    currency = serializers.CharField(
        max_length=3,
        required=False,
        default="INR",
    )

    notes = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
    )

    def validate_currency(self, value):
        value = value.strip().upper()

        if len(value) != 3:
            raise serializers.ValidationError(
                "Currency must be a 3-letter code."
            )

        if value != "INR":
            raise serializers.ValidationError(
                "Direct Orders currently support INR only."
            )

        return value

    def validate_customer(self, value):
        if not value.get("full_name", "").strip():
            raise serializers.ValidationError(
                "Customer name is required."
            )

        if not (
            value.get("email", "").strip()
            or value.get("phone", "").strip()
        ):
            raise serializers.ValidationError(
                "Customer email or phone is required."
            )

        return value

    def validate(self, attrs):
        items = attrs.get("items") or []

        if len(items) > 100:
            raise serializers.ValidationError({
                "items": "A maximum of 100 items is allowed per order."
            })

        return attrs


class DirectOrderResponseSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    order_number = serializers.CharField()
    source = serializers.CharField()
    status = serializers.CharField()
    payment_status = serializers.CharField()
    payment_method = serializers.CharField()

    currency = serializers.CharField()

    subtotal = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
    )
    discount_amount = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
    )
    delivery_charge = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
    )
    tax_amount = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
    )
    grand_total = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
    )

    shipping_full_name = serializers.CharField()
    shipping_phone = serializers.CharField()
    shipping_email = serializers.CharField()

    created_at = serializers.DateTimeField()