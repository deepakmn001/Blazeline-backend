from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from django.db import DatabaseError, transaction
from django.utils import timezone

from .models import AnalyticsEvent, AnalyticsSession


# =============================================================================
# Identity helpers
# =============================================================================


def _parse_uuid(
    value: UUID | str | None,
) -> UUID | None:
    """
    Safely normalize an incoming UUID-like value.

    Invalid analytics identity values are ignored rather than allowed
    to interrupt the customer-facing application.
    """
    if value is None:
        return None

    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _get_request_user_agent(request) -> str:
    """
    Return the directly observed browser user-agent.

    Analytics metadata is observational only and must never become
    an authorization mechanism.
    """
    if request is None:
        return ""

    return str(
        request.META.get("HTTP_USER_AGENT", "")
        or ""
    )


def _parse_user_agent(user_agent: str) -> tuple[str, str, str]:
    """Best-effort dependency-free device/browser/OS classification."""
    ua = str(user_agent or "")
    low = ua.lower()

    if "ipad" in low or "tablet" in low or ("android" in low and "mobile" not in low):
        device = "Tablet"
    elif "mobile" in low or "iphone" in low or "ipod" in low or "windows phone" in low:
        device = "Mobile"
    else:
        device = "Desktop" if ua else ""

    if "edg/" in low:
        browser = "Edge"
    elif "opr/" in low or "opera" in low:
        browser = "Opera"
    elif "samsungbrowser/" in low:
        browser = "Samsung Internet"
    elif "firefox/" in low:
        browser = "Firefox"
    elif "crios/" in low or "chrome/" in low:
        browser = "Chrome"
    elif "safari/" in low and "chrome/" not in low and "crios/" not in low:
        browser = "Safari"
    else:
        browser = "Other" if ua else ""

    if "windows nt" in low:
        os_name = "Windows"
    elif "iphone os" in low or "ipad; cpu os" in low or "cpu os" in low:
        os_name = "iOS"
    elif "mac os x" in low:
        os_name = "macOS"
    elif "android" in low:
        os_name = "Android"
    elif "linux" in low:
        os_name = "Linux"
    else:
        os_name = "Other" if ua else ""

    return device, browser, os_name


def _derive_request_source(request) -> str:
    """Best-effort acquisition source from UTM or directly observed referrer."""
    if request is None:
        return ""

    try:
        params = request.query_params
    except AttributeError:
        params = None

    if params is not None:
        utm_source = str(params.get("utm_source", "") or "").strip()
        if utm_source:
            return utm_source[:255]

    referrer = str(request.META.get("HTTP_REFERER", "") or "").strip()
    if not referrer:
        return ""

    match = re.match(r"^(?:https?://)?([^/]+)", referrer, flags=re.IGNORECASE)
    return (match.group(1) if match else referrer)[:255]


def _get_client_ip(request) -> str | None:
    """
    Resolve the directly observed client address.

    We intentionally do not trust X-Forwarded-For here.

    Reverse-proxy forwarded-address handling should only be enabled
    after the production proxy trust boundary has been explicitly
    configured and verified.
    """
    if request is None:
        return None

    remote_addr = request.META.get("REMOTE_ADDR")

    if not remote_addr:
        return None

    return str(remote_addr).strip() or None


def _resolve_customer_from_request(request):
    """
    Return a Customer only when request.user is actually our
    Customer model.

    Staff/admin identities must never be treated as customer
    analytics identities.
    """
    if request is None:
        return None

    request_user = getattr(
        request,
        "user",
        None,
    )

    if not getattr(
        request_user,
        "is_authenticated",
        False,
    ):
        return None

    from accounts.models import Customer

    if isinstance(
        request_user,
        Customer,
    ):
        return request_user

    return None


def _resolve_guest_id_from_request(
    request,
) -> UUID | None:
    """
    Resolve the anonymous cart/session identity from X-Guest-Id.

    This value is analytics identity only.
    It must never be treated as an authorization credential.
    """
    if request is None:
        return None

    try:
        raw_guest_id = request.headers.get(
            "X-Guest-Id",
        )
    except AttributeError:
        raw_guest_id = None

    return _parse_uuid(raw_guest_id)


