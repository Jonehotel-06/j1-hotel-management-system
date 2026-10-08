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
from django.http import Http404
from rest_framework import exceptions as drf_exceptions
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from apps.core.storage import StorageUploadError

logger = logging.getLogger("apps")


def _envelope(code, message, errors, status_code):
    payload = {"success": False, "code": code, "message": message}
    if errors:
        payload["errors"] = errors
    return Response(payload, status=status_code)


def jone_exception_handler(exc, context):
    if isinstance(exc, Http404):
        exc = drf_exceptions.NotFound()
    elif isinstance(exc, DjangoPermissionDenied):
        exc = drf_exceptions.PermissionDenied()
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
        return _envelope("VALIDATION_ERROR", "Validation failed.", data, response.status_code)

    if isinstance(exc, drf_exceptions.NotAuthenticated):
        return _envelope(
            "UNAUTHORIZED",
            "Authentication credentials were not provided or are invalid.",
            None,
            response.status_code,
        )

    if isinstance(exc, drf_exceptions.AuthenticationFailed):
        return _envelope("UNAUTHORIZED", str(exc.detail), None, response.status_code)

    if isinstance(exc, drf_exceptions.PermissionDenied):
        return _envelope(
            "FORBIDDEN",
            str(exc.detail) if exc.detail else "You do not have permission to perform this action.",
            None,
            response.status_code,
        )

    if isinstance(exc, drf_exceptions.NotFound):
        return _envelope("RESOURCE_NOT_FOUND", "The requested resource was not found.", None, status.HTTP_404_NOT_FOUND)

    if isinstance(exc, drf_exceptions.Throttled):
        wait = getattr(exc, "wait", None)
        message = "Too many requests. Please slow down."
        if wait:
            message = f"Too many requests. Try again in {int(wait)} seconds."
        return _envelope("RATE_LIMITED", message, None, status.HTTP_429_TOO_MANY_REQUESTS)

    # Domain APIExceptions (apps.core.exceptions) carry their own code/detail.
    code = exc.get_codes()
    if isinstance(code, (list, tuple)):
        code = code[0] if code else "ERROR"
    if isinstance(code, dict):
        code = "VALIDATION_ERROR"
    detail = exc.detail
    message = detail if isinstance(detail, str) else "; ".join(map(str, getattr(detail, "__iter__", lambda: [])() or [detail]))
    return _envelope(str(code).upper(), str(message), None, response.status_code)
