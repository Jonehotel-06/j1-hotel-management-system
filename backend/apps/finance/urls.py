from django.urls import path

from . import views

app_name = "finance"

urlpatterns = [
    path("folios/", views.FolioListView.as_view(), name="folios"),
    path("folios/<str:reference>/", views.FolioDetailView.as_view(), name="folio-detail"),
    path("folios/<str:reference>/postings/", views.FolioPostingListView.as_view(), name="folio-postings"),
    path("reports/summary/", views.FinancialSummaryReportView.as_view(), name="financial-summary-report"),
    path("reports/night-audit/", views.NightAuditView.as_view(), name="night-audit-report"),
    path("reports/ar-aging/", views.AccountsReceivableAgingView.as_view(), name="ar-aging-report"),
    path("reports/balance-sheet/", views.BalanceSheetView.as_view(), name="balance-sheet-report"),
    path("reports/cash-flow/", views.CashFlowView.as_view(), name="cash-flow-report"),
    path("chart-of-accounts/", views.ChartOfAccountsView.as_view(), name="chart-of-accounts"),
    path("journals/", views.ManualJournalView.as_view(), name="manual-journals"),
    path("transactions/", views.FinancialTransactionListView.as_view(), name="transactions"),
    path("transactions/<str:reference>/", views.FinancialTransactionDetailView.as_view(), name="transaction-detail"),
    path("cash-sessions/", views.CashSessionListView.as_view(), name="cash-sessions"),
    path("cash-sessions/<str:reference>/variance-review/", views.CashVarianceReviewView.as_view(), name="cash-variance-review"),
    path("cash-sessions/<str:reference>/drop/", views.CashSessionDropView.as_view(), name="cash-session-drop"),
    path("approvals/", views.ApprovalRequestListView.as_view(), name="approvals"),
    path("approvals/<str:reference>/review/", views.ApprovalReviewView.as_view(), name="approval-review"),
    path("expenses/", views.ExpenseListCreateView.as_view(), name="expenses"),
    path("expenses/<str:reference>/", views.ExpenseDetailView.as_view(), name="expense-detail"),
    path("expenses/<str:reference>/submit/", views.ExpenseSubmitView.as_view(), name="expense-submit"),
    path("expenses/<str:reference>/review/", views.ExpenseReviewView.as_view(), name="expense-review"),
    path("expenses/<str:reference>/post/", views.ExpensePostView.as_view(), name="expense-post"),
    path("expenses/<str:reference>/settle-payment/", views.ExpenseSettlePaymentView.as_view(), name="expense-settle-payment"),
    path("expenses/<str:reference>/payment-evidence/", views.ExpensePaymentEvidenceView.as_view(), name="expense-payment-evidence"),
    path("control-policies/", views.FinancialControlPolicyListCreateView.as_view(), name="control-policies"),
    # --- Accounting agent: drillable reconciliation checks -------------------
    # One record per unresolved condition (room, billing, cash drawer,
    # procurement/payables) with the object, amount, why it is flagged and the
    # recommended next action. The night-audit count is rendered from this.
    path("reconciliation/checks/", views.ReconciliationCheckListView.as_view(), name="reconciliation-checks"),
    path(
        "reconciliation/checks/<str:check_id>/review/",
        views.ReconciliationCheckReviewView.as_view(), name="reconciliation-check-review",
    ),
    # --- Accounting agent: purchase-order hand-off and supplier payables -----
    path("procurement/payables/", views.PurchaseOrderPayableListView.as_view(), name="procurement-payables"),
    path(
        "procurement/purchase-orders/<str:reference>/payable/",
        views.PurchaseOrderPayableCreateView.as_view(), name="procurement-purchase-order-payable",
    ),
    # --- Accounting agent: supplier payment request workflow -----------------
    path("supplier-payments/", views.SupplierPaymentRequestListCreateView.as_view(), name="supplier-payments"),
    path("supplier-payments/<str:reference>/", views.SupplierPaymentRequestDetailView.as_view(), name="supplier-payment-detail"),
    path("supplier-payments/<str:reference>/submit/", views.SupplierPaymentRequestSubmitView.as_view(), name="supplier-payment-submit"),
    path("supplier-payments/<str:reference>/review/", views.SupplierPaymentRequestReviewView.as_view(), name="supplier-payment-review"),
    path("supplier-payments/<str:reference>/execute/", views.SupplierPaymentRequestExecuteView.as_view(), name="supplier-payment-execute"),
    path("supplier-payments/<str:reference>/cancel/", views.SupplierPaymentRequestCancelView.as_view(), name="supplier-payment-cancel"),
]
