"""
Production-grade customer authentication/session audit service.

This module is the server-side source-of-truth for customer authentication
sessions and authentication/security events. It is intentionally separate
from analytics/session tracking.

Design goals:
    - One server-side auth session per real customer login/device session.
    - JWTs reference the auth session via a stable UUID claim.
    - PostgreSQL session state is authoritative for every authenticated request.
    - Session activity writes are throttled to avoid a DB write per request.
    - Authentication/security events are append-only at the Django ORM layer.
    - Failed-login correlation stores a keyed HMAC hash of the identifier,
      never the raw attempted password/OTP/token.
    - Client IP extraction is proxy-aware and does not blindly trust
      X-Forwarded-For.
    - User-agent details are parsed centrally so login/session history stays
      consistent across the API.

Expected model API (accounts.models):
    CustomerAuthSession
    CustomerAuthEvent

Recommended settings (all have safe defaults here):
    AUTH_SESSION_LIFETIME_SECONDS = 60 * 60 * 24 * 30
    AUTH_SESSION_ACTIVITY_UPDATE_INTERVAL_SECONDS = 60
    AUTH_SESSION_ONLINE_WINDOW_SECONDS = 300
    AUTH_AUDIT_MAX_METADATA_BYTES = 16 * 1024
    AUTH_TRUSTED_PROXY_CIDRS = []
    AUTH_TRUST_PROXY_HEADERS = False

For production behind a reverse proxy/load balancer, configure
AUTH_TRUSTED_PROXY_CIDRS with the proxy/load-balancer source networks and
set AUTH_TRUST_PROXY_HEADERS = True. Never enable blanket proxy-header trust
without controlling the network path to the application.

User-agent parsing uses ua-parser when installed. Add `ua-parser` to the
production requirements. A conservative fallback is retained so this module
does not make the entire authentication stack fail solely because a parser
package is missing during a deployment transition; the raw user-agent is
still preserved for audit purposes.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import re
from datetime import timedelta
from typing import Any, Iterable
from uuid import UUID

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import Customer, CustomerAuthEvent, CustomerAuthSession

try:  # Optional during deployment transitions; required in production.
    from ua_parser import user_agent_parser
except ImportError:  # pragma: no cover - exercised only when dependency is absent.
    user_agent_parser = None


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class AuthAuditError(Exception):
    """Base exception for authentication-audit service failures."""


class AuthSessionNotFoundError(AuthAuditError):
    """The referenced server-side authentication session does not exist."""


class AuthSessionInvalidError(AuthAuditError):
    """The referenced server-side authentication session is not valid."""


class AuthSessionExpiredError(AuthSessionInvalidError):
    """The referenced server-side authentication session has expired."""


class AuthSessionOwnershipError(AuthSessionInvalidError):
    """The referenced session belongs to a different customer."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


DEFAULT_SESSION_LIFETIME_SECONDS = 60 * 60 * 24 * 30
DEFAULT_ACTIVITY_UPDATE_INTERVAL_SECONDS = 60
DEFAULT_ONLINE_WINDOW_SECONDS = 5 * 60
DEFAULT_MAX_METADATA_BYTES = 16 * 1024


_SENSITIVE_KEY_PARTS = (
    "password",
    "passwd",
    "passcode",
    "otp",
    "token",
    "secret",
    "authorization",
    "cookie",
    "set-cookie",
    "credential",
    "credentials",
    "private_key",
    "access_key",
    "refresh",
)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ---------------------------------------------------------------------------
# Small settings helpers
# ---------------------------------------------------------------------------


def _setting_int(name: str, default: int, *, minimum: int = 0) -> int:
    value = getattr(settings, name, default)
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = default
    return max(value, minimum)


def _session_lifetime() -> timedelta:
    return timedelta(
        seconds=_setting_int(
            "AUTH_SESSION_LIFETIME_SECONDS",
            DEFAULT_SESSION_LIFETIME_SECONDS,
            minimum=60,
        )
    )


