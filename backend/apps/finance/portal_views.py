"""Verified-email guest portal reads over the authoritative folio ledger."""
from drf_spectacular.utils import extend_schema
from rest_framework.exceptions import NotFound
from rest_framework.views import APIView

from apps.core.pagination import StandardPagination
from apps.core.responses import success_response
from apps.portal.authentication import PortalSessionAuthentication
from apps.portal.permissions import HasPortalSession

from .models import Folio, FolioPosting
from .portal_serializers import PortalFolioPostingSerializer, PortalFolioSerializer
from .services.folio_service import folio_balance_queryset


def _page(view, request, queryset, serializer):
    paginator = StandardPagination()
    page = paginator.paginate_queryset(queryset, request, view=view)
    return paginator.get_paginated_response(serializer(page, many=True).data)


@extend_schema(tags=["Guest Portal · Folios"], summary="List authoritative folios owned by the verified email")
class PortalFolioListView(APIView):
    authentication_classes = [PortalSessionAuthentication]
    permission_classes = [HasPortalSession]

    def get(self, request):
        queryset = folio_balance_queryset(
            Folio.objects.filter(guest__email__iexact=request.portal_session.email)
            .select_related("booking", "stay")
            .order_by("-opened_at", "-pk")
        )
        if status_value := request.query_params.get("status"):
            queryset = queryset.filter(status=status_value)
        return _page(self, request, queryset, PortalFolioSerializer)


@extend_schema(tags=["Guest Portal · Folios"], summary="Read an owned folio statement")
class PortalFolioPostingListView(APIView):
    authentication_classes = [PortalSessionAuthentication]
    permission_classes = [HasPortalSession]

    def get(self, request, reference):
        owned = Folio.objects.filter(reference=reference, guest__email__iexact=request.portal_session.email).first()
        if owned is None:
            raise NotFound("Folio not found.")
        queryset = FolioPosting.objects.filter(folio=owned).order_by("-business_date", "-created_at", "-pk")
        return _page(self, request, queryset, PortalFolioPostingSerializer)


@extend_schema(tags=["Guest Portal · Folios"], summary="Read one owned folio with a computed balance")
class PortalFolioDetailView(APIView):
    authentication_classes = [PortalSessionAuthentication]
    permission_classes = [HasPortalSession]

    def get(self, request, reference):
        folio = folio_balance_queryset(
            Folio.objects.filter(reference=reference, guest__email__iexact=request.portal_session.email)
            .select_related("booking", "stay")
        ).first()
        if folio is None:
            raise NotFound("Folio not found.")
        return success_response(PortalFolioSerializer(folio).data)
