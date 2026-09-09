from __future__ import annotations

import uuid

from django.db import models


class AnalyticsSession(models.Model):
    """
    One browser/application session.

    A session may begin as anonymous and later become associated with a
    Customer after login/registration.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    customer = models.ForeignKey(
        "accounts.Customer",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="analytics_sessions",
        db_index=True,
    )

    guest_id = models.UUIDField(
        null=True,
        blank=True,
        db_index=True,
    )

    first_seen_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
    )

    last_seen_at = models.DateTimeField(
        auto_now=True,
        db_index=True,
    )

    last_path = models.CharField(
        max_length=500,
        blank=True,
    )

    landing_page = models.CharField(
        max_length=500,
        blank=True,
    )

    ip_address = models.GenericIPAddressField(
        null=True,
        blank=True,
    )

    user_agent = models.TextField(
        blank=True,
    )

    device_type = models.CharField(
        max_length=30,
        blank=True,
    )

    browser = models.CharField(
        max_length=100,
        blank=True,
    )

    operating_system = models.CharField(
        max_length=100,
        blank=True,
    )

    country = models.CharField(
        max_length=100,
        blank=True,
    )

    source = models.CharField(
        max_length=255,
        blank=True,
    )

    utm_source = models.CharField(
        max_length=255,
        blank=True,
    )

    utm_medium = models.CharField(
        max_length=255,
        blank=True,
    )

    utm_campaign = models.CharField(
        max_length=255,
        blank=True,
    )

    utm_term = models.CharField(
        max_length=255,
        blank=True,
    )

    utm_content = models.CharField(
        max_length=255,
        blank=True,
    )

    is_active = models.BooleanField(
        default=True,
        db_index=True,
    )

    class Meta:
        verbose_name = "Analytics Session"
        verbose_name_plural = "Analytics Sessions"
        indexes = [
            models.Index(
                fields=["customer", "-last_seen_at"],
                name="analytics_sess_customer_last",
            ),
            models.Index(
                fields=["guest_id", "-last_seen_at"],
                name="analytics_sess_guest_last",
            ),
            models.Index(
                fields=["-last_seen_at"],
                name="analytics_sess_last_seen",
            ),
        ]

    def __str__(self) -> str:
        return str(self.id)


class AnalyticsEvent(models.Model):
    """
    Immutable behavioral/business analytics event.

    Commercial truth remains in the transactional models:
    Order, Payment, Cart, ProductVariant, etc.
    """

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    session = models.ForeignKey(
        AnalyticsSession,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="events",
        db_index=True,
    )

    customer = models.ForeignKey(
        "accounts.Customer",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="analytics_events",
        db_index=True,
    )

    guest_id = models.UUIDField(
        null=True,
        blank=True,
        db_index=True,
    )

    event_name = models.CharField(
        max_length=100,
        db_index=True,
    )

    occurred_at = models.DateTimeField(
        db_index=True,
    )

    path = models.CharField(
        max_length=500,
        blank=True,
    )

    page_title = models.CharField(
        max_length=255,
        blank=True,
    )

    product = models.ForeignKey(
        "catalog.Product",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="analytics_events",
        db_index=True,
    )

    variant = models.ForeignKey(
        "catalog.ProductVariant",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="analytics_events",
        db_index=True,
    )

    category = models.ForeignKey(
        "catalog.Category",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="analytics_events",
        db_index=True,
    )

    subcategory = models.ForeignKey(
        "catalog.SubCategory",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="analytics_events",
        db_index=True,
    )

    order = models.ForeignKey(
        "orders.Order",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="analytics_events",
        db_index=True,
    )

    metadata = models.JSONField(
        default=dict,
        blank=True,
    )

    class Meta:
        verbose_name = "Analytics Event"
        verbose_name_plural = "Analytics Events"
        indexes = [
            models.Index(
                fields=["event_name", "-occurred_at"],
                name="analytics_evt_name_time",
            ),
            models.Index(
                fields=["customer", "-occurred_at"],
                name="analytics_evt_customer_time",
            ),
            models.Index(
                fields=["session", "-occurred_at"],
                name="analytics_evt_session_time",
            ),
            models.Index(
                fields=["guest_id", "-occurred_at"],
                name="analytics_evt_guest_time",
            ),
            models.Index(
                fields=["product", "event_name", "-occurred_at"],
                name="analytics_evt_product",
            ),
            models.Index(
                fields=["variant", "event_name", "-occurred_at"],
                name="analytics_evt_variant",
            ),
            models.Index(
                fields=["order", "-occurred_at"],
                name="analytics_evt_order_time",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.event_name} · {self.occurred_at.isoformat()}"