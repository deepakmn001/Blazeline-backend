"""Admin CRM outbound message endpoints.

CRM customer communication is generated automatically from the lead/quote data.
Admins select a channel and send; they do not have to compose a customer-facing
message manually. The generated message is passed to the provider template and
recorded in the immutable CRM activity timeline.
"""

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .crm_messaging import (
    CRMMessageError,
    send_crm_email,
    send_crm_whatsapp,
)
from .crm_serializers import (
    InteriorConsultationDetailSerializer,
    QuoteRequestDetailSerializer,
)
from .models import (
    InteriorConsultation,
    InteriorConsultationActivity,
    QuoteRequest,
    QuoteRequestActivity,
)


class CRMMessageInputSerializer(serializers.Serializer):
    """Channel-only input.

    `message` is intentionally not required: CRM copy is generated server-side
    from the actual record to keep customer communication consistent.
    """
    channel = serializers.ChoiceField(choices=("whatsapp", "email"))
    subject = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=200,
    )


def _clean_text(value) -> str:
    return " ".join(str(value or "").split()).strip()


def _join_nonempty(parts, separator=", ") -> str:
    return separator.join(
        item for item in (_clean_text(part) for part in parts) if item
    )


def _default_email_subject(record_type: str) -> str:
    if record_type == "consultation":
        return "BlazeLine | Your Interior Consultation Request"
    return "BlazeLine | Your Quotation Request"


def _format_consultation_message(consultation: InteriorConsultation) -> str:
    """Build polished customer-facing copy from consultation data."""
    service = _clean_text(consultation.service_required) or "interior consultation"
    property_type = _clean_text(consultation.property_type) or "property"
    location = _clean_text(consultation.property_location) or "your location"
    size = _clean_text(consultation.property_size)
    budget = _clean_text(consultation.estimated_budget)
    timeline = _clean_text(consultation.timeline)
    design = _clean_text(consultation.design_preference)
    preferred_date = (
        consultation.preferred_consultation_date.strftime("%d %b %Y")
        if consultation.preferred_consultation_date
        else ""
    )

    details = []

    if size:
        details.append(f"project size of {size}")
    if budget:
        details.append(f"estimated budget of {budget}")
    if timeline:
        details.append(f"preferred timeline of {timeline}")
    if design:
        details.append(f"design preference of {design}")

    if details:
        detail_sentence = (
            " We have also received the details for "
            + _join_nonempty(details, separator=", ")
            + "."
        )
    else:
        detail_sentence = ""

    preferred_sentence = (
        f" Your preferred consultation date is {preferred_date}."
        if preferred_date
        else ""
    )

    return (
        f"We’ve received your {service} request for your {property_type} "
        f"project in {location}.{detail_sentence}{preferred_sentence} "
        "We’ll review the information submitted and connect with you regarding "
        "the next steps."
    )


def _format_quote_message(quote: QuoteRequest) -> str:
    """Build polished customer-facing copy from quote-request data."""
    project_type = _clean_text(quote.project_type) or "building materials"
    location = _clean_text(quote.project_location) or "your project location"
    materials = quote.materials if isinstance(quote.materials, list) else []

    material_names = []
    for item in materials:
        if isinstance(item, str):
            value = _clean_text(item)
        elif isinstance(item, dict):
            value = _clean_text(
                item.get("name")
                or item.get("label")
                or item.get("material")
                or item.get("title")
            )
        else:
            value = ""
        if value and value not in material_names:
            material_names.append(value)

    source_product = getattr(quote, "source_product", None)
    source_variant = getattr(quote, "source_variant", None)

    product_name = _clean_text(getattr(source_product, "name", ""))
    variant_name = _clean_text(getattr(source_variant, "display_name", ""))

    parts = [
        f"We’ve received your quotation request for {project_type} in {location}."
    ]

    product_context = _join_nonempty(
        [
            (
                f"Product: {product_name}"
                if product_name
                else ""
            ),
            (
                f"Variant: {variant_name}"
                if variant_name
                else ""
            ),
        ],
        separator=" • ",
    )
    if product_context:
        parts.append(f"{product_context}.")

    quantity = getattr(quote, "requested_quantity", None)
    if quantity:
        parts.append(f"Requested quantity: {quantity}.")

    if material_names:
        shown = material_names[:6]
        material_text = ", ".join(shown)
        if len(material_names) > 6:
            material_text += f" and {len(material_names) - 6} more"
        parts.append(f"Requested materials: {material_text}.")

    parts.append(
        "We’ll review the information submitted and connect with you regarding "
        "the next steps."
    )

    return " ".join(parts)


def _generate_customer_message(record_type: str, record) -> str:
    if record_type == "consultation":
        return _format_consultation_message(record)
    return _format_quote_message(record)


def _send(
    channel: str,
    *,
    phone: str,
    email: str,
    customer_name: str,
    message: str,
    subject: str,
) -> dict:
    """Dispatch the generated CRM message through the selected provider."""
    if channel == "whatsapp":
        return send_crm_whatsapp(
            recipient=phone,
            customer_name=customer_name,
            message=message,
        )

    return send_crm_email(
        recipient=email,
        subject=subject,
        message=message,
    )


