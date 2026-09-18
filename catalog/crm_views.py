from datetime import datetime, time as dt_time

from django.contrib.auth.models import User
from django.shortcuts import get_object_or_404
from django.db import transaction
from django.db.models import Q, Count
from django.db.models.functions import Cast
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.db.models import CharField
from rest_framework import status, serializers
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser, MultiPartParser, JSONParser
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny, IsAuthenticated, IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import (
    InteriorConsultation,
    InteriorConsultationActivity,
    QuoteRequest,
    QuoteRequestActivity,
)
from .serializers import InteriorConsultationSerializer
from .crm_serializers import (
    CRMStaffMiniSerializer,
    InteriorConsultationListSerializer,
    InteriorConsultationDetailSerializer,
    InteriorConsultationActivitySerializer,
    QuoteRequestListSerializer,
    QuoteRequestDetailSerializer,
    QuoteRequestActivitySerializer,
)


# ==========================================================
# COMMON
# ==========================================================

class CRMPageNumberPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100


class CRMStaffListAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdminUser]

    def get(self, request):
        staff = (
            User.objects
            .filter(is_active=True, is_staff=True)
            .order_by("first_name", "last_name", "username")
        )

        return Response({
            "count": staff.count(),
            "results": CRMStaffMiniSerializer(
                staff,
                many=True,
            ).data,
        })


def _validate_ordering(value, allowed_fields, default):
    if not value:
        return [default]

    requested = [item.strip() for item in value.split(",") if item.strip()]

    if not requested:
        return [default]

    invalid = []

    for item in requested:
        field = item[1:] if item.startswith("-") else item

        if field not in allowed_fields:
            invalid.append(item)

    if invalid:
        raise ValidationError({
            "ordering": (
                f"Unsupported ordering field(s): {', '.join(invalid)}"
            )
        })

    return requested


def _apply_created_range(queryset, request):
    created_from = request.query_params.get("created_from")
    created_to = request.query_params.get("created_to")

    if created_from:
        parsed = parse_date(created_from)

        if parsed is None:
            raise ValidationError({
                "created_from": "Use YYYY-MM-DD format."
            })

        start = datetime.combine(
            parsed,
            dt_time.min,
        )

        if timezone.is_naive(start):
            start = timezone.make_aware(start)

        queryset = queryset.filter(
            created_at__gte=start
        )

    if created_to:
        parsed = parse_date(created_to)

        if parsed is None:
            raise ValidationError({
                "created_to": "Use YYYY-MM-DD format."
            })

        next_day = datetime.combine(
            parsed,
            dt_time.max,
        )

        if timezone.is_naive(next_day):
            next_day = timezone.make_aware(next_day)

        queryset = queryset.filter(
            created_at__lte=next_day
        )

    return queryset


def _parse_staff_id(value):
    if value in (None, "", "null"):
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValidationError({
            "assigned_to": "assigned_to must be a valid staff user ID."
        })


def _set_quote_timestamp_for_status(quote):
    now = timezone.now()

    if quote.status == "quoted" and quote.quoted_at is None:
        quote.quoted_at = now

    elif quote.status == "approved" and quote.approved_at is None:
        quote.approved_at = now

    elif quote.status == "rejected" and quote.rejected_at is None:
        quote.rejected_at = now


def _set_consultation_timestamp_for_status(consultation):
    now = timezone.now()

    if consultation.status == "contacted" and consultation.contacted_at is None:
        consultation.contacted_at = now

    elif consultation.status == "completed" and consultation.completed_at is None:
        consultation.completed_at = now

    elif consultation.status == "converted" and consultation.converted_at is None:
        consultation.converted_at = now


# ==========================================================
# PUBLIC — INTERIOR CONSULTATION
# ==========================================================

class InteriorConsultationAPIView(APIView):
    permission_classes = [AllowAny]
    parser_classes = [
        MultiPartParser,
        FormParser,
        JSONParser,
    ]

    def post(self, request):
        serializer = InteriorConsultationSerializer(
            data=request.data,
            context={"request": request},
        )

        serializer.is_valid(raise_exception=True)

        consultation = serializer.save()

        return Response(
            {
                "success": True,
                "message": (
                    "Interior consultation request submitted successfully."
                ),
                "consultation_id": str(
                    consultation.consultation_id
                ),
                "status": consultation.status,
            },
            status=status.HTTP_201_CREATED,
        )


