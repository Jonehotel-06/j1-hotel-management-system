# apps/accounts/urls_admin.py
from django.urls import path

from . import views_admin

app_name = "accounts_admin"

urlpatterns = [
    path("", views_admin.AdminUserListCreateView.as_view(), name="user-list"),
    path("terminals/", views_admin.WorkstationListCreateView.as_view(), name="workstation-list"),
    path("terminals/<int:pk>/", views_admin.WorkstationDetailView.as_view(), name="workstation-detail"),
    path("terminals/<int:pk>/desktop-key/", views_admin.WorkstationDesktopKeyView.as_view(), name="workstation-desktop-key"),
    # Assignment-picker projection is capability-gated separately from the
    # administrator-only account management collection.
    path("directory/", views_admin.OperationalStaffDirectoryView.as_view(), name="operational-staff-directory"),
    path("permissions-matrix/", views_admin.AdminPermissionMatrixView.as_view(), name="permissions-matrix"),
    # Registered BEFORE the numeric detail route so "staff/" is never captured
    # as a user id.
    path("staff/<int:pk>/", views_admin.AdminStaffProfileView.as_view(), name="staff-profile"),
    path("<int:pk>/actions/", views_admin.AdminUserAccountActionView.as_view(), name="user-actions"),
    path("<int:pk>/permissions/", views_admin.AdminUserPermissionsView.as_view(), name="user-permissions"),
    path("<int:pk>/", views_admin.AdminUserDetailView.as_view(), name="user-detail"),
]
