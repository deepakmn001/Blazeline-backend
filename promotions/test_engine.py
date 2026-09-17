from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone

from catalog.models import Category, Product, ProductVariant, SubCategory

from .models import Promotion, PromotionRule
from .services import (
    PromotionEngine,
    PromotionLineInput,
    PromotionCalculationError,
)


class PromotionTestMixin:
    """
    Shared catalog/promotion factories for promotion-engine tests.
    """

    def create_category(
        self,
        *,
        name: str = "Electricals",
        slug: str = "electricals",
    ) -> Category:
        return Category.objects.create(
            name=name,
            slug=slug,
        )

    def create_subcategory(
        self,
        *,
        category: Category,
        name: str = "Wires",
        slug: str = "wires",
    ) -> SubCategory:
        return SubCategory.objects.create(
            category=category,
            name=name,
            slug=slug,
        )

    def create_product(
        self,
        *,
        category: Category,
        subcategory: SubCategory,
        name: str = "Finolex Wire",
        slug: str = "finolex-wire",
        brand: str = "Finolex",
    ) -> Product:
        return Product.objects.create(
            category=category,
            subcategory=subcategory,
            name=name,
            slug=slug,
            brand=brand,
        )

    def create_variant(
        self,
        *,
        product: Product,
        sku: str = "FIN-WIRE-001",
        mrp: str = "1500.00",
        selling_price: str = "1000.00",
    ) -> ProductVariant:
        return ProductVariant.objects.create(
            product=product,
            sku=sku,
            mrp=Decimal(mrp),
            selling_price=Decimal(selling_price),
        )

    def create_promotion(
        self,
        *,
        name: str = "Test Promotion",
        discount_type: str = Promotion.DiscountType.PERCENTAGE,
        discount_value: str = "10.00",
        max_discount_amount: str | None = None,
        minimum_cart_value: str = "0.00",
        priority: int = 0,
        stackable: bool = False,
        start_at=None,
        end_at=None,
        is_active: bool = True,
    ) -> Promotion:
        now = timezone.now()

        if start_at is None:
            start_at = now - timedelta(hours=1)

        if end_at is None:
            end_at = now + timedelta(hours=1)

        return Promotion.objects.create(
            name=name,
            discount_type=discount_type,
            discount_value=Decimal(discount_value),
            max_discount_amount=(
                Decimal(max_discount_amount)
                if max_discount_amount is not None
                else None
            ),
            minimum_cart_value=Decimal(minimum_cart_value),
            priority=priority,
            stackable=stackable,
            start_at=start_at,
            end_at=end_at,
            is_active=is_active,
        )

    def add_rule(
        self,
        promotion: Promotion,
        *,
        target_type: str,
        category: Category | None = None,
        subcategory: SubCategory | None = None,
        product: Product | None = None,
        variant: ProductVariant | None = None,
        brand: str | None = None,
    ) -> PromotionRule:
        return PromotionRule.objects.create(
            promotion=promotion,
            target_type=target_type,
            category=category,
            subcategory=subcategory,
            product=product,
            variant=variant,
            brand=brand,
        )

    def calculate_single_variant(
        self,
        variant: ProductVariant,
        quantity: int = 1,
    ):
        return PromotionEngine.calculate(
            [
                PromotionLineInput(
                    variant=variant,
                    quantity=quantity,
                )
            ]
        )


