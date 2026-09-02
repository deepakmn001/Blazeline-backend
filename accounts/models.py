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