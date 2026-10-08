# apps/payments/urls_admin.py
from django.urls import path

from . import views_admin

app_name = "payments_admin"

urlpatterns = [
    path("", views_admin.AdminPaymentListView.as_view(), name="payment-list"),
    path("record/", views_admin.AdminRecordOfflinePaymentView.as_view(), name="payment-record"),
    path("refunds/", views_admin.AdminRefundListView.as_view(), name="refund-list"),
    path("refunds/<int:pk>/", views_admin.AdminRefundDetailView.as_view(), name="refund-detail"),
    path("<str:lookup>/", views_admin.AdminPaymentDetailView.as_view(), name="payment-detail"),
]
