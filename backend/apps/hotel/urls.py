# apps/hotel/urls.py
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views, views_admin

app_name = "hotel"

public_urlpatterns = [
    path("", views.HotelInfoView.as_view(), name="info"),
    path("policies/", views.PolicyListView.as_view(), name="policies"),
]

facilities_public_urlpatterns = [
    path("", views.FacilityListView.as_view(), name="facility-list"),
]

_facility_router = DefaultRouter()
_facility_router.register("", views_admin.FacilityAdminViewSet, basename="admin-facility")

_policy_router = DefaultRouter()
_policy_router.register("", views_admin.PolicyAdminViewSet, basename="admin-policy")

admin_facilities_urlpatterns = _facility_router.urls
admin_policies_urlpatterns = _policy_router.urls

admin_settings_urlpatterns = [
    path("", views_admin.HotelSettingsAdminView.as_view(), name="settings"),
]
