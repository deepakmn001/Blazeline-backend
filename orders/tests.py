from __future__ import annotations

import hashlib
import hmac
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import Customer
from accounts.services import issue_tokens_for_customer
from cart.models import Cart, CartItem
from catalog.models import (
    Category,
    Product,
    ProductVariant,
    SubCategory,
)

from .models import (
    InventoryReservation,
    Order,
    OrderItem,
    Payment,
    PaymentEvent,
)
from .payment_service import (
    PaymentConfigurationError,
    PaymentOwnershipError,
    PaymentSignatureError,
    PaymentStateError,
    PaymentValidationError,
    PaymentWebhookError,
    confirm_checkout_payment,
    process_order_paid_webhook,
    process_payment_captured_webhook,
    process_payment_failed_webhook,
    record_webhook_event,
    verify_checkout_signature,
    verify_webhook_signature,
)
from .services import (
    DeliveryQuote,
    InsufficientStockError,
    create_order_safely,
)


class OrderTestMixin:
    def create_order(
        self,
        *,
        payment_method=Order.PaymentMethod.UPI,
        idempotency_key="test-order-001",
    ):
        return create_order_safely(
            customer=self.customer,
            idempotency_key=idempotency_key,
            payment_method=payment_method,
            shipping=self._shipping(),
            delivery_quote=self._free_delivery_quote(),
            tax_resolver=self._tax_resolver,
            currency="INR",
        )

    def _shipping(self):
        return {
            "full_name": "Blaze Test Customer",
            "phone": "9876543210",
            "email": "test@example.com",
            "company": "",
            "gstin": "",
            "address_line1": "Test Street",
            "address_line2": "",
            "landmark": "",
            "city": "Kolkata",
            "state": "West Bengal",
            "pincode": "700001",
        }

    def _delivery_quote(self):
        return DeliveryQuote(
            charge=Decimal("50.00"),
            free_delivery=False,
            zone_id=None,
            zone_name="Test Zone",
            breakdown=[
                {
                    "rule": "test_rule",
                    "label": "Test Delivery",
                    "amount": "50.00",
                }
            ],
        )

    def _free_delivery_quote(self):
        return DeliveryQuote(
            charge=Decimal("0.00"),
            free_delivery=True,
            zone_id=None,
            zone_name="Test Zone",
            breakdown=[],
        )

    def _tax_resolver(self, variant, line_subtotal):
        return Decimal("18.00")

    def _payment_payload(
        self,
        *,
        payment,
        payment_id="pay_test_001",
        amount=None,
        currency="INR",
    ):
        if amount is None:
            amount = int(payment.amount * Decimal("100"))

        return {
            "entity": {
                "id": payment_id,
                "order_id": payment.provider_order_id,
                "amount": amount,
                "currency": currency,
                "status": "captured",
            }
        }

    def _webhook_payload(
        self,
        *,
        payment,
        payment_id="pay_test_001",
        amount=None,
        currency="INR",
    ):
        return {
            "payload": {
                "payment": self._payment_payload(
                    payment=payment,
                    payment_id=payment_id,
                    amount=amount,
                    currency=currency,
                )
            }
        }

    def _create_payment_event(
        self,
        *,
        payment,
        event_type="payment.captured",
        provider_event_id="evt_test_001",
        payload=None,
    ):
        if payload is None:
            payload = self._webhook_payload(
                payment=payment,
            )

        return PaymentEvent.objects.create(
            provider="razorpay",
            provider_event_id=provider_event_id,
            event_type=event_type,
            payload=payload,
            payment=payment,
            processing_status=PaymentEvent.ProcessingStatus.RECEIVED,
        )


