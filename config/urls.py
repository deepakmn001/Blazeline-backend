from django.contrib import admin
from django.urls import include, path

from django.conf import settings
from django.conf.urls.static import static

from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularSwaggerView,
)
from django.urls import include, path
from catalog.auth_views import AdminLoginView, AdminTokenRefreshView

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
        "api/auth/login/",
        AdminLoginView.as_view(),
        name="admin-login",
    ),
    path(
        "api/auth/refresh/",
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