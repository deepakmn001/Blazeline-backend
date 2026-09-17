from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from .models import Promotion, PromotionRule


class PromotionRuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = PromotionRule
        fields = (
            "id",
            "promotion",
            "target_type",
            "category",
            "subcategory",
            "brand",
            "brand_normalized",
            "product",
            "variant",
            "created_at",
            "updated_at",
        )
        read_only_fields = (
            "id",
            "brand_normalized",
            "created_at",
            "updated_at",
        )

    def validate(self, attrs):
        instance = self.instance or PromotionRule()

        for field, value in attrs.items():
            setattr(instance, field, value)

        try:
            instance.full_clean()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict)

        return attrs


class PromotionRuleReadSerializer(serializers.ModelSerializer):
    class Meta:
        model = PromotionRule
        fields = (
            "id",
            "target_type",
            "category",
            "subcategory",
            "brand",
            "brand_normalized",
            "product",
            "variant",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class PromotionSerializer(serializers.ModelSerializer):
    rules = PromotionRuleReadSerializer(
        many=True,
        read_only=True,
    )

    class Meta:
        model = Promotion
        fields = (
            "id",
            "name",
            "description",
            "discount_type",
            "discount_value",
            "max_discount_amount",
            "minimum_cart_value",
            "is_active",
            "start_at",
            "end_at",
            "priority",
            "stackable",
            "is_currently_active",
            "rules",
            "created_at",
            "updated_at",
        )
        read_only_fields = (
            "id",
            "is_currently_active",
            "rules",
            "created_at",
            "updated_at",
        )

    def validate(self, attrs):
        instance = self.instance or Promotion()

        for field, value in attrs.items():
            setattr(instance, field, value)

        try:
            instance.full_clean()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict)

        return attrs