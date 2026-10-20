from django.urls import path

from . import views, views_catalog

app_name = "portal"

urlpatterns = [
    path("auth/request/", views.PortalAccessRequestView.as_view(), name="auth-request"),
    path("auth/consume/", views.PortalAccessConsumeView.as_view(), name="auth-consume"),
    path("auth/logout/", views.PortalLogoutView.as_view(), name="auth-logout"),
    path("me/", views.PortalOverviewView.as_view(), name="overview"),
    path("catalog/menu/", views_catalog.PortalMenuView.as_view(), name="catalog-menu"),
    path("catalog/services/", views_catalog.PortalServicesView.as_view(), name="catalog-services"),
]
