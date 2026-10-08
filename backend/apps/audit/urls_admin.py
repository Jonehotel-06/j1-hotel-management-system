# apps/audit/urls_admin.py
from django.urls import path

from . import views

app_name = "audit_admin"

urlpatterns = [
    path("", views.AuditLogListView.as_view(), name="audit-log-list"),
    path("actions/", views.AuditLogActionListView.as_view(), name="audit-log-actions"),
    path("<int:pk>/", views.AuditLogDetailView.as_view(), name="audit-log-detail"),
]
