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


def _schema_status():
    """Cheap, read-only schema summary for the readiness body.

    A drifted schema is reported but does NOT by itself fail readiness by
    default. Removing every replica from the load balancer because one newly
    deployed endpoint is ahead of the database would turn a partial outage into
    a total one — and in this project's deployment model the schema migration
    is a deliberately manual, post-deploy job. Operators (and an opt-in
    ``?strict=1`` probe) can still treat drift as not-ready; the detail is
    always in the body so the exact missing table/column is visible without
    reading a traceback.
    """
    from apps.core.health import pending_migration_count, schema_drift_report

    report = schema_drift_report()
    return {
        "ok": report["ok"],
        "checked_models": report["checked_models"],
        "missing_tables": report["missing_tables"][:20],
        "missing_columns": [f"{table}.{column}" for table, column in report["missing_columns"][:20]],
        "pending_migrations": pending_migration_count(),
    }


def _readiness_response(request=None):
    """Readiness body.

    The DEFAULT body is byte-for-byte the established contract
    ``{"success", "status", "database"}`` because existing orchestrator probes
    and the deployment tests assert it exactly. The schema detail that would
    have caught this incident is opt-in, so adding it can never break a
    deployed health check:

      ``?schema=1``  add a ``schema`` object naming any drift
      ``?strict=1``  additionally answer 503 when the schema is behind the code
                     (use this for a deploy gate, not for a liveness probe)
    """
    db_ok = _database_is_available()
    payload = {
        "success": db_ok,
        "status": "ok" if db_ok else "degraded",
        "database": "up" if db_ok else "down",
    }
    want_schema = False
    if request is not None:
        params = request.GET
        want_schema = any(
            str(params.get(key, "")).lower() in {"1", "true", "yes"}
            for key in ("schema", "strict")
        )
    if db_ok and want_schema:
        try:
            schema = _schema_status()
        except Exception:  # noqa: BLE001 - a probe must never 500
            schema = {"ok": True, "unknown": True}
        payload["schema"] = schema
        strict = str(request.GET.get("strict", "")).lower() in {"1", "true", "yes"}
        if strict and schema.get("ok") is False:
            payload["success"] = False
            payload["status"] = "schema-out-of-date"
            return Response(payload, status=drf_status.HTTP_503_SERVICE_UNAVAILABLE)
    # The explicit ``success`` flag lets the project renderer preserve the
    # compact top-level probe contract instead of nesting it under ``data``.
    return Response(
        payload,
        status=drf_status.HTTP_200_OK if db_ok else drf_status.HTTP_503_SERVICE_UNAVAILABLE,
    )


@extend_schema(responses={200: dict}, summary="Process liveness probe", tags=["Meta"])
@api_view(["GET", "HEAD"])
@permission_classes([AllowAny])
def health_liveness(request):
    """Report that the Django process can receive requests without touching DB."""
    return Response({"success": True, "status": "ok"}, status=drf_status.HTTP_200_OK)


@extend_schema(
    responses={200: dict, 503: dict},
    summary="Dependency readiness probe",
    tags=["Meta"],
)
@api_view(["GET", "HEAD"])
@permission_classes([AllowAny])
def health_readiness(request):
    """Report whether the API is ready to serve authoritative hotel data."""
    return _readiness_response(request)


@extend_schema(
    responses={200: dict, 503: dict},
    summary="Health check (legacy readiness alias)",
    tags=["Meta"],
)
@api_view(["GET", "HEAD"])
@permission_classes([AllowAny])
def health_check(request):
    """Keep the established endpoint contract for existing load balancers."""
    return _readiness_response(request)
