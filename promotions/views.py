from django.db.models import Prefetch
from rest_framework import permissions, viewsets

from .models import Promotion, PromotionRule
from .serializers import PromotionRuleSerializer, PromotionSerializer


class PromotionViewSet(viewsets.ModelViewSet):
    """
    Admin-only CRUD API for promotion definitions.

    Customer-facing pricing is NOT exposed through this endpoint.
    Actual promotion calculation remains owned by PromotionEngine.
    """

    permission_classes = (
        permissions.IsAdminUser,
    )

    serializer_class = PromotionSerializer

    queryset = (
        Promotion.objects
        .all()
        .prefetch_related(
            Prefetch(
                "rules",
                queryset=PromotionRule.objects.select_related(
                    "category",
                    "subcategory",
                    "product",
                    "variant",
                ).order_by("id"),
            )
        )
    )


class PromotionRuleViewSet(viewsets.ModelViewSet):
    """
    Admin-only CRUD API for promotion targeting rules.
    """

    permission_classes = (
        permissions.IsAdminUser,
    )

    serializer_class = PromotionRuleSerializer

    queryset = (
        PromotionRule.objects
        .select_related(
            "promotion",
            "category",
            "subcategory",
            "product",
            "variant",
        )
        .all()
        .order_by(
            "promotion_id",
            "target_type",
            "id",
        )
    )