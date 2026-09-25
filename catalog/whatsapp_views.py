from datetime import timedelta

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone

from rest_framework import serializers, status
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated, IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import WhatsAppConversation, WhatsAppMessage
from .whatsapp_messaging import (
    WhatsAppMessagingError,
    send_whatsapp_session_message,
)


# ==========================================================
# PAGINATION
# ==========================================================

class WhatsAppConversationPagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 100


# ==========================================================
# SERIALIZERS
# ==========================================================

class WhatsAppReplySerializer(serializers.Serializer):
    message = serializers.CharField(
        min_length=1,
        max_length=4096,
        trim_whitespace=True,
    )


# ==========================================================
# HELPERS
# ==========================================================

def _serialize_message(message):
    sent_by = None

    if message.sent_by:
        sent_by = {
            "id": message.sent_by.id,
            "username": message.sent_by.username,
            "first_name": message.sent_by.first_name,
            "last_name": message.sent_by.last_name,
        }

    return {
        "id": message.id,
        "direction": message.direction,
        "message_type": message.message_type,
        "text": message.text,
        "status": message.status,
        "provider_message_id": message.provider_message_id,
        "provider_request_id": message.provider_request_id,
        "sent_by": sent_by,
        "occurred_at": message.occurred_at,
        "created_at": message.created_at,
    }


def _serialize_conversation(conversation, *, include_messages=False):
    data = {
        "id": conversation.id,
        "integrated_number": conversation.integrated_number,
        "customer_number": conversation.customer_number,
        "customer_name": conversation.customer_name,
        "status": conversation.status,
        "unread_count": conversation.unread_count,
        "last_message_preview": conversation.last_message_preview,
        "last_message_at": conversation.last_message_at,
        "last_inbound_at": conversation.last_inbound_at,
        "last_outbound_at": conversation.last_outbound_at,
        "session_expires_at": conversation.session_expires_at,
        "created_at": conversation.created_at,
        "updated_at": conversation.updated_at,
    }

    if include_messages:
        messages = (
            conversation.messages
            .select_related("sent_by")
            .order_by("occurred_at", "id")
        )

        data["messages"] = [
            _serialize_message(message)
            for message in messages
        ]

    return data


def _get_session_expiry(conversation):
    """
    Prefer the explicit expiry received from MSG91.

    If MSG91 did not provide an expiry timestamp, fall back to
    24 hours from the most recent inbound customer message.
    """
    if conversation.session_expires_at:
        return conversation.session_expires_at

    if conversation.last_inbound_at:
        return conversation.last_inbound_at + timedelta(hours=24)

    return None


# ==========================================================
# ADMIN — CONVERSATION LIST
# ==========================================================

class AdminWhatsAppConversationListAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdminUser]

    def get(self, request):
        queryset = (
            WhatsAppConversation.objects
            .all()
            .order_by("-last_message_at", "-created_at")
        )

        search = (request.query_params.get("q") or "").strip()

        if search:
            from django.db.models import Q

            queryset = queryset.filter(
                Q(customer_name__icontains=search)
                | Q(customer_number__icontains=search)
                | Q(last_message_preview__icontains=search)
            )

        status_value = (
            request.query_params.get("status") or ""
        ).strip().lower()

        if status_value in {
            WhatsAppConversation.STATUS_OPEN,
            WhatsAppConversation.STATUS_CLOSED,
        }:
            queryset = queryset.filter(
                status=status_value
            )

        paginator = WhatsAppConversationPagination()

        page = paginator.paginate_queryset(
            queryset,
            request,
        )

        results = [
            _serialize_conversation(conversation)
            for conversation in page
        ]

        return paginator.get_paginated_response(results)


# ==========================================================
# ADMIN — CONVERSATION DETAIL
# ==========================================================

class AdminWhatsAppConversationDetailAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdminUser]

    def get(self, request, pk):
        conversation = get_object_or_404(
            WhatsAppConversation,
            pk=pk,
        )

        return Response(
            _serialize_conversation(
                conversation,
                include_messages=True,
            )
        )


# ==========================================================
# ADMIN — MARK AS READ
# ==========================================================

class AdminWhatsAppConversationReadAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdminUser]

    @transaction.atomic
    def post(self, request, pk):
        conversation = get_object_or_404(
            WhatsAppConversation.objects.select_for_update(),
            pk=pk,
        )

        conversation.unread_count = 0

        conversation.save(
            update_fields=[
                "unread_count",
                "updated_at",
            ]
        )

        return Response({
            "success": True,
            "conversation_id": conversation.id,
            "unread_count": 0,
        })


# ==========================================================
# ADMIN — SEND REPLY
# ==========================================================

class AdminWhatsAppConversationReplyAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdminUser]

    def post(self, request, pk):
        serializer = WhatsAppReplySerializer(
            data=request.data,
        )

        serializer.is_valid(
            raise_exception=True,
        )

        message_text = serializer.validated_data[
            "message"
        ].strip()

        conversation = get_object_or_404(
            WhatsAppConversation,
            pk=pk,
        )

        if conversation.status == WhatsAppConversation.STATUS_CLOSED:
            return Response(
                {
                    "success": False,
                    "error": (
                        "This WhatsApp conversation is closed."
                    ),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        session_expiry = _get_session_expiry(
            conversation
        )

        if not session_expiry:
            return Response(
                {
                    "success": False,
                    "error": (
                        "No active WhatsApp customer session "
                        "was found for this conversation."
                    ),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if timezone.now() >= session_expiry:
            return Response(
                {
                    "success": False,
                    "error": (
                        "The WhatsApp customer-service session "
                        "has expired. An approved template is "
                        "required to start a new conversation."
                    ),
                    "session_expires_at": session_expiry,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # --------------------------------------------------
        # Send through MSG91 first.
        # --------------------------------------------------

        try:
            provider_result = send_whatsapp_session_message(
                recipient=conversation.customer_number,
                message=message_text,
            )
        except WhatsAppMessagingError as exc:
            return Response(
                {
                    "success": False,
                    "error": str(exc),
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )

        now = timezone.now()

        # --------------------------------------------------
        # Store outbound message + update conversation.
        # --------------------------------------------------

        with transaction.atomic():

            provider_message_id = (
                provider_result.get(
                    "provider_message_id"
                )
            )

            provider_request_id = ""

            provider_response = provider_result.get(
                "provider_response"
            )

            if isinstance(provider_response, dict):
                provider_request_id = str(
                    provider_response.get(
                        "request_id"
                    )
                    or provider_response.get(
                        "requestId"
                    )
                    or ""
                )

            outbound_message = WhatsAppMessage.objects.create(
                conversation=conversation,
                direction=WhatsAppMessage.DIRECTION_OUTBOUND,
                message_type="text",
                text=message_text,
                provider_message_id=provider_message_id,
                provider_request_id=provider_request_id,
                status=WhatsAppMessage.STATUS_SENT,
                sent_by=request.user,
                raw_payload={
                    "provider_response": provider_response,
                },
                occurred_at=now,
            )

            conversation.last_message_preview = (
                message_text[:500]
            )

            conversation.last_message_at = now
            conversation.last_outbound_at = now

            conversation.save(
                update_fields=[
                    "last_message_preview",
                    "last_message_at",
                    "last_outbound_at",
                    "updated_at",
                ]
            )

        return Response(
            {
                "success": True,
                "message": _serialize_message(
                    outbound_message
                ),
                "conversation": _serialize_conversation(
                    conversation
                ),
            },
            status=status.HTTP_201_CREATED,
        )