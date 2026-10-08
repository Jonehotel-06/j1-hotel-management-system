# apps/core/views.py
"""Public API metadata and operational health probes.

``/api/health/live/`` deliberately performs no dependency checks so an
orchestrator can distinguish a running Django process from a ready-to-serve
hotel API. ``/api/health/ready/`` verifies the database; the long-standing
``/api/health/`` endpoint remains a backwards-compatible readiness alias.
"""
from django.db import connection
from drf_spectacular.utils import extend_schema
from rest_framework import status as drf_status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from apps.core.responses import success_response


@extend_schema(responses={200: dict}, summary="API index", tags=["Meta"])
@api_view(["GET"])
@permission_classes([AllowAny])
def api_index(request):
    return success_response(
        {
            "name": "J-ONE HOTEL & LODGE API",
            "version": "1.0.0",
            "documentation": "/api/docs/",
            "health": "/api/health/",
            "liveness": "/api/health/live/",
            "readiness": "/api/health/ready/",
        }
    )


def _database_is_available():
    """Return whether the authoritative database can answer a trivial query.

    Health checks must not write, migrate, or inspect business records. A
    fresh cursor also gives production connection pools a chance to establish a
    replacement connection after a transient database restart.
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:
        return False
    return True


def _readiness_response():
    db_ok = _database_is_available()
    # The explicit ``success`` flag lets the project renderer preserve the
    # compact top-level probe contract instead of nesting it under ``data``.
    return Response(
        {
            "success": db_ok,
            "status": "ok" if db_ok else "degraded",
            "database": "up" if db_ok else "down",
        },
        status=drf_status.HTTP_200_OK if db_ok else drf_status.HTTP_503_SERVICE_UNAVAILABLE,
    )


@extend_schema(responses={200: dict}, summary="Process liveness probe", tags=["Meta"])
@api_view(["GET"])
@permission_classes([AllowAny])
def health_liveness(request):
    """Report that the Django process can receive requests without touching DB."""
    return Response({"success": True, "status": "ok"}, status=drf_status.HTTP_200_OK)


@extend_schema(
    responses={200: dict, 503: dict},
    summary="Dependency readiness probe",
    tags=["Meta"],
)
@api_view(["GET"])
@permission_classes([AllowAny])
def health_readiness(request):
    """Report whether the API is ready to serve authoritative hotel data."""
    return _readiness_response()


@extend_schema(
    responses={200: dict, 503: dict},
    summary="Health check (legacy readiness alias)",
    tags=["Meta"],
)
@api_view(["GET"])
@permission_classes([AllowAny])
def health_check(request):
    """Keep the established endpoint contract for existing load balancers."""
    return _readiness_response()
