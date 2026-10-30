# apps/gallery/urls.py
from django.urls import path
from rest_framework.routers import DefaultRouter

from . import views
from .homepage import HomepagePublicView, HomepageAdminView

app_name = "gallery"

urlpatterns = [
    path("", views.GalleryPublicListView.as_view(), name="list"),
    path("homepage/", HomepagePublicView.as_view(), name="homepage"),
]

_router = DefaultRouter()
_router.register("", views.GalleryAdminViewSet, basename="admin-gallery")
admin_urlpatterns = [path("homepage/<str:section>/", HomepageAdminView.as_view(), name="homepage-admin")] + _router.urls
