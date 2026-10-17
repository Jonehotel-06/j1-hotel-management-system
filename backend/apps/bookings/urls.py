# apps/bookings/urls.py
from django.urls import path

from . import views

app_name = "bookings"

urlpatterns = [
    path("quote/", views.QuoteView.as_view(), name="quote"),
    path("", views.MyBookingsView.as_view(), name="my-bookings"),
    path("<str:lookup>/", views.BookingDetailView.as_view(), name="detail"),
    path("<str:lookup>/cancel/", views.BookingCancelView.as_view(), name="cancel"),
    path("<str:lookup>/receipt/", views.BookingReceiptView.as_view(), name="receipt"),
    # Emailed secure links: exchange a signed value for a guest session token,
    # and re-send a link to the address on the reservation.
    path("<str:lookup>/access/", views.BookingAccessExchangeView.as_view(), name="access-exchange"),
    path("<str:lookup>/access-link/", views.BookingAccessLinkResendView.as_view(), name="access-link-resend"),
]

# Mounted separately at /api/rooms/availability/
availability_urlpatterns = [
    path("availability/", views.AvailabilityView.as_view(), name="availability"),
]
