from django.urls import path

from . import views

app_name = "guest_services"

staff_urlpatterns = [
    path("laundry/", views.LaundryOrderListCreateView.as_view(), name="laundry-orders"),
    path("laundry/<str:reference>/assign/", views.LaundryOrderAssignView.as_view(), name="laundry-assign"),
    path("laundry/<str:reference>/status/", views.LaundryOrderStatusView.as_view(), name="laundry-status"),
    path("laundry/<str:reference>/bill/", views.LaundryOrderBillView.as_view(), name="laundry-bill"),
    path("laundry/<str:reference>/settle/", views.LaundryOrderSettleView.as_view(), name="laundry-settle"),
    path("", views.ServiceRequestListCreateView.as_view(), name="requests"),
    path("<str:reference>/", views.ServiceRequestDetailView.as_view(), name="request-detail"),
    path("<str:reference>/assign/", views.ServiceRequestAssignView.as_view(), name="request-assign"),
    path("<str:reference>/claim/", views.ServiceRequestClaimView.as_view(), name="request-claim"),
    path("<str:reference>/housekeeping-task/", views.ServiceRequestHousekeepingTaskView.as_view(), name="request-housekeeping-task"),
    path("<str:reference>/maintenance-work-order/", views.ServiceRequestMaintenanceWorkOrderView.as_view(), name="request-maintenance-work-order"),
    path("<str:reference>/status/", views.ServiceRequestStatusView.as_view(), name="request-status"),
    path("<str:reference>/comments/", views.ServiceRequestCommentView.as_view(), name="request-comments"),
    # Receptionist guest amenities + complementary breakfast
    path("reception/amenities/allocate/", views.ReceptionAmenityAllocationView.as_view(), name="reception-amenities-allocate"),
    path("reception/amenities/stock/", views.ReceptionAmenityStockView.as_view(), name="reception-amenities-stock"),
    path("reception/amenities/issues/", views.GuestAmenityIssueListView.as_view(), name="guest-amenity-issues"),
    path("reception/amenities/issue/", views.GuestAmenityIssueView.as_view(), name="guest-amenity-issue"),
    path("breakfast/config/", views.BreakfastConfigView.as_view(), name="breakfast-config"),
    path("breakfast/eligibility/", views.BreakfastEligibilityView.as_view(), name="breakfast-eligibility"),
    path("breakfast/issues/", views.BreakfastIssuanceListView.as_view(), name="breakfast-issuances"),
    path("breakfast/issue/", views.BreakfastIssueView.as_view(), name="breakfast-issue"),
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
    path("catalog/", views.PortalServiceCatalogView.as_view(), name="portal-service-catalog"),
    path("<str:reference>/", views.PortalServiceRequestDetailView.as_view(), name="portal-request-detail"),
    path("<str:reference>/comments/", views.PortalServiceRequestCommentView.as_view(), name="portal-request-comments"),
    path("<str:reference>/cancel/", views.PortalServiceRequestCancelView.as_view(), name="portal-request-cancel"),
    path("<str:reference>/confirm-delivery/", views.PortalServiceRequestConfirmDeliveryView.as_view(), name="portal-request-confirm-delivery"),
]
