from django.urls import path

from . import views

app_name = "finance"

urlpatterns = [
    path("folios/", views.FolioListView.as_view(), name="folios"),
    path("folios/<str:reference>/", views.FolioDetailView.as_view(), name="folio-detail"),
    path("folios/<str:reference>/postings/", views.FolioPostingListView.as_view(), name="folio-postings"),
    path("reports/summary/", views.FinancialSummaryReportView.as_view(), name="financial-summary-report"),
    path("transactions/", views.FinancialTransactionListView.as_view(), name="transactions"),
    path("transactions/<str:reference>/", views.FinancialTransactionDetailView.as_view(), name="transaction-detail"),
    path("cash-sessions/", views.CashSessionListView.as_view(), name="cash-sessions"),
    path("cash-sessions/<str:reference>/variance-review/", views.CashVarianceReviewView.as_view(), name="cash-variance-review"),
    path("approvals/", views.ApprovalRequestListView.as_view(), name="approvals"),
    path("approvals/<str:reference>/review/", views.ApprovalReviewView.as_view(), name="approval-review"),
    path("expenses/", views.ExpenseListCreateView.as_view(), name="expenses"),
    path("expenses/<str:reference>/", views.ExpenseDetailView.as_view(), name="expense-detail"),
    path("expenses/<str:reference>/submit/", views.ExpenseSubmitView.as_view(), name="expense-submit"),
    path("expenses/<str:reference>/review/", views.ExpenseReviewView.as_view(), name="expense-review"),
    path("expenses/<str:reference>/post/", views.ExpensePostView.as_view(), name="expense-post"),
    path("control-policies/", views.FinancialControlPolicyListCreateView.as_view(), name="control-policies"),
]