class OrderDomainTests(OrderTestMixin, TestCase):
    def setUp(self):
        # --------------------------------------------------------------
        # Catalog hierarchy
        # --------------------------------------------------------------

        self.category = Category.objects.create(
            name="Test Category",
            slug="test-category",
        )

        self.subcategory = SubCategory.objects.create(
            category=self.category,
            name="Test Subcategory",
            slug="test-subcategory",
        )

        self.product = Product.objects.create(
            category=self.category,
            subcategory=self.subcategory,
            name="Test Product",
            brand="BlazeLine Test",
            slug="test-product",
            short_description="Test product",
            description="Test product for automated order tests.",
            active=True,
            status="published",
        )

        # --------------------------------------------------------------
        # Sellable variant
        # --------------------------------------------------------------

        self.variant = ProductVariant.objects.create(
            product=self.product,
            sku="TEST-SKU-001",
            mrp=Decimal("120.00"),
            selling_price=Decimal("100.00"),
            stock=10,
            minimum_order_quantity=1,
            active=True,
        )

        # --------------------------------------------------------------
        # Customer
        # --------------------------------------------------------------

        self.customer = Customer.objects.create(
            phone="9876543210",
            full_name="Blaze Test Customer",
        )

        # --------------------------------------------------------------
        # Customer cart
        # --------------------------------------------------------------

        self.cart = Cart.objects.create(
            customer=self.customer,
        )

        self.cart_item = CartItem.objects.create(
            cart=self.cart,
            variant=self.variant,
            quantity=2,
        )

    # ==================================================================
    # ORDER CREATION
    # ==================================================================

    def test_customer_can_create_order_from_owned_cart(self):
        order = create_order_safely(
            customer=self.customer,
            idempotency_key="test-idempotency-001",
            payment_method=Order.PaymentMethod.COD,
            shipping=self._shipping(),
            delivery_quote=self._delivery_quote(),
            tax_resolver=self._tax_resolver,
            currency="INR",
        )

        order.refresh_from_db()
        self.variant.refresh_from_db()

        self.assertEqual(order.customer_id, self.customer.id)
        self.assertEqual(order.subtotal, Decimal("200.00"))
        self.assertEqual(order.delivery_charge, Decimal("50.00"))
        self.assertEqual(order.tax_amount, Decimal("36.00"))
        self.assertEqual(order.cod_fee, Decimal("99.00"))
        self.assertEqual(order.grand_total, Decimal("385.00"))

        self.assertEqual(
    order.status,
    Order.Status.CONFIRMED,
)

        self.assertEqual(
            order.payment_status,
            Order.PaymentStatus.PENDING,
        )

        self.assertEqual(
            OrderItem.objects.filter(order=order).count(),
            1,
        )

        self.assertEqual(
            Payment.objects.filter(order=order).count(),
            1,
        )

        self.assertEqual(
            InventoryReservation.objects.filter(order=order).count(),
            1,
        )

        reservation = InventoryReservation.objects.get(
            order=order,
        )

        self.assertEqual(reservation.quantity, 2)
        self.assertEqual(
            reservation.status,
            InventoryReservation.Status.ACTIVE,
        )

        self.assertEqual(self.variant.stock, 8)

        self.assertFalse(
            Cart.objects.filter(
                pk=self.cart.pk,
            ).exists()
        )

    # ==================================================================
    # STOCK SAFETY
    # ==================================================================

    def test_order_cannot_be_created_from_insufficient_stock(self):
        self.cart_item.quantity = 999
        self.cart_item.save(
            update_fields=["quantity"],
        )

        with self.assertRaises(InsufficientStockError):
            create_order_safely(
                customer=self.customer,
                idempotency_key="stock-test-001",
                payment_method=Order.PaymentMethod.COD,
                shipping=self._shipping(),
                delivery_quote=self._free_delivery_quote(),
                tax_resolver=self._tax_resolver,
                currency="INR",
            )

        self.assertTrue(
            Cart.objects.filter(
                pk=self.cart.pk,
            ).exists()
        )

        self.variant.refresh_from_db()

        self.assertEqual(
            self.variant.stock,
            10,
        )

        self.assertEqual(
            Order.objects.filter(
                customer=self.customer
            ).count(),
            0,
        )

    # ==================================================================
    # IDEMPOTENCY
    # ==================================================================

    def test_idempotency_returns_same_order(self):
        kwargs = {
            "customer": self.customer,
            "idempotency_key": "same-request-001",
            "payment_method": Order.PaymentMethod.COD,
            "shipping": self._shipping(),
            "delivery_quote": self._free_delivery_quote(),
            "tax_resolver": self._tax_resolver,
            "currency": "INR",
        }

        first = create_order_safely(**kwargs)
        second = create_order_safely(**kwargs)

        self.assertEqual(first.pk, second.pk)

        self.assertEqual(
            Order.objects.filter(
                customer=self.customer
            ).count(),
            1,
        )

        self.assertEqual(
            OrderItem.objects.filter(
                order=first
            ).count(),
            1,
        )

        self.assertEqual(
            Payment.objects.filter(
                order=first
            ).count(),
            1,
        )

        self.assertEqual(
            InventoryReservation.objects.filter(
                order=first
            ).count(),
            1,
        )


