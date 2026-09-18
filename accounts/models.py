import uuid

from django.contrib.auth.hashers import check_password as check_password_hash
from django.contrib.auth.hashers import make_password
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


# ==========================================================
# CUSTOMER
#
# Deliberately a plain model, NOT django.contrib.auth's User and NOT
# AUTH_USER_MODEL. This keeps the existing admin-panel staff login
# (django.contrib.auth.User) completely untouched — customers and staff
# are two separate identities, authenticated two separate ways
# (customers via OTP + password + JWT here, staff via the existing
# admin login).
#
# `password` is nullable: a Customer row is only ever created (in
# accounts/views.py CompleteRegistrationView) once a password has been
# set, so in practice every row has one — nullable just guards against
# any future code path that creates a Customer without going through
# that flow.
#
# is_authenticated / is_anonymous are implemented manually so DRF's
# IsAuthenticated permission works on a Customer instance the same way
# it would on a real Django user.
# ==========================================================

class Customer(models.Model):

    email = models.EmailField(unique=True, null=True, blank=True)
    phone = models.CharField(max_length=15, unique=True, null=True, blank=True)
    full_name = models.CharField(max_length=150, blank=True)

    password = models.CharField(max_length=128, null=True, blank=True)

    is_email_verified = models.BooleanField(default=False)
    is_phone_verified = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    date_joined = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Customer"
        verbose_name_plural = "Customers"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(email__isnull=False) | models.Q(phone__isnull=False),
                name="customer_has_email_or_phone",
            ),
        ]

    def __str__(self):
        return self.email or self.phone or f"Customer #{self.pk}"

    def clean(self):
        if not self.email and not self.phone:
            raise ValidationError("A customer must have an email or a phone number.")

    # ---- password helpers ----
    def set_password(self, raw_password):
        self.password = make_password(raw_password)

    def check_password(self, raw_password):
        if not self.password:
            return False
        return check_password_hash(raw_password, self.password)

    def has_usable_password(self):
        return bool(self.password)

    # ---- DRF/Django auth-compatibility shims ----
    @property
    def is_authenticated(self):
        return True

    @property
    def is_anonymous(self):
        return False

# ==========================================================
# CUSTOMER AUTH SESSION
#
# One row = one real authenticated customer session/device login.
# This is separate from the analytics browsing session.
#
# The session is the server-side authority for customer JWT access.
# JWTs should carry this session_id and the authentication layer should
# reject tokens when the session is inactive/revoked/expired.
# ==========================================================

