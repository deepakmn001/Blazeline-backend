from rest_framework.routers import DefaultRouter

from .views import PromotionRuleViewSet, PromotionViewSet


router = DefaultRouter()

router.register(
    "promotions",
    PromotionViewSet,
    basename="promotion",
)

router.register(
    "promotion-rules",
    PromotionRuleViewSet,
    basename="promotion-rule",
)

urlpatterns = router.urls