# =============================================================================
# Central event writer
# =============================================================================


def track_event(
    *,
    event_name: str,
    request=None,
    session: AnalyticsSession | None = None,
    customer=None,
    guest_id: UUID | str | None = None,
    product=None,
    variant=None,
    category=None,
    subcategory=None,
    order=None,
    page_path: str | None = None,
    metadata: dict[str, Any] | None = None,
    occurred_at=None,
) -> AnalyticsEvent | None:
    """
    Central analytics event writer.

    Production guarantees:
    - Analytics is observational only.
    - Client analytics never controls commercial truth.
    - Analytics failures never propagate into auth/cart/checkout/order flows.
    - Customer identity is resolved from trusted request authentication.
    - Guest identity is accepted only as analytics context.
    - Browser page_path is stored instead of the analytics API endpoint path.
    - Product/variant/category/subcategory/order relations are stored as
      real foreign keys when trusted backend objects are supplied.
    - Session identity is updated atomically with the event.
    """

    event_name = str(
        event_name or ""
    ).strip()

    if not event_name:
        return None

    occurred_at = occurred_at or timezone.now()

    try:
        # ---------------------------------------------------------------------
        # Trusted customer identity
        # ---------------------------------------------------------------------

        resolved_customer = customer

        if resolved_customer is None:
            resolved_customer = _resolve_customer_from_request(
                request,
            )

        # ---------------------------------------------------------------------
        # Guest identity
        # ---------------------------------------------------------------------

        resolved_guest_id = _parse_uuid(
            guest_id,
        )

        if resolved_guest_id is None:
            resolved_guest_id = _resolve_guest_id_from_request(
                request,
            )

        # ---------------------------------------------------------------------
        # Browser page path
        # ---------------------------------------------------------------------

        resolved_page_path = str(
            page_path or ""
        ).strip()[:500]

        # Only use the API request path as a final fallback.
        if (
            not resolved_page_path
            and request is not None
        ):
            resolved_page_path = str(
                getattr(
                    request,
                    "path",
                    "",
                )
                or ""
            ).strip()[:500]

        # ---------------------------------------------------------------------
        # Metadata
        # ---------------------------------------------------------------------

        event_metadata: dict[str, Any] = {}

        if isinstance(
            metadata,
            dict,
        ):
            event_metadata.update(
                metadata,
            )

        # ---------------------------------------------------------------------
        # Atomic event + session update
        # ---------------------------------------------------------------------

        with transaction.atomic():
            event = AnalyticsEvent.objects.create(
                session=session,
                customer=resolved_customer,
                guest_id=resolved_guest_id,
                event_name=event_name,
                occurred_at=occurred_at,
                path=resolved_page_path,
                product=product,
                variant=variant,
                category=category,
                subcategory=subcategory,
                order=order,
                metadata=event_metadata,
            )

            if session is not None:
                update_fields: list[str] = []

                # -------------------------------------------------------------
                # Anonymous -> authenticated stitching
                #
                # Never overwrite a known customer with anonymous data.
                # -------------------------------------------------------------

                if (
                    resolved_customer is not None
                    and session.customer_id
                    != resolved_customer.pk
                ):
                    session.customer = resolved_customer
                    update_fields.append(
                        "customer",
                    )

                # -------------------------------------------------------------
                # Preserve guest identity so the anonymous journey remains
                # connected after authentication.
                # -------------------------------------------------------------

                if (
                    resolved_guest_id is not None
                    and session.guest_id
                    != resolved_guest_id
                ):
                    session.guest_id = resolved_guest_id
                    update_fields.append(
                        "guest_id",
                    )

                # -------------------------------------------------------------
                # Activity timestamp
                # -------------------------------------------------------------

                session.last_seen_at = occurred_at
                update_fields.append(
                    "last_seen_at",
                )

                # -------------------------------------------------------------
                # Browser page path
                # -------------------------------------------------------------

                if (
                    resolved_page_path
                    and session.last_path
                    != resolved_page_path
                ):
                    session.last_path = resolved_page_path
                    update_fields.append(
                        "last_path",
                    )

                if update_fields:
                    session.save(
                        update_fields=list(
                            dict.fromkeys(
                                update_fields,
                            )
                        ),
                    )

        return event

    except DatabaseError:
        # Analytics must never break the customer application.
        return None

    except Exception:
        # Absolute safety boundary for analytics.
        return None


