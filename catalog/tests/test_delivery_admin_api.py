from decimal import Decimal
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from catalog.models import (
    Category, SubCategory, DeliveryZone, ServiceablePincode,
    DeliveryRule, DeliveryRuleCondition, DeliveryRuleAction,
)

User = get_user_model()


class DeliveryAdminAPITestCase(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="admin_tester", password="pass12345")
        self.client.force_authenticate(user=self.user)
        self.zone = DeliveryZone.objects.create(name="Kolkata Core", code="kolkata-core", active=True, priority=10)
        self.category = Category.objects.create(name="Building Materials", slug="building-materials")
        self.subcategory = SubCategory.objects.create(category=self.category, name="Tiles", slug="tiles")


class AuthenticationTests(DeliveryAdminAPITestCase):
    def test_anonymous_rejected(self):
        self.client.force_authenticate(user=None)
        resp = self.client.get("/api/admin/delivery/zones/")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_authenticated_allowed(self):
        resp = self.client.get("/api/admin/delivery/zones/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)


class DeliveryZoneAPITests(DeliveryAdminAPITestCase):
    def test_list_paginated_shape(self):
        resp = self.client.get("/api/admin/delivery/zones/")
        self.assertIn("results", resp.data)
        self.assertIn("count", resp.data)

    def test_create(self):
        resp = self.client.post("/api/admin/delivery/zones/", {
            "name": "Howrah Extended", "code": "howrah-extended", "active": True, "priority": 5,
        })
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp.data["pincode_count"], 0)
        self.assertEqual(resp.data["rule_count"], 0)

    def test_duplicate_name_rejected(self):
        resp = self.client.post("/api/admin/delivery/zones/", {
            "name": "Kolkata Core", "code": "kolkata-core-2", "active": True, "priority": 0,
        })
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("name", resp.data)

    def test_update(self):
        resp = self.client.put(f"/api/admin/delivery/zones/{self.zone.id}/", {
            "name": "Kolkata Core", "code": "kolkata-core", "active": False, "priority": 20,
        })
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(resp.data["active"])

    def test_toggle_active(self):
        resp = self.client.post(f"/api/admin/delivery/zones/{self.zone.id}/toggle-active/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.zone.refresh_from_db()
        self.assertFalse(self.zone.active)

    def test_delete_without_rules_succeeds(self):
        resp = self.client.delete(f"/api/admin/delivery/zones/{self.zone.id}/")
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)

    def test_delete_with_rule_blocked(self):
        DeliveryRule.objects.create(name="Zone Rule", code="zone-rule", zone=self.zone)
        resp = self.client.delete(f"/api/admin/delivery/zones/{self.zone.id}/")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(DeliveryZone.objects.filter(id=self.zone.id).exists())

    def test_delete_after_rule_removed_succeeds(self):
        rule = DeliveryRule.objects.create(name="Zone Rule 2", code="zone-rule-2", zone=self.zone)
        rule.delete()
        resp = self.client.delete(f"/api/admin/delivery/zones/{self.zone.id}/")
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)

    def test_delete_does_not_affect_pincode_set_null(self):
        pincode = ServiceablePincode.objects.create(
            pincode="700001", area_name="BBD Bagh", city="Kolkata", state="West Bengal", zone=self.zone,
        )
        resp = self.client.delete(f"/api/admin/delivery/zones/{self.zone.id}/")
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        pincode.refresh_from_db()
        self.assertIsNone(pincode.zone)


