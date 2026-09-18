import hashlib
import hmac
import secrets
from datetime import timedelta

import requests
from django.conf import settings
from django.core import signing
from django.core.mail import send_mail
from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed, PermissionDenied
from rest_framework.permissions import BasePermission
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.tokens import RefreshToken

from .models import OTP, Customer

OTP_LENGTH = 6
OTP_TTL_MINUTES = 10
RESEND_COOLDOWN_SECONDS = 60

VERIFICATION_TOKEN_SALT = "accounts.otp-verification"
VERIFICATION_TOKEN_TTL_SECONDS = 600  # must complete register/reset within 10 min of OTP verify


# ==========================================================
# ERRORS
# ==========================================================

class OTPCooldownError(Exception):
    def __init__(self, seconds_remaining):
        self.seconds_remaining = seconds_remaining
        super().__init__(f"Please wait {seconds_remaining}s before requesting another OTP.")


class OTPInvalidError(Exception):
    pass


class OTPExpiredError(Exception):
    pass


class CustomerAlreadyExistsError(Exception):
    pass


class CustomerNotFoundError(Exception):
    pass


class VerificationTokenError(Exception):
    pass


# ==========================================================
# HELPERS
# ==========================================================

def _hash_code(code):
    return hashlib.sha256(f"{code}{settings.SECRET_KEY}".encode()).hexdigest()


def _generate_code():
    return "".join(
        secrets.choice("0123456789")
        for _ in range(OTP_LENGTH)
    )


def normalize_identifier(identifier, channel):
    identifier = identifier.strip()

    if channel == OTP.CHANNEL_EMAIL:
        return identifier.lower()

    digits = "".join(ch for ch in identifier if ch.isdigit())
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    return digits


def _find_customer(identifier, channel):
    lookup = {"email": identifier} if channel == OTP.CHANNEL_EMAIL else {"phone": identifier}
    return Customer.objects.filter(**lookup).first()


# ==========================================================
# REQUEST / VERIFY OTP
# ==========================================================

def request_otp(identifier, channel, purpose):
    identifier = normalize_identifier(identifier, channel)

    existing_customer = _find_customer(identifier, channel)

    if purpose == OTP.PURPOSE_REGISTER and existing_customer and existing_customer.has_usable_password():
        raise CustomerAlreadyExistsError()

    if purpose == OTP.PURPOSE_RESET_PASSWORD and not existing_customer:
        raise CustomerNotFoundError()

    recent = (
        OTP.objects.filter(identifier=identifier, channel=channel, purpose=purpose, is_used=False)
        .order_by("-created_at")
        .first()
    )

    if recent and (timezone.now() - recent.created_at).total_seconds() < RESEND_COOLDOWN_SECONDS:
        elapsed = (timezone.now() - recent.created_at).total_seconds()
        raise OTPCooldownError(int(RESEND_COOLDOWN_SECONDS - elapsed))

    code = _generate_code()

    otp = OTP.objects.create(
        identifier=identifier,
        channel=channel,
        purpose=purpose,
        code_hash=_hash_code(code),
        expires_at=timezone.now() + timedelta(minutes=OTP_TTL_MINUTES),
    )

    if channel == OTP.CHANNEL_EMAIL:
        _send_email_otp(identifier, code)
    else:
        _send_phone_otp(identifier, code)

    return otp


def verify_otp(identifier, channel, code, purpose):
    identifier = normalize_identifier(identifier, channel)

    from django.db import transaction

    with transaction.atomic():
        otp = (
            OTP.objects
            .select_for_update()
            .filter(
                identifier=identifier,
                channel=channel,
                purpose=purpose,
                is_used=False,
            )
            .order_by("-created_at")
            .first()
        )

        if not otp:
            raise OTPInvalidError("No OTP found. Please request a new one.")
        if otp.is_expired():
            raise OTPExpiredError("OTP has expired. Please request a new one.")
        if otp.attempts >= otp.max_attempts:
            raise OTPInvalidError(
                "Too many incorrect attempts. Please request a new OTP."
            )

        otp.attempts += 1
        otp.save(update_fields=["attempts"])

        if not hmac.compare_digest(
            otp.code_hash,
            _hash_code(code),
        ):
            raise OTPInvalidError("Incorrect OTP.")

        otp.is_used = True
        otp.save(update_fields=["is_used"])

    return identifier


def _send_email_otp(email, code):
    send_mail(
        subject="Your BlazeLine verification code",
        message=f"Your OTP is {code}. It expires in {OTP_TTL_MINUTES} minutes.",
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[email],
        fail_silently=False,
    )


