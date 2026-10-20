# apps/core/exception_handler.py
"""Uniform API error envelope.

Transforms every DRF/Django exception into:

    {"success": false, "code": "MACHINE_READABLE", "message": "...", "errors": {...}}

Unexpected exceptions are logged with a full traceback internally while the
client only ever receives a safe, generic message.
"""
import logging

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import DataError, IntegrityError
from django.http import Http404
from rest_framework import exceptions as drf_exceptions
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from apps.core.storage import StorageUploadError

logger = logging.getLogger("apps")

def _detail_text(detail, fallback):
    """Turn a DRF detail (often a dict) into one human sentence.

    simplejwt rejects a token with ``AuthenticationFailed({"detail": ...,
    "code": "token_not_valid"})``. ``str()`` on that mapping produced a Python
    dict repr in the envelope message ("{'detail': ErrorDetail(string='Given
    token not valid…', code='token_not_valid')}"), which the booking screen then
    showed to the guest verbatim. Pull the sentence out instead.
    """
    if isinstance(detail, dict):
        for key in ("detail", "message", "error"):
            if detail.get(key) is not None:
                return _detail_text(detail[key], fallback)
        return fallback
    if isinstance(detail, (list, tuple)):
        parts = [_detail_text(item, "") for item in detail]
        joined = "; ".join(part for part in parts if part)
        return joined or fallback
    text = str(detail).strip() if detail is not None else ""
    return text or fallback


def _envelope(code, message, errors, status_code, headers=None):
    """Build the uniform error body, preserving any header DRF attached.

    DRF puts machine-readable headers on some errors - ``Retry-After`` on a 429,
    ``WWW-Authenticate`` on a 401 - and a rebuilt Response silently dropped
    them, so a throttled client had to parse prose to know when to come back
    (found while proving the throttle guards, audit SEC-005). Forwarding the
    original response's headers keeps DRF's own contract intact.
    """
    payload = {"success": False, "code": code, "message": message}
    if errors:
        payload["errors"] = errors
    return Response(payload, status=status_code, headers=headers)


def jone_exception_handler(exc, context):
    if isinstance(exc, Http404):
        exc = drf_exceptions.NotFound()
    elif isinstance(exc, DjangoPermissionDenied):
        # Domain services raise this with the actual business reason (for
        # example "A requester cannot approve their own sensitive monetary
        # action."). Blanking it turned every rule-based refusal into
        # "You do not have permission to perform this action.", which reads as
        # a permissions problem, sends staff to the wrong place, and hides the
        # two-person rule from anyone testing it (audit D2). The message is
        # authored in our own code, never a provider payload, so it is safe to
        # surface; the generic text remains the fallback when there is none.
        reason = " ".join(str(arg) for arg in exc.args if arg).strip()
        exc = drf_exceptions.PermissionDenied(reason or None)
    elif isinstance(exc, DjangoValidationError):
        # Domain services deliberately use Django's validation primitive so
        # they are equally safe from HTTP, management-command, and task
        # callers. Convert it at this edge instead of leaking a 500 from an
        # expected business-rule rejection.
        detail = getattr(exc, "message_dict", None) or getattr(exc, "messages", None) or str(exc)
        exc = drf_exceptions.ValidationError(detail)
    elif isinstance(exc, StorageUploadError):
        # The media backend (e.g. Backblaze B2) refused the write. The cause is
        # already logged by apps.core.storage; the client gets an honest, safe
        # failure instead of a false "saved" response.
        return _envelope("STORAGE_UPLOAD_FAILED", str(exc), None, status.HTTP_502_BAD_GATEWAY)
    elif isinstance(exc, DataError):
        view = (context or {}).get("view")
        logger.warning("Database DataError in %s: %s", view, exc)
        return _envelope(
            "VALIDATION_ERROR",
            "One or more fields exceed the maximum allowed length or format.",
            None,
            status.HTTP_400_BAD_REQUEST,
        )

    response = drf_exception_handler(exc, context)

    if response is None:
        # Unhandled server error: log everything, reveal nothing.
        view = (context or {}).get("view")
        logger.exception("Unhandled exception in %s", view, exc_info=exc)
        return _envelope(
            "SERVER_ERROR",
            "An unexpected error occurred. Please try again later.",
            None,
            status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    data = response.data

    if isinstance(exc, drf_exceptions.ValidationError):
        return _envelope("VALIDATION_ERROR", "Validation failed.", data, response.status_code, response.headers)

    if isinstance(exc, drf_exceptions.NotAuthenticated):
        return _envelope(
            "UNAUTHORIZED",
            "Authentication credentials were not provided or are invalid.",
            None,
            response.status_code,
            response.headers,
        )

    if isinstance(exc, drf_exceptions.AuthenticationFailed):
        # Portal access that ended at checkout carries a distinct code so the
        # guest portal can explain it. Every other auth failure stays UNAUTHORIZED.
        if str(exc.get_codes()) == "portal_access_ended":
            return _envelope(
                "PORTAL_ACCESS_ENDED",
                _detail_text(getattr(exc, "detail", None), "Portal access has ended."),
                None,
                response.status_code,
                response.headers,
            )
        return _envelope(
            "UNAUTHORIZED",
            _detail_text(
                getattr(exc, "detail", None),
                "Your session has expired. Please sign in again.",
            ),
            None,
            response.status_code,
            response.headers,
        )

    if isinstance(exc, drf_exceptions.PermissionDenied):
        return _envelope(
            "FORBIDDEN",
            _detail_text(
                getattr(exc, "detail", None),
                "You do not have permission to perform this action.",
            ),
            None,
            response.status_code,
            response.headers,
        )

    if isinstance(exc, drf_exceptions.NotFound):
        return _envelope(
            "RESOURCE_NOT_FOUND", "The requested resource was not found.", None,
            status.HTTP_404_NOT_FOUND, response.headers,
        )

    if isinstance(exc, drf_exceptions.Throttled):
        wait = getattr(exc, "wait", None)
        message = "Too many requests. Please slow down."
        if wait:
            message = f"Too many requests. Try again in {int(wait)} seconds."
        return _envelope(
            "RATE_LIMITED", message, None, status.HTTP_429_TOO_MANY_REQUESTS, response.headers,
        )

    # Domain APIExceptions (apps.core.exceptions) carry their own code/detail.
    code = exc.get_codes()
    if isinstance(code, (list, tuple)):
        code = code[0] if code else "ERROR"
    if isinstance(code, dict):
        code = "VALIDATION_ERROR"
    detail = exc.detail
    message = detail if isinstance(detail, str) else "; ".join(map(str, getattr(detail, "__iter__", lambda: [])() or [detail]))
    # A domain error may carry structured context the client needs in order to
    # act — e.g. a refused staff sign-in reports the address it came from, so an
    # administrator can register that address for the desk. Empty by default.
    errors = getattr(exc, "errors_payload", None)
    return _envelope(str(code).upper(), str(message), errors, response.status_code, response.headers)