class ServiceablePincodeAPITests(DeliveryAdminAPITestCase):
    def test_missing_fields_rejected(self):
        base = {
            "pincode": "700016",
            "area_name": "Area",
            "city": "Kolkata",
            "state": "West Bengal",
            "is_active": True,
        }

        for field in ("area_name", "city", "state"):
            payload = {**base, field: ""}
            resp = self.client.post(
                "/api/admin/delivery/pincodes/",
                payload,
            )
            self.assertEqual(
                resp.status_code,
                status.HTTP_400_BAD_REQUEST,
                msg=f"field={field!r} resp.data={getattr(resp, 'data', resp.content)!r} "
                    f"existing_pincodes={list(ServiceablePincode.objects.values('id', 'pincode', 'area_name'))!r}",
            )
            self.assertIn(field, resp.data)

    def test_no_default_injected(self):
        resp = self.client.post("/api/admin/delivery/pincodes/", {
            "pincode": "700017", "area_name": "Salt Lake", "city": "Kolkata",
            "state": "West Bengal", "is_active": True,
        })
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp.data["city"], "Kolkata")

    def test_invalid_format_rejected(self):
        resp = self.client.post("/api/admin/delivery/pincodes/", {
            "pincode": "70001A", "area_name": "X", "city": "Kolkata", "state": "West Bengal", "is_active": True,
        })
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("pincode", resp.data)

    def test_duplicate_rejected(self):
        ServiceablePincode.objects.create(pincode="700020", area_name="X", city="Kolkata", state="West Bengal")
        resp = self.client.post("/api/admin/delivery/pincodes/", {
            "pincode": "700020", "area_name": "Y", "city": "Kolkata", "state": "West Bengal", "is_active": True,
        })
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("pincode", resp.data)

    def test_update_keeps_own_pincode(self):
        p = ServiceablePincode.objects.create(pincode="700021", area_name="X", city="Kolkata", state="West Bengal")
        resp = self.client.put(f"/api/admin/delivery/pincodes/{p.id}/", {
            "pincode": "700021", "area_name": "X Updated", "city": "Kolkata", "state": "West Bengal", "is_active": True,
        })
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_toggle_active(self):
        p = ServiceablePincode.objects.create(pincode="700022", area_name="X", city="Kolkata", state="West Bengal")
        resp = self.client.post(f"/api/admin/delivery/pincodes/{p.id}/toggle-active/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        p.refresh_from_db()
        self.assertFalse(p.is_active)

    def test_assign_zone(self):
        resp = self.client.post("/api/admin/delivery/pincodes/", {
            "pincode": "700023", "area_name": "X", "city": "Kolkata",
            "state": "West Bengal", "is_active": True, "zone_id": self.zone.id,
        })
        self.assertEqual(resp.data["zone"]["id"], self.zone.id)

    def test_delete(self):
        p = ServiceablePincode.objects.create(pincode="700024", area_name="X", city="Kolkata", state="West Bengal")
        resp = self.client.delete(f"/api/admin/delivery/pincodes/{p.id}/")
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)


