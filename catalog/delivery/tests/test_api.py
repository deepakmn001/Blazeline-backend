from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework import status

from catalog.models import (
    Category,
    SubCategory,
    Product,
    ProductVariant,
    DeliveryZone,
    DeliveryRule,
    DeliveryRuleAction,
    ServiceablePincode,
)


class DeliveryQuoteAPITestBase(TestCase):
    def setUp(self):
        self.client = APIClient()

        self.category = Category.objects.create(
            name="Building Materials",
            slug="building-materials",
            active=True,
        )

        self.subcategory = SubCategory.objects.create(
            category=self.category,
            name="Cement",
            slug="cement",
            active=True,
        )

        self.product = Product.objects.create(
            category=self.category,
            subcategory=self.subcategory,
            name="PPC Cement",
            slug="ppc-cement",
            active=True,
            status="published",
        )

        self.variant = ProductVariant.objects.create(
            product=self.product,
            sku="PPC-50KG",
            mrp=Decimal("500.00"),
            selling_price=Decimal("450.00"),
            stock=100,
            weight=Decimal("50.00"),
            minimum_order_quantity=1,
            lead_time_days=1,
            active=True,
        )

        self.zone = DeliveryZone.objects.create(
            name="Kolkata Core",
            code="kolkata-core",
            active=True,
            priority=10,
        )

        ServiceablePincode.objects.create(
            pincode="700001",
            area_name="Kolkata Core",
            city="Kolkata",
            state="West Bengal",
            is_active=True,
            zone=self.zone,
        )

        self.url = "/api/delivery/quote/"

    def create_rule(
        self,
        *,
        name,
        code,
        priority=0,
        zone=None,
        category=None,
        subcategory=None,
        product=None,
        variant=None,
        combine_mode=DeliveryRule.COMBINE_ADD,
        stop_after=False,
    ):
        return DeliveryRule.objects.create(
            name=name,
            code=code,
            active=True,
            priority=priority,
            zone=zone,
            category=category,
            subcategory=subcategory,
            product=product,
            variant=variant,
            combine_mode=combine_mode,
            stop_after=stop_after,
        )

    def create_action(
        self,
        rule,
        *,
        action_type=DeliveryRuleAction.ACTION_BASE_CHARGE,
        pricing_mode=DeliveryRuleAction.PRICING_FIXED,
        amount="0.00",
        label="",
    ):
        return DeliveryRuleAction.objects.create(
            rule=rule,
            action_type=action_type,
            pricing_mode=pricing_mode,
            amount=Decimal(amount),
            label=label,
            active=True,
        )


