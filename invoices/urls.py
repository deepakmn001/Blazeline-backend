from django.urls import path

from .views import (
    InvoiceAdminDetailAPIView,
    InvoiceAdminListAPIView,
    InvoiceAdminOverviewAPIView,
)


urlpatterns = [
    path(
        "",
        InvoiceAdminListAPIView.as_view(),
        name="admin-invoice-list",
    ),
    path(
        "overview/",
        InvoiceAdminOverviewAPIView.as_view(),
        name="admin-invoice-overview",
    ),
    path(
        "<uuid:pk>/",
        InvoiceAdminDetailAPIView.as_view(),
        name="admin-invoice-detail",
    ),
]