# ==========================================================
# INTERIOR CONSULTATION — ADMIN LIST
# ==========================================================

class AdminInteriorConsultationListAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdminUser]

    def get(self, request):
        queryset = (
            InteriorConsultation.objects
            .select_related("customer", "assigned_to")
            .all()
        )

        q = (request.query_params.get("q") or "").strip()

        if q:
            queryset = queryset.annotate(
                consultation_id_text=Cast(
                    "consultation_id",
                    output_field=CharField(),
                )
            ).filter(
                Q(full_name__icontains=q)
                | Q(phone__icontains=q)
                | Q(email__icontains=q)
                | Q(property_location__icontains=q)
                | Q(pincode__icontains=q)
                | Q(service_required__icontains=q)
                | Q(consultation_id_text__icontains=q)
            )

        status_value = request.query_params.get("status")

        if status_value:
            queryset = queryset.filter(
                status=status_value
            )

        service_required = request.query_params.get(
            "service"
        )

        if service_required:
            queryset = queryset.filter(
                service_required__iexact=service_required
            )

        property_type = request.query_params.get(
            "property_type"
        )

        if property_type:
            queryset = queryset.filter(
                property_type__iexact=property_type
            )

        assigned_to = request.query_params.get(
            "assigned_to"
        )

        if assigned_to == "unassigned":
            queryset = queryset.filter(
                assigned_to__isnull=True
            )
        elif assigned_to:
            queryset = queryset.filter(
                assigned_to_id=_parse_staff_id(assigned_to)
            )

        if request.query_params.get(
            "follow_up"
        ) == "overdue":
            queryset = queryset.filter(
                next_follow_up_at__lt=timezone.now()
            ).exclude(
                status=InteriorConsultation.STATUS_CLOSED
            )

        queryset = _apply_created_range(
            queryset,
            request,
        )

        ordering = _validate_ordering(
            request.query_params.get("ordering"),
            {
                "created_at",
                "updated_at",
                "full_name",
                "status",
                "preferred_consultation_date",
                "scheduled_for",
                "next_follow_up_at",
            },
            "-created_at",
        )

        queryset = queryset.order_by(*ordering)

        paginator = CRMPageNumberPagination()
        page = paginator.paginate_queryset(
            queryset,
            request,
        )

        serializer = InteriorConsultationListSerializer(
            page,
            many=True,
            context={"request": request},
        )

        return paginator.get_paginated_response(
            serializer.data
        )


# ==========================================================
# INTERIOR — ADMIN DETAIL
# ==========================================================

class AdminInteriorConsultationDetailAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdminUser]

    def get(self, request, pk):
        consultation = get_object_or_404(
            InteriorConsultation.objects.select_related(
                "customer",
                "assigned_to",
            ),
            pk=pk,
        )

        serializer = InteriorConsultationDetailSerializer(
            consultation,
            context={"request": request},
        )

        return Response(serializer.data)


# ==========================================================
# INTERIOR — OVERVIEW
# ==========================================================

class AdminInteriorConsultationOverviewAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdminUser]

    def get(self, request):
        queryset = InteriorConsultation.objects.all()
        now = timezone.now()

        data = queryset.aggregate(
            total=Count("id"),
            new=Count(
                "id",
                filter=Q(
                    status=InteriorConsultation.STATUS_NEW
                ),
            ),
            contact_pending=Count(
                "id",
                filter=Q(
                    status=InteriorConsultation.STATUS_CONTACT_PENDING
                ),
            ),
            contacted=Count(
                "id",
                filter=Q(
                    status=InteriorConsultation.STATUS_CONTACTED
                ),
            ),
            scheduled=Count(
                "id",
                filter=Q(
                    status=InteriorConsultation.STATUS_SCHEDULED
                ),
            ),
            completed=Count(
                "id",
                filter=Q(
                    status=InteriorConsultation.STATUS_COMPLETED
                ),
            ),
            converted=Count(
                "id",
                filter=Q(
                    status=InteriorConsultation.STATUS_CONVERTED
                ),
            ),
            closed=Count(
                "id",
                filter=Q(
                    status=InteriorConsultation.STATUS_CLOSED
                ),
            ),
            overdue_followups=Count(
                "id",
                filter=(
                    Q(next_follow_up_at__lt=now)
                    & ~Q(
                        status=InteriorConsultation.STATUS_CLOSED
                    )
                ),
            ),
        )

        data["generated_at"] = now

        return Response(data)