def _send_phone_otp(phone, code):
    """
    Sends the OTP via WhatsApp (MSG91's WhatsApp Business API) — NOT SMS.
    SMS/DLT was intentionally skipped for now (cost). The "phone" channel
    still identifies the customer by phone number; only the delivery
    method is WhatsApp.

    Requires MSG91_AUTH_KEY, MSG91_WHATSAPP_INTEGRATED_NUMBER and
    MSG91_WHATSAPP_TEMPLATE_NAME. Namespace is intentionally null — MSG91's
    own generated code sample for this account/template shows null too.
    In DEBUG without an auth key, prints to console instead — so local
    dev doesn't need a real WhatsApp setup.
    """
    auth_key = getattr(settings, "MSG91_AUTH_KEY", None)

    if settings.DEBUG and not auth_key:
        print(f"[DEV OTP] WhatsApp {phone}: {code}")
        return

    response = requests.post(
        "https://api.msg91.com/api/v5/whatsapp/whatsapp-outbound-message/bulk/",
        headers={
            "Content-Type": "application/json",
            "authkey": auth_key,
        },
        json={
            "integrated_number": settings.MSG91_WHATSAPP_INTEGRATED_NUMBER,
            "content_type": "template",
            "payload": {
                "messaging_product": "whatsapp",
                "type": "template",
                "template": {
                    "name": settings.MSG91_WHATSAPP_TEMPLATE_NAME,
                    "language": {
                        "code": settings.MSG91_WHATSAPP_TEMPLATE_LANGUAGE,
                        "policy": "deterministic",
                    },
                    "namespace": None,
                    "to_and_components": [
                        {
                            "to": [f"91{phone}"],
                            "components": {
                                "body_1": {"type": "text", "value": code},
                                "button_1": {
                                    "subtype": "url",
                                    "type": "text",
                                    "value": code,
                                },
                            },
                        }
                    ],
                },
            },
        },
        timeout=10,
    )
    response.raise_for_status()


# ==========================================================
# VERIFICATION TOKEN
#
# Issued once an OTP is successfully verified. Proves "this
# identifier+channel was OTP-verified for this specific purpose,
# recently" without needing to re-check the (already consumed) OTP
# row. Signed + time-limited so it can't be forged or replayed after
# expiry, and its embedded purpose stops it being used for anything
# other than what it was issued for.
# ==========================================================

def generate_verification_token(identifier, channel, purpose):
    payload = {"identifier": identifier, "channel": channel, "purpose": purpose}
    return signing.dumps(payload, salt=VERIFICATION_TOKEN_SALT)


def parse_verification_token(token, expected_purpose):
    try:
        payload = signing.loads(
            token, salt=VERIFICATION_TOKEN_SALT, max_age=VERIFICATION_TOKEN_TTL_SECONDS
        )
    except signing.SignatureExpired:
        raise VerificationTokenError("This verification has expired. Please verify again.")
    except signing.BadSignature:
        raise VerificationTokenError("Invalid verification token.")

    if payload.get("purpose") != expected_purpose:
        raise VerificationTokenError("Invalid verification token.")

    return payload["identifier"], payload["channel"]


# ==========================================================
# JWT — issuing tokens for a Customer
#
# Customer is NOT AUTH_USER_MODEL, so customer JWTs carry a custom "actor"
# claim and a server-side "auth_session_id" claim.
#
# The auth session is the server-side source of truth:
#   JWT signature/expiry -> token authenticity
#   Customer row        -> account state
#   CustomerAuthSession -> login/session revocation state
#
# Staff/admin JWTs continue through SimpleJWT's normal user resolution and
# are intentionally unaffected.
# ==========================================================

from rest_framework_simplejwt.exceptions import InvalidToken
from rest_framework_simplejwt.serializers import TokenRefreshSerializer

from .auth_audit import (
    AuthSessionExpiredError,
    AuthSessionInvalidError,
    AuthSessionNotFoundError,
    AuthSessionOwnershipError,
    create_auth_session,
    validate_auth_session,
)


AUTH_SESSION_ID_CLAIM = "auth_session_id"
CUSTOMER_ACTOR_CLAIM = "actor"
CUSTOMER_ACTOR_VALUE = "customer"


def _customer_from_validated_token(validated_token):
    """
    Resolve the customer represented by an already signature-validated token.

    This deliberately keeps customer resolution separate from session
    validation so the latter can verify that the server-side session belongs
    to the same customer.
    """
    try:
        customer_id = validated_token[api_settings.USER_ID_CLAIM]
    except KeyError as exc:
        raise AuthenticationFailed(
            "Customer token is missing the user identifier.",
            code="customer_user_id_missing",
        ) from exc

    try:
        return Customer.objects.get(
            pk=customer_id,
            is_active=True,
        )
    except Customer.DoesNotExist as exc:
        raise AuthenticationFailed(
            "Customer not found or inactive.",
            code="customer_not_found",
        ) from exc


