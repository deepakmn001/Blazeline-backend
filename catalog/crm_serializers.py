from django.contrib.auth.models import User
from rest_framework import serializers

from .models import (
    InteriorConsultation,
    InteriorConsultationActivity,
    QuoteRequest,
    QuoteRequestActivity,
    QuoteAttachment,
)
from .serializers import QuoteAttachmentSerializer

class CRMStaffMiniSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = (
            "id",
            "username",
            "first_name",
            "last_name",
            "email",
        )


# ==========================================================
# INTERIOR CONSULTATION — LIST
# ==========================================================

class InteriorConsultationListSerializer(serializers.ModelSerializer):
    assigned_to = CRMStaffMiniSerializer(read_only=True)

    class Meta:
        model = InteriorConsultation
        fields = (
            "id",
            "consultation_id",
            "full_name",
            "phone",
            "email",
            "property_type",
            "service_required",
            "property_size",
            "project_stage",
            "estimated_budget",
            "timeline",
            "preferred_consultation_date",
            "scheduled_for",
            "status",
            "assigned_to",
            "next_follow_up_at",
            "created_at",
            "updated_at",
        )


# ==========================================================
# INTERIOR CONSULTATION — DETAIL
# ==========================================================

class InteriorConsultationDetailSerializer(
    serializers.ModelSerializer
):
    assigned_to = CRMStaffMiniSerializer(read_only=True)

    class Meta:
        model = InteriorConsultation
        fields = (
            "id",
            "consultation_id",
            "customer",

            "property_type",
            "service_required",
            "property_size",
            "project_stage",
            "estimated_budget",
            "timeline",
            "design_preference",

            "inspiration_photo",

            "full_name",
            "phone",
            "email",
            "property_location",
            "pincode",

            "preferred_consultation_date",
            "scheduled_for",

            "additional_message",

            "status",
            "assigned_to",

            "next_follow_up_at",
            "contacted_at",
            "completed_at",
            "converted_at",

            "created_at",
            "updated_at",
        )


class InteriorConsultationActivitySerializer(
    serializers.ModelSerializer
):
    actor = CRMStaffMiniSerializer(read_only=True)

    class Meta:
        model = InteriorConsultationActivity
        fields = (
            "id",
            "event_type",
            "from_status",
            "to_status",
            "note",
            "metadata",
            "actor",
            "occurred_at",
        )
        read_only_fields = fields


# ==========================================================
# QUOTE REQUEST — LIST
# ==========================================================

class QuoteRequestListSerializer(serializers.ModelSerializer):
    assigned_to = CRMStaffMiniSerializer(read_only=True)

    source_product_name = serializers.CharField(
        source="source_product.name",
        read_only=True,
        allow_null=True,
    )

    source_variant_name = serializers.CharField(
        source="source_variant.display_name",
        read_only=True,
        allow_null=True,
    )

    class Meta:
        model = QuoteRequest
        fields = (
            "id",
            "quote_id",
            "full_name",
            "phone",
            "email",
            "company",
            "project_location",
            "delivery_pincode",
            "project_type",
            "materials",
            "status",

            "source",
            "source_product",
            "source_product_name",
            "source_variant",
            "source_variant_name",
            "requested_quantity",

            "assigned_to",
            "next_follow_up_at",

            "created_at",
            "updated_at",
        )


# ==========================================================
# QUOTE REQUEST — DETAIL
# ==========================================================

class QuoteRequestDetailSerializer(
    serializers.ModelSerializer
):
    assigned_to = CRMStaffMiniSerializer(read_only=True)

    source_product_name = serializers.CharField(
        source="source_product.name",
        read_only=True,
        allow_null=True,
    )

    source_variant_name = serializers.CharField(
        source="source_variant.display_name",
        read_only=True,
        allow_null=True,
    )

    attachments = QuoteAttachmentSerializer(
        many=True,
        read_only=True,
    )

    class Meta:
        model = QuoteRequest
        fields = (
            "id",
            "quote_id",

            "full_name",
            "phone",
            "email",
            "company",

            "project_location",
            "delivery_pincode",
            "project_type",
            "materials",
            "requirements",

            "status",

            "source",
            "source_product",
            "source_product_name",
            "source_variant",
            "source_variant_name",
            "requested_quantity",

            "assigned_to",
            "next_follow_up_at",
            "contacted_at",
            "quoted_at",
            "approved_at",
            "rejected_at",

            "attachments",

            "created_at",
            "updated_at",
        )


class QuoteRequestActivitySerializer(
    serializers.ModelSerializer
):
    actor = CRMStaffMiniSerializer(read_only=True)

    class Meta:
        model = QuoteRequestActivity
        fields = (
            "id",
            "event_type",
            "from_status",
            "to_status",
            "note",
            "metadata",
            "actor",
            "occurred_at",
        )
        read_only_fields = fields