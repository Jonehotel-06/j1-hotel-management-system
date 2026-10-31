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
from apps.core.permissions import IsAdminRole, IsDashboardViewer, IsManagerOrAdmin, IsStaffRole
from apps.core.responses import success_response
from apps.core.serializers import EmptySerializer

from .services import dashboard as dashboard_service
from .services import exports
from .services import reports as reports_service


XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _file_response(payload: bytes, content_type: str, filename: str):
    from django.http import HttpResponse

    response = HttpResponse(payload, content_type=content_type)
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def _requested_export(request) -> str:
    """The export format asked for, or "" for the normal JSON response.

    `format` is accepted alongside `export` because the dashboard has used
    both spellings; anything else is ignored rather than guessed at.
    """
    raw = request.query_params.get("export") or request.query_params.get("format") or ""
    raw = raw.strip().lower()
    return raw if raw in ("xlsx", "pdf") else ""


def _export_response(data: dict, export_format: str, *, slug: str, xlsx, pdf):
    """BATCH 01 (task 8). One place that turns a report into a file.

    Previously only occupancy had an export branch, so Revenue and Bookings
    answered `?export=xlsx` with the ordinary JSON envelope under a filename
    ending in .xlsx. The browser saved it, Excel refused to open it, and the
    staff concluded the export was broken rather than missing.
    """
    stem = f"jone-{slug}-{data.get('start_date')}-to-{data.get('end_date')}"
    if export_format == "xlsx":
        return _file_response(xlsx(data), XLSX_CONTENT_TYPE, f"{stem}.xlsx")
    if export_format == "pdf":
        return _file_response(pdf(data), "application/pdf", f"{stem}.pdf")
    return None

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
    # Roles the frontend actually routes to index.html (admin, manager,
    # receptionist, front desk supervisor, general manager). STAFF_ROLES alone
    # 403'd the last two on their own landing page; see DASHBOARD_ROLES.
    permission_classes = [IsDashboardViewer]
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
        download = _export_response(
            data, _requested_export(request), slug="payments",
            xlsx=exports.revenue_xlsx_bytes, pdf=exports.revenue_pdf_bytes,
        )
        return download or success_response(data)


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
        download = _export_response(
            data, _requested_export(request), slug="occupancy",
            xlsx=exports.occupancy_xlsx_bytes, pdf=exports.occupancy_pdf_bytes,
        )
        return download or success_response(data)


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
        download = _export_response(
            data, _requested_export(request), slug="bookings",
            xlsx=exports.bookings_xlsx_bytes, pdf=exports.bookings_pdf_bytes,
        )
        return download or success_response(data)
