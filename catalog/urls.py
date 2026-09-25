from django.urls import path
from rest_framework.routers import DefaultRouter
from .crm_views import (
    InteriorConsultationAPIView,

    CRMStaffListAPIView,

    AdminInteriorConsultationListAPIView,
    AdminInteriorConsultationDetailAPIView,
    AdminInteriorConsultationOverviewAPIView,
    AdminInteriorConsultationStatusAPIView,
    AdminInteriorConsultationAssignAPIView,
    AdminInteriorConsultationScheduleAPIView,
    AdminInteriorConsultationNoteAPIView,
    AdminInteriorConsultationFollowUpAPIView,
    AdminInteriorConsultationContactedAPIView,
    AdminInteriorConsultationActivityAPIView,

    AdminQuoteRequestListAPIView,
    AdminQuoteRequestDetailAPIView,
    AdminQuoteRequestOverviewAPIView,
    AdminQuoteRequestStatusAPIView,
    AdminQuoteRequestAssignAPIView,
    AdminQuoteRequestNoteAPIView,
    AdminQuoteRequestFollowUpAPIView,
    AdminQuoteRequestContactedAPIView,
    AdminQuoteRequestActivityAPIView,
)
from .crm_message_views import (
    AdminInteriorConsultationMessageAPIView,
    AdminQuoteRequestMessageAPIView,
)
from .whatsapp_webhook import msg91_whatsapp_inbound_webhook
from .views import (
    CategoryViewSet,
     HomepageCategoryViewSet,
    SubCategoryViewSet,
    ProductViewSet,
    ProductImageViewSet,
    ProductVariantViewSet,
    ProductSpecificationViewSet,
    DashboardAPIView,
    SubCategoryStatsAPIView,
     AdminSubCategoryViewSet,
    DeliveryCheckAPIView,
     DeliveryQuoteAPIView,
     QuoteRequestAPIView,
     ReverseLocationAPIView,
     AdminCategoryViewSet,
         DeliveryZoneViewSet,
    ServiceablePincodeViewSet,
    DeliveryRuleViewSet,
    DeliveryOverviewAPIView,
)

router = DefaultRouter()

# ==========================================================
# CATEGORY
# ==========================================================

router.register(
    "categories",
    CategoryViewSet,
)
router.register(
    "admin/categories",
    AdminCategoryViewSet,
    basename="admin-categories",
)
router.register(
    "homepage/categories",
    HomepageCategoryViewSet,
    basename="homepage-categories",
)
# ==========================================================
# SUB CATEGORY
# ==========================================================

router.register(
    "subcategories",
    SubCategoryViewSet,
)
router.register(
    "admin/subcategories",
    AdminSubCategoryViewSet,
    basename="admin-subcategories",
)

# ==========================================================
# PRODUCT
# ==========================================================

router.register(
    "products",
    ProductViewSet,
)

# ==========================================================
# PRODUCT IMAGES
# ==========================================================

router.register(
    "product-images",
    ProductImageViewSet,
)

# ==========================================================
# PRODUCT VARIANTS
# ==========================================================

router.register(
    "product-variants",
    ProductVariantViewSet,
)

# ==========================================================
# PRODUCT SPECIFICATIONS
# ==========================================================

router.register(
    "product-specifications",
    ProductSpecificationViewSet,
)

# ==========================================================
# DELIVERY (ADMIN)
# ==========================================================

router.register("admin/delivery/zones", DeliveryZoneViewSet, basename="admin-delivery-zones")
router.register("admin/delivery/pincodes", ServiceablePincodeViewSet, basename="admin-delivery-pincodes")
router.register("admin/delivery/rules", DeliveryRuleViewSet, basename="admin-delivery-rules")
# ==========================================================
# DASHBOARD API
# ==========================================================

