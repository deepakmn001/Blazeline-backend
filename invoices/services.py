from __future__ import annotations

import logging

from django.db import IntegrityError, transaction

from orders.models import Order, Payment

from .models import Invoice, InvoiceItem


logger = logging.getLogger(__name__)


# ============================================================================
# DOMAIN ERRORS
# ============================================================================


class InvoiceError(Exception):
    """Base invoice-domain exception."""


class InvoiceNotEligibleError(InvoiceError):
    """Raised when an order is not yet eligible for invoice generation."""


class InvoiceCreationError(InvoiceError):
    """Raised when invoice creation cannot be completed safely."""

def process_invoice_for_order(
    *,
    order_id: int,
) -> Invoice:
    """
    Complete the post-order invoice workflow.

    Production guarantees:
    - Canonical invoice creation is idempotent.
    - WhatsApp is intentionally disabled for the current rollout.
    - PDF generation/upload happens only when the Cloudinary asset is absent.
    - Email delivery is idempotent and never forced here.
    - A successful payment is never rolled back because invoice delivery fails.
    - Delivery failures are persisted on the invoice for later retry/diagnostics.
    """

    from .email import send_invoice_email
    from .pdf import render_invoice_pdf
    from .storage import upload_invoice_pdf

    invoice = None

    try:
        invoice = create_invoice_for_order(
            order_id=order_id,
        )

        # ------------------------------------------------------------------
        # WhatsApp is deliberately disabled for the current BlazeLine rollout.
        # Keep the field explicit so overall invoice state is email-only.
        # ------------------------------------------------------------------
        if (
            invoice.whatsapp_status
            != Invoice.DeliveryStatus.NOT_APPLICABLE
        ):
            invoice.whatsapp_status = (
                Invoice.DeliveryStatus.NOT_APPLICABLE
            )
            invoice.save(
                update_fields=[
                    "whatsapp_status",
                    "updated_at",
                ]
            )

        # ------------------------------------------------------------------
        # Generate and persist the PDF only once.
        #
        # Authenticated Cloudinary assets are tracked through pdf_public_id;
        # no permanent public URL is persisted.
        # ------------------------------------------------------------------
        if not invoice.pdf_public_id:
            pdf_bytes = render_invoice_pdf(
                invoice=invoice,
            )

            upload_invoice_pdf(
                invoice=invoice,
                pdf_bytes=pdf_bytes,
            )

            invoice.refresh_from_db()

        # ------------------------------------------------------------------
        # Mark the PDF stage as generated.
        # ------------------------------------------------------------------
        if invoice.pdf_public_id:
            if invoice.status == Invoice.Status.PENDING:
                invoice.status = Invoice.Status.GENERATED
                invoice.last_error = ""

                invoice.save(
                    update_fields=[
                        "status",
                        "last_error",
                        "updated_at",
                    ]
                )
        else:
            raise InvoiceCreationError(
                "Invoice PDF asset was not persisted."
            )

        # ------------------------------------------------------------------
        # Email delivery.
        #
        # send_invoice_email() is idempotent and will not resend an already
        # delivered invoice unless force=True is explicitly requested.
        # ------------------------------------------------------------------
        if (
            invoice.email_status
            != Invoice.DeliveryStatus.SENT
        ):
            invoice = send_invoice_email(
                invoice=invoice,
            )
        else:
            invoice.refresh_from_db()

        # ------------------------------------------------------------------
        # Recalculate the canonical invoice lifecycle state.
        # ------------------------------------------------------------------
        invoice = refresh_invoice_delivery_status(
            invoice=invoice,
        )

        return invoice

    except Exception as exc:
        if invoice is not None:
            try:
                record_invoice_error(
                    invoice=invoice,
                    error=exc,
                )
            except Exception:
                logger.exception(
                    "Failed to persist invoice workflow error.",
                    extra={
                        "invoice_id": str(invoice.pk),
                        "invoice_number": invoice.invoice_number,
                        "order_id": invoice.order_id,
                    },
                )

        raise


# ============================================================================
# ELIGIBILITY
# ============================================================================


