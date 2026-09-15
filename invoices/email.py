from __future__ import annotations

import logging
from html import escape

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import EmailMultiAlternatives
from django.utils import timezone

from .models import Invoice
from .pdf import render_invoice_pdf
from .services import refresh_invoice_delivery_status


logger = logging.getLogger(__name__)


# ============================================================================
# CONFIG
# ============================================================================

INVOICE_EMAIL_SUBJECT_TEMPLATE = (
    "BlazeLine Invoice {invoice_number} · Order {order_number}"
)


# ============================================================================
# HELPERS
# ============================================================================


def _require_customer_email(invoice: Invoice) -> str:
    email = str(
        invoice.customer_email or ""
    ).strip()

    if not email:
        raise ValidationError(
            "Invoice does not contain a customer email address."
        )

    return email


def _build_email_subject(
    *,
    invoice: Invoice,
) -> str:
    return INVOICE_EMAIL_SUBJECT_TEMPLATE.format(
        invoice_number=invoice.invoice_number,
        order_number=invoice.order.order_number,
    )


def _build_plain_text_body(
    *,
    invoice: Invoice,
) -> str:
    customer_name = (
        str(invoice.customer_name or "Customer").strip()
    )

    return (
        f"Hello {customer_name},\n\n"
        f"Thank you for choosing BlazeLine.\n\n"
        f"Your invoice for order {invoice.order.order_number} "
        f"is attached to this email.\n\n"
        f"Invoice Number: {invoice.invoice_number}\n"
        f"Invoice Date: {invoice.issued_at.strftime('%d %b %Y')}\n"
        f"Amount: ₹ {invoice.grand_total:,.2f}\n"
        f"Payment Method: {invoice.payment_method.upper()}\n"
        f"Payment Status: {invoice.payment_status.upper()}\n\n"
        "Please keep this invoice for your records.\n\n"
        "Regards,\n"
        "BlazeLine\n"
        "Your Build Should Never Stop.\n"
        "www.blazeline.in"
    )


def _build_html_body(
    *,
    invoice: Invoice,
) -> str:
    customer_name = escape(
        str(invoice.customer_name or "Customer")
    )

    invoice_number = escape(
        invoice.invoice_number
    )

    order_number = escape(
        invoice.order.order_number
    )

    invoice_date = invoice.issued_at.strftime(
        "%d %b %Y"
    )

    payment_method = escape(
        str(invoice.payment_method or "")
        .replace("_", " ")
        .upper()
    )

    payment_status = escape(
        str(invoice.payment_status or "")
        .replace("_", " ")
        .upper()
    )

    amount = f"₹ {invoice.grand_total:,.2f}"

    return f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>BlazeLine Invoice</title>
</head>

<body
    style="
        margin:0;
        padding:0;
        background:#f4f4f4;
        font-family:Arial,Helvetica,sans-serif;
        color:#171717;
    "
