import hashlib
import random
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
    return "".join(random.choices("0123456789", k=OTP_LENGTH))


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

    otp = (
        OTP.objects.filter(identifier=identifier, channel=channel, purpose=purpose, is_used=False)
        .order_by("-created_at")
        .first()
    )

    if not otp:
        raise OTPInvalidError("No OTP found. Please request a new one.")
    if otp.is_expired():
        raise OTPExpiredError("OTP has expired. Please request a new one.")
    if otp.attempts >= otp.max_attempts:
        raise OTPInvalidError("Too many incorrect attempts. Please request a new OTP.")

    otp.attempts += 1
    otp.save(update_fields=["attempts"])

    if otp.code_hash != _hash_code(code):
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
    Sends via MSG91. Requires MSG91_AUTH_KEY + MSG91_OTP_TEMPLATE_ID env
    vars. In DEBUG without those set, prints to console instead — so
    local dev doesn't need a real MSG91 account.
    """
    auth_key = getattr(settings, "MSG91_AUTH_KEY", None)

    if settings.DEBUG and not auth_key:
        print(f"[DEV OTP] Phone {phone}: {code}")
        return

    response = requests.get(
        "https://control.msg91.com/api/v5/otp",
        params={
            "template_id": settings.MSG91_OTP_TEMPLATE_ID,
            "mobile": f"91{phone}",
            "authkey": auth_key,
            "otp": code,
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
# Customer is NOT AUTH_USER_MODEL, so tokens carry a custom "actor"
# claim ("customer") to distinguish them from staff tokens (which have
# no such claim and continue to resolve against auth.User exactly as
# before — admin panel auth is unaffected).
# ==========================================================

def issue_tokens_for_customer(customer):
    refresh = RefreshToken()
    refresh[api_settings.USER_ID_CLAIM] = getattr(customer, api_settings.USER_ID_FIELD)
    refresh["actor"] = "customer"

    return {
        "access": str(refresh.access_token),
        "refresh": str(refresh),
    }


class AppJWTAuthentication(JWTAuthentication):
    """
    Drop-in replacement for simplejwt's default JWTAuthentication.
    Staff tokens (no "actor" claim) resolve exactly as before, against
    auth.User — zero change to existing admin-panel API auth. Tokens
    with actor="customer" resolve against the Customer model instead.
    """

    def get_user(self, validated_token):
        if validated_token.get("actor") == "customer":
            customer_id = validated_token[api_settings.USER_ID_CLAIM]
            try:
                customer = Customer.objects.get(pk=customer_id, is_active=True)
            except Customer.DoesNotExist:
                raise AuthenticationFailed("Customer not found or inactive.", code="customer_not_found")
            return customer

        return super().get_user(validated_token)


class IsCustomer(BasePermission):
    """Use on customer-only endpoints (addresses, orders, etc)."""

    def has_permission(self, request, view):
        if not isinstance(request.user, Customer):
            raise PermissionDenied("This endpoint is for customer accounts only.")
        return True