def _assert_invoice_eligible(order: Order) -> None:
    """
    Validate that the order has reached a commercially valid state for
    invoice creation.

    Rules:
    - Online payments must be PAID.
    - COD and CREDIT orders are eligible once the order is CONFIRMED.
    - Cancelled/failed/delivered states are handled conservatively.
    """

    if order.status in {
        Order.Status.CANCELLED,
        Order.Status.FAILED,
    }:
        raise InvoiceNotEligibleError(
            "Invoice cannot be generated for a cancelled or failed order."
        )

    if order.payment_method in {
        Order.PaymentMethod.COD,
        Order.PaymentMethod.CREDIT,
    }:
        if order.status != Order.Status.CONFIRMED:
            raise InvoiceNotEligibleError(
                "Order is not confirmed yet."
            )
        return

    if order.payment_status != Order.PaymentStatus.PAID:
        raise InvoiceNotEligibleError(
            "Online payment has not been completed."
        )

    if order.status not in {
        Order.Status.CONFIRMED,
        Order.Status.PROCESSING,
        Order.Status.PACKED,
        Order.Status.DISPATCHED,
        Order.Status.DELIVERED,
    }:
        raise InvoiceNotEligibleError(
            "Order has not reached a valid invoicing state."
        )


# ============================================================================
# PAYMENT SNAPSHOT
# ============================================================================


def _get_captured_razorpay_payment(
    *,
    order: Order,
) -> Payment | None:
    """
    Resolve the captured Razorpay payment associated with the order.

    Returns None for COD/CREDIT or when no Razorpay payment exists.
    """

    if order.payment_method in {
        Order.PaymentMethod.COD,
        Order.PaymentMethod.CREDIT,
    }:
        return None

    payment = (
        Payment.objects
        .filter(
            order=order,
            provider="razorpay",
            status=Payment.Status.CAPTURED,
        )
        .order_by("-created_at")
        .first()
    )

    if payment is None:
        raise InvoiceCreationError(
            "No captured Razorpay payment was found for this order."
        )

    return payment


# ============================================================================
# SNAPSHOT BUILDERS
# ============================================================================


def _build_invoice_snapshot(
    *,
    order: Order,
    payment: Payment | None,
) -> dict:
    """
    Build a complete immutable commercial/customer snapshot from the order.
    """

    return {
        "order": order,
        "customer": order.customer,

        "customer_name": order.shipping_full_name,
        "customer_email": order.shipping_email,
        "customer_phone": order.shipping_phone,
        "company_name": order.shipping_company,
        "customer_gstin": order.shipping_gstin,

        "billing_address_line1": order.shipping_address_line1,
        "billing_address_line2": order.shipping_address_line2,
        "billing_landmark": order.shipping_landmark,
        "billing_city": order.shipping_city,
        "billing_state": order.shipping_state,
        "billing_pincode": order.shipping_pincode,

        "currency": order.currency,
        "subtotal": order.subtotal,
        "discount_amount": order.discount_amount,
        "delivery_charge": order.delivery_charge,
        "tax_amount": order.tax_amount,
        "cod_fee": order.cod_fee,
        "grand_total": order.grand_total,

        "payment_method": order.payment_method,
        "payment_status": order.payment_status,

        "razorpay_payment_id": (
            payment.provider_payment_id
            if payment is not None
            else ""
        ),
    }


def _create_invoice_items(
    *,
    invoice: Invoice,
    order: Order,
) -> None:
    """
    Copy immutable commercial line-item snapshots from the order.
    """

    order_items = list(
        order.items.all().order_by("id")
    )

    if not order_items:
        raise InvoiceCreationError(
            "Cannot generate an invoice for an order without items."
        )

    invoice_items = [
        InvoiceItem(
            invoice=invoice,
            product_name=item.product_name,
            sku=item.sku,
            variant_name=item.variant_name,
            quantity=item.quantity,
            unit_price=item.unit_price,
            tax_rate=item.tax_rate,
            tax_amount=item.tax_amount,
            discount_amount=item.discount_amount,
            line_total=item.line_total,
            currency=item.currency,
        )
        for item in order_items
    ]

    InvoiceItem.objects.bulk_create(invoice_items)


# ============================================================================
# PUBLIC SERVICE
# ============================================================================


