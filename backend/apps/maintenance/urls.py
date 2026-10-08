from django.urls import path
from . import views
app_name = "maintenance"
urlpatterns = [
    path("", views.MaintenanceWorkOrderListCreateView.as_view(), name="work-orders"),
    path("<str:reference>/", views.MaintenanceWorkOrderDetailView.as_view(), name="work-order-detail"),
    path("<str:reference>/assign/", views.MaintenanceWorkOrderAssignView.as_view(), name="work-order-assign"),
    path("<str:reference>/claim/", views.MaintenanceWorkOrderClaimView.as_view(), name="work-order-claim"),
    path("<str:reference>/status/", views.MaintenanceWorkOrderStatusView.as_view(), name="work-order-status"),
    path("<str:reference>/comments/", views.MaintenanceWorkOrderCommentView.as_view(), name="work-order-comments"),
    path("<str:reference>/outage/start/", views.MaintenanceOutageStartView.as_view(), name="outage-start"),
    path("<str:reference>/outage/clear/", views.MaintenanceOutageClearView.as_view(), name="outage-clear"),
]
