from django.urls import path

from .views import (
    AnalyticsEventAPIView,
    AnalyticsActivityAPIView,
    ProductAnalyticsAPIView,
)


urlpatterns = [
    path(
        "events/",
        AnalyticsEventAPIView.as_view(),
        name="analytics-events",
    ),
    path(
        "activity/",
        AnalyticsActivityAPIView.as_view(),
        name="analytics-activity",
    ),
    path(
        "products/",
        ProductAnalyticsAPIView.as_view(),
        name="analytics-products",
    ),
]