urlpatterns = [
    path(
    "whatsapp/webhook/msg91/",
    msg91_whatsapp_inbound_webhook,
    name="msg91-whatsapp-inbound-webhook",
),
    path(
        "dashboard/",
        DashboardAPIView.as_view(),
        name="dashboard",
    ),
    path(
    "subcategories/stats/",
    SubCategoryStatsAPIView.as_view(),
    name="subcategory-stats",
),
    path(
    "delivery/check/",
    DeliveryCheckAPIView.as_view(),
    name="delivery-check",
),

path(
    "delivery/quote/",
    DeliveryQuoteAPIView.as_view(),
    name="delivery-quote",
),
path(
    "quote-requests/",
    QuoteRequestAPIView.as_view(),
    name="quote-requests",
),
path(
    "location/reverse/",
    ReverseLocationAPIView.as_view(),
    name="reverse-location",
),
path(
    "admin/delivery/overview/",
    DeliveryOverviewAPIView.as_view(),
    name="admin-delivery-overview",
),
# ==========================================================
# INTERIOR CONSULTATION — PUBLIC
# ==========================================================

path(
    "interior-consultations/",
    InteriorConsultationAPIView.as_view(),
    name="interior-consultations",
),

# ==========================================================
# CRM — STAFF
# ==========================================================

path(
    "admin/crm/staff/",
    CRMStaffListAPIView.as_view(),
    name="crm-staff",
),

# ==========================================================
# CRM — INTERIOR CONSULTATIONS
# ==========================================================

path(
    "admin/interior-consultations/",
    AdminInteriorConsultationListAPIView.as_view(),
    name="admin-interior-consultations",
),

path(
    "admin/interior-consultations/overview/",
    AdminInteriorConsultationOverviewAPIView.as_view(),
    name="admin-interior-consultations-overview",
),

path(
    "admin/interior-consultations/<int:pk>/",
    AdminInteriorConsultationDetailAPIView.as_view(),
    name="admin-interior-consultation-detail",
),

path(
    "admin/interior-consultations/<int:pk>/status/",
    AdminInteriorConsultationStatusAPIView.as_view(),
    name="admin-interior-consultation-status",
),

path(
    "admin/interior-consultations/<int:pk>/assign/",
    AdminInteriorConsultationAssignAPIView.as_view(),
    name="admin-interior-consultation-assign",
),

path(
    "admin/interior-consultations/<int:pk>/schedule/",
    AdminInteriorConsultationScheduleAPIView.as_view(),
    name="admin-interior-consultation-schedule",
),

path(
    "admin/interior-consultations/<int:pk>/note/",
    AdminInteriorConsultationNoteAPIView.as_view(),
    name="admin-interior-consultation-note",
),

path(
    "admin/interior-consultations/<int:pk>/follow-up/",
    AdminInteriorConsultationFollowUpAPIView.as_view(),
    name="admin-interior-consultation-follow-up",
),

path(
    "admin/interior-consultations/<int:pk>/contacted/",
    AdminInteriorConsultationContactedAPIView.as_view(),
    name="admin-interior-consultation-contacted",
),

path(
    "admin/interior-consultations/<int:pk>/activity/",
    AdminInteriorConsultationActivityAPIView.as_view(),
    name="admin-interior-consultation-activity",
),
path(
    "admin/interior-consultations/<int:pk>/message/",
    AdminInteriorConsultationMessageAPIView.as_view(),
    name="admin-interior-consultation-message",
),

# ==========================================================
# CRM — QUOTE REQUESTS
# ==========================================================

path(
    "admin/quote-requests/",
    AdminQuoteRequestListAPIView.as_view(),
    name="admin-quote-requests",
),

path(
    "admin/quote-requests/overview/",
    AdminQuoteRequestOverviewAPIView.as_view(),
    name="admin-quote-requests-overview",
),

path(
    "admin/quote-requests/<int:pk>/",
    AdminQuoteRequestDetailAPIView.as_view(),
    name="admin-quote-request-detail",
),

path(
    "admin/quote-requests/<int:pk>/status/",
    AdminQuoteRequestStatusAPIView.as_view(),
    name="admin-quote-request-status",
),

path(
    "admin/quote-requests/<int:pk>/assign/",
    AdminQuoteRequestAssignAPIView.as_view(),
    name="admin-quote-request-assign",
),

path(
    "admin/quote-requests/<int:pk>/note/",
    AdminQuoteRequestNoteAPIView.as_view(),
    name="admin-quote-request-note",
),

path(
    "admin/quote-requests/<int:pk>/follow-up/",
    AdminQuoteRequestFollowUpAPIView.as_view(),
    name="admin-quote-request-follow-up",
),

path(
    "admin/quote-requests/<int:pk>/contacted/",
    AdminQuoteRequestContactedAPIView.as_view(),
    name="admin-quote-request-contacted",
),

path(
    "admin/quote-requests/<int:pk>/activity/",
    AdminQuoteRequestActivityAPIView.as_view(),
    name="admin-quote-request-activity",
),
path(
    "admin/quote-requests/<int:pk>/message/",
    AdminQuoteRequestMessageAPIView.as_view(),
    name="admin-quote-request-message",
),
]

# ==========================================================
# DRF ROUTER URLS
# ==========================================================

urlpatterns += router.urls
