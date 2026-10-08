# apps/reports/urls.py
from django.urls import path

from . import views

app_name = "reports"

urlpatterns = [
    path("revenue/", views.RevenueReportView.as_view(), name="revenue"),
    path("occupancy/", views.OccupancyReportView.as_view(), name="occupancy"),
    path("bookings/", views.BookingsReportView.as_view(), name="bookings"),
]

dashboard_urlpatterns = [
    path("", views.DashboardView.as_view(), name="dashboard"),
]
