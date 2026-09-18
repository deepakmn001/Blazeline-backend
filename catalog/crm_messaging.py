"""Outbound messaging helpers for the admin CRM.

Uses the existing Django SMTP configuration for email and the existing MSG91
WhatsApp Business integration for approved template messages. WhatsApp CRM
messaging uses a dedicated template and never reuses the OTP template.
"""

import re

import requests
from django.conf import settings
from django.core.mail import send_mail


MSG91_WHATSAPP_BULK_TEMPLATE_URL = (
    "https://api.msg91.com/api/v5/whatsapp/whatsapp-outbound-message/bulk/"
)


class CRMMessageError(Exception):
    """Raised when a CRM outbound message cannot be delivered."""


def _normalize_whatsapp_number(phone: str) -> str:
    """Return a WhatsApp-compatible number without a leading + sign."""
    digits = re.sub(r"\D+", "", str(phone or ""))

    if len(digits) == 10:
        return f"91{digits}"

    if digits.startswith("91") and len(digits) == 12:
        return digits

    if len(digits) >= 10:
        return digits

    raise CRMMessageError(
        "Customer phone number is not valid for WhatsApp delivery."
    )


def _normalize_whatsapp_template_value(
    value: str,
    *,
    field_name: str = "Message",
) -> str:
    """Normalize a WhatsApp template text variable.

    MSG91 rejects newline characters inside template variable values.
    The approved WhatsApp template itself owns the message layout, so
    variable values are flattened to a single line.
    """
    normalized = re.sub(r"[\r\n]+", " ", str(value or ""))
    normalized = re.sub(r"[ \t]+", " ", normalized).strip()

    if not normalized:
        raise CRMMessageError(f"{field_name} cannot be empty.")

    return normalized


def send_crm_email(*, recipient: str, subject: str, message: str) -> dict:
    """Send a CRM email using the existing Django SMTP configuration."""
    if not recipient or not str(recipient).strip():
        raise CRMMessageError("Customer email address is required.")

    if not message or not str(message).strip():
        raise CRMMessageError("Message cannot be empty.")

    try:
        sent = send_mail(
            subject=subject,
            message=message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[recipient],
            fail_silently=False,
        )
    except Exception as exc:  # pragma: no cover - SMTP/provider specific
        raise CRMMessageError(f"Email delivery failed: {exc}") from exc

    if sent != 1:
        raise CRMMessageError(
            "Email delivery was not accepted by the configured mail backend."
        )

    return {
        "provider": "smtp",
        "delivered": True,
        "provider_accepted": True,
    }


def send_crm_whatsapp(
    *,
    recipient: str,
    customer_name: str,
    message: str,
) -> dict:
    """Send one approved CRM WhatsApp template message through MSG91.

    Template variables:
      body_1 -> customer name
      body_2 -> admin's personalized message

    The fixed brand voice, spacing, greeting, and closing are owned by the
    approved MSG91 template itself.
    """
    auth_key = getattr(settings, "MSG91_AUTH_KEY", None)
    integrated_number = getattr(
        settings,
        "MSG91_WHATSAPP_INTEGRATED_NUMBER",
        None,
    )
    template_name = getattr(
        settings,
        "MSG91_CRM_WHATSAPP_TEMPLATE_NAME",
        None,
    )
    template_language = getattr(
        settings,
        "MSG91_CRM_WHATSAPP_TEMPLATE_LANGUAGE",
        "en",
    )
    template_namespace = getattr(
        settings,
        "MSG91_CRM_WHATSAPP_TEMPLATE_NAMESPACE",
        None,
    )

    if not auth_key or not integrated_number:
        raise CRMMessageError(
            "MSG91 WhatsApp credentials are not configured."
        )

    if not template_name:
        raise CRMMessageError(
            "CRM WhatsApp template is not configured. "
            "Set MSG91_CRM_WHATSAPP_TEMPLATE_NAME."
        )

    if not template_namespace:
        raise CRMMessageError(
            "CRM WhatsApp template namespace is not configured. "
            "Set MSG91_CRM_WHATSAPP_TEMPLATE_NAMESPACE."
        )

    phone_number = _normalize_whatsapp_number(recipient)

    customer_name_value = _normalize_whatsapp_template_value(
        customer_name,
        field_name="Customer name",
    )
    message_value = _normalize_whatsapp_template_value(
        message,
        field_name="Message",
    )

    payload = {
        "integrated_number": integrated_number,
        "content_type": "template",
        "payload": {
            "messaging_product": "whatsapp",
            "type": "template",
            "template": {
                "name": template_name,
                "language": {
                    "code": template_language,
                    "policy": "deterministic",
                },
                "namespace": template_namespace,
                "to_and_components": [
                    {
                        "to": [phone_number],
                        "components": {
                            "body_1": {
                                "type": "text",
                                "value": customer_name_value,
                            },
                            "body_2": {
                                "type": "text",
                                "value": message_value,
                            },
                        },
                    }
                ],
            },
        },
    }

    try:
        response = requests.post(
            MSG91_WHATSAPP_BULK_TEMPLATE_URL,
            headers={
                "Content-Type": "application/json",
                "authkey": auth_key,
            },
            json=payload,
            timeout=10,
        )
    except requests.RequestException as exc:  # pragma: no cover
        raise CRMMessageError(
            f"WhatsApp connection failed: {exc}"
        ) from exc

    if not response.ok:
        try:
            error_body = response.json()
        except ValueError:
            error_body = response.text.strip()

        if isinstance(error_body, (dict, list)):
            error_text = str(error_body)
        else:
            error_text = str(
                error_body or "No response body returned."
            )

        raise CRMMessageError(
            "WhatsApp delivery failed: "
            f"MSG91 HTTP {response.status_code}: {error_text[:2000]}"
        )

    try:
        provider_payload = response.json()
    except ValueError:
        provider_payload = {}

    provider_message_id = (
        provider_payload.get("message_id")
        or provider_payload.get("request_id")
        or provider_payload.get("requestId")
    )

    return {
        "provider": "msg91",
        # Backward-compatible field: MSG91 accepted the request. This is not
        # a final Delivered/Read receipt.
        "delivered": True,
        "provider_accepted": True,
        "provider_message_id": provider_message_id,
        "provider_response": provider_payload,
    }