>
    <table
        role="presentation"
        width="100%"
        cellspacing="0"
        cellpadding="0"
        border="0"
        style="background:#f4f4f4;padding:32px 12px;"
    >
        <tr>
            <td align="center">

                <table
                    role="presentation"
                    width="100%"
                    cellspacing="0"
                    cellpadding="0"
                    border="0"
                    style="
                        max-width:620px;
                        background:#ffffff;
                        border-radius:10px;
                        overflow:hidden;
                    "
                >

                    <!-- BRAND BAR -->
                    <tr>
                        <td
                            style="
                                height:6px;
                                background:#FF6A00;
                                font-size:0;
                                line-height:0;
                            "
                        >
                        </td>
                    </tr>

                    <!-- HEADER -->
                    <tr>
                        <td style="padding:30px 34px 20px 34px;">

                            <table
                                role="presentation"
                                width="100%"
                                cellspacing="0"
                                cellpadding="0"
                                border="0"
                            >
                                <tr>

                                    <td
                                        valign="top"
                                        style="
                                            font-size:24px;
                                            line-height:28px;
                                            font-weight:800;
                                            color:#0B0B0B;
                                            letter-spacing:-0.4px;
                                        "
                                    >
                                        BLAZELINE
                                    </td>

                                    <td
                                        valign="top"
                                        align="right"
                                        style="
                                            font-size:12px;
                                            line-height:18px;
                                            color:#6B7280;
                                        "
                                    >
                                        TAX INVOICE
                                    </td>

                                </tr>
                            </table>

                            <div
                                style="
                                    margin-top:7px;
                                    font-size:11px;
                                    line-height:16px;
                                    color:#6B7280;
                                "
                            >
                                Accelerating Every Build.
                            </div>

                        </td>
                    </tr>

                    <!-- GREETING -->
                    <tr>
                        <td
                            style="
                                padding:18px 34px 8px 34px;
                            "
                        >

                            <div
                                style="
                                    font-size:18px;
                                    line-height:26px;
                                    font-weight:700;
                                    color:#0B0B0B;
                                "
                            >
                                Hello {customer_name},
                            </div>

                            <div
                                style="
                                    margin-top:8px;
                                    font-size:14px;
                                    line-height:22px;
                                    color:#4B5563;
                                "
                            >
                                Thank you for choosing BlazeLine.
                                Your invoice is attached to this email.
                            </div>

                        </td>
                    </tr>

                    <!-- INVOICE SUMMARY -->
                    <tr>
                        <td style="padding:20px 34px;">

                            <table
                                role="presentation"
                                width="100%"
                                cellspacing="0"
                                cellpadding="0"
                                border="0"
                                style="
                                    background:#F7F7F7;
                                    border:1px solid #E5E7EB;
                                    border-radius:8px;
                                "
                            >
                                <tr>
                                    <td style="padding:18px;">

                                        <table
                                            role="presentation"
                                            width="100%"
                                            cellspacing="0"
                                            cellpadding="0"
                                            border="0"
                                        >

                                            <tr>
                                                <td
                                                    style="
                                                        font-size:11px;
                                                        color:#6B7280;
                                                        padding-bottom:6px;
                                                    "
                                                >
                                                    INVOICE NUMBER
                                                </td>
                                                <td
                                                    align="right"
                                                    style="
                                                        font-size:13px;
                                                        font-weight:700;
                                                        color:#0B0B0B;
                                                        padding-bottom:6px;
                                                    "
                                                >
                                                    {invoice_number}
                                                </td>
                                            </tr>

                                            <tr>
                                                <td
                                                    style="
                                                        font-size:11px;
                                                        color:#6B7280;
                                                        padding:6px 0;
                                                    "
                                                >
                                                    ORDER NUMBER
                                                </td>
                                                <td
                                                    align="right"
                                                    style="
                                                        font-size:13px;
                                                        font-weight:700;
                                                        color:#0B0B0B;
                                                        padding:6px 0;
                                                    "
                                                >
                                                    {order_number}
                                                </td>
                                            </tr>

                                            <tr>
                                                <td
                                                    style="
                                                        font-size:11px;
                                                        color:#6B7280;
                                                        padding:6px 0;
                                                    "
                                                >
                                                    INVOICE DATE
                                                </td>
                                                <td
                                                    align="right"
                                                    style="
                                                        font-size:13px;
                                                        font-weight:700;
                                                        color:#0B0B0B;
                                                        padding:6px 0;
                                                    "
                                                >
                                                    {invoice_date}
                                                </td>
                                            </tr>

                                            <tr>
                                                <td
                                                    style="
                                                        font-size:11px;
                                                        color:#6B7280;
                                                        padding:6px 0;
                                                    "
                                                >
                                                    PAYMENT
                                                </td>
                                                <td
                                                    align="right"
                                                    style="
                                                        font-size:13px;
                                                        font-weight:700;
                                                        color:#15803D;
                                                        padding:6px 0;
                                                    "
                                                >
                                                    {payment_status}
                                                </td>
                                            </tr>

                                            <tr>
                                                <td
                                                    style="
                                                        font-size:11px;
                                                        color:#6B7280;
                                                        padding-top:6px;
                                                    "
                                                >
                                                    TOTAL
                                                </td>
                                                <td
                                                    align="right"
                                                    style="
                                                        font-size:18px;
                                                        line-height:24px;
                                                        font-weight:800;
                                                        color:#FF6A00;
                                                        padding-top:6px;
                                                    "
                                                >
                                                    {amount}
                                                </td>
                                            </tr>

                                        </table>

                                    </td>
                                </tr>
                            </table>

                        </td>
                    </tr>

                    <!-- ATTACHMENT MESSAGE -->
                    <tr>
                        <td
                            style="
                                padding:0 34px 24px 34px;
                            "
                        >

                            <div
                                style="
                                    font-size:13px;
                                    line-height:21px;
                                    color:#4B5563;
                                "
                            >
                                Your official BlazeLine invoice PDF is
                                attached with this email for your records.
                            </div>

                        </td>
                    </tr>

                    <!-- COMPANY -->
                    <tr>
                        <td
                            style="
                                padding:22px 34px;
                                background:#0B0B0B;
                            "
                        >

                            <div
                                style="
                                    font-size:14px;
                                    line-height:20px;
                                    font-weight:700;
                                    color:#FFFFFF;
                                "
                            >
                                BLAZELINE VENTURES PRIVATE LIMITED
                            </div>

                            <div
                                style="
                                    margin-top:5px;
                                    font-size:11px;
                                    line-height:17px;
                                    color:#B8B8B8;
                                "
                            >
                                58/5B B.T Road,
                                Kolkata - 700002
                            </div>

                            <div
                                style="
                                    margin-top:3px;
                                    font-size:11px;
                                    line-height:17px;
                                    color:#B8B8B8;
                                "
                            >
                                GSTIN: 19AAOCB7883M1ZM
                            </div>

                            <div
                                style="
                                    margin-top:10px;
                                    font-size:11px;
                                    line-height:17px;
                                    color:#FF8A33;
                                "
                            >
                                Your Build Should Never Stop.
                            </div>

                        </td>
                    </tr>

                </table>

            </td>
        </tr>
    </table>
