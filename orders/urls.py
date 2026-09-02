from django.urls import path

from .views import (
    CustomerOrderDetailAPIView,
    CustomerOrderListAPIView,
    OrderCreateAPIView,
    RazorpayPaymentCreateAPIView,
    RazorpayPaymentVerifyAPIView,
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