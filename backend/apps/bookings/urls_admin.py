# apps/bookings/urls_admin.py
from django.urls import path

from . import views_admin

app_name = "bookings_admin"

bookings_urlpatterns = [
    path("", views_admin.AdminBookingListCreateView.as_view(), name="booking-list"),
    # Static routes MUST precede <str:lookup>/ or they would be captured by it.
    path("calendar/", views_admin.AdminOccupancyCalendarView.as_view(), name="booking-calendar"),
    path("missed/", views_admin.AdminMissedBookingListView.as_view(), name="booking-missed"),
    path("late-arrivals/", views_admin.AdminLateArrivalBookingListView.as_view(), name="booking-late-arrivals"),
    path("<str:lookup>/", views_admin.AdminBookingDetailView.as_view(), name="booking-detail"),
    path("<str:lookup>/confirm/", views_admin.AdminBookingConfirmView.as_view(), name="booking-confirm"),
    path("<str:lookup>/cancel/", views_admin.AdminBookingCancelView.as_view(), name="booking-cancel"),
    path("<str:lookup>/check-in/", views_admin.AdminBookingCheckInView.as_view(), name="booking-check-in"),
    path("<str:lookup>/check-out/", views_admin.AdminBookingCheckOutView.as_view(), name="booking-check-out"),
    path("<str:lookup>/no-show/", views_admin.AdminBookingNoShowView.as_view(), name="booking-no-show"),
    path("<str:lookup>/assign-room/", views_admin.AdminBookingAssignRoomView.as_view(), name="booking-assign-room"),
    path("<str:lookup>/reschedule/", views_admin.AdminBookingRescheduleView.as_view(), name="booking-reschedule"),
    path("<str:lookup>/send-receipt/", views_admin.AdminBookingSendReceiptView.as_view(), name="booking-send-receipt"),
]

guests_urlpatterns = [
    path("", views_admin.AdminGuestListView.as_view(), name="guest-list"),
    path("<int:pk>/", views_admin.AdminGuestDetailView.as_view(), name="guest-detail"),
]
