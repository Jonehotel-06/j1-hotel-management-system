# apps/offers/urls.py
from django.urls import path
from rest_framework.routers import DefaultRouter

from . import views

app_name = "offers"

urlpatterns = [
    path("", views.OfferPublicListView.as_view(), name="offer-list"),
]

_router = DefaultRouter()
_router.register("", views.OfferAdminViewSet, basename="admin-offer")
admin_urlpatterns = _router.urls

# Individual guest discounts live on their own admin route so the offers
# contract is untouched.
_discount_router = DefaultRouter()
_discount_router.register("", views.GuestDiscountAdminViewSet, basename="admin-guest-discount")
admin_guest_discount_urlpatterns = _discount_router.urls