# ==========================================================
# INTERIOR — STATUS
# ==========================================================

class AdminInteriorConsultationStatusAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdminUser]

    class InputSerializer(serializers.Serializer):
        status = serializers.ChoiceField(
            choices=InteriorConsultation.STATUS_CHOICES
        )
        note = serializers.CharField(
            required=False,
            allow_blank=True,
            max_length=5000,
        )

    @transaction.atomic
    def post(self, request, pk):
        serializer = self.InputSerializer(
            data=request.data
        )
        serializer.is_valid(raise_exception=True)

        consultation = get_object_or_404(
            InteriorConsultation.objects.select_for_update(),
            pk=pk,
        )

        new_status = serializer.validated_data["status"]
        note = serializer.validated_data.get("note", "")

        old_status = consultation.status

        if old_status == new_status:
            return Response(
                InteriorConsultationDetailSerializer(
                    consultation,
                    context={"request": request},
                ).data
            )

        consultation.status = new_status
        _set_consultation_timestamp_for_status(
            consultation
        )
        consultation.save()

        InteriorConsultationActivity.objects.create(
            consultation=consultation,
            actor=request.user,
            event_type=(
                InteriorConsultationActivity.EVENT_STATUS_CHANGED
            ),
            from_status=old_status,
            to_status=new_status,
            note=note,
        )

        return Response(
            InteriorConsultationDetailSerializer(
                consultation,
                context={"request": request},
            ).data
        )


# ==========================================================
# INTERIOR — ASSIGN
# ==========================================================

class AdminInteriorConsultationAssignAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdminUser]

    class InputSerializer(serializers.Serializer):
        assigned_to = serializers.PrimaryKeyRelatedField(
            queryset=User.objects.filter(
                is_active=True,
                is_staff=True,
            ),
            allow_null=True,
            required=True,
        )
        note = serializers.CharField(
            required=False,
            allow_blank=True,
            max_length=5000,
        )

    @transaction.atomic
    def post(self, request, pk):
        serializer = self.InputSerializer(
            data=request.data
        )
        serializer.is_valid(raise_exception=True)

        consultation = get_object_or_404(
            InteriorConsultation.objects.select_for_update(),
            pk=pk,
        )

        new_assignee = serializer.validated_data["assigned_to"]
        note = serializer.validated_data.get("note", "")

        old_assignee = consultation.assigned_to

        if (
            (old_assignee.id if old_assignee else None)
            == (new_assignee.id if new_assignee else None)
        ):
            return Response(
                InteriorConsultationDetailSerializer(
                    consultation,
                    context={"request": request},
                ).data
            )

        consultation.assigned_to = new_assignee
        consultation.save(
            update_fields=[
                "assigned_to",
                "updated_at",
            ]
        )

        InteriorConsultationActivity.objects.create(
            consultation=consultation,
            actor=request.user,
            event_type=(
                InteriorConsultationActivity.EVENT_ASSIGNED
            ),
            note=note,
            metadata={
                "from_assigned_to": (
                    old_assignee.id
                    if old_assignee
                    else None
                ),
                "to_assigned_to": (
                    new_assignee.id
                    if new_assignee
                    else None
                ),
            },
        )

        return Response(
            InteriorConsultationDetailSerializer(
                consultation,
                context={"request": request},
            ).data
        )


# ==========================================================
# INTERIOR — SCHEDULE / RESCHEDULE
# ==========================================================

class AdminInteriorConsultationScheduleAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdminUser]

    class InputSerializer(serializers.Serializer):
        scheduled_for = serializers.DateTimeField(
            required=True
        )
        note = serializers.CharField(
            required=False,
            allow_blank=True,
            max_length=5000,
        )

    @transaction.atomic
    def post(self, request, pk):
        serializer = self.InputSerializer(
            data=request.data
        )
        serializer.is_valid(raise_exception=True)

        consultation = get_object_or_404(
            InteriorConsultation.objects.select_for_update(),
            pk=pk,
        )

        scheduled_for = serializer.validated_data[
            "scheduled_for"
        ]
        note = serializer.validated_data.get("note", "")

        if consultation.status in {
            InteriorConsultation.STATUS_COMPLETED,
            InteriorConsultation.STATUS_CONVERTED,
            InteriorConsultation.STATUS_CLOSED,
        }:
            raise ValidationError({
                "scheduled_for": (
                    "A completed, converted, or closed "
                    "consultation cannot be scheduled."
                )
            })

        previous = consultation.scheduled_for
        old_status = consultation.status

        consultation.scheduled_for = scheduled_for
        consultation.status = (
            InteriorConsultation.STATUS_SCHEDULED
        )

        consultation.save(
            update_fields=[
                "scheduled_for",
                "status",
                "updated_at",
            ]
        )

        InteriorConsultationActivity.objects.create(
            consultation=consultation,
            actor=request.user,
            event_type=(
                InteriorConsultationActivity.EVENT_RESCHEDULED
            ),
            from_status=old_status,
            to_status=InteriorConsultation.STATUS_SCHEDULED,
            note=note,
            metadata={
                "previous_scheduled_for": (
                    previous.isoformat()
                    if previous
                    else None
                ),
                "scheduled_for": (
                    scheduled_for.isoformat()
                ),
            },
        )

        return Response(
            InteriorConsultationDetailSerializer(
                consultation,
                context={"request": request},
            ).data
        )


# ==========================================================
# INTERIOR — NOTE
# ==========================================================

class AdminInteriorConsultationNoteAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdminUser]

    class InputSerializer(serializers.Serializer):
        note = serializers.CharField(
            min_length=1,
            max_length=10000,
        )

    def post(self, request, pk):
        serializer = self.InputSerializer(
            data=request.data
        )
        serializer.is_valid(raise_exception=True)

        consultation = get_object_or_404(
            InteriorConsultation,
            pk=pk,
        )

        InteriorConsultationActivity.objects.create(
            consultation=consultation,
            actor=request.user,
            event_type=(
                InteriorConsultationActivity.EVENT_INTERNAL_NOTE
            ),
            note=serializer.validated_data["note"].strip(),
        )

        return Response({
            "success": True,
            "message": "Internal note added.",
        })


# ==========================================================
# INTERIOR — FOLLOW UP
# ==========================================================

class AdminInteriorConsultationFollowUpAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdminUser]

    class InputSerializer(serializers.Serializer):
        next_follow_up_at = serializers.DateTimeField(
            required=False,
            allow_null=True,
        )
        note = serializers.CharField(
            required=False,
            allow_blank=True,
            max_length=5000,
        )

    @transaction.atomic
    def post(self, request, pk):
        serializer = self.InputSerializer(
            data=request.data
        )
        serializer.is_valid(raise_exception=True)

        consultation = get_object_or_404(
            InteriorConsultation.objects.select_for_update(),
            pk=pk,
        )

        previous = consultation.next_follow_up_at
        next_follow_up_at = serializer.validated_data.get(
            "next_follow_up_at"
        )

        consultation.next_follow_up_at = next_follow_up_at
        consultation.save(
            update_fields=[
                "next_follow_up_at",
                "updated_at",
            ]
        )

        InteriorConsultationActivity.objects.create(
            consultation=consultation,
            actor=request.user,
            event_type=(
                InteriorConsultationActivity.EVENT_FOLLOW_UP_SET
            ),
            note=serializer.validated_data.get(
                "note",
                "",
            ),
            metadata={
                "previous": (
                    previous.isoformat()
                    if previous
                    else None
                ),
                "next": (
                    next_follow_up_at.isoformat()
                    if next_follow_up_at
                    else None
                ),
            },
        )

        return Response(
            InteriorConsultationDetailSerializer(
                consultation,
                context={"request": request},
            ).data
        )


# ==========================================================
# INTERIOR — CONTACTED
# ==========================================================

class AdminInteriorConsultationContactedAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdminUser]

    class InputSerializer(serializers.Serializer):
        note = serializers.CharField(
            required=False,
            allow_blank=True,
            max_length=5000,
        )

    @transaction.atomic
    def post(self, request, pk):
        serializer = self.InputSerializer(
            data=request.data
        )
        serializer.is_valid(raise_exception=True)

        consultation = get_object_or_404(
            InteriorConsultation.objects.select_for_update(),
            pk=pk,
        )

        old_status = consultation.status
        now = timezone.now()

        consultation.contacted_at = now

        if consultation.status in {
            InteriorConsultation.STATUS_NEW,
            InteriorConsultation.STATUS_CONTACT_PENDING,
        }:
            consultation.status = (
                InteriorConsultation.STATUS_CONTACTED
            )

        consultation.save(
            update_fields=[
                "contacted_at",
                "status",
                "updated_at",
            ]
        )

        if old_status != consultation.status:
            InteriorConsultationActivity.objects.create(
                consultation=consultation,
                actor=request.user,
                event_type=(
                    InteriorConsultationActivity.EVENT_STATUS_CHANGED
                ),
                from_status=old_status,
                to_status=consultation.status,
            )

        InteriorConsultationActivity.objects.create(
            consultation=consultation,
            actor=request.user,
            event_type=(
                InteriorConsultationActivity.EVENT_CONTACTED
            ),
            note=serializer.validated_data.get(
                "note",
                "",
            ),
        )

        return Response(
            InteriorConsultationDetailSerializer(
                consultation,
                context={"request": request},
            ).data
        )