class PromotionModelTests(
    PromotionTestMixin,
    TestCase,
):
    def setUp(self):
        self.category = self.create_category()
        self.subcategory = self.create_subcategory(
            category=self.category,
        )
        self.product = self.create_product(
            category=self.category,
            subcategory=self.subcategory,
        )
        self.variant = self.create_variant(
            product=self.product,
        )

    def test_percentage_discount_constraint(self):
        promotion = self.create_promotion(
            discount_value="15.00",
        )

        self.assertEqual(
            promotion.discount_value,
            Decimal("15.00"),
        )

    def test_percentage_above_100_is_rejected_by_model_validation(self):
        now = timezone.now()

        promotion = Promotion(
            name="Invalid Percentage Promotion",
            discount_type=Promotion.DiscountType.PERCENTAGE,
            discount_value=Decimal("101.00"),
            minimum_cart_value=Decimal("0.00"),
            start_at=now - timedelta(hours=1),
            end_at=now + timedelta(hours=1),
            is_active=True,
        )

        with self.assertRaises(ValidationError):
            promotion.full_clean()

    def test_database_rejects_percentage_above_100(self):
        now = timezone.now()

        promotion = Promotion(
            name="Invalid DB Promotion",
            discount_type=Promotion.DiscountType.PERCENTAGE,
            discount_value=Decimal("101.00"),
            minimum_cart_value=Decimal("0.00"),
            start_at=now - timedelta(hours=1),
            end_at=now + timedelta(hours=1),
            is_active=True,
        )

        with self.assertRaises(IntegrityError):
            promotion.save(force_insert=True)

    def test_end_time_must_be_after_start_time(self):
        now = timezone.now()

        promotion = self.create_promotion(
            start_at=now + timedelta(hours=2),
            end_at=now + timedelta(hours=1),
        )

        with self.assertRaises(ValidationError):
            promotion.full_clean()

    def test_brand_normalized_is_persisted_automatically(self):
        promotion = self.create_promotion(
            name="Brand Sale",
        )

        rule = self.add_rule(
            promotion,
            target_type=PromotionRule.TargetType.BRAND,
            brand="  FiNoLeX  ",
        )

        rule.refresh_from_db()

        self.assertEqual(
            rule.brand,
            "  FiNoLeX  ",
        )

        self.assertEqual(
            rule.brand_normalized,
            "finolex",
        )

    def test_non_brand_rule_has_no_brand_normalized_value(self):
        promotion = self.create_promotion()

        rule = self.add_rule(
            promotion,
            target_type=PromotionRule.TargetType.PRODUCT,
            product=self.product,
        )

        rule.refresh_from_db()

        self.assertIsNone(
            rule.brand_normalized,
        )