def _activity_update_interval() -> timedelta:
    return timedelta(
        seconds=_setting_int(
            "AUTH_SESSION_ACTIVITY_UPDATE_INTERVAL_SECONDS",
            DEFAULT_ACTIVITY_UPDATE_INTERVAL_SECONDS,
            minimum=1,
        )
    )


def _online_window() -> timedelta:
    return timedelta(
        seconds=_setting_int(
            "AUTH_SESSION_ONLINE_WINDOW_SECONDS",
            DEFAULT_ONLINE_WINDOW_SECONDS,
            minimum=1,
        )
    )


def _max_metadata_bytes() -> int:
    return _setting_int(
        "AUTH_AUDIT_MAX_METADATA_BYTES",
        DEFAULT_MAX_METADATA_BYTES,
        minimum=1024,
    )


def _trusted_proxy_networks() -> list[ipaddress._BaseNetwork]:
    raw = getattr(settings, "AUTH_TRUSTED_PROXY_CIDRS", []) or []
    if isinstance(raw, str):
        raw = [raw]

    networks: list[ipaddress._BaseNetwork] = []
    for value in raw:
        try:
            networks.append(ipaddress.ip_network(str(value).strip(), strict=False))
        except ValueError:
            logger.warning("Ignoring invalid AUTH_TRUSTED_PROXY_CIDRS entry: %r", value)
    return networks


def _trust_proxy_headers() -> bool:
    return bool(getattr(settings, "AUTH_TRUST_PROXY_HEADERS", False))


# ---------------------------------------------------------------------------
# Client/request metadata
# ---------------------------------------------------------------------------


def _parse_ip(value: str | None) -> ipaddress._BaseAddress | None:
    if not value:
        return None
    try:
        return ipaddress.ip_address(value.strip())
    except ValueError:
        return None


def get_client_ip(request) -> str | None:
    if request is None:
        return None
    """
    Return the client IP using a trust-boundary-aware strategy.

    Without explicit proxy trust, REMOTE_ADDR is authoritative and proxy
    headers are ignored.

    With trusted proxies configured, X-Forwarded-For is walked from right to
    left and the first non-trusted hop is selected. This prevents a client
    from simply prepending an arbitrary address to a trusted proxy chain.
    """
    remote_addr = _parse_ip(request.META.get("REMOTE_ADDR"))

    if not _trust_proxy_headers():
        return str(remote_addr) if remote_addr else None

    trusted_networks = _trusted_proxy_networks()
    if not remote_addr:
        return None

    def is_trusted(address: ipaddress._BaseAddress) -> bool:
        return any(address in network for network in trusted_networks)

    # If the immediate peer is not trusted, headers are not authoritative.
    if not is_trusted(remote_addr):
        return str(remote_addr)

    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    chain = [item.strip() for item in forwarded.split(",") if item.strip()]

    # X-Forwarded-For is conventionally client, proxy1, proxy2, ...
    # Walking from the nearest proxy back toward the client is safest.
    for candidate in reversed(chain):
        parsed = _parse_ip(candidate)
        if parsed is None:
            continue
        if not is_trusted(parsed):
            return str(parsed)

    # All hops were trusted, so the oldest valid address is the best available
    # client candidate; if none exists, fall back to the immediate proxy IP.
    for candidate in chain:
        parsed = _parse_ip(candidate)
        if parsed is not None:
            return str(parsed)

    return str(remote_addr)


def get_request_user_agent(request) -> str:
    if request is None:
        return ""

    value = ""
    try:
        value = request.headers.get("User-Agent", "")
    except AttributeError:
        value = request.META.get("HTTP_USER_AGENT", "")

    value = str(value or "").strip()

    # Browser user-agent strings can be very large. Preserve enough for useful
    # audit evidence while protecting the auth tables from oversized headers.
    return value[:2048]


def _format_version(parts: dict[str, Any]) -> str:
    values = [
        str(parts.get("major") or "").strip(),
        str(parts.get("minor") or "").strip(),
        str(parts.get("patch") or "").strip(),
    ]
    while values and not values[-1]:
        values.pop()
    return ".".join(values)


