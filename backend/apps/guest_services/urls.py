from django.urls import path

from . import views

app_name = "guest_services"

staff_urlpatterns = [
    path("", views.ServiceRequestListCreateView.as_view(), name="requests"),
    path("<str:reference>/", views.ServiceRequestDetailView.as_view(), name="request-detail"),
    path("<str:reference>/assign/", views.ServiceRequestAssignView.as_view(), name="request-assign"),
    path("<str:reference>/claim/", views.ServiceRequestClaimView.as_view(), name="request-claim"),
    path("<str:reference>/housekeeping-task/", views.ServiceRequestHousekeepingTaskView.as_view(), name="request-housekeeping-task"),
    path("<str:reference>/maintenance-work-order/", views.ServiceRequestMaintenanceWorkOrderView.as_view(), name="request-maintenance-work-order"),
    path("<str:reference>/status/", views.ServiceRequestStatusView.as_view(), name="request-status"),
    path("<str:reference>/comments/", views.ServiceRequestCommentView.as_view(), name="request-comments"),
]

qr_admin_urlpatterns = [
    path("", views.ServiceQRLinkListCreateView.as_view(), name="service-qr-links"),
    path("<str:reference>/rotate/", views.ServiceQRLinkRotateView.as_view(), name="service-qr-rotate"),
    path("<str:reference>/revoke/", views.ServiceQRLinkRevokeView.as_view(), name="service-qr-revoke"),
]

qr_public_urlpatterns = [
    path("context/", views.PublicServiceQRContextView.as_view(), name="service-qr-context"),
    path("requests/", views.PublicServiceQRRequestView.as_view(), name="service-qr-requests"),
]

portal_urlpatterns = [
    path("", views.PortalServiceRequestListCreateView.as_view(), name="portal-requests"),
    path("<str:reference>/", views.PortalServiceRequestDetailView.as_view(), name="portal-request-detail"),
    path("<str:reference>/comments/", views.PortalServiceRequestCommentView.as_view(), name="portal-request-comments"),
    path("<str:reference>/cancel/", views.PortalServiceRequestCancelView.as_view(), name="portal-request-cancel"),
]
