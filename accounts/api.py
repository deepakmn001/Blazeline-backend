from __future__ import annotations

import logging
from datetime import timedelta
from uuid import UUID

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Count, Max, Q
from django.urls import include, path
from django.utils import timezone
from rest_framework import permissions, serializers, status, viewsets
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.routers import DefaultRouter
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenRefreshView

from analytics.services import identify_session, track_event

from . import auth_audit, services
from .models import (
    OTP,
    Address,
    Customer,
    CustomerAuthEvent,
    CustomerAuthSession,
)


logger = logging.getLogger(__name__)


# ==========================================================
# SERIALIZERS
# ==========================================================


class RequestOTPSerializer(serializers.Serializer):
    channel = serializers.ChoiceField(choices=OTP.CHANNEL_CHOICES)
    identifier = serializers.CharField()
    purpose = serializers.ChoiceField(choices=OTP.PURPOSE_CHOICES)

    def validate(self, attrs):
        channel = attrs["channel"]
        identifier = attrs["identifier"].strip()

        if channel == OTP.CHANNEL_EMAIL:
            serializers.EmailField().run_validation(identifier)
        else:
            digits = "".join(ch for ch in identifier if ch.isdigit())
            if len(digits) not in (10, 12):
                raise serializers.ValidationError(
                    {"identifier": "Enter a valid 10-digit phone number."}
                )
        return attrs


class VerifyOTPSerializer(serializers.Serializer):
    channel = serializers.ChoiceField(choices=OTP.CHANNEL_CHOICES)
    identifier = serializers.CharField()
    code = serializers.CharField(max_length=6, min_length=4)
    purpose = serializers.ChoiceField(choices=OTP.PURPOSE_CHOICES)