def _parse_user_agent_fallback(user_agent: str) -> tuple[str, str, str]:
    """Conservative fallback parser used only when ua-parser is unavailable."""
    value = user_agent.lower()

    device = "Desktop"
    if "bot" in value or "spider" in value or "crawler" in value:
        device = "Bot"
    elif "ipad" in value or "tablet" in value:
        device = "Tablet"
    elif any(marker in value for marker in ("mobile", "android", "iphone", "ipod")):
        device = "Mobile"

    if "edg/" in value:
        browser = "Edge"
    elif "opr/" in value or "opera" in value:
        browser = "Opera"
    elif "chrome/" in value and "chromium" not in value:
        browser = "Chrome"
    elif "firefox/" in value:
        browser = "Firefox"
    elif "safari/" in value and "chrome/" not in value:
        browser = "Safari"
    elif "msie " in value or "trident/" in value:
        browser = "Internet Explorer"
    else:
        browser = "Unknown"

    if "windows" in value:
        operating_system = "Windows"
    elif "android" in value:
        operating_system = "Android"
    elif "iphone" in value or "ipad" in value or "ipod" in value:
        operating_system = "iOS"
    elif "mac os x" in value or "macintosh" in value:
        operating_system = "macOS"
    elif "linux" in value:
        operating_system = "Linux"
    else:
        operating_system = "Unknown"

    return device, browser, operating_system


def parse_user_agent(user_agent: str) -> tuple[str, str, str]:
    """Return normalized device/browser/OS labels for audit storage."""
    user_agent = str(user_agent or "").strip()
    if not user_agent:
        return "Unknown", "Unknown", "Unknown"

    if user_agent_parser is None:
        return _parse_user_agent_fallback(user_agent)

    try:
        parsed = user_agent_parser.Parse(user_agent)

        ua = parsed.get("user_agent") or {}
        device_info = parsed.get("device") or {}
        os_info = parsed.get("os") or {}

        browser_family = str(ua.get("family") or "Unknown").strip()
        browser_version = _format_version(ua)
        # ua-parser exposes browser version as major/minor/patch keys. The
        # helper is intentionally conservative; if only major exists, it
        # still yields a useful version string.
        if browser_version:
            browser = f"{browser_family} {browser_version}".strip()
        else:
            browser = browser_family

        device_family = str(device_info.get("family") or "Other").strip()
        if device_family in {"Other", "Spider", "Generic Smartphone"}:
            # Translate common parser families to a stable customer-facing
            # device category rather than exposing low-value parser details.
            lower_ua = user_agent.lower()
            if "tablet" in lower_ua or "ipad" in lower_ua:
                device = "Tablet"
            elif any(marker in lower_ua for marker in ("mobile", "iphone", "android")):
                device = "Mobile"
            else:
                device = "Desktop"
        else:
            device = device_family

        os_family = str(os_info.get("family") or "Unknown").strip()
        os_version = ".".join(
            str(os_info.get(key) or "").strip()
            for key in ("major", "minor", "patch", "patch_minor")
            if str(os_info.get(key) or "").strip()
        )
        operating_system = (
            f"{os_family} {os_version}".strip() if os_version else os_family
        )

        return (
            device[:100],
            browser[:100],
            operating_system[:100],
        )
    except Exception:
        logger.exception("User-agent parsing failed; using fallback parser")
        return _parse_user_agent_fallback(user_agent)


# ---------------------------------------------------------------------------
# Identifier hashing / metadata safety
# ---------------------------------------------------------------------------


def normalize_identifier_for_hash(identifier: str | None) -> str:
    """Normalize an email/phone-like identifier before hashing it."""
    value = str(identifier or "").strip().lower()
    if _EMAIL_RE.match(value):
        return value

    # Keep a leading plus for E.164-like values, otherwise retain digits.
    if value.startswith("+"):
        return "+" + re.sub(r"\D", "", value)
    return re.sub(r"\D", "", value)


