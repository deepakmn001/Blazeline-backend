import hashlib
import json
from datetime import datetime, timezone as dt_timezone

from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from .models import WhatsAppConversation, WhatsAppMessage


def _digits_only(value):
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _parse_timestamp(value):
    """
    Accepts:
    - ISO timestamps
    - Unix seconds
    - Unix milliseconds
    """
    if value in (None, "", "null"):
        return None

    value = str(value).strip()

    # Unix timestamp
    try:
        numeric = float(value)

        # Milliseconds → seconds
        if numeric > 10_000_000_000:
            numeric = numeric / 1000

        return datetime.fromtimestamp(
            numeric,
            tz=dt_timezone.utc,
        )
    except (ValueError, TypeError, OverflowError):
        pass

    # ISO timestamp
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt_timezone.utc)

        return parsed.astimezone(dt_timezone.utc)
    except (ValueError, TypeError):
        return None


def _extract_message_text(payload):
    """
    Prefer MSG91's direct text field.
    Fall back to common nested/stringified message structures.
    """
    text = payload.get("text")

    if isinstance(text, str) and text.strip():
        return text.strip()

    messages = payload.get("messages")

    if isinstance(messages, str) and messages.strip():
        try:
            messages = json.loads(messages)
        except json.JSONDecodeError:
            messages = None

    if isinstance(messages, list) and messages:
        first = messages[0]

        if isinstance(first, dict):
            nested_text = first.get("text")

            if isinstance(nested_text, dict):
                body = nested_text.get("body")
                if body:
                    return str(body).strip()

            if nested_text:
                return str(nested_text).strip()

    content = payload.get("content")

    if isinstance(content, str) and content.strip():
        try:
            parsed = json.loads(content)

            if isinstance(parsed, dict):
                nested_text = parsed.get("text")

                if isinstance(nested_text, str) and nested_text.strip():
                    return nested_text.strip()
        except json.JSONDecodeError:
            pass

        return content.strip()

    button = payload.get("button")

    if isinstance(button, str) and button.strip():
        try:
            parsed = json.loads(button)

            if isinstance(parsed, dict):
                button_text = parsed.get("text")
                if button_text:
                    return str(button_text).strip()
        except json.JSONDecodeError:
            pass

        return button.strip()

    return ""


def _fallback_message_id(payload, raw_body):
    """
    MSG91 normally provides uuid (WAMID).
    requestId is the secondary fallback.
    If both are missing, use a deterministic body hash.
    """
    uuid_value = str(payload.get("uuid") or "").strip()

    if uuid_value:
        return uuid_value

    request_id = str(payload.get("requestId") or "").strip()

    if request_id:
        return request_id

    digest = hashlib.sha256(raw_body).hexdigest()

    return f"msg91-body-{digest}"