def _authenticate_customer_token(
    validated_token,
    *,
    request=None,
):
    """
    Resolve and server-validate one customer access token.

    Missing/invalid auth-session claims intentionally fail closed. Customer
    tokens issued before the auth-session rollout are therefore not accepted;
    they must obtain a fresh token through the new login/registration flow.
    """
    customer = _customer_from_validated_token(validated_token)

    raw_session_id = validated_token.get(
        AUTH_SESSION_ID_CLAIM
    )
    if not raw_session_id:
        raise AuthenticationFailed(
            "Authentication session is missing or invalid.",
            code="auth_session_missing",
        )

    try:
        validate_auth_session(
            raw_session_id,
            customer=customer,
            request=request,
            touch=True,
        )
    except AuthSessionNotFoundError as exc:
        raise AuthenticationFailed(
            "Authentication session was not found.",
            code="auth_session_not_found",
        ) from exc
    except AuthSessionOwnershipError as exc:
        raise AuthenticationFailed(
            "Authentication session ownership mismatch.",
            code="auth_session_ownership_mismatch",
        ) from exc
    except AuthSessionExpiredError as exc:
        raise AuthenticationFailed(
            "Authentication session has expired.",
            code="auth_session_expired",
        ) from exc
    except AuthSessionInvalidError as exc:
        raise AuthenticationFailed(
            "Authentication session is no longer active.",
            code="auth_session_invalid",
        ) from exc

    return customer


def issue_tokens_for_customer(
    customer,
    *,
    request=None,
    event_type=None,
    event_metadata=None,
):
    """
    Create a new customer auth session and issue its JWT pair.

    The session is created before the token pair and the initial audit event
    are part of the same database transaction. This prevents issuing a token
    for a session that was not durably recorded.

    `event_type` defaults to login_success. Registration/password-reset flows
    should pass their specific CustomerAuthEvent event type from views.py.
    """
    from django.db import transaction
    from .models import CustomerAuthEvent

    if not isinstance(customer, Customer):
        raise TypeError(
            "issue_tokens_for_customer() requires a Customer instance."
        )
    if not customer.is_active:
        raise AuthenticationFailed(
            "Customer account is inactive.",
            code="customer_inactive",
        )

    resolved_event_type = (
        event_type
        or CustomerAuthEvent.EVENT_LOGIN_SUCCESS
    )

    with transaction.atomic():
        auth_session = create_auth_session(
            customer=customer,
            request=request,
            event_type=resolved_event_type,
            event_metadata=event_metadata,
        )

        refresh = RefreshToken()

        refresh[api_settings.USER_ID_CLAIM] = getattr(
            customer,
            api_settings.USER_ID_FIELD,
        )
        refresh[CUSTOMER_ACTOR_CLAIM] = CUSTOMER_ACTOR_VALUE
        refresh[AUTH_SESSION_ID_CLAIM] = str(
            auth_session.session_id
        )

        access = refresh.access_token
        access[api_settings.USER_ID_CLAIM] = getattr(
            customer,
            api_settings.USER_ID_FIELD,
        )
        access[CUSTOMER_ACTOR_CLAIM] = CUSTOMER_ACTOR_VALUE
        access[AUTH_SESSION_ID_CLAIM] = str(
            auth_session.session_id
        )

        return {
            "access": str(access),
            "refresh": str(refresh),
        }


class AppJWTAuthentication(JWTAuthentication):
    """
    Drop-in customer/staff JWT authentication.

    Customer token:
        1. SimpleJWT verifies signature and token lifetime.
        2. Customer account is loaded and must be active.
        3. auth_session_id is required.
        4. PostgreSQL CustomerAuthSession must exist and be active.
        5. Session ownership must match the customer.
        6. Session hard expiry is enforced.
        7. last_activity_at is throttled by auth_audit.py.

    Staff token:
        SimpleJWT's existing auth.User resolution is preserved unchanged.
    """

    def authenticate(self, request):
        """
        Reproduce SimpleJWT's normal authentication flow while passing the
        request into the customer session validator for IP/activity context.
        """
        header = self.get_header(request)

        if header is None:
            return None

        raw_token = self.get_raw_token(header)

        if raw_token is None:
            return None

        validated_token = self.get_validated_token(raw_token)

        if validated_token.get(CUSTOMER_ACTOR_CLAIM) == CUSTOMER_ACTOR_VALUE:
            customer = _authenticate_customer_token(
                validated_token,
                request=request,
            )
            return customer, validated_token

        return super().get_user(validated_token), validated_token

    def get_user(self, validated_token):
        """
        Retained for compatibility with callers that directly invoke
        get_user(). Customer tokens still fail closed when no request is
        available; activity can still be validated server-side.
        """
        if validated_token.get(CUSTOMER_ACTOR_CLAIM) == CUSTOMER_ACTOR_VALUE:
            return _authenticate_customer_token(
                validated_token,
                request=None,
            )

        return super().get_user(validated_token)