class PaymentSecurityTests(OrderTestMixin, TestCase):
    def setUp(self):
        self.category = Category.objects.create(
            name="Payment Test Category",
            slug="payment-test-category",
        )

        self.subcategory = SubCategory.objects.create(
            category=self.category,
            name="Payment Test Subcategory",
            slug="payment-test-subcategory",
        )

        self.product = Product.objects.create(
            category=self.category,
            subcategory=self.subcategory,
            name="Payment Test Product",
            brand="BlazeLine Test",
            slug="payment-test-product",
            short_description="Payment test product",
            description="Payment testing product",
            active=True,
            status="published",
        )

        self.variant = ProductVariant.objects.create(
            product=self.product,
            sku="PAYMENT-SKU-001",
            mrp=Decimal("120.00"),
            selling_price=Decimal("100.00"),
            stock=10,
            minimum_order_quantity=1,
            active=True,
        )

        self.customer = Customer.objects.create(
            phone="9876543211",
            full_name="Payment Test Customer",
        )

        self.cart = Cart.objects.create(
            customer=self.customer,
        )

        CartItem.objects.create(
            cart=self.cart,
            variant=self.variant,
            quantity=2,
        )

        self.order = create_order_safely(
            customer=self.customer,
            idempotency_key="payment-order-001",
            payment_method=Order.PaymentMethod.UPI,
            shipping=self._shipping(),
            delivery_quote=self._free_delivery_quote(),
            tax_resolver=self._tax_resolver,
            currency="INR",
        )

        self.order.refresh_from_db()

        self.payment = Payment.objects.get(
            order=self.order,
        )

        self.payment.provider_order_id = "order_test_001"
        self.payment.status = Payment.Status.PENDING
        self.payment.save(
            update_fields=[
                "provider_order_id",
                "status",
                "updated_at",
            ],
        )

        self.reservation = InventoryReservation.objects.get(
            order=self.order,
        )

    # ==================================================================
    # SIGNATURE SECURITY
    # ==================================================================

    @patch(
        "orders.payment_service.RAZORPAY_KEY_SECRET",
        "test-secret",
    )
    def test_valid_checkout_signature_is_accepted(self):
        message = (
            "order_test_001|pay_test_001"
        ).encode("utf-8")

        signature = hmac.new(
            b"test-secret",
            message,
            hashlib.sha256,
        ).hexdigest()

        verify_checkout_signature(
            razorpay_order_id="order_test_001",
            razorpay_payment_id="pay_test_001",
            razorpay_signature=signature,
        )

    @patch(
        "orders.payment_service.RAZORPAY_KEY_SECRET",
        "test-secret",
    )
    def test_invalid_checkout_signature_is_rejected(self):
        with self.assertRaises(PaymentSignatureError):
            verify_checkout_signature(
                razorpay_order_id="order_test_001",
                razorpay_payment_id="pay_test_001",
                razorpay_signature="invalid-signature",
            )

    @patch(
        "orders.payment_service.RAZORPAY_WEBHOOK_SECRET",
        "webhook-secret",
    )
    def test_valid_webhook_signature_is_accepted(self):
        body = b'{"event":"payment.captured"}'

        signature = hmac.new(
            b"webhook-secret",
            body,
            hashlib.sha256,
        ).hexdigest()

        verify_webhook_signature(
            raw_body=body,
            signature=signature,
        )

    @patch(
        "orders.payment_service.RAZORPAY_WEBHOOK_SECRET",
        "webhook-secret",
    )
    def test_invalid_webhook_signature_is_rejected(self):
        body = b'{"event":"payment.captured"}'

        with self.assertRaises(PaymentWebhookError):
            verify_webhook_signature(
                raw_body=body,
                signature="bad-signature",
            )

    # ==================================================================
    # CLIENT PAYMENT CONFIRMATION
    # ==================================================================

    @patch(
        "orders.payment_service.RAZORPAY_KEY_SECRET",
        "test-secret",
    )
    def test_checkout_confirmation_rejects_wrong_gateway_order(self):
        message = (
            "attacker_order|pay_test_001"
        ).encode("utf-8")

        signature = hmac.new(
            b"test-secret",
            message,
            hashlib.sha256,
        ).hexdigest()

        with self.assertRaises(PaymentValidationError):
            confirm_checkout_payment(
                customer=self.customer,
                order_number=self.order.order_number,
                razorpay_order_id="attacker_order",
                razorpay_payment_id="pay_test_001",
                razorpay_signature=signature,
            )

    @patch(
        "orders.payment_service.RAZORPAY_KEY_SECRET",
        "test-secret",
    )
    def test_checkout_confirmation_rejects_other_customer_order(self):
        attacker = Customer.objects.create(
            phone="9876543212",
            full_name="Attacker",
        )

        message = (
            "order_test_001|pay_test_001"
        ).encode("utf-8")

        signature = hmac.new(
            b"test-secret",
            message,
            hashlib.sha256,
        ).hexdigest()

        with self.assertRaises(PaymentOwnershipError):
            confirm_checkout_payment(
                customer=attacker,
                order_number=self.order.order_number,
                razorpay_order_id="order_test_001",
                razorpay_payment_id="pay_test_001",
                razorpay_signature=signature,
            )

    # ==================================================================
    # CAPTURED WEBHOOK
    # ==================================================================

    def test_payment_captured_webhook_marks_order_paid(self):
        event = self._create_payment_event(
            payment=self.payment,
            event_type="payment.captured",
            provider_event_id="evt_capture_001",
        )

        processed_payment = process_payment_captured_webhook(
            event=event,
        )

        self.assertEqual(
            processed_payment.pk,
            self.payment.pk,
        )

        self.payment.refresh_from_db()
        self.order.refresh_from_db()
        self.reservation.refresh_from_db()

        self.assertEqual(
            self.payment.status,
            Payment.Status.CAPTURED,
        )

        self.assertEqual(
            self.payment.provider_payment_id,
            "pay_test_001",
        )

        self.assertEqual(
            self.order.payment_status,
            Order.PaymentStatus.PAID,
        )

        self.assertEqual(
            self.order.status,
            Order.Status.CONFIRMED,
        )

        self.assertEqual(
            self.reservation.status,
            InventoryReservation.Status.CONSUMED,
        )

        event.refresh_from_db()

        self.assertEqual(
            event.processing_status,
            PaymentEvent.ProcessingStatus.PROCESSED,
        )

        self.assertIsNotNone(
            event.processed_at,
        )

    # ==================================================================
    # AMOUNT / CURRENCY TAMPERING
    # ==================================================================

    def test_payment_captured_webhook_rejects_amount_tampering(self):
        event = self._create_payment_event(
            payment=self.payment,
            event_type="payment.captured",
            provider_event_id="evt_bad_amount_001",
            payload=self._webhook_payload(
                payment=self.payment,
                payment_id="pay_bad_amount",
                amount=1,
            ),
        )

        with self.assertRaises(PaymentWebhookError):
            process_payment_captured_webhook(
                event=event,
            )

        self.payment.refresh_from_db()
        self.order.refresh_from_db()
        self.reservation.refresh_from_db()

        self.assertEqual(
            self.payment.status,
            Payment.Status.PENDING,
        )

        self.assertEqual(
            self.order.payment_status,
            Order.PaymentStatus.PENDING,
        )

        self.assertEqual(
            self.reservation.status,
            InventoryReservation.Status.ACTIVE,
        )

    def test_payment_captured_webhook_rejects_currency_tampering(self):
        event = self._create_payment_event(
            payment=self.payment,
            event_type="payment.captured",
            provider_event_id="evt_bad_currency_001",
            payload=self._webhook_payload(
                payment=self.payment,
                payment_id="pay_bad_currency",
                currency="USD",
            ),
        )

        with self.assertRaises(PaymentWebhookError):
            process_payment_captured_webhook(
                event=event,
            )

        self.payment.refresh_from_db()
        self.order.refresh_from_db()

        self.assertEqual(
            self.payment.status,
            Payment.Status.PENDING,
        )

        self.assertEqual(
            self.order.payment_status,
            Order.PaymentStatus.PENDING,
        )

    # ==================================================================
    # DUPLICATE / IDEMPOTENT WEBHOOK
    # ==================================================================

    def test_same_webhook_event_id_is_deduplicated(self):
        payload = self._webhook_payload(
            payment=self.payment,
        )

        first, created_first = record_webhook_event(
            provider_event_id="evt_duplicate_001",
            event_type="payment.captured",
            payload=payload,
        )

        second, created_second = record_webhook_event(
            provider_event_id="evt_duplicate_001",
            event_type="payment.captured",
            payload=payload,
        )

        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first.pk, second.pk)

        self.assertEqual(
            PaymentEvent.objects.filter(
                provider="razorpay",
                provider_event_id="evt_duplicate_001",
            ).count(),
            1,
        )

    def test_already_captured_payment_is_idempotent(self):
        event = self._create_payment_event(
            payment=self.payment,
            event_type="payment.captured",
            provider_event_id="evt_first_capture_001",
        )

        process_payment_captured_webhook(
            event=event,
        )

        self.payment.refresh_from_db()

        first_paid_at = self.payment.paid_at

        duplicate_event = self._create_payment_event(
            payment=self.payment,
            event_type="payment.captured",
            provider_event_id="evt_second_capture_001",
            payload=self._webhook_payload(
                payment=self.payment,
                payment_id="pay_test_001",
            ),
        )

        process_payment_captured_webhook(
            event=duplicate_event,
        )

        self.payment.refresh_from_db()

        self.assertEqual(
            self.payment.status,
            Payment.Status.CAPTURED,
        )

        self.assertEqual(
            self.payment.provider_payment_id,
            "pay_test_001",
        )

        self.assertEqual(
            self.payment.paid_at,
            first_paid_at,
        )

        self.assertEqual(
            InventoryReservation.objects.filter(
                order=self.order,
                status=InventoryReservation.Status.ACTIVE,
            ).count(),
            0,
        )

    # ==================================================================
    # ORDER.PAID WEBHOOK
    # ==================================================================

    def test_order_paid_webhook_uses_same_success_path(self):
        event = self._create_payment_event(
            payment=self.payment,
            event_type="order.paid",
            provider_event_id="evt_order_paid_001",
        )

        process_order_paid_webhook(
            event=event,
        )

        self.payment.refresh_from_db()
        self.order.refresh_from_db()
        self.reservation.refresh_from_db()

        self.assertEqual(
            self.payment.status,
            Payment.Status.CAPTURED,
        )

        self.assertEqual(
            self.order.payment_status,
            Order.PaymentStatus.PAID,
        )

        self.assertEqual(
            self.order.status,
            Order.Status.CONFIRMED,
        )

        self.assertEqual(
            self.reservation.status,
            InventoryReservation.Status.CONSUMED,
        )

    # ==================================================================
    # PAYMENT FAILED
    # ==================================================================

    def test_payment_failed_webhook_does_not_downgrade_captured_payment(self):
        captured_event = self._create_payment_event(
            payment=self.payment,
            event_type="payment.captured",
            provider_event_id="evt_capture_before_failure_001",
        )

        process_payment_captured_webhook(
            event=captured_event,
        )

        failed_payload = {
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_test_001",
                        "order_id": self.payment.provider_order_id,
                        "amount": int(self.payment.amount * Decimal("100")),
                        "currency": "INR",
                        "status": "failed",
                        "error": {
                            "code": "BAD_REQUEST_ERROR",
                            "description": "Late failure event",
                        },
                    }
                }
            }
        }

        failed_event = self._create_payment_event(
            payment=self.payment,
            event_type="payment.failed",
            provider_event_id="evt_late_failure_001",
            payload=failed_payload,
        )

        process_payment_failed_webhook(
            event=failed_event,
        )

        self.payment.refresh_from_db()
        self.order.refresh_from_db()

        self.assertEqual(
            self.payment.status,
            Payment.Status.CAPTURED,
        )

        self.assertEqual(
            self.order.payment_status,
            Order.PaymentStatus.PAID,
        )

    def test_payment_failed_webhook_marks_unpaid_payment_failed(self):
        event = self._create_payment_event(
            payment=self.payment,
            event_type="payment.failed",
            provider_event_id="evt_failure_001",
            payload={
                "payload": {
                    "payment": {
                        "entity": {
                            "id": "pay_failed_001",
                            "order_id": self.payment.provider_order_id,
                            "amount": int(
                                self.payment.amount * Decimal("100")
                            ),
                            "currency": "INR",
                            "status": "failed",
                            "error": {
                                "code": "BAD_REQUEST_ERROR",
                                "description": "Payment failed",
                            },
                        }
                    }
                }
            },
        )

        process_payment_failed_webhook(
            event=event,
        )

        self.payment.refresh_from_db()
        self.order.refresh_from_db()

        self.assertEqual(
            self.payment.status,
            Payment.Status.FAILED,
        )

        self.assertEqual(
            self.order.payment_status,
            Order.PaymentStatus.FAILED,
        )

        self.assertEqual(
    self.order.status,
    Order.Status.PENDING_PAYMENT,
)

    # ==================================================================
    # LATE CAPTURE AFTER RESERVATION EXPIRY
    # ==================================================================

    def test_late_capture_does_not_resurrect_expired_reservation(self):
        self.reservation.status = InventoryReservation.Status.EXPIRED
        self.reservation.save(
            update_fields=["status"],
)

        self.variant.refresh_from_db()
        stock_before_capture = self.variant.stock

        event = self._create_payment_event(
            payment=self.payment,
            event_type="payment.captured",
            provider_event_id="evt_late_capture_001",
            payload=self._webhook_payload(
                payment=self.payment,
                payment_id="pay_late_capture",
            ),
        )

        process_payment_captured_webhook(
            event=event,
        )

        self.payment.refresh_from_db()
        self.order.refresh_from_db()
        self.reservation.refresh_from_db()
        self.variant.refresh_from_db()

        self.assertEqual(
            self.payment.status,
            Payment.Status.CAPTURED,
        )

        self.assertEqual(
            self.order.payment_status,
            Order.PaymentStatus.PAID,
        )

        self.assertEqual(
            self.reservation.status,
            InventoryReservation.Status.EXPIRED,
        )

        # Payment settlement must not magically add stock back or mutate
        # the expired reservation into CONSUMED.
        self.assertEqual(
            self.variant.stock,
            stock_before_capture,
        )

    # ==================================================================
    # PAYMENT RECORD CONFLICT
    # ==================================================================

    def test_different_payment_id_cannot_replace_existing_payment(self):
        first_event = self._create_payment_event(
            payment=self.payment,
            event_type="payment.captured",
            provider_event_id="evt_original_payment_001",
            payload=self._webhook_payload(
                payment=self.payment,
                payment_id="pay_original_001",
            ),
        )

        process_payment_captured_webhook(
            event=first_event,
        )

        conflicting_event = self._create_payment_event(
            payment=self.payment,
            event_type="payment.captured",
            provider_event_id="evt_conflicting_payment_001",
            payload=self._webhook_payload(
                payment=self.payment,
                payment_id="pay_attacker_001",
            ),
        )

        with self.assertRaises(PaymentValidationError):
            process_payment_captured_webhook(
                event=conflicting_event,
            )

        self.payment.refresh_from_db()

        self.assertEqual(
            self.payment.provider_payment_id,
            "pay_original_001",
        )

        self.assertEqual(
            self.payment.status,
            Payment.Status.CAPTURED,
        )


