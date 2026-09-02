from django.contrib import admin
from django.urls import include, path

from django.conf import settings
from django.conf.urls.static import static

from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularSwaggerView,
)

from catalog.auth_views import AdminLoginView, AdminTokenRefreshView
from orders.views import RazorpayWebhookAPIView

urlpatterns = [
     #path("silk/", include("silk.urls")),

    # Django Admin
    path(
        "admin/",
        admin.site.urls,
    ),

    # BlazeLine APIs
    path(
        "api/",
        include("catalog.urls"),
    ),
    
    # Admin Auth (JWT)
        path(
        "api/auth/admin/login/",
        AdminLoginView.as_view(),
        name="admin-login",
    ),
        path(
        "api/auth/admin/refresh/",
        AdminTokenRefreshView.as_view(),
        name="admin-refresh",
    ),

 #Catalog Import APIs
#path(
 #   "api/",
  #  include("catalog_import.urls"),
#),
    # OpenAPI Schema
    path(
        "api/schema/",
        SpectacularAPIView.as_view(),
        name="schema",
    ),
    path("api/auth/", include("accounts.api")),
path("api/cart/", include("cart.api")),
path(
    "api/orders/",
    include("orders.urls"),
),
path(
    "api/payments/webhook/",
    RazorpayWebhookAPIView.as_view(),
    name="razorpay-webhook",
),
    # Swagger UI
    path(
        "api/docs/",
        SpectacularSwaggerView.as_view(
            url_name="schema"
        ),
        name="swagger-ui",
    ),
]


if settings.DEBUG:

    urlpatterns += static(
        settings.MEDIA_URL,
        document_root=settings.MEDIA_ROOT,
    )