# ==========================================================
# INTERIOR — ACTIVITY
# ==========================================================

class AdminInteriorConsultationActivityAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdminUser]

    def get(self, request, pk):
        consultation = get_object_or_404(
            InteriorConsultation,
            pk=pk,
        )

        activities = (
            consultation.activities
            .select_related("actor")
            .all()
        )

        return Response(
            InteriorConsultationActivitySerializer(
                activities,
                many=True,
            ).data
        )


# ==========================================================
# QUOTE — ADMIN LIST
# ==========================================================

class AdminQuoteRequestListAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdminUser]

    def get(self, request):
        queryset = (
            QuoteRequest.objects
            .select_related(
                "assigned_to",
                "source_product",
                "source_variant",
            )
            .prefetch_related(
                "source_variant__variant_options__option_value__option",
            )
            .all()
        )

        q = (request.query_params.get("q") or "").strip()

        if q:
            queryset = queryset.annotate(
                quote_id_text=Cast(
                    "quote_id",
                    output_field=CharField(),
                )
            ).filter(
                Q(full_name__icontains=q)
                | Q(phone__icontains=q)
                | Q(email__icontains=q)
                | Q(company__icontains=q)
                | Q(project_location__icontains=q)
                | Q(project_type__icontains=q)
                | Q(quote_id_text__icontains=q)
            )

        status_value = request.query_params.get(
            "status"
        )

        if status_value:
            queryset = queryset.filter(
                status=status_value
            )

        project_type = request.query_params.get(
            "project_type"
        )

        if project_type:
            queryset = queryset.filter(
                project_type__iexact=project_type
            )

        source = request.query_params.get(
            "source"
        )

        if source:
            queryset = queryset.filter(
                source=source
            )

        assigned_to = request.query_params.get(
            "assigned_to"
        )

        if assigned_to == "unassigned":
            queryset = queryset.filter(
                assigned_to__isnull=True
            )
        elif assigned_to:
            queryset = queryset.filter(
                assigned_to_id=_parse_staff_id(assigned_to)
            )

        if request.query_params.get(
            "follow_up"
        ) == "overdue":
            queryset = queryset.filter(
                next_follow_up_at__lt=timezone.now()
            ).exclude(
                status="rejected"
            )

        queryset = _apply_created_range(
            queryset,
            request,
        )

        ordering = _validate_ordering(
            request.query_params.get("ordering"),
            {
                "created_at",
                "updated_at",
                "full_name",
                "status",
                "next_follow_up_at",
                "quoted_at",
                "approved_at",
            },
            "-created_at",
        )

        queryset = queryset.order_by(*ordering)

        paginator = CRMPageNumberPagination()
        page = paginator.paginate_queryset(
            queryset,
            request,
        )

        serializer = QuoteRequestListSerializer(
            page,
            many=True,
            context={"request": request},
        )

        return paginator.get_paginated_response(
            serializer.data
        )


# ==========================================================
# QUOTE — DETAIL
# ==========================================================

class AdminQuoteRequestDetailAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdminUser]

    def get(self, request, pk):
        quote = get_object_or_404(
            QuoteRequest.objects
            .select_related(
                "assigned_to",
                "source_product",
                "source_variant",
            )
            .prefetch_related(
                "attachments",
                "source_variant__variant_options__option_value__option",
            ),
            pk=pk,
        )

        serializer = QuoteRequestDetailSerializer(
            quote,
            context={"request": request},
        )

        return Response(serializer.data)


# ==========================================================
# QUOTE — OVERVIEW
# ==========================================================

class AdminQuoteRequestOverviewAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdminUser]

    def get(self, request):
        queryset = QuoteRequest.objects.all()
        now = timezone.now()

        data = queryset.aggregate(
            total=Count("id"),
            pending=Count(
                "id",
                filter=Q(status="pending"),
            ),
            reviewing=Count(
                "id",
                filter=Q(status="reviewing"),
            ),
            quoted=Count(
                "id",
                filter=Q(status="quoted"),
            ),
            approved=Count(
                "id",
                filter=Q(status="approved"),
            ),
            rejected=Count(
                "id",
                filter=Q(status="rejected"),
            ),
            overdue_followups=Count(
                "id",
                filter=(
                    Q(next_follow_up_at__lt=now)
                    & ~Q(status="rejected")
                ),
            ),
        )

        data["generated_at"] = now

        return Response(data)


# ==========================================================
# QUOTE — STATUS
# ==========================================================

class AdminQuoteRequestStatusAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdminUser]

    class InputSerializer(serializers.Serializer):
        status = serializers.ChoiceField(
            choices=QuoteRequest.STATUS_CHOICES
        )
        note = serializers.CharField(
            required=False,
            allow_blank=True,
            max_length=5000,
        )

    @transaction.atomic
    def post(self, request, pk):
        serializer = self.InputSerializer(
            data=request.data
        )
        serializer.is_valid(raise_exception=True)

        quote = get_object_or_404(
            QuoteRequest.objects.select_for_update(),
            pk=pk,
        )

        old_status = quote.status
        new_status = serializer.validated_data["status"]
        note = serializer.validated_data.get("note", "")

        if old_status == new_status:
            return Response(
                QuoteRequestDetailSerializer(
                    quote,
                    context={"request": request},
                ).data
            )

        quote.status = new_status
        _set_quote_timestamp_for_status(quote)
        quote.save()

        QuoteRequestActivity.objects.create(
            quote=quote,
            actor=request.user,
            event_type=QuoteRequestActivity.EVENT_STATUS_CHANGED,
            from_status=old_status,
            to_status=new_status,
            note=note,
        )

        if new_status == "quoted":
            QuoteRequestActivity.objects.create(
                quote=quote,
                actor=request.user,
                event_type=QuoteRequestActivity.EVENT_QUOTE_SENT,
                note=note,
            )

        if new_status == "approved":
            QuoteRequestActivity.objects.create(
                quote=quote,
                actor=request.user,
                event_type=QuoteRequestActivity.EVENT_APPROVED,
                note=note,
            )

        if new_status == "rejected":
            QuoteRequestActivity.objects.create(
                quote=quote,
                actor=request.user,
                event_type=QuoteRequestActivity.EVENT_REJECTED,
                note=note,
            )

        return Response(
            QuoteRequestDetailSerializer(
                quote,
                context={"request": request},
            ).data
        )


# ==========================================================
# QUOTE — ASSIGN
# ==========================================================

class AdminQuoteRequestAssignAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdminUser]

    class InputSerializer(serializers.Serializer):
        assigned_to = serializers.PrimaryKeyRelatedField(
            queryset=User.objects.filter(
                is_active=True,
                is_staff=True,
            ),
            allow_null=True,
            required=True,
        )
        note = serializers.CharField(
            required=False,
            allow_blank=True,
            max_length=5000,
        )

    @transaction.atomic
    def post(self, request, pk):
        serializer = self.InputSerializer(
            data=request.data
        )
        serializer.is_valid(raise_exception=True)

        quote = get_object_or_404(
            QuoteRequest.objects.select_for_update(),
            pk=pk,
        )

        new_assignee = serializer.validated_data["assigned_to"]
        old_assignee = quote.assigned_to

        if (
            (old_assignee.id if old_assignee else None)
            == (new_assignee.id if new_assignee else None)
        ):
            return Response(
                QuoteRequestDetailSerializer(
                    quote,
                    context={"request": request},
                ).data
            )

        quote.assigned_to = new_assignee
        quote.save(
            update_fields=[
                "assigned_to",
                "updated_at",
            ]
        )

        QuoteRequestActivity.objects.create(
            quote=quote,
            actor=request.user,
            event_type=QuoteRequestActivity.EVENT_ASSIGNED,
            note=serializer.validated_data.get(
                "note",
                "",
            ),
            metadata={
                "from_assigned_to": (
                    old_assignee.id
                    if old_assignee
                    else None
                ),
                "to_assigned_to": (
                    new_assignee.id
                    if new_assignee
                    else None
                ),
            },
        )

        return Response(
            QuoteRequestDetailSerializer(
                quote,
                context={"request": request},
            ).data
        )