class CompleteRegistrationSerializer(serializers.Serializer):
    verification_token = serializers.CharField()
    full_name = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=150,
    )
    password = serializers.CharField(write_only=True)

    def validate_password(self, value):
        try:
            validate_password(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(list(exc.messages))
        return value


class LoginSerializer(serializers.Serializer):
    identifier = serializers.CharField()
    password = serializers.CharField(write_only=True)


class ResetPasswordSerializer(serializers.Serializer):
    verification_token = serializers.CharField()
    new_password = serializers.CharField(write_only=True)

    def validate_new_password(self, value):
        try:
            validate_password(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(list(exc.messages))
        return value


class CustomerSerializer(serializers.ModelSerializer):
    class Meta:
        model = Customer
        fields = [
            "id",
            "email",
            "phone",
            "full_name",
            "is_email_verified",
            "is_phone_verified",
            "date_joined",
        ]
        read_only_fields = fields


class AdminCustomerListSerializer(serializers.ModelSerializer):
    active_session_count = serializers.IntegerField(read_only=True)
    last_auth_activity = serializers.DateTimeField(read_only=True, allow_null=True)

    class Meta:
        model = Customer
        fields = [
            "id",
            "email",
            "phone",
            "full_name",
            "is_email_verified",
            "is_phone_verified",
            "is_active",
            "date_joined",
            "active_session_count",
            "last_auth_activity",
        ]
        read_only_fields = fields


class AdminLoginActivitySerializer(serializers.ModelSerializer):
    customer_id = serializers.SerializerMethodField()
    customer_name = serializers.SerializerMethodField()
    customer_email = serializers.SerializerMethodField()
    customer_phone = serializers.SerializerMethodField()
    session_id = serializers.SerializerMethodField()
    activity_type = serializers.SerializerMethodField()
    reason = serializers.SerializerMethodField()

    class Meta:
        model = CustomerAuthEvent
        fields = [
            "event_id",
            "activity_type",
            "occurred_at",
            "customer_id",
            "customer_name",
            "customer_email",
            "customer_phone",
            "session_id",
            "device",
            "browser",
            "os",
            "ip_address",
            "reason",
        ]
        read_only_fields = fields

    def get_customer_id(self, obj):
        return obj.customer_id

    def get_customer_name(self, obj):
        if obj.customer is None:
            return None
        return obj.customer.full_name or None

    def get_customer_email(self, obj):
        if obj.customer is None:
            return None
        return obj.customer.email or None

    def get_customer_phone(self, obj):
        if obj.customer is None:
            return None
        return obj.customer.phone or None

    def get_session_id(self, obj):
        if obj.session is None:
            return None
        return str(obj.session.session_id)

    def get_activity_type(self, obj):
        return obj.event_type

    def get_reason(self, obj):
        metadata = obj.metadata or {}
        return metadata.get("reason")


class AddressSerializer(serializers.ModelSerializer):
    class Meta:
        model = Address
        fields = [
            "id",
            "full_name",
            "phone",
            "address_line1",
            "address_line2",
            "landmark",
            "city",
            "state",
            "pincode",
            "is_default",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]


class CustomerAuthSessionSerializer(serializers.ModelSerializer):
    is_online = serializers.SerializerMethodField()
    is_current = serializers.SerializerMethodField()

    class Meta:
        model = CustomerAuthSession
        fields = [
            "session_id",
            "created_at",
            "last_activity_at",
            "logged_out_at",
            "expires_at",
            "is_active",
            "revoked_at",
            "revocation_reason",
            "ip_address",
            "user_agent",
            "device",
            "browser",
            "os",
            "is_online",
            "is_current",
        ]
        read_only_fields = fields

    def get_is_online(self, obj):
        return auth_audit.is_auth_session_online(obj)

    def get_is_current(self, obj):
        request = self.context.get("request")
        if request is None:
            return False

        current_session_id = auth_audit.get_auth_session_id_from_request(request)
        return current_session_id == obj.session_id


class CustomerAuthEventSerializer(serializers.ModelSerializer):
    session_id = serializers.UUIDField(
        source="session.session_id",
        read_only=True,
        allow_null=True,
    )

    class Meta:
        model = CustomerAuthEvent
        fields = [
            "event_id",
            "event_type",
            "occurred_at",
            "session_id",
            "ip_address",
            "user_agent",
            "device",
            "browser",
            "os",
            "metadata",
        ]
        read_only_fields = fields


class AuthAuditPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100


# ==========================================================
# HELPERS
# ==========================================================


def _lookup_for(identifier, channel):
    return (
        {"email": identifier}
        if channel == OTP.CHANNEL_EMAIL
        else {"phone": identifier}
    )


def _merge_guest_cart_if_present(request, customer):
    guest_id = request.data.get("guest_id")
    if guest_id:
        from cart.services import merge_guest_cart_into_customer

        merge_guest_cart_into_customer(guest_id, customer)


def _get_analytics_session_id(request):
    """
    Read the browser analytics session identity from the request.

    Invalid/missing IDs are intentionally ignored.
    """
    raw_session_id = (
        request.headers.get("X-Analytics-Session-Id")
        or request.data.get("analytics_session_id")
    )

    if not raw_session_id:
        return None

    return str(raw_session_id).strip() or None


def _track_auth_analytics_best_effort(
    *,
    event_name,
    request,
    customer,
    guest_id=None,
    metadata=None,
):
    """
    Analytics is observational only.

    Authentication, token issuance, session creation, cart merging and
    password/security state must never depend on analytics availability.
    """
    try:
        analytics_session_id = _get_analytics_session_id(request)
        session = identify_session(
            session_id=analytics_session_id,
            customer=customer,
            guest_id=guest_id,
        )

        track_event(
            event_name=event_name,
            request=request,
            session=session,
            customer=customer,
            guest_id=guest_id,
            metadata=dict(metadata or {}),
        )
    except Exception:
        logger.exception(
            "Customer authentication analytics tracking failed",
            extra={
                "event_name": event_name,
                "customer_id": getattr(customer, "pk", None),
            },
        )


def _valid_uuid(value):
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _refresh_token_matches_customer_session(refresh, request):
    """
    Verify that the supplied refresh token belongs to the same customer and
    server-side auth session as the already-authenticated access token.
    """
    customer = request.user
    if not isinstance(customer, Customer):
        return False

    if refresh.get(services.CUSTOMER_ACTOR_CLAIM) != services.CUSTOMER_ACTOR_VALUE:
        return False

    refresh_customer_id = refresh.get(services.api_settings.USER_ID_CLAIM)
    if str(refresh_customer_id) != str(customer.pk):
        return False

    request_session_id = auth_audit.get_auth_session_id_from_request(request)
    refresh_session_id = _valid_uuid(
        refresh.get(services.AUTH_SESSION_ID_CLAIM)
    )

    if request_session_id is None or refresh_session_id is None:
        return False

    return request_session_id == refresh_session_id


# ==========================================================
# OTP VIEWS
# ==========================================================


class RequestOTPView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = RequestOTPSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        identifier = serializer.validated_data["identifier"]
        channel = serializer.validated_data["channel"]
        purpose = serializer.validated_data["purpose"]

        try:
            services.request_otp(identifier, channel, purpose)
        except services.OTPCooldownError as exc:
            return Response(
                {
                    "detail": str(exc),
                    "retry_after_seconds": exc.seconds_remaining,
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )
        except services.CustomerAlreadyExistsError:
            return Response(
                {
                    "detail": (
                        "An account already exists for this identifier. "
                        "Please log in instead."
                    )
                },
                status=status.HTTP_409_CONFLICT,
            )
        except services.CustomerNotFoundError:
            return Response(
                {"detail": "No account found for this identifier."},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response({"detail": f"OTP sent via {channel}."})


class VerifyOTPView(APIView):
    """
    Only confirms the OTP and hands back a short-lived verification_token.
    It never creates a Customer or issues login tokens itself — that happens
    in CompleteRegistrationView / ResetPasswordView, scoped to the token's
    purpose.
    """

    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = VerifyOTPSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        channel = serializer.validated_data["channel"]
        identifier = serializer.validated_data["identifier"]
        code = serializer.validated_data["code"]
        purpose = serializer.validated_data["purpose"]

        try:
            normalized_identifier = services.verify_otp(
                identifier,
                channel,
                code,
                purpose,
            )
        except (services.OTPInvalidError, services.OTPExpiredError) as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        verification_token = services.generate_verification_token(
            normalized_identifier,
            channel,
            purpose,
        )

        return Response(
            {
                "verified": True,
                "verification_token": verification_token,
            }
        )


# ==========================================================
# REGISTRATION / LOGIN / PASSWORD RESET
# ==========================================================


class CompleteRegistrationView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = CompleteRegistrationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            identifier, channel = services.parse_verification_token(
                serializer.validated_data["verification_token"],
                OTP.PURPOSE_REGISTER,
            )
        except services.VerificationTokenError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        lookup = _lookup_for(identifier, channel)
        existing = Customer.objects.filter(**lookup).first()

        if existing and existing.has_usable_password():
            return Response(
                {
                    "detail": (
                        "An account already exists for this identifier. "
                        "Please log in instead."
                    )
                },
                status=status.HTTP_409_CONFLICT,
            )

        created = existing is None

        with transaction.atomic():
            customer = existing or Customer(**lookup)

            full_name = serializer.validated_data.get("full_name", "").strip()
            if full_name:
                customer.full_name = full_name

            if channel == OTP.CHANNEL_EMAIL:
                customer.is_email_verified = True
            else:
                customer.is_phone_verified = True

            customer.set_password(serializer.validated_data["password"])
            customer.save()

            tokens = services.issue_tokens_for_customer(
                customer,
                request=request,
                event_type=CustomerAuthEvent.EVENT_REGISTERED,
                event_metadata={
                    "registration_channel": channel,
                    "created": created,
                },
            )

            _merge_guest_cart_if_present(request, customer)

        guest_id = request.data.get("guest_id")
        _track_auth_analytics_best_effort(
            event_name="customer_registered",
            request=request,
            customer=customer,
            guest_id=guest_id,
            metadata={
                "registration_channel": channel,
                "created": created,
            },
        )

        return Response(
            {
                "created": created,
                "customer": CustomerSerializer(customer).data,
                "tokens": tokens,
            },
            status=status.HTTP_201_CREATED,
        )


class LoginView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        raw_identifier = serializer.validated_data["identifier"].strip()
        password = serializer.validated_data["password"]

        channel = (
            OTP.CHANNEL_EMAIL
            if "@" in raw_identifier
            else OTP.CHANNEL_PHONE
        )
        identifier = services.normalize_identifier(raw_identifier, channel)

        customer = Customer.objects.filter(
            **_lookup_for(identifier, channel)
        ).first()

        if not customer or not customer.check_password(password):
            auth_audit.record_login_failed(
                request=request,
                identifier=identifier,
                channel=channel,
                reason="invalid_credentials",
            )
            return Response(
                {"detail": "Invalid phone/email or password."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not customer.is_active:
            auth_audit.record_login_failed(
                request=request,
                identifier=identifier,
                channel=channel,
                reason="account_inactive",
                metadata={"customer_id": customer.pk},
            )
            return Response(
                {
                    "detail": (
                        "This account has been deactivated. "
                        "Please contact support."
                    )
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        tokens = services.issue_tokens_for_customer(
            customer,
            request=request,
            event_type=CustomerAuthEvent.EVENT_LOGIN_SUCCESS,
            event_metadata={"login_channel": channel},
        )

        _merge_guest_cart_if_present(request, customer)

        guest_id = request.data.get("guest_id")
        _track_auth_analytics_best_effort(
            event_name="login_success",
            request=request,
            customer=customer,
            guest_id=guest_id,
            metadata={"login_channel": channel},
        )

        return Response(
            {
                "customer": CustomerSerializer(customer).data,
                "tokens": tokens,
            }
        )


class ResetPasswordView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = ResetPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            identifier, channel = services.parse_verification_token(
                serializer.validated_data["verification_token"],
                OTP.PURPOSE_RESET_PASSWORD,
            )
        except services.VerificationTokenError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        customer = Customer.objects.filter(
            **_lookup_for(identifier, channel)
        ).first()
        if not customer:
            return Response(
                {"detail": "No account found for this identifier."},
                status=status.HTTP_404_NOT_FOUND,
            )

        with transaction.atomic():
            customer.set_password(serializer.validated_data["new_password"])
            customer.save(update_fields=["password", "updated_at"])

            # Password reset is a security boundary: every pre-existing
            # authentication session is invalidated before a fresh session is
            # issued for this successful reset flow.
            auth_audit.revoke_all_customer_sessions(
                customer,
                reason=CustomerAuthSession.REVOCATION_PASSWORD_RESET,
                request=request,
                event_metadata={
                    "trigger": "password_reset",
                },
            )

            tokens = services.issue_tokens_for_customer(
                customer,
                request=request,
                event_type=CustomerAuthEvent.EVENT_PASSWORD_RESET,
                event_metadata={
                    "reset_channel": channel,
                },
            )

        return Response(
            {
                "customer": CustomerSerializer(customer).data,
                "tokens": tokens,
            }
        )


class LogoutView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        refresh_token = request.data.get("refresh")
        if not refresh_token:
            return Response(
                {"detail": "Refresh token is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Customer sessions are authoritative server-side. Revoke the session
        # first, then blacklist the supplied refresh token as defense-in-depth.
        if isinstance(request.user, Customer):
            request_session_id = auth_audit.get_auth_session_id_from_request(request)
            if request_session_id is None:
                return Response(
                    {"detail": "Authentication session is missing or invalid."},
                    status=status.HTTP_401_UNAUTHORIZED,
                )

            try:
                refresh = RefreshToken(str(refresh_token))
            except TokenError:
                return Response(
                    {"detail": "Invalid refresh token."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if not _refresh_token_matches_customer_session(refresh, request):
                return Response(
                    {"detail": "Refresh token does not belong to this session."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            auth_audit.revoke_auth_session(
                request_session_id,
                customer=request.user,
                reason=CustomerAuthSession.REVOCATION_LOGOUT,
                request=request,
                event_type=CustomerAuthEvent.EVENT_LOGOUT,
                logged_out=True,
            )

            try:
                refresh.blacklist()
            except TokenError:
                # Server-side session revocation has already completed. An
                # already-blacklisted refresh token is therefore idempotent.
                pass
            except Exception:
                # Do not turn a successful server-side logout into a failed
                # logout solely because the optional blacklist write failed.
                logger.exception(
                    "Customer refresh-token blacklist failed during logout",
                    extra={"customer_id": request.user.pk},
                )

            return Response(status=status.HTTP_205_RESET_CONTENT)

        # Preserve the existing staff/admin logout behavior.
        try:
            RefreshToken(str(refresh_token)).blacklist()
        except TokenError:
            return Response(
                {"detail": "Invalid or already-blacklisted token."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(status=status.HTTP_205_RESET_CONTENT)


# ==========================================================
# CUSTOMER PROFILE / ADDRESS
# ==========================================================


class MeView(APIView):
    permission_classes = [permissions.IsAuthenticated, services.IsCustomer]

    def get(self, request):
        return Response(CustomerSerializer(request.user).data)


class AddressViewSet(viewsets.ModelViewSet):
    serializer_class = AddressSerializer
    permission_classes = [permissions.IsAuthenticated, services.IsCustomer]

    def get_queryset(self):
        return Address.objects.filter(customer=self.request.user)

    def perform_create(self, serializer):
        serializer.save(customer=self.request.user)


# ==========================================================
# CUSTOMER AUTH SESSION / AUDIT APIs
# ==========================================================


class CustomerSessionListView(APIView):
    permission_classes = [permissions.IsAuthenticated, services.IsCustomer]

    def get(self, request):
        active_only = str(
            request.query_params.get("active", "")
        ).lower() in {"1", "true", "yes"}

        queryset = auth_audit.customer_sessions_queryset(
            request.user,
            active_only=active_only,
        )

        paginator = AuthAuditPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        serializer = CustomerAuthSessionSerializer(
            page,
            many=True,
            context={"request": request},
        )
        return paginator.get_paginated_response(serializer.data)


class CustomerSessionRevokeView(APIView):
    permission_classes = [permissions.IsAuthenticated, services.IsCustomer]

    def post(self, request, session_id):
        try:
            auth_audit.revoke_auth_session(
                session_id,
                customer=request.user,
                reason=CustomerAuthSession.REVOCATION_SECURITY,
                request=request,
                event_type=CustomerAuthEvent.EVENT_SESSION_REVOKED,
                event_metadata={
                    "trigger": "customer_session_revoke",
                },
            )
        except auth_audit.AuthSessionNotFoundError:
            return Response(
                {"detail": "Authentication session not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        except auth_audit.AuthSessionOwnershipError:
            # Do not reveal whether another customer's session UUID exists.
            return Response(
                {"detail": "Authentication session not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response(
            {"revoked": True},
            status=status.HTTP_200_OK,
        )


class CustomerSessionRevokeAllView(APIView):
    permission_classes = [permissions.IsAuthenticated, services.IsCustomer]

    def post(self, request):
        revoked_count = auth_audit.revoke_all_customer_sessions(
            request.user,
            reason=CustomerAuthSession.REVOCATION_REVOKE_ALL,
            request=request,
            event_metadata={
                "trigger": "customer_revoke_all_sessions",
            },
        )

        return Response(
            {"revoked_sessions": revoked_count},
            status=status.HTTP_200_OK,
        )


class CustomerAuthHistoryView(APIView):
    permission_classes = [permissions.IsAuthenticated, services.IsCustomer]

    def get(self, request):
        event_types_raw = str(
            request.query_params.get("event_type", "")
        ).strip()

        event_types = None
        if event_types_raw:
            event_types = [
                value.strip()
                for value in event_types_raw.split(",")
                if value.strip()
            ]

            allowed = {
                choice[0]
                for choice in CustomerAuthEvent.EVENT_CHOICES
            }
            invalid = [value for value in event_types if value not in allowed]
            if invalid:
                return Response(
                    {
                        "detail": "Unsupported authentication event type.",
                        "invalid_event_types": invalid,
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        queryset = auth_audit.customer_auth_history_queryset(
            request.user,
            event_types=event_types,
        )

        paginator = AuthAuditPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        serializer = CustomerAuthEventSerializer(
            page,
            many=True,
            context={"request": request},
        )
        return paginator.get_paginated_response(serializer.data)


# ==========================================================
# ADMIN AUTH AUDIT
# ==========================================================


class AdminLoginActivityView(APIView):
    """
    Staff-only, centralized customer login activity feed.

    Supported query parameters:
        days     -> 30 or 90 (default: 30)
        event    -> all, login_success, login_failed (default: all)
        q        -> customer name/email/phone or IP search
        ordering -> only -occurred_at is supported (default)
    """

    permission_classes = [permissions.IsAuthenticated, permissions.IsAdminUser]

    ALLOWED_DAYS = {30, 90}
    ALLOWED_EVENTS = {
        "all",
        CustomerAuthEvent.EVENT_LOGIN_SUCCESS,
        CustomerAuthEvent.EVENT_LOGIN_FAILED,
    }

    def get(self, request):
        raw_days = str(request.query_params.get("days", "30")).strip()
        try:
            days = int(raw_days)
        except (TypeError, ValueError):
            days = 30

        if days not in self.ALLOWED_DAYS:
            days = 30

        event_filter = str(
            request.query_params.get("event", "all")
        ).strip().lower()

        if event_filter not in self.ALLOWED_EVENTS:
            event_filter = "all"

        cutoff = timezone.now() - timedelta(days=days)

        base_queryset = (
            CustomerAuthEvent.objects
            .filter(
                occurred_at__gte=cutoff,
                event_type__in=[
                    CustomerAuthEvent.EVENT_LOGIN_SUCCESS,
                    CustomerAuthEvent.EVENT_LOGIN_FAILED,
                ],
            )
            .select_related("customer", "session")
            .order_by("-occurred_at")
        )

        query = str(request.query_params.get("q", "")).strip()
        if query:
            base_queryset = base_queryset.filter(
                Q(customer__full_name__icontains=query)
                | Q(customer__email__icontains=query)
                | Q(customer__phone__icontains=query)
                | Q(ip_address__icontains=query)
            )

        success_count = base_queryset.filter(
            event_type=CustomerAuthEvent.EVENT_LOGIN_SUCCESS
        ).count()
        failed_count = base_queryset.filter(
            event_type=CustomerAuthEvent.EVENT_LOGIN_FAILED
        ).count()

        queryset = base_queryset
        if event_filter != "all":
            queryset = queryset.filter(event_type=event_filter)

        paginator = AuthAuditPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        serializer = AdminLoginActivitySerializer(
            page,
            many=True,
            context={"request": request},
        )

        response = paginator.get_paginated_response(serializer.data)
        response.data["range_days"] = days
        response.data["event_filter"] = event_filter
        response.data["summary"] = {
            "total": success_count + failed_count,
            "successful_logins": success_count,
            "failed_logins": failed_count,
        }
        response.data["generated_at"] = timezone.now()
        return response


class AdminCustomerDirectoryView(APIView):
    """
    Staff-only customer directory for the admin application.

    This endpoint is read-only and exposes only operational customer metadata
    needed by the admin UI. Passwords, OTPs, JWTs and identifier hashes are
    never returned.

    Query parameters:
        q      -> case-insensitive search across name/email/phone
        active -> true/false filter
    """

    permission_classes = [permissions.IsAuthenticated, permissions.IsAdminUser]

    ORDERING = {
        "joined": "-date_joined",
        "joined_oldest": "date_joined",
        "activity": "-last_auth_activity",
        "name": "full_name",
    }

    def get(self, request):
        queryset = (
            Customer.objects
            .annotate(
                active_session_count=Count(
                    "auth_sessions",
                    filter=Q(auth_sessions__is_active=True),
                    distinct=True,
                ),
                last_auth_activity=Max("auth_sessions__last_activity_at"),
            )
        )

        query = str(request.query_params.get("q", "")).strip()
        if query:
            queryset = queryset.filter(
                Q(full_name__icontains=query)
                | Q(email__icontains=query)
                | Q(phone__icontains=query)
            )

        active_raw = str(request.query_params.get("active", "")).strip().lower()
        if active_raw in {"true", "1", "yes"}:
            queryset = queryset.filter(is_active=True)
        elif active_raw in {"false", "0", "no"}:
            queryset = queryset.filter(is_active=False)

        ordering = str(request.query_params.get("ordering", "joined")).strip()
        queryset = queryset.order_by(
            self.ORDERING.get(ordering, self.ORDERING["joined"])
        )

        paginator = AuthAuditPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        serializer = AdminCustomerListSerializer(
            page,
            many=True,
            context={"request": request},
        )
        return paginator.get_paginated_response(serializer.data)




class AdminCustomerSessionRevokeView(APIView):
    """
    Staff-only endpoint to revoke one authentication session belonging to a
    specific customer.

    The server remains authoritative: the target session is revoked in the
    database and a security audit event is written. No token is returned.
    """

    permission_classes = [permissions.IsAuthenticated, permissions.IsAdminUser]

    def post(self, request, customer_id, session_id):
        customer = Customer.objects.filter(pk=customer_id).first()
        if customer is None:
            return Response(
                {"detail": "Customer not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            revoked = auth_audit.revoke_auth_session(
                session_id,
                customer=customer,
                reason=CustomerAuthSession.REVOCATION_SECURITY,
                request=request,
                event_type=CustomerAuthEvent.EVENT_SESSION_REVOKED,
                event_metadata={
                    "trigger": "admin_security_action",
                    "action": "revoke_session",
                    "actor_type": "staff_admin",
                    "admin_user_id": getattr(request.user, "pk", None),
                    "admin_username": getattr(
                        request.user,
                        "get_username",
                        lambda: "",
                    )(),
                },
            )
        except (
            auth_audit.AuthSessionNotFoundError,
            auth_audit.AuthSessionOwnershipError,
        ):
            return Response(
                {"detail": "Authentication session not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response(
            {
                "revoked": bool(revoked),
                "session_id": str(session_id),
            },
            status=status.HTTP_200_OK,
        )


class AdminCustomerRevokeAllSessionsView(APIView):
    """
    Staff-only endpoint to revoke every active authentication session for a
    customer. The operation is audited server-side.
    """

    permission_classes = [permissions.IsAuthenticated, permissions.IsAdminUser]

    def post(self, request, customer_id):
        customer = Customer.objects.filter(pk=customer_id).first()
        if customer is None:
            return Response(
                {"detail": "Customer not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        revoked_sessions = auth_audit.revoke_all_customer_sessions(
            customer,
            reason=CustomerAuthSession.REVOCATION_SECURITY,
            request=request,
            event_metadata={
                "trigger": "admin_security_action",
                "action": "revoke_all_sessions",
                "actor_type": "staff_admin",
                "admin_user_id": getattr(request.user, "pk", None),
                "admin_username": getattr(
                    request.user,
                    "get_username",
                    lambda: "",
                )(),
            },
        )

        return Response(
            {
                "revoked_sessions": revoked_sessions,
            },
            status=status.HTTP_200_OK,
        )


class AdminSecuritySummaryView(APIView):
    """
    Staff-only security summary for the Admin dashboard.

    All values are calculated server-side from Customer, CustomerAuthSession,
    and CustomerAuthEvent. The Admin UI must treat this response as the
    authoritative source for these metrics.
    """

    permission_classes = [permissions.IsAuthenticated, permissions.IsAdminUser]

    def get(self, request):
        now = timezone.now()
        cutoff_24h = now - timedelta(hours=24)
        online_cutoff = now - timedelta(
            seconds=max(
                1,
                int(getattr(settings, "AUTH_SESSION_ONLINE_WINDOW_SECONDS", 300)),
            )
        )

        active_sessions = CustomerAuthSession.objects.filter(is_active=True)
        online_sessions = active_sessions.filter(last_activity_at__gte=online_cutoff)

        return Response(
            {
                "customers": {
                    "total": Customer.objects.count(),
                    "active": Customer.objects.filter(is_active=True).count(),
                },
                "auth": {
                    "active_sessions": active_sessions.count(),
                    "online_sessions": online_sessions.count(),
                    "online_customers": online_sessions.values("customer_id")
                    .distinct()
                    .count(),
                    "failed_logins_24h": CustomerAuthEvent.objects.filter(
                        event_type=CustomerAuthEvent.EVENT_LOGIN_FAILED,
                        occurred_at__gte=cutoff_24h,
                    ).count(),
                    "sessions_revoked_24h": CustomerAuthEvent.objects.filter(
                        event_type=CustomerAuthEvent.EVENT_SESSION_REVOKED,
                        occurred_at__gte=cutoff_24h,
                    ).count(),
                },
                "generated_at": now,
            }
        )


class AdminCustomerAuthAuditView(APIView):
    """
    Staff-only lifetime auth audit endpoint.

    kind=sessions  -> authenticated session history
    kind=events    -> immutable authentication/security event history

    Results are paginated so a customer's lifetime history never causes an
    unbounded response.
    """

    permission_classes = [permissions.IsAuthenticated, permissions.IsAdminUser]

    def get(self, request, customer_id):
        customer = (
            Customer.objects
            .annotate(
                active_session_count=Count(
                    "auth_sessions",
                    filter=Q(auth_sessions__is_active=True),
                    distinct=True,
                ),
                last_auth_activity=Max("auth_sessions__last_activity_at"),
            )
            .filter(pk=customer_id)
            .first()
        )
        if customer is None:
            return Response(
                {"detail": "Customer not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        kind = str(
            request.query_params.get("kind", "events")
        ).lower()

        if kind == "sessions":
            active_only = str(
                request.query_params.get("active", "")
            ).lower() in {"1", "true", "yes"}
            queryset = auth_audit.customer_sessions_queryset(
                customer,
                active_only=active_only,
            )
            serializer_class = CustomerAuthSessionSerializer
        elif kind == "events":
            queryset = auth_audit.customer_auth_history_queryset(customer)
            serializer_class = CustomerAuthEventSerializer
        else:
            return Response(
                {"detail": "kind must be either 'sessions' or 'events'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        paginator = AuthAuditPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        serializer = serializer_class(
            page,
            many=True,
            context={"request": request},
        )
        response = paginator.get_paginated_response(serializer.data)
        response.data["customer"] = AdminCustomerListSerializer(customer).data
        response.data["kind"] = kind
        return response


# ==========================================================
# TOKEN REFRESH
# ==========================================================


class AppTokenRefreshView(TokenRefreshView):
    """
    Shared staff/customer refresh endpoint.

    Customer refresh tokens are checked against the PostgreSQL auth session
    before a new access token is minted. Staff tokens retain SimpleJWT's
    normal refresh behavior.
    """

    serializer_class = services.CustomerTokenRefreshSerializer
    permission_classes = [permissions.AllowAny]


# ==========================================================
# URLS
# ==========================================================

router = DefaultRouter()
router.register("addresses", AddressViewSet, basename="address")

urlpatterns = [
    path("otp/request/", RequestOTPView.as_view(), name="otp-request"),
    path("otp/verify/", VerifyOTPView.as_view(), name="otp-verify"),
    path(
        "register/complete/",
        CompleteRegistrationView.as_view(),
        name="register-complete",
    ),
    path("login/", LoginView.as_view(), name="login"),
    path(
        "password/reset/",
        ResetPasswordView.as_view(),
        name="password-reset",
    ),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("token/refresh/", AppTokenRefreshView.as_view(), name="token-refresh"),
    path("me/", MeView.as_view(), name="me"),
    path("sessions/", CustomerSessionListView.as_view(), name="session-list"),
    path(
        "sessions/revoke-all/",
        CustomerSessionRevokeAllView.as_view(),
        name="session-revoke-all",
    ),
    path(
        "sessions/<uuid:session_id>/revoke/",
        CustomerSessionRevokeView.as_view(),
        name="session-revoke",
    ),
    path(
        "auth-history/",
        CustomerAuthHistoryView.as_view(),
        name="auth-history",
    ),
    path(
        "admin/customers/",
        AdminCustomerDirectoryView.as_view(),
        name="admin-customer-directory",
    ),
    path(
        "admin/login-activity/",
        AdminLoginActivityView.as_view(),
        name="admin-login-activity",
    ),
    path(
        "admin/security-summary/",
        AdminSecuritySummaryView.as_view(),
        name="admin-security-summary",
    ),
    path(
        "admin/customers/<int:customer_id>/sessions/revoke-all/",
        AdminCustomerRevokeAllSessionsView.as_view(),
        name="admin-customer-revoke-all-sessions",
    ),
    path(
        "admin/customers/<int:customer_id>/sessions/<uuid:session_id>/revoke/",
        AdminCustomerSessionRevokeView.as_view(),
        name="admin-customer-session-revoke",
    ),
    path(
        "admin/customers/<int:customer_id>/auth/",
        AdminCustomerAuthAuditView.as_view(),
        name="admin-customer-auth-audit",
    ),
    path("", include(router.urls)),
]