# =============================================================================
# Analytics session resolver
# =============================================================================


def get_or_create_session(
    *,
    session_id: UUID | str | None = None,
    customer=None,
    guest_id: UUID | str | None = None,
    request=None,
    source: str = "",
    landing_page: str = "",
    utm_source: str = "",
    utm_medium: str = "",
    utm_campaign: str = "",
    utm_term: str = "",
    utm_content: str = "",
) -> AnalyticsSession:
    """
    Resolve or create an analytics session.

    Session identity comes from the application/browser.

    A session may start anonymously and later become associated
    with a Customer after authentication.
    """

    # -------------------------------------------------------------------------
    # Normalize identities
    # -------------------------------------------------------------------------

    resolved_session_id = _parse_uuid(
        session_id,
    )

    resolved_guest_id = _parse_uuid(
        guest_id,
    )

    if resolved_guest_id is None:
        resolved_guest_id = _resolve_guest_id_from_request(
            request,
        )

    resolved_customer = customer

    if resolved_customer is None:
        resolved_customer = _resolve_customer_from_request(
            request,
        )

    # -------------------------------------------------------------------------
    # Request page path
    # -------------------------------------------------------------------------

    request_path = ""

    if request is not None:
        request_path = str(
            getattr(
                request,
                "path",
                "",
            )
            or ""
        ).strip()[:500]

    # -------------------------------------------------------------------------
    # Landing page
    # -------------------------------------------------------------------------

    resolved_landing_page = str(
        landing_page or ""
    ).strip()[:500]

    if not resolved_landing_page:
        resolved_landing_page = request_path

    # -------------------------------------------------------------------------
    # New-session defaults
    # -------------------------------------------------------------------------

    defaults: dict[str, Any] = {
        "customer": resolved_customer,
        "guest_id": resolved_guest_id,
        "source": str(
            source or ""
        ).strip()[:255],
        "landing_page": resolved_landing_page,
        "utm_source": str(
            utm_source or ""
        ).strip()[:255],
        "utm_medium": str(
            utm_medium or ""
        ).strip()[:255],
        "utm_campaign": str(
            utm_campaign or ""
        ).strip()[:255],
        "utm_term": str(
            utm_term or ""
        ).strip()[:255],
        "utm_content": str(
            utm_content or ""
        ).strip()[:255],
    }

    # -------------------------------------------------------------------------
    # Request environment metadata
    # -------------------------------------------------------------------------

    if request is not None:
        user_agent = _get_request_user_agent(
            request,
        )
        device, browser, os_name = _parse_user_agent(
            user_agent,
        )

        # Prefer the browser page/landing path over a backend API route.
        defaults["last_path"] = resolved_landing_page
        defaults["ip_address"] = _get_client_ip(
            request,
        )
        defaults["user_agent"] = user_agent
        defaults["device_type"] = device
        defaults["browser"] = browser
        defaults["operating_system"] = os_name

        if not defaults["source"]:
            defaults["source"] = _derive_request_source(
                request,
            )

    # -------------------------------------------------------------------------
    # Existing browser session
    # -------------------------------------------------------------------------

    if resolved_session_id is not None:
        session, created = (
            AnalyticsSession.objects.get_or_create(
                id=resolved_session_id,
                defaults=defaults,
            )
        )

        if created:
            return session

        update_fields: list[str] = []

        # ---------------------------------------------------------------------
        # Best-effort enrichment for older sessions.
        # Never overwrite an existing value with an empty value.
        # ---------------------------------------------------------------------
        if request is not None:
            user_agent = _get_request_user_agent(
                request,
            )

            if user_agent and not getattr(session, "user_agent", ""):
                session.user_agent = user_agent
                update_fields.append("user_agent")

            device, browser, os_name = _parse_user_agent(
                user_agent,
            )

            if device and not getattr(session, "device_type", ""):
                session.device_type = device
                update_fields.append("device_type")

            if browser and not getattr(session, "browser", ""):
                session.browser = browser
                update_fields.append("browser")

            if os_name and not getattr(session, "operating_system", ""):
                session.operating_system = os_name
                update_fields.append("operating_system")

            if not getattr(session, "ip_address", None):
                client_ip = _get_client_ip(request)
                if client_ip:
                    session.ip_address = client_ip
                    update_fields.append("ip_address")

            if not getattr(session, "source", ""):
                source = _derive_request_source(request)
                if source:
                    session.source = source
                    update_fields.append("source")

        # ---------------------------------------------------------------------
        # Never overwrite a known customer with anonymous data.
        # ---------------------------------------------------------------------

        if (
            resolved_customer is not None
            and session.customer_id
            != resolved_customer.pk
        ):
            session.customer = resolved_customer
            update_fields.append(
                "customer",
            )

        # ---------------------------------------------------------------------
        # Preserve guest identity for anonymous -> authenticated stitching.
        # ---------------------------------------------------------------------

        if (
            resolved_guest_id is not None
            and session.guest_id
            != resolved_guest_id
        ):
            session.guest_id = resolved_guest_id
            update_fields.append(
                "guest_id",
            )

        # ---------------------------------------------------------------------
        # Latest browser route
        # ---------------------------------------------------------------------

        if (
            request_path
            and session.last_path
            != request_path
        ):
            session.last_path = request_path
            update_fields.append(
                "last_path",
            )

        # ---------------------------------------------------------------------
        # Latest activity timestamp
        # ---------------------------------------------------------------------

        session.last_seen_at = timezone.now()
        update_fields.append(
            "last_seen_at",
        )

        if update_fields:
            session.save(
                update_fields=list(
                    dict.fromkeys(
                        update_fields,
                    )
                ),
            )

        return session

    # -------------------------------------------------------------------------
    # No browser session id yet
    # -------------------------------------------------------------------------

    return AnalyticsSession.objects.create(
        **defaults,
    )


