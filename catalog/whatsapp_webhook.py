"""
BlazeLine — MSG91 WhatsApp Inbound Webhook
Production-hardened implementation.

Responsibilities:
- Authenticate MSG91 using a shared webhook secret.
- Validate and normalize inbound payloads.
- Extract customer text safely from MSG91's supported payload shapes.
- Persist conversation + message atomically.
- Protect against duplicate webhook delivery using provider_message_id.
- Recover once from transient database connection/interface failures.
- Recover once from concurrent conversation creation races.
- Never convert an unexpected persistence failure into a fake 200.
- Log failures without exposing secrets or full customer numbers.
"""

import hashlib
import hmac
import json
import logging
import time
import uuid
from datetime import datetime, timezone as dt_timezone

from django.conf import settings
from django.db import (
    IntegrityError,
    InterfaceError,
    OperationalError,
    connection,
    transaction,
)
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from .models import WhatsAppConversation, WhatsAppMessage


logger = logging.getLogger(__name__)

# Keep webhook retries intentionally small.
# MSG91/provider-level retries remain the final fallback.
_STORAGE_MAX_ATTEMPTS = 2
_RETRY_DELAYS_SECONDS = (0.05, 0.20)

# MSG91 webhook payloads are normally tiny. This is a safety guard against
# accidentally accepting an unexpectedly large request body.
_MAX_WEBHOOK_BODY_BYTES = 2 * 1024 * 1024


def _digits_only(value):
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _mask_number(value):
    """
    Keep only the last 4 digits for operational logs.
    """
    digits = _digits_only(value)

    if len(digits) <= 4:
        return digits or "-"

    return f"***{digits[-4:]}"


def _short_id(value, length=16):
    """
    Keep provider identifiers useful for correlation without logging the
    complete provider ID.
    """
    value = str(value or "").strip()

    if not value:
        return "-"

    if len(value) <= length:
        return value

    return f"...{value[-length:]}"


def _request_correlation_id(request):
    """
    Reuse an upstream request ID when available, otherwise generate one.
    """
    supplied = str(
        request.headers.get("X-Request-ID")
        or request.headers.get("X-Correlation-ID")
        or ""
    ).strip()

    if supplied:
        return supplied[:128]

    return uuid.uuid4().hex


def _parse_timestamp(value):
    """
    Accept:
    - ISO timestamps
    - Unix seconds
    - Unix milliseconds

    Always return an aware UTC datetime.
    """
    if value in (None, "", "null"):
        return None

    value = str(value).strip()

    # Unix timestamp.
    try:
        numeric = float(value)

        # Milliseconds → seconds.
        if numeric > 10_000_000_000:
            numeric /= 1000

        return datetime.fromtimestamp(
            numeric,
            tz=dt_timezone.utc,
        )

    except (ValueError, TypeError, OverflowError):
        pass

    # ISO timestamp.
    try:
        parsed = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt_timezone.utc)

        return parsed.astimezone(dt_timezone.utc)

    except (ValueError, TypeError):
        return None


def _loads_if_json(value):
    """
    Parse stringified JSON dictionaries/lists where MSG91 sends them as
    strings. Return None on invalid JSON.
    """
    if not isinstance(value, str):
        return value

    value = value.strip()

    if not value:
        return None

    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _extract_message_text(payload):
    """
    Extract customer-facing text without accidentally storing the ad/referral
    creative itself as the customer's message.

    Priority:
    1. MSG91 direct text
    2. nested message text.body
    3. nested message text
    4. nested referral.text
    5. string/dict content
    6. button text
    7. media caption
    """
    # 1. Direct MSG91 text.
    direct_text = payload.get("text")

    if isinstance(direct_text, str) and direct_text.strip():
        return direct_text.strip()

    # 2–4. Nested message structures.
    messages = _loads_if_json(payload.get("messages"))

    if isinstance(messages, list) and messages:
        first = messages[0]

        if isinstance(first, dict):
            nested_text = first.get("text")

            if isinstance(nested_text, dict):
                body = nested_text.get("body")

                if body:
                    return str(body).strip()

            elif nested_text:
                return str(nested_text).strip()

            # Some referral-shaped payloads keep the customer text inside
            # referral. Never use referral["body"] here because that is the
            # advertisement/creative body, not the user's message.
            referral = first.get("referral")

            if isinstance(referral, dict):
                referral_text = referral.get("text")

                if isinstance(referral_text, dict):
                    body = referral_text.get("body")

                    if body:
                        return str(body).strip()

                elif referral_text:
                    return str(referral_text).strip()

    # 5. Content.
    content = payload.get("content")

    if isinstance(content, dict):
        nested_text = content.get("text")

        if isinstance(nested_text, dict):
            body = nested_text.get("body")

            if body:
                return str(body).strip()

        elif nested_text:
            return str(nested_text).strip()

        body = content.get("body")

        if body:
            return str(body).strip()

    elif isinstance(content, str) and content.strip():
        parsed = _loads_if_json(content)

        if isinstance(parsed, dict):
            nested_text = parsed.get("text")

            if isinstance(nested_text, dict):
                body = nested_text.get("body")

                if body:
                    return str(body).strip()

            elif nested_text and str(nested_text).strip():
                return str(nested_text).strip()

            body = parsed.get("body")

            if body:
                return str(body).strip()

        return content.strip()

    # 6. Button / reply text.
    button = payload.get("button")

    if isinstance(button, dict):
        button_text = button.get("text") or button.get("title")

        if button_text:
            return str(button_text).strip()

    elif isinstance(button, str) and button.strip():
        parsed = _loads_if_json(button)

        if isinstance(parsed, dict):
            button_text = parsed.get("text") or parsed.get("title")

            if button_text:
                return str(button_text).strip()

        return button.strip()

    # 7. Media caption.
    caption = payload.get("caption")

    if isinstance(caption, str) and caption.strip():
        return caption.strip()

    return ""


