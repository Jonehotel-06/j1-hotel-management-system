# config/urls.py
"""Root URL configuration.

Routing plan
------------
/api/                  JSON envelope index
/api/health/           backwards-compatible readiness probe
/api/health/live/      process liveness probe (no dependency checks)
/api/health/ready/     dependency readiness probe
/api/docs/             Swagger UI (OpenAPI 3 via drf-spectacular)
/api/schema/           raw OpenAPI schema
/api/redoc/            ReDoc

/api/auth/             register / login / tokens / profile / passwords
/api/hotel/            public hotel information + policies
/api/facilities/       public facilities
/api/gallery/          public photo gallery
/api/offers/           public offers
/api/rooms/            public room-type catalog + detail
/api/rooms/availability/  authoritative availability search

/api/bookings/         guest booking flow (quote / list / create / detail / cancel / receipt)
/api/payments/         payment init / verification / Paystack webhook
/api/enquiries/        public enquiry submission
/api/notifications/    authenticated notification center

/api/admin/            staff-only hotel operations namespace
/django-admin/         Django's built-in admin (internal fallback only)

Note on versioning: the API is currently exposed unversioned at /api/ with a
single consistent contract; a future incompatible contract will be introduced
as /api/v2/ alongside this one (see docs/FRONTEND_CONTRACT.md).
"""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView, SpectacularSwaggerView

from apps.bookings import urls as booking_urls
from apps.bookings import urls_admin as booking_admin_urls
from apps.core import views as core_views
from apps.enquiries import urls as enquiry_urls
from apps.finance import urls as finance_urls
from apps.guest_services import urls as guest_services_urls
from apps.gallery import urls as gallery_urls
from apps.hotel import urls as hotel_urls
from apps.housekeeping import urls as housekeeping_urls
from apps.maintenance import urls as maintenance_urls
from apps.inventory import urls as inventory_urls
from apps.staff_operations import urls as staff_operations_urls
from apps.notifications import urls as notification_urls
from apps.offers import urls as offer_urls
from apps.payments import urls_admin as payment_admin_urls
from apps.portal import urls as portal_urls
from apps.pos import urls as pos_urls
from apps.reports import urls as report_urls
from apps.rooms import urls as room_urls

api_patterns = [
    path("", core_views.api_index, name="api-index"),
    # ``health/`` remains the established readiness endpoint. New deploy
    # systems can use live vs. ready to avoid restarting a healthy process
    # solely because MySQL is briefly unavailable.
    path("health/", core_views.health_check, name="health"),
    path("health/live/", core_views.health_liveness, name="health-live"),
    path("health/ready/", core_views.health_readiness, name="health-ready"),
    path("schema/", SpectacularAPIView.as_view(), name="schema"),
    path("docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),

    path("auth/", include("apps.accounts.urls", namespace="accounts")),

    # --- Public site content -------------------------------------------------
    path("hotel/", include((hotel_urls.public_urlpatterns, "hotel"), namespace="hotel")),
    path("facilities/", include((hotel_urls.facilities_public_urlpatterns, "hotel"), namespace="facilities")),
    path("gallery/", include("apps.gallery.urls", namespace="gallery")),
    path("offers/", include("apps.offers.urls", namespace="offers")),
    # Availability MUST be registered before the room-type slug route, or the
    # url "availability" would be captured by <slug:slug>/.
    path("rooms/", include((booking_urls.availability_urlpatterns, "bookings"), namespace="rooms-availability")),
    path("rooms/", include("apps.rooms.urls", namespace="rooms")),

    # --- Authenticated guest flows -------------------------------------------
    path("bookings/", include("apps.bookings.urls", namespace="bookings")),
    path("payments/", include("apps.payments.urls", namespace="payments")),
    path("enquiries/", include((enquiry_urls.urlpatterns, "enquiries"), namespace="enquiries")),
    path("notifications/", include("apps.notifications.urls", namespace="notifications")),
    # Verified-email guest portal session + intentionally scoped read model.
    # It never upgrades a legacy booking token into a general identity session.
    path("portal/", include("apps.portal.urls", namespace="portal")),
    path("portal/", include("apps.finance.portal_urls", namespace="portal-finance")),
    path("portal/requests/", include((guest_services_urls.portal_urlpatterns, "guest_services"), namespace="portal-service-requests")),
    # Guest review flow: verification-gated submit only — reviews are NEVER
    # listed publicly; management lives under /api/admin/reviews/ (admin-only).
    path("reviews/", include("apps.reviews.urls", namespace="reviews")),

    # --- Staff hotel operations ----------------------------------------------
    path("admin/dashboard/", include((report_urls.dashboard_urlpatterns, "reports"), namespace="dashboard")),
    path("admin/reports/", include("apps.reports.urls", namespace="reports")),
    path("admin/bookings/", include((booking_admin_urls.bookings_urlpatterns, "bookings_admin"), namespace="admin-bookings")),
    path("admin/guests/", include((booking_admin_urls.guests_urlpatterns, "bookings_admin"), namespace="admin-guests")),
    path("admin/payments/", include("apps.payments.urls_admin", namespace="admin-payments")),
    path("admin/pos/", include("apps.pos.urls", namespace="admin-pos")),
    path("admin/finance/", include("apps.finance.urls", namespace="admin-finance")),
    path("admin/service-requests/", include((guest_services_urls.staff_urlpatterns, "guest_services"), namespace="admin-service-requests")),
    path("admin/housekeeping/", include("apps.housekeeping.urls", namespace="admin-housekeeping")),
    path("admin/maintenance/", include("apps.maintenance.urls", namespace="admin-maintenance")),
    path("admin/inventory/", include("apps.inventory.urls", namespace="admin-inventory")),
    path("admin/staff-operations/", include("apps.staff_operations.urls", namespace="admin-staff-operations")),
    path("admin/rooms/", include((room_urls.admin_rooms_urlpatterns, "rooms"), namespace="admin-rooms")),
    path("admin/room-types/", include((room_urls.admin_room_types_urlpatterns, "rooms"), namespace="admin-room-types")),
    path("admin/room-images/", include((room_urls.admin_room_images_urlpatterns, "rooms"), namespace="admin-room-images")),
    path("admin/amenities/", include((room_urls.admin_amenities_urlpatterns, "rooms"), namespace="admin-amenities")),
    path("admin/facilities/", include((hotel_urls.admin_facilities_urlpatterns, "hotel"), namespace="admin-facilities")),
    path("admin/policies/", include((hotel_urls.admin_policies_urlpatterns, "hotel"), namespace="admin-policies")),
    path("admin/settings/", include((hotel_urls.admin_settings_urlpatterns, "hotel"), namespace="admin-settings")),
    path("admin/offers/", include((offer_urls.admin_urlpatterns, "offers"), namespace="admin-offers")),
    path("admin/guest-discounts/", include((offer_urls.admin_guest_discount_urlpatterns, "offers"), namespace="admin-guest-discounts")),
    path("admin/gallery/", include((gallery_urls.admin_urlpatterns, "gallery"), namespace="admin-gallery")),
    path("admin/enquiries/", include((enquiry_urls.admin_urlpatterns, "enquiries"), namespace="admin-enquiries")),
    path("admin/users/", include("apps.accounts.urls_admin", namespace="admin-users")),
    path("admin/audit-logs/", include("apps.audit.urls_admin", namespace="admin-audit")),
    path("admin/reviews/", include("apps.reviews.urls_admin", namespace="admin-reviews")),
]

urlpatterns = [
    path("django-admin/", admin.site.urls),
    path("api/", include(api_patterns)),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