class CustomerTokenRefreshSerializer(TokenRefreshSerializer):
    """
    Refresh serializer that enforces the server-side auth session before a
    customer refresh token can mint another access token.

    This class must be wired into the project's refresh endpoint/view during
    the views.py/settings stage. Staff refresh tokens remain handled by
    SimpleJWT's normal serializer path.
    """

    def _validate_customer_refresh_session(self, refresh):
        if refresh.get(CUSTOMER_ACTOR_CLAIM) != CUSTOMER_ACTOR_VALUE:
            return

        raw_session_id = refresh.get(
            AUTH_SESSION_ID_CLAIM
        )
        if not raw_session_id:
            raise InvalidToken(
                {
                    "detail": "Authentication session is missing.",
                    "code": "auth_session_missing",
                }
            )

        try:
            customer = Customer.objects.get(
                pk=refresh[api_settings.USER_ID_CLAIM],
                is_active=True,
            )
        except (Customer.DoesNotExist, KeyError) as exc:
            raise InvalidToken(
                {
                    "detail": "Customer not found or inactive.",
                    "code": "customer_not_found",
                }
            ) from exc

        request = self.context.get("request")

        try:
            validate_auth_session(
                raw_session_id,
                customer=customer,
                request=request,
                touch=True,
            )
        except AuthSessionNotFoundError as exc:
            raise InvalidToken(
                {
                    "detail": "Authentication session was not found.",
                    "code": "auth_session_not_found",
                }
            ) from exc
        except AuthSessionOwnershipError as exc:
            raise InvalidToken(
                {
                    "detail": "Authentication session ownership mismatch.",
                    "code": "auth_session_ownership_mismatch",
                }
            ) from exc
        except AuthSessionExpiredError as exc:
            raise InvalidToken(
                {
                    "detail": "Authentication session has expired.",
                    "code": "auth_session_expired",
                }
            ) from exc
        except AuthSessionInvalidError as exc:
            raise InvalidToken(
                {
                    "detail": "Authentication session is no longer active.",
                    "code": "auth_session_invalid",
                }
            ) from exc

    def validate(self, attrs):
        refresh = self.token_class(
            attrs["refresh"]
        )

        # Staff refresh tokens keep SimpleJWT's original behavior.
        if refresh.get(CUSTOMER_ACTOR_CLAIM) != CUSTOMER_ACTOR_VALUE:
            return super().validate(attrs)

        # Customer refresh tokens are validated against the BlazeLine
        # server-side auth session before any new token is minted.
        self._validate_customer_refresh_session(
            refresh
        )

        data = {
            "access": str(
                refresh.access_token
            )
        }

        # Preserve SimpleJWT's configured refresh-token rotation semantics
        # without invoking its default user-model lookup (Customer is not
        # AUTH_USER_MODEL in BlazeLine).
        if api_settings.ROTATE_REFRESH_TOKENS:
            if api_settings.BLACKLIST_AFTER_ROTATION:
                try:
                    refresh.blacklist()
                except AttributeError:
                    pass

            refresh.set_jti()
            refresh.set_exp()
            refresh.set_iat()
            refresh.outstand()

            data["refresh"] = str(
                refresh
            )

        # Defensive verification that the new access token preserves the
        # same server-side session binding as the refresh token.
        access = data.get("access")
        if access:
            from rest_framework_simplejwt.tokens import AccessToken

            validated_access = AccessToken(
                access
            )

            if validated_access.get(
                AUTH_SESSION_ID_CLAIM
            ) != refresh.get(
                AUTH_SESSION_ID_CLAIM
            ):
                raise InvalidToken(
                    {
                        "detail": (
                            "Refreshed token session binding is invalid."
                        ),
                        "code": "auth_session_binding_invalid",
                    }
                )

        return data


class IsCustomer(BasePermission):
    """Use on customer-only endpoints (addresses, orders, etc)."""

    def has_permission(self, request, view):
        if not isinstance(request.user, Customer):
            raise PermissionDenied(
                "This endpoint is for customer accounts only."
            )
        return True
