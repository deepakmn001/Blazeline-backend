from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from rest_framework import status
from rest_framework.test import APITestCase

from catalog.models import (
    Category,
    Product,
    ProductVariant,
    SubCategory,
)

from promotions.models import Promotion, PromotionRule


User = get_user_model()


class PromotionAPITestCase(APITestCase):
    """
    Production-oriented API tests for promotion management.

    Existing promotion-engine tests remain untouched.
    These tests verify:
        - admin permission boundaries
        - promotion CRUD
        - promotion validation
        - promotion rule CRUD
        - target validation
        - brand normalization
    """

    def setUp(self):
        now = timezone.now()

        self.admin_user = User.objects.create_user(
            username="promotion_admin",
            password="StrongPassword123!",
            is_staff=True,
            is_superuser=False,
        )

        self.normal_user = User.objects.create_user(
            username="promotion_customer",
            password="StrongPassword123!",
            is_staff=False,
            is_superuser=False,
        )

        self.category = Category.objects.create(
            name="API Test Category",
            slug="api-test-category",
            active=True,
        )

        self.subcategory = SubCategory.objects.create(
            category=self.category,
            name="API Test Subcategory",
            slug="api-test-subcategory",
            active=True,
        )

        self.product = Product.objects.create(
            name="API Test Product",
            slug="api-test-product",
            category=self.category,
            subcategory=self.subcategory,
            status="published",
            active=True,
        )

        self.variant = ProductVariant.objects.create(
            product=self.product,
            sku="API-PROMO-001",
            mrp=Decimal("1000.00"),
            selling_price=Decimal("800.00"),
            stock=20,
            is_default=True,
        )

        self.promotion = Promotion.objects.create(
            name="API Test Promotion",
            description="Promotion API test",
            discount_type=Promotion.DiscountType.PERCENTAGE,
            discount_value=Decimal("10.00"),
            max_discount_amount=None,
            minimum_cart_value=Decimal("0.00"),
            is_active=True,
            start_at=now - timedelta(hours=1),
            end_at=now + timedelta(hours=1),
            priority=100,
            stackable=False,
        )

        self.rule = PromotionRule.objects.create(
            promotion=self.promotion,
            target_type=PromotionRule.TargetType.VARIANT,
            variant=self.variant,
        )

        self.promotion_list_url = reverse("promotion-list")
        self.promotion_detail_url = reverse(
            "promotion-detail",
            kwargs={"pk": self.promotion.pk},
        )

        self.rule_list_url = reverse("promotion-rule-list")
        self.rule_detail_url = reverse(
            "promotion-rule-detail",
            kwargs={"pk": self.rule.pk},
        )

    # ==========================================================
    # AUTHORIZATION
    # ==========================================================

    def test_promotion_list_requires_admin(self):
        response = self.client.get(self.promotion_list_url)

        self.assertEqual(
            response.status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

    def test_normal_authenticated_user_cannot_access_promotions(self):
        self.client.force_authenticate(user=self.normal_user)

        response = self.client.get(self.promotion_list_url)

        self.assertEqual(
            response.status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_admin_can_access_promotions(self):
        self.client.force_authenticate(user=self.admin_user)

        response = self.client.get(self.promotion_list_url)

        self.assertEqual(
            response.status_code,
            status.HTTP_200_OK,
        )

    def test_normal_authenticated_user_cannot_access_promotion_rules(self):
        self.client.force_authenticate(user=self.normal_user)

        response = self.client.get(self.rule_list_url)

        self.assertEqual(
            response.status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_admin_can_access_promotion_rules(self):
        self.client.force_authenticate(user=self.admin_user)

        response = self.client.get(self.rule_list_url)

        self.assertEqual(
            response.status_code,
            status.HTTP_200_OK,
        )

    # ==========================================================
    # PROMOTION READ
    # ==========================================================

    def test_admin_can_retrieve_promotion(self):
        self.client.force_authenticate(user=self.admin_user)

        response = self.client.get(self.promotion_detail_url)

        self.assertEqual(
            response.status_code,
            status.HTTP_200_OK,
        )

        self.assertEqual(
            response.data["id"],
            self.promotion.id,
        )

        self.assertEqual(
            response.data["name"],
            "API Test Promotion",
        )

        self.assertEqual(
            response.data["discount_type"],
            Promotion.DiscountType.PERCENTAGE,
        )

        self.assertEqual(
            response.data["discount_value"],
            "10.00",
        )

        self.assertTrue(
            response.data["is_currently_active"]
        )

        self.assertEqual(
            len(response.data["rules"]),
            1,
        )

    # ==========================================================
    # PROMOTION CREATE
    # ==========================================================

    def test_admin_can_create_percentage_promotion(self):
        self.client.force_authenticate(user=self.admin_user)

        now = timezone.now()

        payload = {
            "name": "Created API Promotion",
            "description": "Created through API",
            "discount_type": Promotion.DiscountType.PERCENTAGE,
            "discount_value": "15.00",
            "max_discount_amount": "250.00",
            "minimum_cart_value": "1000.00",
            "is_active": True,
            "start_at": (now - timedelta(minutes=5)).isoformat(),
            "end_at": (now + timedelta(days=2)).isoformat(),
            "priority": 50,
            "stackable": False,
        }

        response = self.client.post(
            self.promotion_list_url,
            payload,
            format="json",
        )

        self.assertEqual(
            response.status_code,
            status.HTTP_201_CREATED,
        )

        promotion = Promotion.objects.get(
            name="Created API Promotion"
        )

        self.assertEqual(
            promotion.discount_value,
            Decimal("15.00"),
        )

        self.assertEqual(
            promotion.minimum_cart_value,
            Decimal("1000.00"),
        )

    def test_admin_can_create_fixed_amount_promotion(self):
        self.client.force_authenticate(user=self.admin_user)

        now = timezone.now()

        payload = {
            "name": "Fixed API Promotion",
            "description": "Fixed amount API test",
            "discount_type": Promotion.DiscountType.FIXED_AMOUNT,
            "discount_value": "100.00",
            "minimum_cart_value": "500.00",
            "is_active": True,
            "start_at": (now - timedelta(minutes=5)).isoformat(),
            "end_at": (now + timedelta(days=1)).isoformat(),
            "priority": 10,
            "stackable": True,
        }

        response = self.client.post(
            self.promotion_list_url,
            payload,
            format="json",
        )

        self.assertEqual(
            response.status_code,
            status.HTTP_201_CREATED,
        )

        created = Promotion.objects.get(
            name="Fixed API Promotion"
        )

        self.assertEqual(
            created.discount_type,
            Promotion.DiscountType.FIXED_AMOUNT,
        )

        self.assertEqual(
            created.discount_value,
            Decimal("100.00"),
        )

    # ==========================================================
    # PROMOTION UPDATE
    # ==========================================================

    def test_admin_can_patch_promotion(self):
        self.client.force_authenticate(user=self.admin_user)

        response = self.client.patch(
            self.promotion_detail_url,
            {
                "priority": 250,
                "stackable": True,
                "description": "Updated through API",
            },
            format="json",
        )

        self.assertEqual(
            response.status_code,
            status.HTTP_200_OK,
        )

        self.promotion.refresh_from_db()

        self.assertEqual(
            self.promotion.priority,
            250,
        )

        self.assertTrue(
            self.promotion.stackable
        )

        self.assertEqual(
            self.promotion.description,
            "Updated through API",
        )

    # ==========================================================
    # PROMOTION DELETE
    # ==========================================================

    def test_admin_can_delete_promotion(self):
        self.client.force_authenticate(user=self.admin_user)

        response = self.client.delete(
            self.promotion_detail_url
        )

        self.assertEqual(
            response.status_code,
            status.HTTP_204_NO_CONTENT,
        )

        self.assertFalse(
            Promotion.objects.filter(
                pk=self.promotion.pk
            ).exists()
        )

        self.assertFalse(
            PromotionRule.objects.filter(
                pk=self.rule.pk
            ).exists()
        )

    # ==========================================================
    # PROMOTION VALIDATION
    # ==========================================================

    def test_percentage_above_100_is_rejected(self):
        self.client.force_authenticate(user=self.admin_user)

        now = timezone.now()

        payload = {
            "name": "Invalid Percentage",
            "description": "",
            "discount_type": Promotion.DiscountType.PERCENTAGE,
            "discount_value": "101.00",
            "minimum_cart_value": "0.00",
            "is_active": True,
            "start_at": (now - timedelta(minutes=5)).isoformat(),
            "end_at": (now + timedelta(days=1)).isoformat(),
            "priority": 1,
            "stackable": False,
        }

        response = self.client.post(
            self.promotion_list_url,
            payload,
            format="json",
        )

        self.assertEqual(
            response.status_code,
            status.HTTP_400_BAD_REQUEST,
        )

        self.assertIn(
            "discount_value",
            response.data,
        )

    def test_zero_discount_is_rejected(self):
        self.client.force_authenticate(user=self.admin_user)

        now = timezone.now()

        payload = {
            "name": "Zero Discount",
            "discount_type": Promotion.DiscountType.PERCENTAGE,
            "discount_value": "0.00",
            "minimum_cart_value": "0.00",
            "is_active": True,
            "start_at": (now - timedelta(minutes=5)).isoformat(),
            "end_at": (now + timedelta(days=1)).isoformat(),
            "priority": 1,
            "stackable": False,
        }

        response = self.client.post(
            self.promotion_list_url,
            payload,
            format="json",
        )

        self.assertEqual(
            response.status_code,
            status.HTTP_400_BAD_REQUEST,
        )

    def test_invalid_promotion_schedule_is_rejected(self):
        self.client.force_authenticate(user=self.admin_user)

        now = timezone.now()

        payload = {
            "name": "Invalid Schedule",
            "discount_type": Promotion.DiscountType.PERCENTAGE,
            "discount_value": "10.00",
            "minimum_cart_value": "0.00",
            "is_active": True,
            "start_at": (now + timedelta(days=2)).isoformat(),
            "end_at": (now + timedelta(days=1)).isoformat(),
            "priority": 1,
            "stackable": False,
        }

        response = self.client.post(
            self.promotion_list_url,
            payload,
            format="json",
        )

        self.assertEqual(
            response.status_code,
            status.HTTP_400_BAD_REQUEST,
        )

    # ==========================================================
    # RULE READ
    # ==========================================================

    def test_admin_can_retrieve_rule(self):
        self.client.force_authenticate(user=self.admin_user)

        response = self.client.get(
            self.rule_detail_url
        )

        self.assertEqual(
            response.status_code,
            status.HTTP_200_OK,
        )

        self.assertEqual(
            response.data["target_type"],
            PromotionRule.TargetType.VARIANT,
        )

        self.assertEqual(
            response.data["variant"],
            self.variant.id,
        )

    # ==========================================================
    # RULE CREATE
    # ==========================================================

    def test_admin_can_create_category_rule(self):
        self.client.force_authenticate(user=self.admin_user)

        payload = {
            "promotion": self.promotion.id,
            "target_type": PromotionRule.TargetType.CATEGORY,
            "category": self.category.id,
            "subcategory": None,
            "brand": None,
            "product": None,
            "variant": None,
        }

        response = self.client.post(
            self.rule_list_url,
            payload,
            format="json",
        )

        self.assertEqual(
            response.status_code,
            status.HTTP_201_CREATED,
        )

        rule = PromotionRule.objects.get(
            promotion=self.promotion,
            target_type=PromotionRule.TargetType.CATEGORY,
            category=self.category,
        )

        self.assertEqual(
            rule.category_id,
            self.category.id,
        )

    def test_admin_can_create_brand_rule(self):
        self.client.force_authenticate(user=self.admin_user)

        # The product is not required for creating a brand rule.
        payload = {
            "promotion": self.promotion.id,
            "target_type": PromotionRule.TargetType.BRAND,
            "category": None,
            "subcategory": None,
            "brand": "  FINOLEX  ",
            "product": None,
            "variant": None,
        }

        response = self.client.post(
            self.rule_list_url,
            payload,
            format="json",
        )

        self.assertEqual(
            response.status_code,
            status.HTTP_201_CREATED,
        )

        rule = PromotionRule.objects.get(
    promotion=self.promotion,
    target_type=PromotionRule.TargetType.BRAND,
    brand="FINOLEX",
)

        self.assertEqual(
            rule.brand_normalized,
            "finolex",
        )

    # ==========================================================
    # RULE VALIDATION
    # ==========================================================

    def test_category_rule_requires_category(self):
        self.client.force_authenticate(user=self.admin_user)

        payload = {
            "promotion": self.promotion.id,
            "target_type": PromotionRule.TargetType.CATEGORY,
            "category": None,
            "subcategory": None,
            "brand": None,
            "product": None,
            "variant": None,
        }

        response = self.client.post(
            self.rule_list_url,
            payload,
            format="json",
        )

        self.assertEqual(
            response.status_code,
            status.HTTP_400_BAD_REQUEST,
        )

        self.assertIn(
            "category",
            response.data,
        )

    def test_brand_rule_requires_brand(self):
        self.client.force_authenticate(user=self.admin_user)

        payload = {
            "promotion": self.promotion.id,
            "target_type": PromotionRule.TargetType.BRAND,
            "category": None,
            "subcategory": None,
            "brand": "",
            "product": None,
            "variant": None,
        }

        response = self.client.post(
            self.rule_list_url,
            payload,
            format="json",
        )

        self.assertEqual(
            response.status_code,
            status.HTTP_400_BAD_REQUEST,
        )

        self.assertIn(
            "brand",
            response.data,
        )

    def test_product_rule_requires_product(self):
        self.client.force_authenticate(user=self.admin_user)

        payload = {
            "promotion": self.promotion.id,
            "target_type": PromotionRule.TargetType.PRODUCT,
            "category": None,
            "subcategory": None,
            "brand": None,
            "product": None,
            "variant": None,
        }

        response = self.client.post(
            self.rule_list_url,
            payload,
            format="json",
        )

        self.assertEqual(
            response.status_code,
            status.HTTP_400_BAD_REQUEST,
        )

        self.assertIn(
            "product",
            response.data,
        )

    def test_variant_rule_requires_variant(self):
        self.client.force_authenticate(user=self.admin_user)

        payload = {
            "promotion": self.promotion.id,
            "target_type": PromotionRule.TargetType.VARIANT,
            "category": None,
            "subcategory": None,
            "brand": None,
            "product": None,
            "variant": None,
        }

        response = self.client.post(
            self.rule_list_url,
            payload,
            format="json",
        )

        self.assertEqual(
            response.status_code,
            status.HTTP_400_BAD_REQUEST,
        )

        self.assertIn(
            "variant",
            response.data,
        )

    def test_all_rule_cannot_have_specific_target(self):
        self.client.force_authenticate(user=self.admin_user)

        payload = {
            "promotion": self.promotion.id,
            "target_type": PromotionRule.TargetType.ALL,
            "category": self.category.id,
            "subcategory": None,
            "brand": None,
            "product": None,
            "variant": None,
        }

        response = self.client.post(
            self.rule_list_url,
            payload,
            format="json",
        )

        self.assertEqual(
            response.status_code,
            status.HTTP_400_BAD_REQUEST,
        )

    # ==========================================================
    # RULE UPDATE
    # ==========================================================

    def test_admin_can_patch_rule(self):
        self.client.force_authenticate(user=self.admin_user)

        response = self.client.patch(
            self.rule_detail_url,
            {
                "target_type": PromotionRule.TargetType.PRODUCT,
                "category": None,
                "subcategory": None,
                "brand": None,
                "product": self.product.id,
                "variant": None,
            },
            format="json",
        )

        self.assertEqual(
            response.status_code,
            status.HTTP_200_OK,
        )

        self.rule.refresh_from_db()

        self.assertEqual(
            self.rule.target_type,
            PromotionRule.TargetType.PRODUCT,
        )

        self.assertEqual(
            self.rule.product_id,
            self.product.id,
        )

        self.assertIsNone(
            self.rule.variant_id
        )

    # ==========================================================
    # RULE DELETE
    # ==========================================================

    def test_admin_can_delete_rule(self):
        self.client.force_authenticate(user=self.admin_user)

        response = self.client.delete(
            self.rule_detail_url
        )

        self.assertEqual(
            response.status_code,
            status.HTTP_204_NO_CONTENT,
        )

        self.assertFalse(
            PromotionRule.objects.filter(
                pk=self.rule.pk
            ).exists()
        )