class CustomerAuthSession(models.Model):

    REVOCATION_LOGOUT = "logout"
    REVOCATION_PASSWORD_RESET = "password_reset"
    REVOCATION_PASSWORD_CHANGED = "password_changed"
    REVOCATION_ADMIN = "admin_revoke"
    REVOCATION_REVOKE_ALL = "revoke_all"
    REVOCATION_EXPIRED = "expired"
    REVOCATION_SECURITY = "security"
    REVOCATION_CUSTOMER_DEACTIVATED = "customer_deactivated"

    customer = models.ForeignKey(
        Customer,
        on_delete=models.CASCADE,
        related_name="auth_sessions",
    )

    # Stable server-side identifier for the authenticated session.
    # This value is safe to place in JWT claims as the auth-session
    # reference; never use the access/refresh token itself as the
    # persistent session identifier.
    session_id = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        db_index=True,
        editable=False,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    last_activity_at = models.DateTimeField(
        default=timezone.now,
        db_index=True,
    )

    logged_out_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    # Optional hard lifetime for the server-side session.
    # Authentication code must reject the session once this time is
    # reached, even when is_active is still True.
    expires_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
    )

    is_active = models.BooleanField(
        default=True,
        db_index=True,
    )

    # Set whenever the server explicitly revokes the session. Keeping
    # this separate from logged_out_at distinguishes a normal logout
    # from password/security/admin/session-expiry revocations.
    revoked_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
    )

    revocation_reason = models.CharField(
        max_length=64,
        blank=True,
        help_text=(
            "Server-side reason for revocation, for example logout, "
            "password_reset, admin_revoke, revoke_all, or expired."
        ),
    )

    ip_address = models.GenericIPAddressField(
        null=True,
        blank=True,
    )

    user_agent = models.TextField(
        blank=True,
    )

    device = models.CharField(
        max_length=100,
        blank=True,
    )

    browser = models.CharField(
        max_length=100,
        blank=True,
    )

    os = models.CharField(
        max_length=100,
        blank=True,
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Customer Auth Session"
        verbose_name_plural = "Customer Auth Sessions"
        indexes = [
            models.Index(fields=["customer", "-created_at"]),
            models.Index(fields=["customer", "is_active"]),
            models.Index(fields=["session_id"]),
            models.Index(fields=["expires_at", "is_active"]),
        ]

    def __str__(self):
        return f"{self.customer} - {self.session_id}"

    @property
    def is_expired(self):
        """Return True when the server-side hard session lifetime has ended."""
        return bool(
            self.expires_at is not None
            and timezone.now() >= self.expires_at
        )

    @property
    def is_valid(self):
        """
        Server-side validity flag.

        JWT authentication code should still perform its own database
        validation rather than trusting this property alone.
        """
        return bool(
            self.is_active
            and not self.is_expired
            and self.customer.is_active
        )

    def mark_activity(self):
        """
        Update the server-side activity timestamp.

        Throttling how often this method is called belongs in the
        authentication/audit service so every authenticated request does
        not necessarily write to PostgreSQL.
        """
        if not self.is_active or self.is_expired:
            return

        self.last_activity_at = timezone.now()
        self.save(update_fields=["last_activity_at"])

    def revoke(self, reason, *, logged_out_at=False):
        """
        Revoke the session at the source-of-truth layer.

        `logged_out_at=True` is reserved for a normal customer logout.
        Other security/admin revocations should leave logged_out_at null
        while still making the session inactive.
        """
        now = timezone.now()

        update_fields = [
            "is_active",
            "revoked_at",
            "revocation_reason",
        ]

        self.is_active = False
        self.revoked_at = now
        self.revocation_reason = str(reason)[:64]

        if logged_out_at:
            self.logged_out_at = now
            update_fields.append("logged_out_at")

        self.save(update_fields=update_fields)

    def logout(self):
        """Backward-compatible normal logout helper."""
        self.revoke(self.REVOCATION_LOGOUT, logged_out_at=True)


# ==========================================================
# CUSTOMER AUTH EVENT
#
# Immutable lifetime audit trail.
# One row = one authentication/security event.
#
# This is intentionally separate from analytics events. Analytics can
# fail, be delayed, be sampled, or be retention-limited; this table is
# the PostgreSQL source-of-truth for customer authentication history.
#
# The append-only protections below operate at the application/ORM
# layer. PostgreSQL superuser access or raw SQL can still bypass Django;
# a database trigger would be required for database-enforced physical
# immutability.
# ==========================================================

class CustomerAuthEvent(models.Model):

    EVENT_REGISTERED = "registered"
    EVENT_LOGIN_SUCCESS = "login_success"
    EVENT_LOGIN_FAILED = "login_failed"
    EVENT_LOGOUT = "logout"
    EVENT_PASSWORD_RESET = "password_reset"
    EVENT_PASSWORD_CHANGED = "password_changed"
    EVENT_SESSION_REVOKED = "session_revoked"
    EVENT_SESSION_EXPIRED = "session_expired"

    EVENT_CHOICES = [
        (EVENT_REGISTERED, "Registered"),
        (EVENT_LOGIN_SUCCESS, "Login Success"),
        (EVENT_LOGIN_FAILED, "Login Failed"),
        (EVENT_LOGOUT, "Logout"),
        (EVENT_PASSWORD_RESET, "Password Reset"),
        (EVENT_PASSWORD_CHANGED, "Password Changed"),
        (EVENT_SESSION_REVOKED, "Session Revoked"),
        (EVENT_SESSION_EXPIRED, "Session Expired"),
    ]

    # Separate immutable event identifier for external/admin references.
    event_id = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        db_index=True,
        editable=False,
    )

    customer = models.ForeignKey(
        Customer,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="auth_events",
    )

    session = models.ForeignKey(
        CustomerAuthSession,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="events",
    )

    event_type = models.CharField(
        max_length=40,
        choices=EVENT_CHOICES,
        db_index=True,
    )

    occurred_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
    )

    ip_address = models.GenericIPAddressField(
        null=True,
        blank=True,
    )

    user_agent = models.TextField(
        blank=True,
    )

    device = models.CharField(
        max_length=100,
        blank=True,
    )

    browser = models.CharField(
        max_length=100,
        blank=True,
    )

    os = models.CharField(
        max_length=100,
        blank=True,
    )

    # SHA-256 of a normalized login identifier (email/phone) for failed
    # login correlation without retaining the raw attempted identifier.
    identifier_hash = models.CharField(
        max_length=64,
        blank=True,
        db_index=True,
    )

    # Keep only safe structured audit metadata here. Never put passwords,
    # OTP codes, access tokens, refresh tokens, Authorization headers, or
    # other secrets into this JSON field.
    metadata = models.JSONField(
        default=dict,
        blank=True,
    )

    class Meta:
        ordering = ["-occurred_at"]
        verbose_name = "Customer Auth Event"
        verbose_name_plural = "Customer Auth Events"
        indexes = [
            models.Index(fields=["customer", "-occurred_at"]),
            models.Index(fields=["customer", "event_type", "-occurred_at"]),
            models.Index(fields=["session", "-occurred_at"]),
            models.Index(fields=["identifier_hash", "-occurred_at"]),
        ]

    def __str__(self):
        customer = self.customer or "Unknown customer"
        return f"{customer} - {self.event_type} - {self.occurred_at}"

    def save(self, *args, **kwargs):
        """
        Application-level append-only protection.

        Existing auth events must never be modified through the Django ORM.
        New audit rows may only be inserted.
        """
        if self.pk is not None:
            raise ValidationError(
                "CustomerAuthEvent is append-only and cannot be updated."
            )

        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        """Prevent application-level deletion of auth audit events."""
        raise ValidationError(
            "CustomerAuthEvent is append-only and cannot be deleted."
        )