class PromotionEngineBasicTests(
    PromotionTestMixin,
    TestCase,
):
    def setUp(self):
        self.category = self.create_category()

        self.subcategory = self.create_subcategory(
            category=self.category,
        )

        self.product = self.create_product(
            category=self.category,
            subcategory=self.subcategory,
        )

        self.variant = self.create_variant(
            product=self.product,
            selling_price="1000.00",
        )

    def test_no_promotion_returns_original_price(self):
        result = self.calculate_single_variant(
            self.variant,
        )

        self.assertEqual(
            result.subtotal,
            Decimal("1000.00"),
        )
        self.assertEqual(
            result.discount_amount,
            Decimal("0.00"),
        )
        self.assertEqual(
            result.discounted_subtotal,
            Decimal("1000.00"),
        )

        line = result.lines[0]

        self.assertEqual(
            line.base_unit_price,
            Decimal("1000.00"),
        )
        self.assertEqual(
            line.final_unit_price,
            Decimal("1000.00"),
        )
        self.assertFalse(
            line.has_discount,
        )

    def test_percentage_discount(self):
        promotion = self.create_promotion(
            discount_value="10.00",
        )

        self.add_rule(
            promotion,
            target_type=PromotionRule.TargetType.ALL,
        )

        result = self.calculate_single_variant(
            self.variant,
        )

        self.assertEqual(
            result.discount_amount,
            Decimal("100.00"),
        )
        self.assertEqual(
            result.discounted_subtotal,
            Decimal("900.00"),
        )

        line = result.lines[0]

        self.assertEqual(
            line.final_unit_price,
            Decimal("900.00"),
        )
        self.assertEqual(
            line.discount_percent,
            Decimal("10.00"),
        )
        self.assertEqual(
            line.discount_label,
            "10% OFF",
        )

    def test_fixed_discount_is_per_unit(self):
        promotion = self.create_promotion(
            discount_type=Promotion.DiscountType.FIXED_AMOUNT,
            discount_value="100.00",
        )

        self.add_rule(
            promotion,
            target_type=PromotionRule.TargetType.ALL,
        )

        result = self.calculate_single_variant(
            self.variant,
            quantity=3,
        )

        self.assertEqual(
            result.subtotal,
            Decimal("3000.00"),
        )

        self.assertEqual(
            result.discount_amount,
            Decimal("300.00"),
        )

        self.assertEqual(
            result.discounted_subtotal,
            Decimal("2700.00"),
        )

    def test_discount_cannot_exceed_line_subtotal(self):
        promotion = self.create_promotion(
            discount_type=Promotion.DiscountType.FIXED_AMOUNT,
            discount_value="5000.00",
        )

        self.add_rule(
            promotion,
            target_type=PromotionRule.TargetType.ALL,
        )

        result = self.calculate_single_variant(
            self.variant,
        )

        self.assertEqual(
            result.discount_amount,
            Decimal("1000.00"),
        )

        self.assertEqual(
            result.discounted_subtotal,
            Decimal("0.00"),
        )

    def test_inactive_promotion_is_ignored(self):
        promotion = self.create_promotion(
            discount_value="50.00",
            is_active=False,
        )

        self.add_rule(
            promotion,
            target_type=PromotionRule.TargetType.ALL,
        )

        result = self.calculate_single_variant(
            self.variant,
        )

        self.assertEqual(
            result.discount_amount,
            Decimal("0.00"),
        )

    def test_future_promotion_is_ignored(self):
        now = timezone.now()

        promotion = self.create_promotion(
            discount_value="50.00",
            start_at=now + timedelta(hours=1),
            end_at=now + timedelta(hours=2),
        )

        self.add_rule(
            promotion,
            target_type=PromotionRule.TargetType.ALL,
        )

        result = self.calculate_single_variant(
            self.variant,
        )

        self.assertEqual(
            result.discount_amount,
            Decimal("0.00"),
        )

    def test_expired_promotion_is_ignored(self):
        now = timezone.now()

        promotion = self.create_promotion(
            discount_value="50.00",
            start_at=now - timedelta(hours=2),
            end_at=now - timedelta(hours=1),
        )

        self.add_rule(
            promotion,
            target_type=PromotionRule.TargetType.ALL,
        )

        result = self.calculate_single_variant(
            self.variant,
        )

        self.assertEqual(
            result.discount_amount,
            Decimal("0.00"),
        )

    def test_minimum_cart_value(self):
        promotion = self.create_promotion(
            discount_value="20.00",
            minimum_cart_value="1500.00",
        )

        self.add_rule(
            promotion,
            target_type=PromotionRule.TargetType.ALL,
        )

        result = self.calculate_single_variant(
            self.variant,
        )

        self.assertEqual(
            result.discount_amount,
            Decimal("0.00"),
        )

        result = PromotionEngine.calculate(
            [
                PromotionLineInput(
                    variant=self.variant,
                    quantity=2,
                )
            ]
        )

        self.assertEqual(
            result.subtotal,
            Decimal("2000.00"),
        )

        self.assertEqual(
            result.discount_amount,
            Decimal("400.00"),
        )