def hash_identifier(identifier: str | None) -> str:
    """
    Return a keyed SHA-256 hash suitable for failed-login correlation.

    The Django SECRET_KEY is used as the HMAC key so a leaked audit table
    cannot be trivially converted into a dictionary of common emails/phones.
    """
    normalized = normalize_identifier_for_hash(identifier)
    if not normalized:
        return ""

    secret = str(getattr(settings, "SECRET_KEY", "")).encode("utf-8")
    if not secret:
        raise RuntimeError("Django SECRET_KEY is required for identifier hashing.")

    return hmac.new(
        secret,
        normalized.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _is_sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9_-]", "", str(key).lower())
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _sanitize_metadata_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 5:
        return "[truncated]"

    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, UUID):
        return str(value)

    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)[:100]
            cleaned[key] = (
                "[REDACTED]"
                if _is_sensitive_key(key)
                else _sanitize_metadata_value(raw_value, depth=depth + 1)
            )
        return cleaned

    if isinstance(value, (list, tuple, set)):
        return [
            _sanitize_metadata_value(item, depth=depth + 1)
            for item in list(value)[:100]
        ]

    return str(value)[:1000]


def sanitize_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """
    Safely serialize caller-supplied audit metadata.

    Secret-looking keys are redacted and the final JSON document is bounded
    so a malicious/buggy caller cannot grow the audit row without limit.
    """
    if not metadata:
        return {}

    if not isinstance(metadata, dict):
        metadata = {"value": metadata}

    cleaned = _sanitize_metadata_value(metadata)
    if not isinstance(cleaned, dict):
        cleaned = {"value": cleaned}

    try:
        encoded = json.dumps(
            cleaned,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    except (TypeError, ValueError):
        return {"audit_metadata_error": "unserializable_metadata"}

    max_bytes = _max_metadata_bytes()
    if len(encoded) <= max_bytes:
        return cleaned

    # Preserve a bounded prefix rather than allowing oversized JSON into the
    # database. The marker makes truncation explicit in admin/security views.
    return {
        "_truncated": True,
        "_original_bytes": len(encoded),
        "_max_bytes": max_bytes,
        "metadata": encoded[: max_bytes - 64].decode(
            "utf-8",
            errors="ignore",
        ),
    }


# ---------------------------------------------------------------------------
# Authentication session lifecycle
# ---------------------------------------------------------------------------


def _resolve_session_expiry(*, created_at=None, expires_at=None):
    if expires_at is not None:
        return expires_at
    base = created_at or timezone.now()
    return base + _session_lifetime()


def create_auth_session(
    *,
    customer: Customer,
    request=None,
    expires_at=None,
    event_type: str | None = None,
    event_metadata: dict[str, Any] | None = None,
) -> CustomerAuthSession:
    """
    Create a new server-side authenticated customer session.

    Session creation and its initial audit event are committed atomically.
    If the event cannot be recorded, the session creation is rolled back
    rather than leaving an unaudited authenticated session in PostgreSQL.
    """
    if not isinstance(customer, Customer):
        raise TypeError("customer must be an accounts.models.Customer instance")
    if not customer.is_active:
        raise AuthSessionInvalidError("Cannot create a session for an inactive customer.")

    now = timezone.now()
    resolved_expiry = _resolve_session_expiry(
        created_at=now,
        expires_at=expires_at,
    )
    if resolved_expiry <= now:
        raise ValueError("expires_at must be in the future")

    client_ip = get_client_ip(request)
    user_agent = get_request_user_agent(request)
    device, browser, operating_system = parse_user_agent(user_agent)

    with transaction.atomic():
        session = CustomerAuthSession.objects.create(
            customer=customer,
            last_activity_at=now,
            expires_at=resolved_expiry,
            is_active=True,
            ip_address=client_ip,
            user_agent=user_agent,
            device=device,
            browser=browser,
            os=operating_system,
        )

        if event_type:
            record_auth_event(
                event_type=event_type,
                request=request,
                customer=customer,
                session=session,
                metadata=event_metadata,
            )

    return session


def record_auth_event(
    *,
    event_type: str,
    request=None,
    customer: Customer | None = None,
    session: CustomerAuthSession | None = None,
    identifier: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> CustomerAuthEvent:
    """Insert exactly one append-only authentication/security audit event."""
    allowed = {choice[0] for choice in CustomerAuthEvent.EVENT_CHOICES}
    if event_type not in allowed:
        raise ValueError(f"Unsupported customer auth event type: {event_type}")

    user_agent = get_request_user_agent(request) if request is not None else ""
    device, browser, operating_system = parse_user_agent(user_agent)
    client_ip = get_client_ip(request) if request is not None else None

    if session is not None and customer is None:
        customer = session.customer

    return CustomerAuthEvent.objects.create(
        customer=customer,
        session=session,
        event_type=event_type,
        ip_address=client_ip,
        user_agent=user_agent,
        device=device,
        browser=browser,
        os=operating_system,
        identifier_hash=hash_identifier(identifier),
        metadata=sanitize_metadata(metadata),
    )


def _coerce_session_id(session_id: UUID | str) -> UUID:
    if isinstance(session_id, UUID):
        return session_id
    try:
        return UUID(str(session_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise AuthSessionNotFoundError("Invalid authentication session ID.") from exc


def get_auth_session(
    session_id: UUID | str,
    *,
    for_update: bool = False,
) -> CustomerAuthSession:
    """Load one server-side auth session with its customer."""
    normalized_id = _coerce_session_id(session_id)
    query = CustomerAuthSession.objects.select_related("customer")
    if for_update:
        query = query.select_for_update()

    try:
        return query.get(session_id=normalized_id)
    except CustomerAuthSession.DoesNotExist as exc:
        raise AuthSessionNotFoundError("Authentication session not found.") from exc


def expire_auth_session(
    session: CustomerAuthSession,
    *,
    request=None,
    record_event: bool = True,
) -> CustomerAuthSession:
    """
    Transition an active expired session to inactive exactly once.

    The row is locked to prevent two simultaneous requests from generating
    duplicate expiry transitions/events.
    """
    with transaction.atomic():
        locked = get_auth_session(session.session_id, for_update=True)
        if not locked.is_active:
            return locked
        if not locked.is_expired:
            return locked

        now = timezone.now()
        locked.is_active = False
        locked.revoked_at = now
        locked.revocation_reason = CustomerAuthSession.REVOCATION_EXPIRED
        locked.save(update_fields=["is_active", "revoked_at", "revocation_reason"])

        if record_event:
            record_auth_event(
                event_type=CustomerAuthEvent.EVENT_SESSION_EXPIRED,
                request=request,
                customer=locked.customer,
                session=locked,
                metadata={"reason": "session_expired"},
            )

        return locked


def validate_auth_session(
    session_id: UUID | str,
    *,
    customer: Customer | None = None,
    request=None,
    touch: bool = True,
) -> CustomerAuthSession:
    """
    Validate a JWT's server-side auth session.

    This function is intended to be called by the authentication class on
    every authenticated request. JWT signature/expiry validation alone is not
    enough because it cannot immediately reflect server-side logout,
    password reset, admin revocation, or account deactivation.
    """
    session = get_auth_session(session_id)

    if customer is not None and session.customer_id != customer.pk:
        raise AuthSessionOwnershipError("Authentication session ownership mismatch.")

    if not session.customer.is_active:
        raise AuthSessionInvalidError("Customer account is inactive.")

    if not session.is_active:
        raise AuthSessionInvalidError("Authentication session is no longer active.")

    if session.is_expired:
        session = expire_auth_session(session, request=request, record_event=True)
        raise AuthSessionExpiredError("Authentication session has expired.")

    if touch:
        touch_auth_session(session, request=request)
        # `touch_auth_session` updates the object timestamp when a DB write is
        # actually performed. This keeps callers' in-memory state coherent.
        session.refresh_from_db(
            fields=["last_activity_at", "is_active", "expires_at"]
        )

    return session


def touch_auth_session(
    session: CustomerAuthSession,
    *,
    request=None,
    now=None,
) -> bool:
    """
    Throttled last-activity update.

    Returns True only when PostgreSQL was actually updated. The conditional
    update prevents a write on every request and behaves well across multiple
    Gunicorn workers.
    """
    now = now or timezone.now()
    cutoff = now - _activity_update_interval()

    if not session.is_active or session.is_expired:
        return False

    updated = (
        CustomerAuthSession.objects
        .filter(
            pk=session.pk,
            is_active=True,
        )
        .filter(expires_at__gt=now)
        .filter(last_activity_at__lt=cutoff)
        .update(last_activity_at=now)
    )

    if updated:
        session.last_activity_at = now
        return True

    return False


def revoke_auth_session(
    session_id: UUID | str,
    *,
    customer: Customer | None = None,
    reason: str,
    request=None,
    event_type: str | None = None,
    logged_out: bool = False,
    event_metadata: dict[str, Any] | None = None,
) -> CustomerAuthSession:
    """
    Revoke one server-side authentication session.

    The DB state is changed before the success response should be emitted by
    the caller. Refresh-token blacklisting should remain a second defence,
    not the only authority.
    """
    reason = str(reason or "security")[:64]
    event_type = event_type or (
        CustomerAuthEvent.EVENT_LOGOUT
        if logged_out
        else CustomerAuthEvent.EVENT_SESSION_REVOKED
    )

    with transaction.atomic():
        session = get_auth_session(session_id, for_update=True)

        if customer is not None and session.customer_id != customer.pk:
            raise AuthSessionOwnershipError("Authentication session ownership mismatch.")

        if not session.is_active:
            # A previously-revoked session is already safe. Do not emit a
            # second lifecycle event for an idempotent revoke request.
            return session

        session.revoke(reason, logged_out_at=logged_out)

        payload = dict(event_metadata or {})
        payload.setdefault("revocation_reason", reason)
        record_auth_event(
            event_type=event_type,
            request=request,
            customer=session.customer,
            session=session,
            metadata=payload,
        )

        return session


def revoke_all_customer_sessions(
    customer: Customer,
    *,
    reason: str,
    request=None,
    exclude_session_id: UUID | str | None = None,
    event_type: str = CustomerAuthEvent.EVENT_SESSION_REVOKED,
    event_metadata: dict[str, Any] | None = None,
) -> int:
    """
    Revoke every active session for a customer, optionally preserving one.

    Each session gets its own audit event so admin/security history can answer
    exactly which session was revoked and why.
    """
    if not isinstance(customer, Customer):
        raise TypeError("customer must be an accounts.models.Customer instance")

    excluded_id = (
        _coerce_session_id(exclude_session_id)
        if exclude_session_id is not None
        else None
    )
    reason = str(reason or "revoke_all")[:64]
    count = 0

    with transaction.atomic():
        sessions = (
            CustomerAuthSession.objects
            .select_for_update()
            .select_related("customer")
            .filter(customer=customer, is_active=True)
            .order_by("pk")
        )

        for session in sessions:
            if excluded_id is not None and session.session_id == excluded_id:
                continue

            session.revoke(reason, logged_out_at=False)

            payload = dict(event_metadata or {})
            payload.setdefault("revocation_reason", reason)
            record_auth_event(
                event_type=event_type,
                request=request,
                customer=customer,
                session=session,
                metadata=payload,
            )
            count += 1

    return count


# ---------------------------------------------------------------------------
# Auth-session/request helpers for JWT integrations
# ---------------------------------------------------------------------------


def get_auth_session_id_from_request(request) -> UUID | None:
    """
    Read the server-side session UUID from a SimpleJWT authentication object.

    No token parsing is performed here. The JWT authentication layer should
    already have verified the token signature before calling this helper.
    """
    auth = getattr(request, "auth", None)
    if auth is None:
        return None

    raw = None
    try:
        raw = auth.get("auth_session_id")
    except AttributeError:
        try:
            raw = auth["auth_session_id"]
        except (KeyError, TypeError):
            raw = None

    if not raw:
        return None

    try:
        return _coerce_session_id(raw)
    except AuthSessionNotFoundError:
        return None


def is_auth_session_online(
    session: CustomerAuthSession,
    *,
    now=None,
) -> bool:
    """Return whether the session has been active within the configured window."""
    now = now or timezone.now()
    if not session.is_active or session.is_expired:
        return False
    return session.last_activity_at >= now - _online_window()


# ---------------------------------------------------------------------------
# Query helpers used by customer/admin APIs
# ---------------------------------------------------------------------------


def customer_sessions_queryset(
    customer: Customer,
    *,
    active_only: bool = False,
):
    """Return a stable, efficiently ordered queryset for session-history APIs."""
    queryset = CustomerAuthSession.objects.filter(customer=customer).order_by("-created_at")
    if active_only:
        queryset = queryset.filter(is_active=True)
    return queryset


def customer_auth_history_queryset(
    customer: Customer,
    *,
    event_types: Iterable[str] | None = None,
):
    """Return the lifetime authentication/security audit trail for a customer."""
    queryset = CustomerAuthEvent.objects.filter(customer=customer).select_related("session")
    if event_types:
        queryset = queryset.filter(event_type__in=list(event_types))
    return queryset.order_by("-occurred_at")


# ---------------------------------------------------------------------------
# Lifecycle convenience helpers
# ---------------------------------------------------------------------------


def record_registration(
    *,
    customer: Customer,
    session: CustomerAuthSession,
    request,
    channel: str,
    created: bool,
) -> CustomerAuthEvent:
    return record_auth_event(
        event_type=CustomerAuthEvent.EVENT_REGISTERED,
        request=request,
        customer=customer,
        session=session,
        metadata={
            "registration_channel": str(channel)[:32],
            "created": bool(created),
        },
    )


def record_login_failed(
    *,
    request,
    identifier: str,
    channel: str | None = None,
    reason: str = "invalid_credentials",
    metadata: dict[str, Any] | None = None,
) -> CustomerAuthEvent:
    payload = dict(metadata or {})
    payload.setdefault("reason", str(reason)[:64])
    if channel:
        payload.setdefault("channel", str(channel)[:32])

    return record_auth_event(
        event_type=CustomerAuthEvent.EVENT_LOGIN_FAILED,
        request=request,
        identifier=identifier,
        metadata=payload,
    )


def record_login_success(
    *,
    customer: Customer,
    session: CustomerAuthSession,
    request,
    channel: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> CustomerAuthEvent:
    payload = dict(metadata or {})
    if channel:
        payload.setdefault("channel", str(channel)[:32])

    return record_auth_event(
        event_type=CustomerAuthEvent.EVENT_LOGIN_SUCCESS,
        request=request,
        customer=customer,
        session=session,
        metadata=payload,
    )


def record_password_event(
    *,
    event_type: str,
    customer: Customer,
    request,
    session: CustomerAuthSession | None = None,
    metadata: dict[str, Any] | None = None,
) -> CustomerAuthEvent:
    if event_type not in {
        CustomerAuthEvent.EVENT_PASSWORD_RESET,
        CustomerAuthEvent.EVENT_PASSWORD_CHANGED,
    }:
        raise ValueError("event_type must be a password reset/change event")

    return record_auth_event(
        event_type=event_type,
        request=request,
        customer=customer,
        session=session,
        metadata=metadata,
    )


__all__ = [
    "AuthAuditError",
    "AuthSessionNotFoundError",
    "AuthSessionInvalidError",
    "AuthSessionExpiredError",
    "AuthSessionOwnershipError",
    "create_auth_session",
    "record_auth_event",
    "record_registration",
    "record_login_failed",
    "record_login_success",
    "record_password_event",
    "get_client_ip",
    "get_request_user_agent",
    "parse_user_agent",
    "hash_identifier",
    "normalize_identifier_for_hash",
    "sanitize_metadata",
    "get_auth_session",
    "validate_auth_session",
    "expire_auth_session",
    "touch_auth_session",
    "revoke_auth_session",
    "revoke_all_customer_sessions",
    "get_auth_session_id_from_request",
    "is_auth_session_online",
    "customer_sessions_queryset",
    "customer_auth_history_queryset",
]