def _consultation_after_send(
    pk: int,
    request,
    *,
    channel: str,
    message: str,
    provider_result: dict,
):
    """Persist CRM contact state only after successful provider acceptance."""
    with transaction.atomic():
        consultation = get_object_or_404(
            InteriorConsultation.objects.select_for_update(),
            pk=pk,
        )

        old_status = consultation.status
        consultation.contacted_at = timezone.now()

        if consultation.status in {
            InteriorConsultation.STATUS_NEW,
            InteriorConsultation.STATUS_CONTACT_PENDING,
        }:
            consultation.status = InteriorConsultation.STATUS_CONTACTED

        consultation.save(
            update_fields=["contacted_at", "status", "updated_at"]
        )

        if old_status != consultation.status:
            InteriorConsultationActivity.objects.create(
                consultation=consultation,
                actor=request.user,
                event_type=InteriorConsultationActivity.EVENT_STATUS_CHANGED,
                from_status=old_status,
                to_status=consultation.status,
                metadata={
                    "trigger": "crm_message",
                    "channel": channel,
                    "auto_generated": True,
                },
            )

        InteriorConsultationActivity.objects.create(
            consultation=consultation,
            actor=request.user,
            event_type=InteriorConsultationActivity.EVENT_CONTACTED,
            note=message,
            metadata={
                "channel": channel,
                "provider": provider_result.get("provider"),
                "provider_accepted": provider_result.get("provider_accepted"),
                "provider_message_id": provider_result.get(
                    "provider_message_id"
                ),
                "trigger": "crm_message",
                "auto_generated": True,
            },
        )

        return consultation


def _quote_after_send(
    pk: int,
    request,
    *,
    channel: str,
    message: str,
    provider_result: dict,
):
    """Persist CRM contact state only after successful provider acceptance."""
    with transaction.atomic():
        quote = get_object_or_404(
            QuoteRequest.objects.select_for_update(),
            pk=pk,
        )

        old_status = quote.status
        quote.contacted_at = timezone.now()

        if quote.status == "pending":
            quote.status = "reviewing"

        quote.save(
            update_fields=["contacted_at", "status", "updated_at"]
        )

        if old_status != quote.status:
            QuoteRequestActivity.objects.create(
                quote=quote,
                actor=request.user,
                event_type=QuoteRequestActivity.EVENT_STATUS_CHANGED,
                from_status=old_status,
                to_status=quote.status,
                metadata={
                    "trigger": "crm_message",
                    "channel": channel,
                    "auto_generated": True,
                },
            )

        QuoteRequestActivity.objects.create(
            quote=quote,
            actor=request.user,
            event_type=QuoteRequestActivity.EVENT_CONTACTED,
            note=message,
            metadata={
                "channel": channel,
                "provider": provider_result.get("provider"),
                "provider_accepted": provider_result.get("provider_accepted"),
                "provider_message_id": provider_result.get(
                    "provider_message_id"
                ),
                "trigger": "crm_message",
                "auto_generated": True,
            },
        )

        return quote


class AdminInteriorConsultationMessageAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdminUser]

    def post(self, request, pk):
        serializer = CRMMessageInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        consultation = get_object_or_404(
            InteriorConsultation.objects,
            pk=pk,
        )
        channel = serializer.validated_data["channel"]
        subject = (
            serializer.validated_data.get("subject", "").strip()
            or _default_email_subject("consultation")
        )
        message = _generate_customer_message("consultation", consultation)

        try:
            provider_result = _send(
                channel,
                phone=consultation.phone,
                email=consultation.email,
                customer_name=consultation.full_name,
                message=message,
                subject=subject,
            )
        except CRMMessageError as exc:
            return Response(
                {"detail": str(exc)},
                status=502,
            )

        consultation = _consultation_after_send(
            pk,
            request,
            channel=channel,
            message=message,
            provider_result=provider_result,
        )

        return Response(
            {
                "success": True,
                "channel": channel,
                "provider": provider_result.get("provider"),
                "provider_accepted": provider_result.get("provider_accepted"),
                "provider_message_id": provider_result.get(
                    "provider_message_id"
                ),
                "auto_generated": True,
                "generated_message": message,
                "consultation": InteriorConsultationDetailSerializer(
                    consultation,
                    context={"request": request},
                ).data,
            }
        )


class AdminQuoteRequestMessageAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdminUser]

    def post(self, request, pk):
        serializer = CRMMessageInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        quote = get_object_or_404(
            QuoteRequest.objects.select_related(
                "source_product",
                "source_variant",
            ),
            pk=pk,
        )
        channel = serializer.validated_data["channel"]
        subject = (
            serializer.validated_data.get("subject", "").strip()
            or _default_email_subject("quote")
        )
        message = _generate_customer_message("quote", quote)

        try:
            provider_result = _send(
                channel,
                phone=quote.phone,
                email=quote.email,
                customer_name=quote.full_name,
                message=message,
                subject=subject,
            )
        except CRMMessageError as exc:
            return Response(
                {"detail": str(exc)},
                status=502,
            )

        quote = _quote_after_send(
            pk,
            request,
            channel=channel,
            message=message,
            provider_result=provider_result,
        )

        return Response(
            {
                "success": True,
                "channel": channel,
                "provider": provider_result.get("provider"),
                "provider_accepted": provider_result.get("provider_accepted"),
                "provider_message_id": provider_result.get(
                    "provider_message_id"
                ),
                "auto_generated": True,
                "generated_message": message,
                "quote": QuoteRequestDetailSerializer(
                    quote,
                    context={"request": request},
                ).data,
            }
        )