class DeliveryRuleAPITests(DeliveryAdminAPITestCase):
    def _payload(self, **overrides):
        p = {
            "name": "Test Rule", "code": "test-rule", "active": True, "priority": 0,
            "combine_mode": "add", "stop_after": False, "starts_at": None, "ends_at": None,
            "conditions": [], "actions": [],
        }
        p.update(overrides)
        return p

    def test_create_minimal(self):
        resp = self.client.post("/api/admin/delivery/rules/", self._payload(), format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp.data["computed_status"], "active")
        self.assertEqual(resp.data["specificity"], 0)

    def test_create_with_conditions_and_actions(self):
        payload = self._payload(
            category_id=self.category.id,
            conditions=[{"field": "cart_value", "operator": "gte", "value": "500", "sort_order": 0}],
            actions=[{"action_type": "surcharge", "pricing_mode": "fixed", "amount": "50.00",
                      "label": "Handling Fee", "metadata": {}, "sort_order": 0, "active": True}],
        )
        resp = self.client.post("/api/admin/delivery/rules/", payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(len(resp.data["conditions"]), 1)
        self.assertEqual(len(resp.data["actions"]), 1)
        self.assertEqual(resp.data["specificity"], 1)

    def test_mismatched_subcategory_rejected(self):
        other = Category.objects.create(name="Electrical", slug="electrical")
        payload = self._payload(category_id=other.id, subcategory_id=self.subcategory.id)
        resp = self.client.post("/api/admin/delivery/rules/", payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("subcategory", resp.data)

    def test_start_after_end_rejected(self):
        now = timezone.now()
        payload = self._payload(
            starts_at=(now + timedelta(days=5)).isoformat(),
            ends_at=(now + timedelta(days=1)).isoformat(),
        )
        resp = self.client.post("/api/admin/delivery/rules/", payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("ends_at", resp.data)

    def test_discount_with_override_rejected_on_create(self):
        payload = self._payload(
            combine_mode="override",
            actions=[{"action_type": "discount", "pricing_mode": "fixed", "amount": "10.00",
                      "label": "", "metadata": {}, "sort_order": 0, "active": True}],
        )
        resp = self.client.post("/api/admin/delivery/rules/", payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("actions", resp.data)

    def test_discount_with_override_rejected_on_combine_mode_only_update(self):
        rule = DeliveryRule.objects.create(name="R1", code="r1", combine_mode=DeliveryRule.COMBINE_ADD)
        DeliveryRuleAction.objects.create(
            rule=rule, action_type=DeliveryRuleAction.ACTION_DISCOUNT,
            pricing_mode=DeliveryRuleAction.PRICING_FIXED, amount=Decimal("10.00"),
        )
        resp = self.client.put(f"/api/admin/delivery/rules/{rule.id}/", {
            "name": "R1", "code": "r1", "active": True, "priority": 0,
            "combine_mode": "override", "stop_after": False,
        }, format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("actions", resp.data)

    def test_in_operator_requires_value(self):
        payload = self._payload(conditions=[{"field": "customer_type", "operator": "in", "value": "", "sort_order": 0}])
        resp = self.client.post("/api/admin/delivery/rules/", payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_comparison_on_text_field_rejected(self):
        payload = self._payload(conditions=[{"field": "customer_type", "operator": "gt", "value": "wholesale", "sort_order": 0}])
        resp = self.client.post("/api/admin/delivery/rules/", payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_non_numeric_on_numeric_field_rejected(self):
        payload = self._payload(conditions=[{"field": "cart_value", "operator": "gte", "value": "abc", "sort_order": 0}])
        resp = self.client.post("/api/admin/delivery/rules/", payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_partial_update_keeps_conditions_and_actions(self):
        rule = DeliveryRule.objects.create(name="R2", code="r2")
        DeliveryRuleCondition.objects.create(rule=rule, field="cart_value", operator="gte", value="100", sort_order=0)
        DeliveryRuleAction.objects.create(rule=rule, action_type="surcharge", pricing_mode="fixed", amount=Decimal("20.00"))
        resp = self.client.put(f"/api/admin/delivery/rules/{rule.id}/", {
            "name": "R2 Renamed", "code": "r2", "active": True, "priority": 0,
            "combine_mode": "add", "stop_after": False,
        }, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        rule.refresh_from_db()
        self.assertEqual(rule.conditions.count(), 1)
        self.assertEqual(rule.actions.count(), 1)
        self.assertEqual(rule.name, "R2 Renamed")

    def test_update_condition_in_place(self):
        rule = DeliveryRule.objects.create(name="R3", code="r3")
        cond = DeliveryRuleCondition.objects.create(rule=rule, field="cart_value", operator="gte", value="100", sort_order=0)
        payload = self._payload(
            name="R3", code="r3",
            conditions=[{"id": cond.id, "field": "cart_value", "operator": "gte", "value": "999", "sort_order": 0}],
        )
        resp = self.client.put(f"/api/admin/delivery/rules/{rule.id}/", payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        cond.refresh_from_db()
        self.assertEqual(cond.value, "999")
        self.assertEqual(rule.conditions.count(), 1)

    def test_removing_condition_deletes_it(self):
        rule = DeliveryRule.objects.create(name="R4", code="r4")
        DeliveryRuleCondition.objects.create(rule=rule, field="cart_value", operator="gte", value="100", sort_order=0)
        payload = self._payload(name="R4", code="r4", conditions=[])
        resp = self.client.put(f"/api/admin/delivery/rules/{rule.id}/", payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(rule.conditions.count(), 0)

    def test_toggle_active_returns_list_shape(self):
        rule = DeliveryRule.objects.create(name="R5", code="r5", active=True)
        resp = self.client.post(f"/api/admin/delivery/rules/{rule.id}/toggle-active/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn("scope", resp.data)
        rule.refresh_from_db()
        self.assertFalse(rule.active)

    def test_status_filter_scheduled(self):
        now = timezone.now()
        DeliveryRule.objects.create(name="Future Rule", code="future-rule", starts_at=now + timedelta(days=1))
        resp = self.client.get("/api/admin/delivery/rules/?status=scheduled")
        names = [r["name"] for r in resp.data["results"]]
        self.assertIn("Future Rule", names)

    def test_status_filter_expired(self):
        now = timezone.now()
        DeliveryRule.objects.create(name="Past Rule", code="past-rule", ends_at=now - timedelta(days=1))
        resp = self.client.get("/api/admin/delivery/rules/?status=expired")
        names = [r["name"] for r in resp.data["results"]]
        self.assertIn("Past Rule", names)

    def test_scope_filter_category(self):
        DeliveryRule.objects.create(name="Cat Rule", code="cat-rule", category=self.category)
        resp = self.client.get("/api/admin/delivery/rules/?scope=category")
        names = [r["name"] for r in resp.data["results"]]
        self.assertIn("Cat Rule", names)

    def test_delete_cascades_conditions_and_actions(self):
        rule = DeliveryRule.objects.create(name="R6", code="r6")
        DeliveryRuleCondition.objects.create(rule=rule, field="cart_value", operator="gte", value="1")
        DeliveryRuleAction.objects.create(rule=rule, action_type="base_charge", pricing_mode="fixed", amount=Decimal("1"))
        resp = self.client.delete(f"/api/admin/delivery/rules/{rule.id}/")
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(DeliveryRuleCondition.objects.filter(rule_id=rule.id).count(), 0)


class DeliveryOverviewAPITests(DeliveryAdminAPITestCase):
    def test_counts(self):
        DeliveryZone.objects.create(name="Zone B", code="zone-b", active=False)
        ServiceablePincode.objects.create(pincode="700030", area_name="X", city="Kolkata", state="WB", is_active=False)
        active_rule = DeliveryRule.objects.create(name="Active Rule", code="active-rule", active=True)
        DeliveryRuleAction.objects.create(
            rule=active_rule, action_type=DeliveryRuleAction.ACTION_FREE_DELIVERY,
            pricing_mode=DeliveryRuleAction.PRICING_FIXED, amount=Decimal("0"),
        )
        DeliveryRule.objects.create(name="Inactive Rule", code="inactive-rule", active=False)

        resp = self.client.get("/api/admin/delivery/overview/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["zones"]["active"], 1)
        self.assertEqual(resp.data["zones"]["inactive"], 1)
        self.assertEqual(resp.data["rules"]["free_delivery"], 1)
        self.assertEqual(resp.data["rules"]["inactive"], 1)
        self.assertLessEqual(len(resp.data["recent_changes"]), 5)

    def test_requires_auth(self):
        self.client.force_authenticate(user=None)
        resp = self.client.get("/api/admin/delivery/overview/")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)


class ForeignChildIDTests(DeliveryAdminAPITestCase):
    def _payload(self, **overrides):
        p = {
            "name": "Test Rule", "code": "test-rule", "active": True, "priority": 0,
            "combine_mode": "add", "stop_after": False, "starts_at": None, "ends_at": None,
            "conditions": [], "actions": [],
        }
        p.update(overrides)
        return p

    def test_foreign_condition_id_rejected(self):
        rule_a = DeliveryRule.objects.create(name="RA", code="ra")
        cond_a = DeliveryRuleCondition.objects.create(
            rule=rule_a, field="cart_value", operator="gte", value="100", sort_order=0,
        )
        rule_b = DeliveryRule.objects.create(name="RB", code="rb")

        payload = self._payload(
            name="RB", code="rb",
            conditions=[{
                "id": cond_a.id, "field": "cart_value", "operator": "gte",
                "value": "999", "sort_order": 0,
            }],
        )
        resp = self.client.put(f"/api/admin/delivery/rules/{rule_b.id}/", payload, format="json")

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(rule_b.conditions.count(), 0)
        cond_a.refresh_from_db()
        self.assertEqual(cond_a.value, "100")

    def test_foreign_action_id_rejected(self):
        rule_a = DeliveryRule.objects.create(name="RA2", code="ra2")
        action_a = DeliveryRuleAction.objects.create(
            rule=rule_a, action_type="surcharge", pricing_mode="fixed", amount=Decimal("10.00"),
        )
        rule_b = DeliveryRule.objects.create(name="RB2", code="rb2")

        payload = self._payload(
            name="RB2", code="rb2",
            actions=[{
                "id": action_a.id, "action_type": "surcharge", "pricing_mode": "fixed",
                "amount": "999.00", "label": "", "metadata": {}, "sort_order": 0, "active": True,
            }],
        )
        resp = self.client.put(f"/api/admin/delivery/rules/{rule_b.id}/", payload, format="json")

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(rule_b.actions.count(), 0)
        action_a.refresh_from_db()
        self.assertEqual(action_a.amount, Decimal("10.00"))


class NumericInValidationTests(DeliveryAdminAPITestCase):
    def _payload(self, **overrides):
        p = {
            "name": "In Test Rule", "code": "in-test-rule", "active": True, "priority": 0,
            "combine_mode": "add", "stop_after": False, "starts_at": None, "ends_at": None,
            "conditions": [], "actions": [],
        }
        p.update(overrides)
        return p

    def test_cart_value_in_with_non_numeric_rejected(self):
        payload = self._payload(
            code="in-cart-1",
            conditions=[{"field": "cart_value", "operator": "in", "value": "100,abc", "sort_order": 0}],
        )
        resp = self.client.post("/api/admin/delivery/rules/", payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_weight_in_with_non_numeric_rejected(self):
        payload = self._payload(
            code="in-weight-1",
            conditions=[{"field": "weight", "operator": "in", "value": "1.5,xyz", "sort_order": 0}],
        )
        resp = self.client.post("/api/admin/delivery/rules/", payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_quantity_in_with_non_numeric_rejected(self):
        payload = self._payload(
            code="in-qty-1",
            conditions=[{"field": "quantity", "operator": "in", "value": "1,abc", "sort_order": 0}],
        )
        resp = self.client.post("/api/admin/delivery/rules/", payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_total_quantity_in_with_negative_rejected(self):
        payload = self._payload(
            code="in-totalqty-1",
            conditions=[{"field": "total_quantity", "operator": "in", "value": "1,-2", "sort_order": 0}],
        )
        resp = self.client.post("/api/admin/delivery/rules/", payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_valid_numeric_in_accepted(self):
        payload = self._payload(
            code="in-valid-1",
            conditions=[{"field": "cart_value", "operator": "in", "value": "100,200,300", "sort_order": 0}],
        )
        resp = self.client.post("/api/admin/delivery/rules/", payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_empty_comma_entry_rejected(self):
        payload = self._payload(
            code="in-empty-1",
            conditions=[{"field": "cart_value", "operator": "in", "value": "100,,200", "sort_order": 0}],
        )
        resp = self.client.post("/api/admin/delivery/rules/", payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class AtomicRuleUpdateTests(DeliveryAdminAPITestCase):
    def _payload(self, **overrides):
        p = {
            "name": "Atomic Rule", "code": "atomic-rule", "active": True, "priority": 0,
            "combine_mode": "add", "stop_after": False, "starts_at": None, "ends_at": None,
            "conditions": [], "actions": [],
        }
        p.update(overrides)
        return p

    def test_atomic_update_rejects_when_one_action_invalid(self):
        rule = DeliveryRule.objects.create(name="Atomic Rule", code="atomic-rule")
        cond = DeliveryRuleCondition.objects.create(
            rule=rule, field="cart_value", operator="gte", value="100", sort_order=0,
        )
        action = DeliveryRuleAction.objects.create(
            rule=rule, action_type="surcharge", pricing_mode="fixed", amount=Decimal("20.00"),
        )

        # combine_mode=override + a discount action is the known-invalid
        # combo (see test_discount_with_override_rejected_on_create) —
        # reusing it here so the invalidity itself isn't a guess.
        payload = self._payload(
            combine_mode="override",
            conditions=[{
                "id": cond.id, "field": "cart_value", "operator": "gte",
                "value": "150", "sort_order": 0,
            }],
            actions=[
                {"id": action.id, "action_type": "surcharge", "pricing_mode": "fixed",
                 "amount": "25.00", "label": "", "metadata": {}, "sort_order": 0, "active": True},
                {"action_type": "discount", "pricing_mode": "fixed", "amount": "10.00",
                 "label": "", "metadata": {}, "sort_order": 1, "active": True},
            ],
        )
        resp = self.client.put(f"/api/admin/delivery/rules/{rule.id}/", payload, format="json")

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

        cond.refresh_from_db()
        action.refresh_from_db()
        self.assertEqual(cond.value, "100")
        self.assertEqual(action.amount, Decimal("20.00"))
        self.assertEqual(rule.conditions.count(), 1)
        self.assertEqual(rule.actions.count(), 1)


class PricingModeContractTests(DeliveryAdminAPITestCase):
    def _payload(self, **overrides):
        p = {
            "name": "Pricing Rule", "code": "pricing-rule", "active": True, "priority": 0,
            "combine_mode": "add", "stop_after": False, "starts_at": None, "ends_at": None,
            "conditions": [], "actions": [],
        }
        p.update(overrides)
        return p

    def _action(self, pricing_mode, amount):
        return [{
            "action_type": "surcharge", "pricing_mode": pricing_mode, "amount": amount,
            "label": "", "metadata": {}, "sort_order": 0, "active": True,
        }]

    def test_pricing_mode_fixed(self):
        payload = self._payload(code="pm-fixed", actions=self._action("fixed", "50.00"))
        resp = self.client.post("/api/admin/delivery/rules/", payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_pricing_mode_per_item(self):
        payload = self._payload(code="pm-per-item", actions=self._action("per_item", "5.00"))
        resp = self.client.post("/api/admin/delivery/rules/", payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_pricing_mode_per_unit(self):
        payload = self._payload(code="pm-per-unit", actions=self._action("per_unit", "5.00"))
        resp = self.client.post("/api/admin/delivery/rules/", payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_pricing_mode_per_kg(self):
        payload = self._payload(code="pm-per-kg", actions=self._action("per_kg", "3.00"))
        resp = self.client.post("/api/admin/delivery/rules/", payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_pricing_mode_percentage(self):
        payload = self._payload(code="pm-percentage", actions=self._action("percentage", "10"))
        resp = self.client.post("/api/admin/delivery/rules/", payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_free_delivery_action_contract(self):
        payload = self._payload(
            code="pm-free-delivery",
            actions=[{
                "action_type": "free_delivery", "pricing_mode": "fixed", "amount": "0.00",
                "label": "", "metadata": {}, "sort_order": 0, "active": True,
            }],
        )
        resp = self.client.post("/api/admin/delivery/rules/", payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        actions = resp.data["actions"]
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["action_type"], "free_delivery")
        self.assertEqual(Decimal(actions[0]["amount"]), Decimal("0.00"))