from django.urls import path

from .views import (
    CustomerOrderDetailAPIView,
    CustomerOrderListAPIView,
    OrderCreateAPIView,
    RazorpayPaymentCreateAPIView,
    RazorpayPaymentVerifyAPIView,
)
from .admin_views import (
    AdminDirectOrderCreateAPIView,
    AdminDirectOrderDetailAPIView,
    AdminDirectOrderListAPIView,
    AdminDirectOrderPaymentLinkAPIView,
)


urlpatterns = [
    path(
        "",
        CustomerOrderListAPIView.as_view(),
        name="order-list",
    ),
    path(
        "create/",
        OrderCreateAPIView.as_view(),
        name="order-create",
    ),
        # ======================================================
    # ADMIN — DIRECT ORDERS
    # ======================================================

    path(
        "direct-orders/",
        AdminDirectOrderListAPIView.as_view(),
        name="admin-direct-order-list",
    ),
    path(
        "direct-orders/create/",
        AdminDirectOrderCreateAPIView.as_view(),
        name="admin-direct-order-create",
    ),
    path(
        "direct-orders/<str:order_number>/",
        AdminDirectOrderDetailAPIView.as_view(),
        name="admin-direct-order-detail",
    ),
    path(
        "direct-orders/<str:order_number>/payment-link/",
        AdminDirectOrderPaymentLinkAPIView.as_view(),
        name="admin-direct-order-payment-link",
    ),
    path(
        "<str:order_number>/",
        CustomerOrderDetailAPIView.as_view(),
        name="order-detail",
    ),
    path(
        "<str:order_number>/payment/create/",
        RazorpayPaymentCreateAPIView.as_view(),
        name="payment-create",
    ),
    path(
        "<str:order_number>/payment/verify/",
        RazorpayPaymentVerifyAPIView.as_view(),
        name="payment-verify",
    ),
]