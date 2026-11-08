# apps/accounts/urls_admin.py
from django.urls import path

from . import views_admin

app_name = "accounts_admin"

urlpatterns = [
    path("", views_admin.AdminUserListCreateView.as_view(), name="user-list"),
    path("terminals/", views_admin.WorkstationListCreateView.as_view(), name="workstation-list"),
    path("terminals/<int:pk>/", views_admin.WorkstationDetailView.as_view(), name="workstation-detail"),
    path("terminals/<int:pk>/desktop-key/", views_admin.WorkstationDesktopKeyView.as_view(), name="workstation-desktop-key"),
    # Approved front desk devices: a browser identity that survives an address
    # change. Requesting is open to any signed-in staff account (it grants
    # nothing); approving and revoking require terminal.manage.
    path("terminals/devices/", views_admin.FrontDeskDeviceListView.as_view(), name="device-list"),
    path("terminals/devices/request/", views_admin.FrontDeskDeviceRequestView.as_view(), name="device-request"),
    path("terminals/devices/<int:pk>/approve/", views_admin.FrontDeskDeviceApproveView.as_view(), name="device-approve"),
    path("terminals/devices/<int:pk>/revoke/", views_admin.FrontDeskDeviceRevokeView.as_view(), name="device-revoke"),
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