# ==========================================================
# QUOTE — NOTE
# ==========================================================

class AdminQuoteRequestNoteAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdminUser]

    class InputSerializer(serializers.Serializer):
        note = serializers.CharField(
            min_length=1,
            max_length=10000,
        )

    def post(self, request, pk):
        serializer = self.InputSerializer(
            data=request.data
        )
        serializer.is_valid(raise_exception=True)

        quote = get_object_or_404(
            QuoteRequest,
            pk=pk,
        )

        QuoteRequestActivity.objects.create(
            quote=quote,
            actor=request.user,
            event_type=(
                QuoteRequestActivity.EVENT_INTERNAL_NOTE
            ),
            note=serializer.validated_data["note"].strip(),
        )

        return Response({
            "success": True,
            "message": "Internal note added.",
        })


# ==========================================================
# QUOTE — FOLLOW UP
# ==========================================================

class AdminQuoteRequestFollowUpAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdminUser]

    class InputSerializer(serializers.Serializer):
        next_follow_up_at = serializers.DateTimeField(
            required=False,
            allow_null=True,
        )
        note = serializers.CharField(
            required=False,
            allow_blank=True,
            max_length=5000,
        )

    @transaction.atomic
    def post(self, request, pk):
        serializer = self.InputSerializer(
            data=request.data
        )
        serializer.is_valid(raise_exception=True)

        quote = get_object_or_404(
            QuoteRequest.objects.select_for_update(),
            pk=pk,
        )

        previous = quote.next_follow_up_at
        next_follow_up_at = serializer.validated_data.get(
            "next_follow_up_at"
        )

        quote.next_follow_up_at = next_follow_up_at

        quote.save(
            update_fields=[
                "next_follow_up_at",
                "updated_at",
            ]
        )

        QuoteRequestActivity.objects.create(
            quote=quote,
            actor=request.user,
            event_type=(
                QuoteRequestActivity.EVENT_FOLLOW_UP_SET
            ),
            note=serializer.validated_data.get(
                "note",
                "",
            ),
            metadata={
                "previous": (
                    previous.isoformat()
                    if previous
                    else None
                ),
                "next": (
                    next_follow_up_at.isoformat()
                    if next_follow_up_at
                    else None
                ),
            },
        )

        return Response(
            QuoteRequestDetailSerializer(
                quote,
                context={"request": request},
            ).data
        )


# ==========================================================
# QUOTE — CONTACTED
# ==========================================================

class AdminQuoteRequestContactedAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdminUser]

    class InputSerializer(serializers.Serializer):
        note = serializers.CharField(
            required=False,
            allow_blank=True,
            max_length=5000,
        )

    @transaction.atomic
    def post(self, request, pk):
        serializer = self.InputSerializer(
            data=request.data
        )
        serializer.is_valid(raise_exception=True)

        quote = get_object_or_404(
            QuoteRequest.objects.select_for_update(),
            pk=pk,
        )

        old_status = quote.status
        quote.contacted_at = timezone.now()

        if quote.status == "pending":
            quote.status = "reviewing"

        quote.save(
            update_fields=[
                "contacted_at",
                "status",
                "updated_at",
            ]
        )

        if old_status != quote.status:
            QuoteRequestActivity.objects.create(
                quote=quote,
                actor=request.user,
                event_type=(
                    QuoteRequestActivity.EVENT_STATUS_CHANGED
                ),
                from_status=old_status,
                to_status=quote.status,
            )

        QuoteRequestActivity.objects.create(
            quote=quote,
            actor=request.user,
            event_type=(
                QuoteRequestActivity.EVENT_CONTACTED
            ),
            note=serializer.validated_data.get(
                "note",
                "",
            ),
        )

        return Response(
            QuoteRequestDetailSerializer(
                quote,
                context={"request": request},
            ).data
        )


# ==========================================================
# QUOTE — ACTIVITY
# ==========================================================

class AdminQuoteRequestActivityAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdminUser]

    def get(self, request, pk):
        quote = get_object_or_404(
            QuoteRequest,
            pk=pk,
        )

        activities = (
            quote.activities
            .select_related("actor")
            .all()
        )

        return Response(
            QuoteRequestActivitySerializer(
                activities,
                many=True,
            ).data
        )