@transaction.atomic
def create_invoice_for_order(
    *,
    order_id: int,
) -> Invoice:
    """
    Create or return the canonical invoice for an eligible order.

    Production guarantees:
    - Order row is locked during invoice creation.
    - Only one canonical invoice can exist per order.
    - Commercial/customer values come from the authoritative Order snapshot.
    - Line items come from authoritative OrderItem snapshots.
    - Payment reference comes from the captured payment.
    - Repeated calls safely return the existing invoice.
    - Invoice and invoice-items are committed atomically.
    """

    order = (
        Order.objects
        .select_for_update()
        .select_related("customer")
        .prefetch_related("items")
        .get(pk=order_id)
    )

    # ------------------------------------------------------------------
    # Idempotency fast path
    # ------------------------------------------------------------------

    existing_invoice = (
        Invoice.objects
        .filter(order=order)
        .first()
    )

    if existing_invoice is not None:
        return existing_invoice

    # ------------------------------------------------------------------
    # Eligibility
    # ------------------------------------------------------------------

    _assert_invoice_eligible(order)

    # ------------------------------------------------------------------
    # Payment snapshot
    # ------------------------------------------------------------------

    payment = _get_captured_razorpay_payment(
        order=order,
    )

    # ------------------------------------------------------------------
    # Create invoice header
    # ------------------------------------------------------------------

    snapshot = _build_invoice_snapshot(
        order=order,
        payment=payment,
    )

    try:
        invoice = Invoice.objects.create(
            **snapshot,
            status=Invoice.Status.PENDING,
        )
    except IntegrityError:
        # A concurrent request may have won the OneToOne race.
        existing_invoice = (
            Invoice.objects
            .select_for_update()
            .filter(order=order)
            .first()
        )

        if existing_invoice is not None:
            return existing_invoice

        raise InvoiceCreationError(
            "Invoice could not be created."
        )

    # ------------------------------------------------------------------
    # Create immutable line-item snapshots
    # ------------------------------------------------------------------

    _create_invoice_items(
        invoice=invoice,
        order=order,
    )

    return invoice


# ============================================================================
# HELPERS FOR DOWNSTREAM DELIVERY
# ============================================================================


def mark_invoice_generated(
    *,
    invoice: Invoice,
) -> Invoice:
    """
    Mark the invoice as generated after the PDF asset has been successfully
    produced and persisted.

    Cloudinary invoices are stored as authenticated raw assets. Therefore
    pdf_public_id is the authoritative persistence marker; pdf_url may remain
    intentionally blank.
    """

    invoice.refresh_from_db()

    if not invoice.pdf_public_id:
        raise InvoiceCreationError(
            "Cannot mark invoice as generated without a stored PDF asset."
        )

    invoice.status = Invoice.Status.GENERATED
    invoice.last_error = ""

    invoice.save(
        update_fields=[
            "status",
            "last_error",
            "updated_at",
        ]
    )

    return invoice


def refresh_invoice_delivery_status(
    *,
    invoice: Invoice,
) -> Invoice:
    """
    Recalculate the invoice lifecycle from the currently enabled delivery
    channel.

    WhatsApp is intentionally treated as NOT_APPLICABLE for the current
    rollout, so email is the sole customer-delivery channel.
    """

    if (
        invoice.whatsapp_status
        != Invoice.DeliveryStatus.NOT_APPLICABLE
    ):
        invoice.whatsapp_status = (
            Invoice.DeliveryStatus.NOT_APPLICABLE
        )

    if invoice.email_status == Invoice.DeliveryStatus.SENT:
        invoice.status = Invoice.Status.SENT

    elif invoice.email_status == Invoice.DeliveryStatus.FAILED:
        invoice.status = Invoice.Status.FAILED

    elif invoice.pdf_public_id:
        invoice.status = Invoice.Status.GENERATED

    else:
        invoice.status = Invoice.Status.PENDING

    invoice.save(
        update_fields=[
            "status",
            "whatsapp_status",
            "updated_at",
        ]
    )

    return invoice


def record_invoice_error(
    *,
    invoice: Invoice,
    error: Exception,
) -> Invoice:
    """
    Record a non-sensitive diagnostic error.

    Never store credentials, authorization headers, OTPs, tokens,
    or complete third-party responses here.
    """

    message = str(error).strip()

    if len(message) > 4000:
        message = message[:4000]

    invoice.last_error = message
    invoice.status = Invoice.Status.FAILED
    invoice.delivery_attempts += 1

    invoice.save(
        update_fields=[
            "last_error",
            "status",
            "delivery_attempts",
            "updated_at",
        ]
    )

    logger.exception(
        "Invoice workflow failed.",
        extra={
            "invoice_id": str(invoice.pk),
            "invoice_number": invoice.invoice_number,
            "order_id": invoice.order_id,
        },
    )

    return invoice