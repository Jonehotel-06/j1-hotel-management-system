from django.urls import path

from . import views

app_name = "portal"

urlpatterns = [
    path("auth/request/", views.PortalAccessRequestView.as_view(), name="auth-request"),
    path("auth/consume/", views.PortalAccessConsumeView.as_view(), name="auth-consume"),
    path("auth/logout/", views.PortalLogoutView.as_view(), name="auth-logout"),
    path("me/", views.PortalOverviewView.as_view(), name="overview"),
]
