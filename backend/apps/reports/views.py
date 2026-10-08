# apps/reports/views.py
"""Dashboard + report endpoints."""
import logging
from datetime import datetime

from drf_spectacular.utils import OpenApiParameter, extend_schema
from drf_spectacular.types import OpenApiTypes
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.exceptions import InvalidDatesError
from apps.core.permissions import IsAdminRole, IsManagerOrAdmin, IsStaffRole
from apps.core.responses import success_response
from apps.core.serializers import EmptySerializer

from .services import dashboard as dashboard_service
from .services import reports as reports_service

logger = logging.getLogger("apps")


def _date_param(request, name):
    raw = request.query_params.get(name)
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        raise InvalidDatesError(f"Parameter '{name}' must be YYYY-MM-DD.")


@extend_schema(tags=["Admin · Dashboard"], summary="Aggregated staff dashboard")
class DashboardView(APIView):
    permission_classes = [IsStaffRole]
    serializer_class = EmptySerializer  # response contract: docs/FRONTEND_CONTRACT.md

    def get(self, request):
        return success_response(dashboard_service.build_dashboard(request.user))


@extend_schema(tags=["Admin · Reports"], parameters=[
    OpenApiParameter("start_date", OpenApiTypes.DATE, required=True),
    OpenApiParameter("end_date", OpenApiTypes.DATE, required=True),
])
class RevenueReportView(APIView):
    permission_classes = [IsManagerOrAdmin]
    serializer_class = EmptySerializer

    def get(self, request):
        data = reports_service.revenue_report(
            _date_param(request, "start_date"), _date_param(request, "end_date")
        )
        return success_response(data)


@extend_schema(tags=["Admin · Reports"], parameters=[
    OpenApiParameter("start_date", OpenApiTypes.DATE, required=True),
    OpenApiParameter("end_date", OpenApiTypes.DATE, required=True),
])
class OccupancyReportView(APIView):
    permission_classes = [IsManagerOrAdmin]
    serializer_class = EmptySerializer

    def get(self, request):
        data = reports_service.occupancy_report(
            _date_param(request, "start_date"), _date_param(request, "end_date")
        )
        return success_response(data)


@extend_schema(tags=["Admin · Reports"], parameters=[
    OpenApiParameter("start_date", OpenApiTypes.DATE, required=True),
    OpenApiParameter("end_date", OpenApiTypes.DATE, required=True),
])
class BookingsReportView(APIView):
    permission_classes = [IsManagerOrAdmin]
    serializer_class = EmptySerializer

    def get(self, request):
        data = reports_service.bookings_report(
            _date_param(request, "start_date"), _date_param(request, "end_date")
        )
        return success_response(data)