class PromotionTargetingTests(
    PromotionTestMixin,
    TestCase,
):
    def setUp(self):
        self.category = self.create_category()

        self.subcategory = self.create_subcategory(
            category=self.category,
        )

        self.product = self.create_product(
            category=self.category,
            subcategory=self.subcategory,
            brand="Finolex",
        )

        self.variant = self.create_variant(
            product=self.product,
            selling_price="1000.00",
        )

    def test_category_target(self):
        promotion = self.create_promotion(
            discount_value="10.00",
        )

        self.add_rule(
            promotion,
            target_type=PromotionRule.TargetType.CATEGORY,
            category=self.category,
        )

        result = self.calculate_single_variant(
            self.variant,
        )

        self.assertEqual(
            result.discount_amount,
            Decimal("100.00"),
        )

    def test_subcategory_target(self):
        promotion = self.create_promotion(
            discount_value="15.00",
        )

        self.add_rule(
            promotion,
            target_type=PromotionRule.TargetType.SUBCATEGORY,
            subcategory=self.subcategory,
        )

        result = self.calculate_single_variant(
            self.variant,
        )

        self.assertEqual(
            result.discount_amount,
            Decimal("150.00"),
        )

    def test_brand_target_is_case_insensitive(self):
        promotion = self.create_promotion(
            discount_value="20.00",
        )

        self.add_rule(
            promotion,
            target_type=PromotionRule.TargetType.BRAND,
            brand=" FINOLEX ",
        )

        result = self.calculate_single_variant(
            self.variant,
        )

        self.assertEqual(
            result.discount_amount,
            Decimal("200.00"),
        )

    def test_product_target(self):
        promotion = self.create_promotion(
            discount_value="25.00",
        )

        self.add_rule(
            promotion,
            target_type=PromotionRule.TargetType.PRODUCT,
            product=self.product,
        )

        result = self.calculate_single_variant(
            self.variant,
        )

        self.assertEqual(
            result.discount_amount,
            Decimal("250.00"),
        )

    def test_variant_target(self):
        promotion = self.create_promotion(
            discount_value="30.00",
        )

        self.add_rule(
            promotion,
            target_type=PromotionRule.TargetType.VARIANT,
            variant=self.variant,
        )

        result = self.calculate_single_variant(
            self.variant,
        )

        self.assertEqual(
            result.discount_amount,
            Decimal("300.00"),
        )


class PromotionSpecificityTests(
    PromotionTestMixin,
    TestCase,
):
    def setUp(self):
        self.category = self.create_category()

        self.subcategory = self.create_subcategory(
            category=self.category,
        )

        self.product = self.create_product(
            category=self.category,
            subcategory=self.subcategory,
            brand="Finolex",
        )

        self.variant = self.create_variant(
            product=self.product,
            selling_price="1000.00",
        )

    def test_most_specific_target_wins(self):
        all_promotion = self.create_promotion(
            name="All 10",
            discount_value="10.00",
        )
        self.add_rule(
            all_promotion,
            target_type=PromotionRule.TargetType.ALL,
        )

        category_promotion = self.create_promotion(
            name="Category 15",
            discount_value="15.00",
        )
        self.add_rule(
            category_promotion,
            target_type=PromotionRule.TargetType.CATEGORY,
            category=self.category,
        )

        subcategory_promotion = self.create_promotion(
            name="Subcategory 20",
            discount_value="20.00",
        )
        self.add_rule(
            subcategory_promotion,
            target_type=PromotionRule.TargetType.SUBCATEGORY,
            subcategory=self.subcategory,
        )

        brand_promotion = self.create_promotion(
            name="Brand 25",
            discount_value="25.00",
        )
        self.add_rule(
            brand_promotion,
            target_type=PromotionRule.TargetType.BRAND,
            brand="Finolex",
        )

        product_promotion = self.create_promotion(
            name="Product 30",
            discount_value="30.00",
        )
        self.add_rule(
            product_promotion,
            target_type=PromotionRule.TargetType.PRODUCT,
            product=self.product,
        )

        variant_promotion = self.create_promotion(
            name="Variant 35",
            discount_value="35.00",
        )
        self.add_rule(
            variant_promotion,
            target_type=PromotionRule.TargetType.VARIANT,
            variant=self.variant,
        )

        result = self.calculate_single_variant(
            self.variant,
        )

        self.assertEqual(
            result.discount_amount,
            Decimal("350.00"),
        )

        self.assertEqual(
            result.applied_promotion_names,
            ("Variant 35",),
        )

    def test_higher_priority_wins_same_specificity(self):
        low = self.create_promotion(
            name="Low Priority",
            discount_value="10.00",
            priority=10,
        )
        self.add_rule(
            low,
            target_type=PromotionRule.TargetType.PRODUCT,
            product=self.product,
        )

        high = self.create_promotion(
            name="High Priority",
            discount_value="20.00",
            priority=100,
        )
        self.add_rule(
            high,
            target_type=PromotionRule.TargetType.PRODUCT,
            product=self.product,
        )

        result = self.calculate_single_variant(
            self.variant,
        )

        self.assertEqual(
            result.discount_amount,
            Decimal("200.00"),
        )

        self.assertEqual(
            result.applied_promotion_names,
            ("High Priority",),
        )