class DeliveryQuoteValidationAPITests(DeliveryQuoteAPITestBase):

    def test_valid_quote_request(self):
        rule = self.create_rule(
            name="Base Delivery",
            code="api-base-delivery",
        )

        self.create_action(
            rule,
            amount="100.00",
            label="Delivery Charge",
        )

        response = self.client.post(
            self.url,
            {
                "pincode": "700001",
                "items": [
                    {
                        "variant_id": self.variant.id,
                        "quantity": 2,
                    }
                ],
            },
            format="json",
        )

        self.assertEqual(
            response.status_code,
            status.HTTP_200_OK,
        )

        data = response.json()

        self.assertTrue(data["deliverable"])
        self.assertEqual(
            data["delivery_charge"],
            "100.00",
        )
        self.assertEqual(
            data["zone"]["id"],
            self.zone.id,
        )

    def test_pincode_whitespace_is_normalized(self):
        rule = self.create_rule(
            name="Whitespace Pincode",
            code="api-whitespace-pincode",
        )

        self.create_action(
            rule,
            amount="75.00",
        )

        response = self.client.post(
            self.url,
            {
                "pincode": " 700001 ",
                "items": [
                    {
                        "variant_id": self.variant.id,
                        "quantity": 1,
                    }
                ],
            },
            format="json",
        )

        self.assertEqual(
            response.status_code,
            status.HTTP_200_OK,
        )

        self.assertEqual(
            response.json()["delivery_charge"],
            "75.00",
        )

    def test_invalid_pincode_returns_400(self):
        response = self.client.post(
            self.url,
            {
                "pincode": "70001",
                "items": [
                    {
                        "variant_id": self.variant.id,
                        "quantity": 1,
                    }
                ],
            },
            format="json",
        )

        self.assertEqual(
            response.status_code,
            status.HTTP_400_BAD_REQUEST,
        )

    def test_unserviceable_pincode_returns_non_deliverable(self):
        response = self.client.post(
            self.url,
            {
                "pincode": "700999",
                "items": [
                    {
                        "variant_id": self.variant.id,
                        "quantity": 1,
                    }
                ],
            },
            format="json",
        )

        self.assertEqual(
            response.status_code,
            status.HTTP_200_OK,
        )

        data = response.json()

        self.assertFalse(data["deliverable"])
        self.assertIsNone(data["delivery_charge"])

    def test_invalid_variant_returns_400(self):
        response = self.client.post(
            self.url,
            {
                "pincode": "700001",
                "items": [
                    {
                        "variant_id": 999999,
                        "quantity": 1,
                    }
                ],
            },
            format="json",
        )

        self.assertEqual(
            response.status_code,
            status.HTTP_400_BAD_REQUEST,
        )

    def test_duplicate_variant_returns_400(self):
        response = self.client.post(
            self.url,
            {
                "pincode": "700001",
                "items": [
                    {
                        "variant_id": self.variant.id,
                        "quantity": 1,
                    },
                    {
                        "variant_id": self.variant.id,
                        "quantity": 2,
                    },
                ],
            },
            format="json",
        )

        self.assertEqual(
            response.status_code,
            status.HTTP_400_BAD_REQUEST,
        )

    def test_invalid_quantity_returns_400(self):
        response = self.client.post(
            self.url,
            {
                "pincode": "700001",
                "items": [
                    {
                        "variant_id": self.variant.id,
                        "quantity": 0,
                    }
                ],
            },
            format="json",
        )

        self.assertEqual(
            response.status_code,
            status.HTTP_400_BAD_REQUEST,
        )

    def test_quantity_above_limit_returns_400(self):
        response = self.client.post(
            self.url,
            {
                "pincode": "700001",
                "items": [
                    {
                        "variant_id": self.variant.id,
                        "quantity": 100001,
                    }
                ],
            },
            format="json",
        )

        self.assertEqual(
            response.status_code,
            status.HTTP_400_BAD_REQUEST,
        )


class DeliveryQuoteRuleAPITests(DeliveryQuoteAPITestBase):

    def test_product_targeted_rule(self):
        rule = self.create_rule(
            name="Cement Delivery",
            code="api-product-rule",
            product=self.product,
        )

        self.create_action(
            rule,
            amount="150.00",
        )

        response = self.client.post(
            self.url,
            {
                "pincode": "700001",
                "items": [
                    {
                        "variant_id": self.variant.id,
                        "quantity": 1,
                    }
                ],
            },
            format="json",
        )

        self.assertEqual(
            response.status_code,
            status.HTTP_200_OK,
        )

        self.assertEqual(
            response.json()["delivery_charge"],
            "150.00",
        )

    def test_free_delivery_returns_zero(self):
        rule = self.create_rule(
            name="Free Delivery",
            code="api-free-delivery",
            priority=100,
        )

        self.create_action(
            rule,
            action_type=DeliveryRuleAction.ACTION_FREE_DELIVERY,
            amount="0.00",
        )

        response = self.client.post(
            self.url,
            {
                "pincode": "700001",
                "items": [
                    {
                        "variant_id": self.variant.id,
                        "quantity": 1,
                    }
                ],
            },
            format="json",
        )

        self.assertEqual(
            response.status_code,
            status.HTTP_200_OK,
        )

        data = response.json()

        self.assertTrue(data["deliverable"])
        self.assertEqual(
            data["delivery_charge"],
            "0.00",
        )
        self.assertTrue(
            data["free_delivery"]
        )

    def test_per_unit_delivery(self):
        rule = self.create_rule(
            name="Per Unit Delivery",
            code="api-per-unit",
        )

        self.create_action(
            rule,
            pricing_mode=DeliveryRuleAction.PRICING_PER_UNIT,
            amount="20.00",
        )

        response = self.client.post(
            self.url,
            {
                "pincode": "700001",
                "items": [
                    {
                        "variant_id": self.variant.id,
                        "quantity": 5,
                    }
                ],
            },
            format="json",
        )

        self.assertEqual(
            response.status_code,
            status.HTTP_200_OK,
        )

        self.assertEqual(
            response.json()["delivery_charge"],
            "100.00",
        )