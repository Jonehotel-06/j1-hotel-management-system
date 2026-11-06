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
# Literal action routes MUST come before the router's "<pk>/" detail route,
# otherwise "12/activate/" is captured by the detail pattern.
admin_urlpatterns = [
    path("<int:pk>/activate/", views.OfferActivateView.as_view(), name="admin-offer-activate"),
    path("<int:pk>/deactivate/", views.OfferDeactivateView.as_view(), name="admin-offer-deactivate"),
] + _router.urls

# Guest-specific offer restrictions live on their own admin route so the
# offers and guest-discounts contracts stay untouched.
_restriction_router = DefaultRouter()
_restriction_router.register("", views.OfferGuestRestrictionAdminViewSet,
                             basename="admin-offer-restriction")
admin_offer_restriction_urlpatterns = _restriction_router.urls

# Individual guest discounts live on their own admin route so the offers
# contract is untouched.
_discount_router = DefaultRouter()
_discount_router.register("", views.GuestDiscountAdminViewSet, basename="admin-guest-discount")
admin_guest_discount_urlpatterns = _discount_router.urls
