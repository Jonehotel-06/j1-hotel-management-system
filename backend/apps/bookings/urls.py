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
]

# Mounted separately at /api/rooms/availability/
availability_urlpatterns = [
    path("availability/", views.AvailabilityView.as_view(), name="availability"),
]