def _fallback_message_id(payload, raw_body):
    """
    MSG91 normally provides uuid (WhatsApp WAMID).
    requestId is the secondary fallback.
    If both are missing, use a deterministic body hash.

    The deterministic fallback preserves idempotency for an identical retry.
    """
    uuid_value = str(payload.get("uuid") or "").strip()

    if uuid_value:
        return uuid_value

    request_id = str(payload.get("requestId") or "").strip()

    if request_id:
        return request_id

    digest = hashlib.sha256(raw_body).hexdigest()

    return f"msg91-body-{digest}"


def _normalize_message_id(value):
    """
    The database field is max_length=255. Provider IDs are expected to be
    short, but avoid a DB-level length exception if a malformed/unknown
    provider sends an oversized ID.
    """
    value = str(value or "").strip()

    if len(value) <= 255:
        return value

    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()

    return f"msg91-id-{digest}"


def _normalize_customer_name(value):
    return str(value or "").strip()[:150]


def _normalize_message_type(payload):
    """
    Prefer the actual WhatsApp message type over contentType.
    MSG91 may send contentType='referral' while messageType='text'.
    """
    value = str(
        payload.get("messageType")
        or payload.get("contentType")
        or "text"
    ).strip().lower()

    return (value or "text")[:30]


def _store_whatsapp_event(
    *,
    integrated_number,
    customer_number,
    customer_name,
    message_type,
    message_text,
    provider_message_id,
    provider_request_id,
    occurred_at,
    session_expires_at,
    payload,
):
    """
    Persist one inbound WhatsApp event.

    The whole unit is retried from the beginning, never from inside a broken
    transaction. This is important for both transient DB failures and
    concurrent conversation creation races.
    """
    for attempt in range(_STORAGE_MAX_ATTEMPTS):
        try:
            with transaction.atomic():
                conversation, conversation_created = (
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

                # Provider retry / duplicate delivery.
                if not message_created:
                    return {
                        "conversation": conversation,
                        "message": message,
                        "conversation_created": False,
                        "message_created": False,
                    }

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

                return {
                    "conversation": conversation,
                    "message": message,
                    "conversation_created": conversation_created,
                    "message_created": True,
                }

        except (OperationalError, InterfaceError):
            # The connection may have become unusable. Close it and retry the
            # entire transaction on a clean connection.
            connection.close()

            if attempt + 1 >= _STORAGE_MAX_ATTEMPTS:
                raise

            time.sleep(_RETRY_DELAYS_SECONDS[attempt])

        except IntegrityError:
            # A race may occur when two webhook deliveries for the same
            # customer arrive concurrently and both try to create the
            # conversation. The failed atomic block has already rolled back;
            # retry the entire transaction from the outside.
            if attempt + 1 >= _STORAGE_MAX_ATTEMPTS:
                raise

            time.sleep(_RETRY_DELAYS_SECONDS[attempt])

    # Defensive fallback; the loop either returns or raises.
    raise RuntimeError("WhatsApp event storage exhausted retry attempts.")


@csrf_exempt
def msg91_whatsapp_inbound_webhook(request):
    """
    Receives inbound WhatsApp messages from MSG91.

    Security:
    - POST only
    - shared webhook secret using constant-time comparison

    Reliability:
    - atomic DB persistence
    - duplicate-safe
    - one controlled retry for transient DB failures/races
    - exceptions remain 500 so MSG91 can retry rather than losing data
    """
    request_id = _request_correlation_id(request)

    if request.method != "POST":
        return JsonResponse(
            {
                "ok": False,
                "error": "POST method required",
            },
            status=405,
        )

    expected_secret = str(
        getattr(
            settings,
            "MSG91_WHATSAPP_WEBHOOK_SECRET",
            "",
        )
    ).strip()

    if not expected_secret:
        logger.critical(
            "MSG91 webhook unavailable: secret not configured | request_id=%s",
            request_id,
        )

        return JsonResponse(
            {
                "ok": False,
                "error": "Webhook secret is not configured.",
            },
            status=503,
        )

    received_secret = str(
        request.headers.get(
            "X-MSG91-WEBHOOK-SECRET",
            "",
        )
    ).strip()

    if not received_secret or not hmac.compare_digest(
        received_secret,
        expected_secret,
    ):
        logger.warning(
            "MSG91 webhook authentication failed | request_id=%s",
            request_id,
        )

        return JsonResponse(
            {
                "ok": False,
                "error": "Invalid webhook secret",
            },
            status=403,
        )

    # Reject unexpectedly large payloads before JSON parsing.
    content_length = request.META.get("CONTENT_LENGTH")

    try:
        if content_length and int(content_length) > _MAX_WEBHOOK_BODY_BYTES:
            return JsonResponse(
                {
                    "ok": False,
                    "error": "Webhook payload too large",
                },
                status=413,
            )
    except (TypeError, ValueError):
        pass

    raw_body = request.body

    if len(raw_body) > _MAX_WEBHOOK_BODY_BYTES:
        return JsonResponse(
            {
                "ok": False,
                "error": "Webhook payload too large",
            },
            status=413,
        )

    try:
        payload = json.loads(
            raw_body.decode("utf-8")
        )
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JsonResponse(
            {
                "ok": False,
                "error": "Invalid JSON payload",
            },
            status=400,
        )

    if not isinstance(payload, dict):
        return JsonResponse(
            {
                "ok": False,
                "error": "JSON object expected",
            },
            status=400,
        )

    # ------------------------------------------------------
    # Validate integrated WhatsApp number
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
        and incoming_integrated_number
        != configured_integrated_number
    ):
        logger.warning(
            "MSG91 webhook rejected: unknown integrated number | "
            "request_id=%s | integrated_number=%s",
            request_id,
            _mask_number(incoming_integrated_number),
        )

        return JsonResponse(
            {
                "ok": False,
                "error": "Unknown integrated number",
            },
            status=400,
        )

    integrated_number = (
        incoming_integrated_number
        or configured_integrated_number
    )

    if not integrated_number:
        return JsonResponse(
            {
                "ok": False,
                "error": "integratedNumber is required",
            },
            status=400,
        )

    # ------------------------------------------------------
    # Only process inbound messages.
    # direction = 0 → inbound
    # direction = 1 → outbound
    # ------------------------------------------------------

    direction = str(
        payload.get("direction", "")
    ).strip()

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
            {
                "ok": False,
                "error": "customerNumber is required",
            },
            status=400,
        )

    customer_name = _normalize_customer_name(
        payload.get("customerName")
    )

    message_type = _normalize_message_type(payload)
    message_text = _extract_message_text(payload)

    provider_message_id = _normalize_message_id(
        _fallback_message_id(
            payload,
            raw_body,
        )
    )

    provider_request_id = str(
        payload.get("requestId") or ""
    ).strip()[:255]

    occurred_at = (
        _parse_timestamp(
            payload.get("ts")
        )
        or timezone.now()
    )

    session_expires_at = _parse_timestamp(
        payload.get("conversationExpTimestamp")
    )

    # ------------------------------------------------------
    # Durable storage
    # ------------------------------------------------------

    try:
        result = _store_whatsapp_event(
            integrated_number=integrated_number,
            customer_number=customer_number,
            customer_name=customer_name,
            message_type=message_type,
            message_text=message_text,
            provider_message_id=provider_message_id,
            provider_request_id=provider_request_id,
            occurred_at=occurred_at,
            session_expires_at=session_expires_at,
            payload=payload,
        )

    except Exception:
        logger.exception(
            "MSG91 webhook persistence failed | "
            "request_id=%s | customer=%s | message_id=%s | "
            "request_msg_id=%s | type=%s",
            request_id,
            _mask_number(customer_number),
            _short_id(provider_message_id),
            _short_id(provider_request_id),
            message_type,
        )

        # IMPORTANT:
        # Do NOT convert unexpected persistence errors into HTTP 200.
        # MSG91 must see the failure so it can retry the event.
        raise

    conversation = result["conversation"]
    message = result["message"]
    message_created = result["message_created"]
    conversation_created = result["conversation_created"]

    if not message_created:
        logger.info(
            "MSG91 duplicate webhook accepted | "
            "request_id=%s | customer=%s | message_id=%s",
            request_id,
            _mask_number(customer_number),
            _short_id(message.provider_message_id),
        )

        return JsonResponse(
            {
                "ok": True,
                "duplicate": True,
                "conversation_id": str(
                    conversation.id
                ),
                "message_id": message.provider_message_id,
            },
            status=200,
        )

    logger.info(
        "MSG91 inbound webhook persisted | "
        "request_id=%s | customer=%s | message_id=%s | "
        "conversation_id=%s",
        request_id,
        _mask_number(customer_number),
        _short_id(message.provider_message_id),
        conversation.id,
    )

    return JsonResponse(
        {
            "ok": True,
            "created": True,
            "conversation_created": conversation_created,
            "conversation_id": str(conversation.id),
            "message_id": message.provider_message_id,
        },
        status=200,
    )