# ==========================================================
# OTP
#
# `purpose` scopes an OTP to exactly one flow (register / reset
# password) so an OTP requested for one purpose can never be replayed
# to complete a different, more sensitive action.
# ==========================================================

class OTP(models.Model):

    CHANNEL_EMAIL = "email"
    CHANNEL_PHONE = "phone"
    CHANNEL_CHOICES = [
        (CHANNEL_EMAIL, "Email"),
        (CHANNEL_PHONE, "Phone"),
    ]

    PURPOSE_REGISTER = "register"
    PURPOSE_RESET_PASSWORD = "reset_password"
    PURPOSE_CHOICES = [
        (PURPOSE_REGISTER, "Register"),
        (PURPOSE_RESET_PASSWORD, "Reset Password"),
    ]

    identifier = models.CharField(
        max_length=255,
        db_index=True,
        help_text="Normalized email or 10-digit phone number.",
    )
    channel = models.CharField(max_length=10, choices=CHANNEL_CHOICES)
    purpose = models.CharField(max_length=20, choices=PURPOSE_CHOICES, default=PURPOSE_REGISTER)

    code_hash = models.CharField(max_length=128)
    attempts = models.PositiveIntegerField(default=0)
    max_attempts = models.PositiveIntegerField(default=5)

    is_used = models.BooleanField(default=False)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "OTP"
        verbose_name_plural = "OTPs"
        indexes = [
            models.Index(fields=["identifier", "channel", "purpose", "is_used"]),
        ]

    def is_expired(self):
        return timezone.now() > self.expires_at

    def __str__(self):
        return f"OTP({self.channel}:{self.identifier}:{self.purpose})"


# ==========================================================
# ADDRESS
# ==========================================================

class Address(models.Model):

    customer = models.ForeignKey(
        Customer,
        on_delete=models.CASCADE,
        related_name="addresses",
    )

    full_name = models.CharField(max_length=150)
    phone = models.CharField(max_length=15)

    address_line1 = models.CharField(max_length=255)
    address_line2 = models.CharField(max_length=255, blank=True)
    landmark = models.CharField(max_length=255, blank=True)

    city = models.CharField(max_length=100)
    state = models.CharField(max_length=100)
    pincode = models.CharField(max_length=6)

    is_default = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-is_default", "-created_at"]
        verbose_name = "Address"
        verbose_name_plural = "Addresses"

    def __str__(self):
        return f"{self.full_name} - {self.pincode}"

    def save(self, *args, **kwargs):
        if self.is_default:
            Address.objects.filter(
                customer=self.customer, is_default=True
            ).exclude(pk=self.pk).update(is_default=False)
        super().save(*args, **kwargs)