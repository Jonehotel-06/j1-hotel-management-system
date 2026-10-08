# apps/enquiries/urls.py
from django.urls import path

from . import views

app_name = "enquiries"

urlpatterns = [
    path("cancellation-status/<str:reference>/", views.CancellationStatusView.as_view(), name="cancellation-status"),
    path("", views.EnquiryCreateView.as_view(), name="create"),
]

admin_urlpatterns = [
    path("", views.EnquiryAdminListView.as_view(), name="admin-list"),
    path("<int:pk>/", views.EnquiryAdminDetailView.as_view(), name="admin-detail"),
    path("<int:pk>/review/", views.CancellationReviewView.as_view(), name="cancellation-review"),
    path("<int:pk>/approve-cancellation/", views.CancellationApproveView.as_view(), name="cancellation-approve"),
    path("<int:pk>/reject-cancellation/", views.CancellationRejectView.as_view(), name="cancellation-reject"),
    path("<int:pk>/process-refund/", views.CancellationProcessRefundView.as_view(), name="cancellation-process-refund"),
    path("<int:pk>/close/", views.CancellationCloseView.as_view(), name="cancellation-close"),
]
