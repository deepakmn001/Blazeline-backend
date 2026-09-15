from __future__ import annotations

import logging
from datetime import timedelta

import cloudinary
import cloudinary.uploader
import cloudinary.utils

from django.core.exceptions import ValidationError
from django.utils import timezone

from .models import Invoice


logger = logging.getLogger(__name__)


# ============================================================================
# CONFIG
# ============================================================================

INVOICE_RESOURCE_TYPE = "raw"
INVOICE_DELIVERY_TYPE = "authenticated"
INVOICE_FORMAT = "pdf"

# Short-lived access URL.
# This is deliberately not stored permanently in the database.
DEFAULT_DOWNLOAD_TTL = timedelta(hours=24)


# ============================================================================
# VALIDATION
# ============================================================================


def _validate_pdf_bytes(pdf_bytes: bytes) -> None:
    if not isinstance(pdf_bytes, bytes):
        raise ValidationError(
            "Invoice PDF must be provided as bytes."
        )

    if not pdf_bytes:
        raise ValidationError(
            "Invoice PDF is empty."
        )

    if not pdf_bytes.startswith(b"%PDF"):
        raise ValidationError(
            "Invoice PDF data is invalid."
        )


# ============================================================================
# PUBLIC ID
# ============================================================================


def invoice_public_id(invoice: Invoice) -> str:
    """
    Return the deterministic Cloudinary public ID for an invoice.

    No secrets or customer PII are included in the public ID.
    """

    if not invoice.invoice_number:
        raise ValidationError(
            "Invoice number is required before Cloudinary upload."
        )

    return (
        f"invoices/{invoice.invoice_number}"
    )


# ============================================================================
# UPLOAD
# ============================================================================


def upload_invoice_pdf(
    *,
    invoice: Invoice,
    pdf_bytes: bytes,
) -> dict:
    """
    Upload the canonical invoice PDF to Cloudinary.

    Security:
    - raw asset
    - authenticated delivery
    - deterministic public ID
    - overwrite enabled so regeneration cannot create duplicates
    - no public customer-facing URL is persisted
    """

    _validate_pdf_bytes(pdf_bytes)

    public_id = invoice_public_id(
        invoice
    )

    try:
        result = cloudinary.uploader.upload(
            pdf_bytes,
            resource_type=INVOICE_RESOURCE_TYPE,
            type=INVOICE_DELIVERY_TYPE,
            public_id=public_id,
            format=INVOICE_FORMAT,
            overwrite=True,
            invalidate=True,
            use_filename=False,
            unique_filename=False,
        )
    except Exception as exc:
        logger.exception(
            "Invoice PDF upload to Cloudinary failed.",
            extra={
                "invoice_id": str(invoice.pk),
                "invoice_number": invoice.invoice_number,
            },
        )

        raise ValidationError(
            "Invoice PDF could not be stored."
        ) from exc

    returned_public_id = str(
        result.get("public_id") or public_id
    ).strip()

    if not returned_public_id:
        raise ValidationError(
            "Cloudinary did not return an invoice public ID."
        )

    invoice.pdf_public_id = returned_public_id

    # Do not persist secure_url.
    #
    # Authenticated Cloudinary assets require signed access URLs.
    # A signed URL is intentionally generated only when needed.
    invoice.pdf_url = ""

    invoice.save(
        update_fields=[
            "pdf_public_id",
            "pdf_url",
            "updated_at",
        ]
    )

    logger.info(
        "Invoice PDF uploaded to Cloudinary.",
        extra={
            "invoice_id": str(invoice.pk),
            "invoice_number": invoice.invoice_number,
            "public_id": returned_public_id,
        },
    )

    return {
        "public_id": returned_public_id,
        "resource_type": INVOICE_RESOURCE_TYPE,
        "delivery_type": INVOICE_DELIVERY_TYPE,
    }


# ============================================================================
# SIGNED DOWNLOAD URL
# ============================================================================


def generate_invoice_download_url(
    *,
    invoice: Invoice,
    expires_in: timedelta = DEFAULT_DOWNLOAD_TTL,
    attachment: bool = False,
) -> str:
    """
    Generate a time-limited signed Cloudinary URL.

    The URL is generated on demand and is NOT stored permanently because
    authenticated download URLs expire.

    Used later for:
        - WhatsApp invoice links
        - customer invoice download
        - admin invoice download
    """

    public_id = str(
        invoice.pdf_public_id or ""
    ).strip()

    if not public_id:
        raise ValidationError(
            "Invoice PDF has not been uploaded to Cloudinary."
        )

    if expires_in.total_seconds() <= 0:
        raise ValidationError(
            "Invoice download expiration must be positive."
        )

    expires_at = int(
        (
            timezone.now() + expires_in
        ).timestamp()
    )

    try:
        url = cloudinary.utils.private_download_url(
            public_id=public_id,
            format=INVOICE_FORMAT,
            resource_type=INVOICE_RESOURCE_TYPE,
            type=INVOICE_DELIVERY_TYPE,
            expires_at=expires_at,
            attachment=attachment,
            secure=True,
        )
    except Exception as exc:
        logger.exception(
            "Failed to generate invoice download URL.",
            extra={
                "invoice_id": str(invoice.pk),
                "invoice_number": invoice.invoice_number,
            },
        )

        raise ValidationError(
            "Invoice download link could not be generated."
        ) from exc

    if not url:
        raise ValidationError(
            "Cloudinary returned an empty invoice download URL."
        )

    return str(url)