from django.urls import path

from . import views

app_name = "staff_operations"

urlpatterns = [
    path("profiles/", views.StaffProfileListCreateView.as_view(), name="profiles"),
    path("profiles/<int:pk>/", views.StaffProfileDetailView.as_view(), name="profile-detail"),
    path("shift-templates/", views.ShiftTemplateListCreateView.as_view(), name="shift-templates"),
    path("shift-templates/<int:pk>/", views.ShiftTemplateDetailView.as_view(), name="shift-template-detail"),
    path("shifts/", views.ShiftAssignmentListCreateView.as_view(), name="shifts"),
    path("shifts/<str:reference>/", views.ShiftAssignmentDetailView.as_view(), name="shift-detail"),
    path("shifts/<str:reference>/cancel/", views.ShiftAssignmentCancelView.as_view(), name="shift-cancel"),
    path("attendance/", views.AttendanceRecordListView.as_view(), name="attendance"),
    path("attendance/clock/", views.AttendanceClockView.as_view(), name="attendance-clock"),
    path("attendance/<str:reference>/", views.AttendanceRecordDetailView.as_view(), name="attendance-detail"),
    path("leave-requests/", views.LeaveRequestListCreateView.as_view(), name="leave-requests"),
    path("leave-requests/<str:reference>/", views.LeaveRequestDetailView.as_view(), name="leave-detail"),
    path("leave-requests/<str:reference>/review/", views.LeaveReviewView.as_view(), name="leave-review"),
    path("leave-requests/<str:reference>/cancel/", views.LeaveCancelView.as_view(), name="leave-cancel"),
]
