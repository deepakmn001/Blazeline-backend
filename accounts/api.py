from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.urls import include, path
from rest_framework import permissions, serializers, status, viewsets
from rest_framework.response import Response
from rest_framework.routers import DefaultRouter
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from . import services
from .models import OTP, Address, Customer

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
    full_name = serializers.CharField(required=False, allow_blank=True, max_length=150)
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
        fields = ["id", "email", "phone", "full_name", "is_email_verified", "is_phone_verified", "date_joined"]
        read_only_fields = fields


class AddressSerializer(serializers.ModelSerializer):
    class Meta:
        model = Address
        fields = [
            "id", "full_name", "phone",
            "address_line1", "address_line2", "landmark",
            "city", "state", "pincode", "is_default", "created_at",
        ]
        read_only_fields = ["id", "created_at"]


def _lookup_for(identifier, channel):
    return {"email": identifier} if channel == OTP.CHANNEL_EMAIL else {"phone": identifier}


def _merge_guest_cart_if_present(request, customer):
    guest_id = request.data.get("guest_id")
    if guest_id:
        from cart.services import merge_guest_cart_into_customer
        merge_guest_cart_into_customer(guest_id, customer)


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
                {"detail": str(exc), "retry_after_seconds": exc.seconds_remaining},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )
        except services.CustomerAlreadyExistsError:
            return Response(
                {"detail": "An account already exists for this identifier. Please log in instead."},
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
    Only confirms the OTP and hands back a short-lived
    verification_token. It never creates a Customer or issues login
    tokens itself — that happens in CompleteRegistrationView /
    ResetPasswordView, scoped to the token's purpose.
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
            normalized_identifier = services.verify_otp(identifier, channel, code, purpose)
        except (services.OTPInvalidError, services.OTPExpiredError) as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        verification_token = services.generate_verification_token(normalized_identifier, channel, purpose)

        return Response({
            "verified": True,
            "verification_token": verification_token,
        })


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
                serializer.validated_data["verification_token"], OTP.PURPOSE_REGISTER
            )
        except services.VerificationTokenError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        lookup = _lookup_for(identifier, channel)
        existing = Customer.objects.filter(**lookup).first()

        if existing and existing.has_usable_password():
            return Response(
                {"detail": "An account already exists for this identifier. Please log in instead."},
                status=status.HTTP_409_CONFLICT,
            )

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

        tokens = services.issue_tokens_for_customer(customer)
        _merge_guest_cart_if_present(request, customer)

        return Response(
            {
                "created": existing is None,
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

        channel = OTP.CHANNEL_EMAIL if "@" in raw_identifier else OTP.CHANNEL_PHONE
        identifier = services.normalize_identifier(raw_identifier, channel)

        customer = Customer.objects.filter(**_lookup_for(identifier, channel)).first()

        if not customer or not customer.check_password(password):
            return Response(
                {"detail": "Invalid phone/email or password."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not customer.is_active:
            return Response(
                {"detail": "This account has been deactivated. Please contact support."},
                status=status.HTTP_403_FORBIDDEN,
            )

        tokens = services.issue_tokens_for_customer(customer)
        _merge_guest_cart_if_present(request, customer)

        return Response({
            "customer": CustomerSerializer(customer).data,
            "tokens": tokens,
        })


class ResetPasswordView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = ResetPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            identifier, channel = services.parse_verification_token(
                serializer.validated_data["verification_token"], OTP.PURPOSE_RESET_PASSWORD
            )
        except services.VerificationTokenError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        customer = Customer.objects.filter(**_lookup_for(identifier, channel)).first()
        if not customer:
            return Response({"detail": "No account found for this identifier."}, status=status.HTTP_404_NOT_FOUND)

        customer.set_password(serializer.validated_data["new_password"])
        customer.save(update_fields=["password", "updated_at"])

        tokens = services.issue_tokens_for_customer(customer)

        return Response({
            "customer": CustomerSerializer(customer).data,
            "tokens": tokens,
        })


class LogoutView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        refresh_token = request.data.get("refresh")
        if not refresh_token:
            return Response({"detail": "Refresh token is required."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            RefreshToken(refresh_token).blacklist()
        except TokenError:
            return Response({"detail": "Invalid or already-blacklisted token."}, status=status.HTTP_400_BAD_REQUEST)
        return Response(status=status.HTTP_205_RESET_CONTENT)


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
# URLS
# ==========================================================

router = DefaultRouter()
router.register("addresses", AddressViewSet, basename="address")

urlpatterns = [
    path("otp/request/", RequestOTPView.as_view(), name="otp-request"),
    path("otp/verify/", VerifyOTPView.as_view(), name="otp-verify"),
    path("register/complete/", CompleteRegistrationView.as_view(), name="register-complete"),
    path("login/", LoginView.as_view(), name="login"),
    path("password/reset/", ResetPasswordView.as_view(), name="password-reset"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("me/", MeView.as_view(), name="me"),
    path("", include(router.urls)),
]