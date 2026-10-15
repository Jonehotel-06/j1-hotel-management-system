# apps/reports/urls.py
from django.urls import path

from . import approvals_views, views

app_name = "reports"

urlpatterns = [
    path("revenue/", views.RevenueReportView.as_view(), name="revenue"),
    path("occupancy/", views.OccupancyReportView.as_view(), name="occupancy"),
    path("bookings/", views.BookingsReportView.as_view(), name="bookings"),
    path("approvals/", approvals_views.ApprovalsInboxView.as_view(), name="approvals"),
]

dashboard_urlpatterns = [
    path("", views.DashboardView.as_view(), name="dashboard"),
]