# =============================================================================
# Explicit identity stitching
# =============================================================================


def identify_session(
    *,
    session_id: UUID | str | None = None,
    customer=None,
    guest_id: UUID | str | None = None,
) -> AnalyticsSession | None:
    """
    Attach an existing analytics session to an authenticated customer.

    Identity-stitching boundary:

        anonymous session
              ↓
        customer login/register
              ↓
        same session → customer

    No new session is created here.
    """

    resolved_session_id = _parse_uuid(
        session_id,
    )

    if (
        resolved_session_id is None
        or customer is None
    ):
        return None

    resolved_guest_id = _parse_uuid(
        guest_id,
    )

    try:
        with transaction.atomic():
            session = (
                AnalyticsSession.objects
                .select_for_update()
                .filter(
                    id=resolved_session_id,
                )
                .first()
            )

            if session is None:
                return None

            # -------------------------------------------------------------
            # Attach authenticated customer
            # -------------------------------------------------------------

            session.customer = customer

            # -------------------------------------------------------------
            # Preserve anonymous identity
            # -------------------------------------------------------------

            if resolved_guest_id is not None:
                session.guest_id = resolved_guest_id

            # -------------------------------------------------------------
            # Refresh activity timestamp
            # -------------------------------------------------------------

            session.last_seen_at = timezone.now()

            update_fields = [
                "customer",
                "last_seen_at",
            ]

            if resolved_guest_id is not None:
                update_fields.append(
                    "guest_id",
                )

            session.save(
                update_fields=list(
                    dict.fromkeys(
                        update_fields,
                    )
                ),
            )

            return session

    except DatabaseError:
        return None

    except Exception:
        return None