</body>
</html>
"""


# ============================================================================
# PUBLIC API
# ============================================================================


def send_invoice_email(
    *,
    invoice: Invoice,
    force: bool = False,
) -> Invoice:
    """
    Send the canonical invoice PDF to the customer's email.

    Idempotency:
        If the invoice is already marked SENT, no second email is sent
        unless force=True.

    The PDF is rendered from the immutable invoice snapshot rather than
    reading mutable order/catalog state.
    """

    email = _require_customer_email(
        invoice
    )

    if (
        invoice.email_status
        == Invoice.DeliveryStatus.SENT
        and not force
    ):
        return invoice

    try:
        # --------------------------------------------------------------
        # Render canonical PDF
        # --------------------------------------------------------------

        pdf_bytes = render_invoice_pdf(
            invoice=invoice
        )

        if not pdf_bytes:
            raise ValidationError(
                "Invoice PDF generation returned empty data."
            )

        # --------------------------------------------------------------
        # Build email
        # --------------------------------------------------------------

        subject = _build_email_subject(
            invoice=invoice
        )

        plain_text = _build_plain_text_body(
            invoice=invoice
        )

        html_body = _build_html_body(
            invoice=invoice
        )

        from_email = getattr(
            settings,
            "DEFAULT_FROM_EMAIL",
            None,
        )

        if not from_email:
            raise ValidationError(
                "DEFAULT_FROM_EMAIL is not configured."
            )

        reply_to = getattr(
            settings,
            "DEFAULT_REPLY_TO_EMAIL",
            None,
        )

        email_message = EmailMultiAlternatives(
            subject=subject,
            body=plain_text,
            from_email=from_email,
            to=[email],
            reply_to=(
                [reply_to]
                if reply_to
                else None
            ),
        )

        email_message.attach_alternative(
            html_body,
            "text/html",
        )

        filename = (
            f"{invoice.invoice_number}.pdf"
        )

        email_message.attach(
            filename,
            pdf_bytes,
            "application/pdf",
        )

        # --------------------------------------------------------------
        # Send
        # --------------------------------------------------------------

        invoice.delivery_attempts += 1

        sent_count = email_message.send(
            fail_silently=False
        )

        if sent_count != 1:
            raise ValidationError(
                "Email backend did not confirm invoice delivery."
            )

        # --------------------------------------------------------------
        # Persist success
        # --------------------------------------------------------------

        invoice.email_status = (
            Invoice.DeliveryStatus.SENT
        )

        invoice.email_sent_at = timezone.now()

        invoice.email_error = ""

        invoice.last_error = ""

        invoice.save(
            update_fields=[
                "email_status",
                "email_sent_at",
                "email_error",
                "last_error",
                "delivery_attempts",
                "updated_at",
            ]
        )

        refresh_invoice_delivery_status(
            invoice=invoice
        )

        logger.info(
            "Invoice email sent successfully.",
            extra={
                "invoice_id": str(invoice.pk),
                "invoice_number": invoice.invoice_number,
                "order_id": invoice.order_id,
            },
        )

        return invoice

    except Exception as exc:
        message = str(exc).strip()

        if len(message) > 4000:
            message = message[:4000]

        invoice.email_status = (
            Invoice.DeliveryStatus.FAILED
        )

        invoice.email_error = message

        invoice.last_error = message

        invoice.save(
            update_fields=[
                "email_status",
                "email_error",
                "last_error",
                "updated_at",
            ]
        )

        logger.exception(
            "Invoice email delivery failed.",
            extra={
                "invoice_id": str(invoice.pk),
                "invoice_number": invoice.invoice_number,
                "order_id": invoice.order_id,
            },
        )

        refresh_invoice_delivery_status(
            invoice=invoice
        )

        raise ValidationError(
            "Invoice email could not be sent."
        ) from exc