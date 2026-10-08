# apps/gallery/urls.py
from django.urls import path
from rest_framework.routers import DefaultRouter

from . import views

app_name = "gallery"

urlpatterns = [
    path("", views.GalleryPublicListView.as_view(), name="list"),
]

_router = DefaultRouter()
_router.register("", views.GalleryAdminViewSet, basename="admin-gallery")
admin_urlpatterns = _router.urls