class PromotionCapAndStackingTests(
    PromotionTestMixin,
    TestCase,
):
    def setUp(self):
        self.category = self.create_category()

        self.subcategory = self.create_subcategory(
            category=self.category,
        )

        self.product = self.create_product(
            category=self.category,
            subcategory=self.subcategory,
        )

        self.variant = self.create_variant(
            product=self.product,
            selling_price="1000.00",
        )

    def test_max_discount_cap(self):
        promotion = self.create_promotion(
            discount_value="50.00",
            max_discount_amount="100.00",
        )

        self.add_rule(
            promotion,
            target_type=PromotionRule.TargetType.ALL,
        )

        result = self.calculate_single_variant(
            self.variant,
        )

        self.assertEqual(
            result.discount_amount,
            Decimal("100.00"),
        )

    def test_non_stackable_promotion_prevents_combination(self):
        first = self.create_promotion(
            name="First",
            discount_value="20.00",
            priority=100,
            stackable=False,
        )

        self.add_rule(
            first,
            target_type=PromotionRule.TargetType.ALL,
        )

        second = self.create_promotion(
            name="Second",
            discount_value="30.00",
            priority=50,
            stackable=False,
        )

        self.add_rule(
            second,
            target_type=PromotionRule.TargetType.ALL,
        )

        result = self.calculate_single_variant(
            self.variant,
        )

        self.assertEqual(
            result.discount_amount,
            Decimal("200.00"),
        )

        self.assertEqual(
            result.applied_promotion_names,
            ("First",),
        )

    def test_stackable_promotions_can_combine_when_both_are_stackable(self):
        first = self.create_promotion(
            name="First",
            discount_value="10.00",
            priority=100,
            stackable=True,
        )

        self.add_rule(
            first,
            target_type=PromotionRule.TargetType.ALL,
        )

        second = self.create_promotion(
            name="Second",
            discount_value="20.00",
            priority=50,
            stackable=True,
        )

        self.add_rule(
            second,
            target_type=PromotionRule.TargetType.ALL,
        )

        result = self.calculate_single_variant(
            self.variant,
        )

        self.assertEqual(
            result.discount_amount,
            Decimal("300.00"),
        )

        self.assertEqual(
            set(result.applied_promotion_names),
            {"First", "Second"},
        )

    def test_stackable_promotions_never_discount_below_zero(self):
        first = self.create_promotion(
            name="First",
            discount_type=Promotion.DiscountType.FIXED_AMOUNT,
            discount_value="800.00",
            priority=100,
            stackable=True,
        )

        self.add_rule(
            first,
            target_type=PromotionRule.TargetType.ALL,
        )

        second = self.create_promotion(
            name="Second",
            discount_type=Promotion.DiscountType.FIXED_AMOUNT,
            discount_value="800.00",
            priority=50,
            stackable=True,
        )

        self.add_rule(
            second,
            target_type=PromotionRule.TargetType.ALL,
        )

        result = self.calculate_single_variant(
            self.variant,
        )

        self.assertEqual(
            result.discount_amount,
            Decimal("1000.00"),
        )

        self.assertEqual(
            result.discounted_subtotal,
            Decimal("0.00"),
        )


