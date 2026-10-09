from django.urls import path

from . import payroll_views, views

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
    path("payroll/compensation/", payroll_views.StaffCompensationListCreateView.as_view(), name="payroll-compensation"),
    path("payroll/tax-identities/", payroll_views.PayrollTaxIdentityListCreateView.as_view(), name="payroll-tax-identities"),
    path("payroll/statutory-rules/", payroll_views.PayrollStatutoryRuleSetListCreateView.as_view(), name="payroll-statutory-rules"),
    path("payroll/statutory-rules/<str:reference>/review/", payroll_views.PayrollStatutoryRuleSetReviewView.as_view(), name="payroll-statutory-rules-review"),
    path("payroll/statutory-rules/<str:reference>/", payroll_views.PayrollStatutoryRuleSetDetailView.as_view(), name="payroll-statutory-rules-detail"),
    path("payroll/periods/", payroll_views.PayrollPeriodListCreateView.as_view(), name="payroll-periods"),
    path("payroll/periods/<str:reference>/submit/", payroll_views.PayrollSubmitView.as_view(), name="payroll-submit"),
    path("payroll/periods/<str:reference>/review/", payroll_views.PayrollReviewView.as_view(), name="payroll-review"),
    path("payroll/periods/<str:reference>/pay/", payroll_views.PayrollPayView.as_view(), name="payroll-pay"),
    path("payroll/periods/<str:reference>/lines/<int:line_id>/payments/", payroll_views.PayrollSalaryPaymentListCreateView.as_view(), name="payroll-salary-payments"),
    path("payroll/periods/<str:reference>/lines/<int:line_id>/payments/<str:payment_reference>/reverse/", payroll_views.PayrollSalaryPaymentReverseView.as_view(), name="payroll-salary-payment-reverse"),
    path("payroll/periods/<str:reference>/", payroll_views.PayrollPeriodDetailView.as_view(), name="payroll-period-detail"),
    path("payroll/payslips/<str:reference>/", payroll_views.PayrollPayslipView.as_view(), name="payroll-payslip"),
]
