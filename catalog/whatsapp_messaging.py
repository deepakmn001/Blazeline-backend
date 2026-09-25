import re

import requests

from django.conf import settings


MSG91_WHATSAPP_SESSION_MESSAGE_URL = (
    "https://control.msg91.com/api/v5/whatsapp/whatsapp-outbound-message/"
)


class WhatsAppMessagingError(Exception):
    """Raised when a WhatsApp session message cannot be sent."""


def normalize_whatsapp_number(phone: str) -> str:
    """
    Return a WhatsApp-compatible number without a leading + sign.
    """
    digits = re.sub(r"\D+", "", str(phone or ""))

    if len(digits) == 10:
        return f"91{digits}"

    if digits.startswith("91") and len(digits) == 12:
        return digits

    if len(digits) >= 10:
        return digits

    raise WhatsAppMessagingError(
        "Customer phone number is not valid for WhatsApp delivery."
    )


def send_whatsapp_session_message(
    *,
    recipient: str,
    message: str,
) -> dict:
    """
    Send a plain-text WhatsApp message during an active
    customer-service session.

    This is intentionally separate from CRM template messaging.
    """

    auth_key = getattr(settings, "MSG91_AUTH_KEY", None)

    integrated_number = getattr(
        settings,
        "MSG91_WHATSAPP_INTEGRATED_NUMBER",
        None,
    )

    if not auth_key or not integrated_number:
        raise WhatsAppMessagingError(
            "MSG91 WhatsApp credentials are not configured."
        )

    normalized_message = str(message or "").strip()

    if not normalized_message:
        raise WhatsAppMessagingError(
            "Message cannot be empty."
        )

    if len(normalized_message) > 4096:
        raise WhatsAppMessagingError(
            "Message is too long. Maximum allowed length is 4096 characters."
        )

    phone_number = normalize_whatsapp_number(recipient)

    payload = {
        "integrated_number": integrated_number,
        "recipient_number": phone_number,
        "content_type": "text",
        "text": normalized_message,
    }

    try:
        response = requests.post(
            MSG91_WHATSAPP_SESSION_MESSAGE_URL,
            headers={
                "Content-Type": "application/json",
                "authkey": auth_key,
            },
            json=payload,
            timeout=10,
        )
    except requests.RequestException as exc:
        raise WhatsAppMessagingError(
            f"WhatsApp connection failed: {exc}"
        ) from exc

    if not response.ok:
        try:
            error_body = response.json()
        except ValueError:
            error_body = response.text.strip()

        raise WhatsAppMessagingError(
            "WhatsApp delivery failed: "
            f"MSG91 HTTP {response.status_code}: "
            f"{str(error_body)[:2000]}"
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
        "provider_accepted": True,
        "provider_message_id": provider_message_id,
        "provider_response": provider_payload,
    }