class PromotionMultiLineTests(
    PromotionTestMixin,
    TestCase,
):
    def setUp(self):
        self.category = self.create_category()

        self.subcategory = self.create_subcategory(
            category=self.category,
        )

        self.product_one = self.create_product(
            category=self.category,
            subcategory=self.subcategory,
            name="Product One",
            slug="product-one",
            brand="Brand One",
        )

        self.product_two = self.create_product(
            category=self.category,
            subcategory=self.subcategory,
            name="Product Two",
            slug="product-two",
            brand="Brand Two",
        )

        self.variant_one = self.create_variant(
            product=self.product_one,
            sku="SKU-ONE",
            selling_price="1000.00",
        )

        self.variant_two = self.create_variant(
            product=self.product_two,
            sku="SKU-TWO",
            selling_price="2000.00",
        )

    def test_cart_subtotal_and_total_discount_across_multiple_lines(self):
        promotion = self.create_promotion(
            discount_value="10.00",
        )

        self.add_rule(
            promotion,
            target_type=PromotionRule.TargetType.ALL,
        )

        result = PromotionEngine.calculate(
            [
                PromotionLineInput(
                    variant=self.variant_one,
                    quantity=2,
                ),
                PromotionLineInput(
                    variant=self.variant_two,
                    quantity=1,
                ),
            ]
        )

        self.assertEqual(
            result.subtotal,
            Decimal("4000.00"),
        )

        self.assertEqual(
            result.discount_amount,
            Decimal("400.00"),
        )

        self.assertEqual(
            result.discounted_subtotal,
            Decimal("3600.00"),
        )

        self.assertEqual(
            len(result.lines),
            2,
        )

    def test_promotion_level_cap_applies_across_cart_lines(self):
        promotion = self.create_promotion(
            name="Cart capped",
            discount_value="20.00",
            max_discount_amount="300.00",
        )

        self.add_rule(
            promotion,
            target_type=PromotionRule.TargetType.ALL,
        )

        result = PromotionEngine.calculate(
            [
                PromotionLineInput(
                    variant=self.variant_one,
                    quantity=1,
                ),
                PromotionLineInput(
                    variant=self.variant_two,
                    quantity=1,
                ),
            ]
        )

        self.assertEqual(
            result.subtotal,
            Decimal("3000.00"),
        )

        self.assertEqual(
            result.discount_amount,
            Decimal("300.00"),
        )

        self.assertEqual(
            result.discounted_subtotal,
            Decimal("2700.00"),
        )


class PromotionValidationTests(
    PromotionTestMixin,
    TestCase,
):
    def setUp(self):
        self.category = self.create_category()

        self.subcategory = self.create_subcategory(
            category=self.category,
        )

        self.product = self.create_product(
            category=self.category,
            subcategory=self.subcategory,
        )

        self.variant = self.create_variant(
            product=self.product,
        )

    def test_invalid_quantity_is_rejected(self):
        with self.assertRaises(PromotionCalculationError):
            PromotionEngine.calculate(
                [
                    PromotionLineInput(
                        variant=self.variant,
                        quantity=0,
                    )
                ]
            )

    def test_negative_quantity_is_rejected(self):
        with self.assertRaises(PromotionCalculationError):
            PromotionEngine.calculate(
                [
                    PromotionLineInput(
                        variant=self.variant,
                        quantity=-1,
                    )
                ]
            )

    def test_boolean_quantity_is_rejected(self):
        with self.assertRaises(PromotionCalculationError):
            PromotionEngine.calculate(
                [
                    PromotionLineInput(
                        variant=self.variant,
                        quantity=True,
                    )
                ]
            )

    def test_empty_cart_is_safe(self):
        result = PromotionEngine.calculate([])

        self.assertEqual(
            result.subtotal,
            Decimal("0.00"),
        )

        self.assertEqual(
            result.discount_amount,
            Decimal("0.00"),
        )

        self.assertEqual(
            result.discounted_subtotal,
            Decimal("0.00"),
        )

        self.assertEqual(
            result.lines,
            tuple(),
        )