@csrf_exempt
def msg91_whatsapp_inbound_webhook(request):
    """
    Receives inbound WhatsApp messages from MSG91.

    IMPORTANT:
    MSG91 cannot use our normal JWT/admin authentication,
    so this endpoint is protected using a custom webhook secret.
    """

    if request.method != "POST":
        return JsonResponse(
            {"ok": False, "error": "POST method required"},
            status=405,
        )

    expected_secret = str(
        getattr(settings, "MSG91_WHATSAPP_WEBHOOK_SECRET", "")
    ).strip()

    if not expected_secret:
        return JsonResponse(
            {
                "ok": False,
                "error": "Webhook secret is not configured.",
            },
            status=503,
        )

    received_secret = str(
        request.headers.get("X-MSG91-WEBHOOK-SECRET", "")
    ).strip()

    if received_secret != expected_secret:
        return JsonResponse(
            {"ok": False, "error": "Invalid webhook secret"},
            status=403,
        )

    raw_body = request.body

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JsonResponse(
            {"ok": False, "error": "Invalid JSON payload"},
            status=400,
        )

    if not isinstance(payload, dict):
        return JsonResponse(
            {"ok": False, "error": "JSON object expected"},
            status=400,
        )

    # ------------------------------------------------------
    # Validate the WhatsApp integrated number
    # ------------------------------------------------------

    incoming_integrated_number = _digits_only(
        payload.get("integratedNumber")
    )

    configured_integrated_number = _digits_only(
        getattr(
            settings,
            "MSG91_WHATSAPP_INTEGRATED_NUMBER",
            "",
        )
    )

    if (
        configured_integrated_number
        and incoming_integrated_number
        and incoming_integrated_number != configured_integrated_number
    ):
        return JsonResponse(
            {"ok": False, "error": "Unknown integrated number"},
            status=400,
        )

    # ------------------------------------------------------
    # Only process inbound messages
    # direction = 0 → inbound
    # direction = 1 → outbound
    # ------------------------------------------------------

    direction = str(payload.get("direction", "")).strip()

    if direction and direction != "0":
        return JsonResponse(
            {
                "ok": True,
                "ignored": True,
                "reason": "Not an inbound message",
            },
            status=200,
        )

    # ------------------------------------------------------
    # Customer information
    # ------------------------------------------------------

    customer_number = _digits_only(
        payload.get("customerNumber")
    )

    if not customer_number:
        return JsonResponse(
            {"ok": False, "error": "customerNumber is required"},
            status=400,
        )

    customer_name = str(
        payload.get("customerName") or ""
    ).strip()

    integrated_number = (
        incoming_integrated_number
        or configured_integrated_number
    )

    message_type = str(
        payload.get("contentType") or "text"
    ).strip().lower()

    message_text = _extract_message_text(payload)

    provider_message_id = _fallback_message_id(
        payload,
        raw_body,
    )

    provider_request_id = str(
        payload.get("requestId") or ""
    ).strip()

    occurred_at = (
        _parse_timestamp(payload.get("ts"))
        or timezone.now()
    )

    session_expires_at = _parse_timestamp(
        payload.get("conversationExpTimestamp")
    )

    # ------------------------------------------------------
    # Store conversation + message atomically
    # ------------------------------------------------------

    try:
        with transaction.atomic():

            try:
                conversation, created = (
                    WhatsAppConversation.objects
                    .select_for_update()
                    .get_or_create(
                        integrated_number=integrated_number,
                        customer_number=customer_number,
                        defaults={
                            "customer_name": customer_name,
                            "status": WhatsAppConversation.STATUS_OPEN,
                            "unread_count": 0,
                        },
                    )
                )
            except IntegrityError:
                conversation = (
                    WhatsAppConversation.objects
                    .select_for_update()
                    .get(
                        integrated_number=integrated_number,
                        customer_number=customer_number,
                    )
                )
                created = False

            # --------------------------------------------------
            # Duplicate protection
            # --------------------------------------------------

            message, message_created = (
                WhatsAppMessage.objects.get_or_create(
                    provider_message_id=provider_message_id,
                    defaults={
                        "conversation": conversation,
                        "direction": WhatsAppMessage.DIRECTION_INBOUND,
                        "message_type": message_type,
                        "text": message_text,
                        "provider_request_id": provider_request_id,
                        "status": WhatsAppMessage.STATUS_RECEIVED,
                        "raw_payload": payload,
                        "occurred_at": occurred_at,
                    },
                )
            )

            # --------------------------------------------------
            # Existing duplicate webhook → ignore safely
            # --------------------------------------------------

            if not message_created:
                return JsonResponse(
                    {
                        "ok": True,
                        "duplicate": True,
                        "message_id": message.provider_message_id,
                    },
                    status=200,
                )

            # --------------------------------------------------
            # Update conversation
            # --------------------------------------------------

            conversation.status = WhatsAppConversation.STATUS_OPEN

            if customer_name:
                conversation.customer_name = customer_name

            conversation.last_message_preview = (
                message_text[:500]
                if message_text
                else f"[{message_type}]"
            )

            conversation.last_message_at = occurred_at
            conversation.last_inbound_at = occurred_at
            conversation.session_expires_at = session_expires_at
            conversation.unread_count += 1

            conversation.save(
                update_fields=[
                    "status",
                    "customer_name",
                    "last_message_preview",
                    "last_message_at",
                    "last_inbound_at",
                    "session_expires_at",
                    "unread_count",
                    "updated_at",
                ]
            )

    except Exception:
        # Return 500 so MSG91 can retry temporary failures.
        raise

    return JsonResponse(
        {
            "ok": True,
            "created": True,
            "conversation_id": str(conversation.id),
            "message_id": message.provider_message_id,
        },
        status=200,
    )