class RazorpayConfigurationTests(TestCase):
    @patch(
        "orders.payment_service.RAZORPAY_KEY_ID",
        "",
    )
    @patch(
        "orders.payment_service.RAZORPAY_KEY_SECRET",
        "",
    )
    def test_missing_razorpay_credentials_fail_closed(self):
        from .payment_service import _get_client

        with self.assertRaises(PaymentConfigurationError):
            _get_client()



class OrderAPITests(OrderTestMixin, TestCase):
    def setUp(self):
        self.client = APIClient()

        # --------------------------------------------------------------
        # Catalog
        # --------------------------------------------------------------

        self.category = Category.objects.create(
            name="API Test Category",
            slug="api-test-category",
        )

        self.subcategory = SubCategory.objects.create(
            category=self.category,
            name="API Test Subcategory",
            slug="api-test-subcategory",
        )

        self.product = Product.objects.create(
            category=self.category,
            subcategory=self.subcategory,
            name="API Test Product",
            brand="BlazeLine API Test",
            slug="api-test-product",
            short_description="API test product",
            description="API test product.",
            active=True,
            status="published",
        )

        self.variant = ProductVariant.objects.create(
            product=self.product,
            sku="API-SKU-001",
            mrp=Decimal("120.00"),
            selling_price=Decimal("100.00"),
            stock=10,
            minimum_order_quantity=1,
            active=True,
        )

        # --------------------------------------------------------------
        # Customer + cart
        # --------------------------------------------------------------

        self.customer = Customer.objects.create(
            phone="9876543222",
            full_name="API Test Customer",
        )

        self.cart = Cart.objects.create(
            customer=self.customer,
        )

        CartItem.objects.create(
            cart=self.cart,
            variant=self.variant,
            quantity=2,
        )

        tokens = issue_tokens_for_customer(
            self.customer,
        )

        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {tokens['access']}",
        )

    # ==================================================================
    # AUTHORIZATION
    # ==================================================================

    def test_unauthenticated_order_list_is_rejected(self):
        self.client.credentials()

        response = self.client.get(
            "/api/orders/",
        )

        self.assertEqual(
            response.status_code,
            401,
        )

    @patch(
        "orders.views._build_delivery_quote",
    )
    def test_authenticated_customer_can_create_order(
        self,
        mock_build_delivery_quote,
    ):
        # The API test should isolate order creation from the external/
        # configurable delivery rules. Delivery calculation has its own
        # dedicated tests; here we only verify the order API contract.
        mock_build_delivery_quote.return_value = self._free_delivery_quote()

        response = self.client.post(
            "/api/orders/create/",
            {
                "payment_method": Order.PaymentMethod.COD,
                "currency": "INR",
                "shipping": self._shipping(),
                "notes": "API test order",
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="api-order-001",
        )

        self.assertEqual(
            response.status_code,
            201,
        )

        self.assertTrue(
            response.data["success"],
        )

        self.assertIn(
            "order",
            response.data,
        )

        self.assertEqual(
            Order.objects.filter(
                customer=self.customer,
            ).count(),
            1,
        )

    def test_order_create_requires_idempotency_key(self):
        response = self.client.post(
            "/api/orders/create/",
            {
                "payment_method": Order.PaymentMethod.COD,
                "currency": "INR",
                "shipping": self._shipping(),
            },
            format="json",
        )

        self.assertEqual(
            response.status_code,
            400,
        )

        self.assertEqual(
            response.data["code"],
            "missing_idempotency_key",
        )

    def test_customer_cannot_read_another_customers_order(self):
        order = create_order_safely(
            customer=self.customer,
            idempotency_key="api-owned-order-001",
            payment_method=Order.PaymentMethod.COD,
            shipping=self._shipping(),
            delivery_quote=self._free_delivery_quote(),
            tax_resolver=self._tax_resolver,
            currency="INR",
        )

        another_customer = Customer.objects.create(
            phone="9876543223",
            full_name="Another Customer",
        )

        other_tokens = issue_tokens_for_customer(
            another_customer,
        )

        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {other_tokens['access']}",
        )

        response = self.client.get(
            f"/api/orders/{order.order_number}/",
        )

        self.assertEqual(
            response.status_code,
            404,
        )

    # ==================================================================
    # RAZORPAY CREATE API
    # ==================================================================

    @patch(
        "orders.views.create_razorpay_order_for_customer",
    )
    def test_payment_create_returns_gateway_order_data(
        self,
        mock_create,
    ):
        order = create_order_safely(
            customer=self.customer,
            idempotency_key="api-payment-order-001",
            payment_method=Order.PaymentMethod.UPI,
            shipping=self._shipping(),
            delivery_quote=self._free_delivery_quote(),
            tax_resolver=self._tax_resolver,
            currency="INR",
        )

        mock_create.return_value = {
            "order_number": order.order_number,
            "payment_id": 101,
            "razorpay_key_id": "rzp_test_example",
            "razorpay_order_id": "order_test_001",
            "amount": 23600,
            "currency": "INR",
        }

        response = self.client.post(
            f"/api/orders/{order.order_number}/payment/create/",
            {},
            format="json",
        )

        self.assertEqual(
            response.status_code,
            200,
        )

        self.assertTrue(
            response.data["success"],
        )

        self.assertEqual(
            response.data["razorpay_order_id"],
            "order_test_001",
        )

        mock_create.assert_called_once_with(
            customer=self.customer,
            order_number=order.order_number,
        )

    def test_payment_create_requires_authentication(self):
        self.client.credentials()

        response = self.client.post(
            "/api/orders/anything/payment/create/",
            {},
            format="json",
        )

        self.assertEqual(
            response.status_code,
            401,
        )

    # ==================================================================
    # RAZORPAY VERIFY API
    # ==================================================================

    @patch(
        "orders.views.confirm_checkout_payment",
    )
    def test_payment_verify_success_response(
        self,
        mock_confirm,
    ):
        order = create_order_safely(
            customer=self.customer,
            idempotency_key="api-payment-verify-001",
            payment_method=Order.PaymentMethod.UPI,
            shipping=self._shipping(),
            delivery_quote=self._free_delivery_quote(),
            tax_resolver=self._tax_resolver,
            currency="INR",
        )

        payment = Payment.objects.get(
            order=order,
        )

        payment.provider_order_id = "order_verify_001"
        payment.provider_payment_id = "pay_verify_001"
        payment.status = Payment.Status.CAPTURED

        payment.save(
            update_fields=[
                "provider_order_id",
                "provider_payment_id",
                "status",
                "updated_at",
            ],
        )

        mock_confirm.return_value = payment

        response = self.client.post(
            f"/api/orders/{order.order_number}/payment/verify/",
            {
                "razorpay_order_id": "order_verify_001",
                "razorpay_payment_id": "pay_verify_001",
                "razorpay_signature": "test-signature",
            },
            format="json",
        )

        self.assertEqual(
            response.status_code,
            200,
        )

        self.assertTrue(
            response.data["success"],
        )

        self.assertEqual(
            response.data["payment"]["id"],
            payment.id,
        )

        mock_confirm.assert_called_once_with(
            customer=self.customer,
            order_number=order.order_number,
            razorpay_order_id="order_verify_001",
            razorpay_payment_id="pay_verify_001",
            razorpay_signature="test-signature",
        )

    def test_payment_verify_requires_all_payment_fields(self):
        order = create_order_safely(
            customer=self.customer,
            idempotency_key="api-payment-verify-fields-001",
            payment_method=Order.PaymentMethod.UPI,
            shipping=self._shipping(),
            delivery_quote=self._free_delivery_quote(),
            tax_resolver=self._tax_resolver,
            currency="INR",
        )

        response = self.client.post(
            f"/api/orders/{order.order_number}/payment/verify/",
            {
                "razorpay_order_id": "order_missing_fields",
            },
            format="json",
        )

        self.assertEqual(
            response.status_code,
            400,
        )

        self.assertEqual(
            response.data["code"],
            "missing_payment_fields",
        )

    def test_order_list_returns_only_authenticated_customers_orders(self):
        own_order = create_order_safely(
            customer=self.customer,
            idempotency_key="api-order-list-own-001",
            payment_method=Order.PaymentMethod.COD,
            shipping=self._shipping(),
            delivery_quote=self._free_delivery_quote(),
            tax_resolver=self._tax_resolver,
            currency="INR",
        )

        response = self.client.get(
            "/api/orders/",
        )

        self.assertEqual(
            response.status_code,
            200,
        )

        returned_order_numbers = [
            item["order_number"]
            for item in response.data["results"]
        ]

        self.assertIn(
            own_order.order_number,
            returned_order_numbers,
        )
