from django.urls import path

from . import portal_views

app_name = "finance_portal"

urlpatterns = [
    path("folios/", portal_views.PortalFolioListView.as_view(), name="folios"),
    path("folios/<str:reference>/", portal_views.PortalFolioDetailView.as_view(), name="folio-detail"),
    path("folios/<str:reference>/postings/", portal_views.PortalFolioPostingListView.as_view(), name="folio-postings"),
]
