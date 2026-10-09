"""Attach a safe correlation id and initialize per-request audit context."""
import re
import secrets

from .request_context import reset_request_context, set_request_context

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,80}$")


class RequestContextMiddleware:
    """Correlate API/audit rows without trusting arbitrary header values."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        supplied = (request.META.get("HTTP_X_REQUEST_ID") or "").strip()
        request_id = supplied if _REQUEST_ID_RE.fullmatch(supplied) else secrets.token_hex(16)
        request.request_id = request_id
        token = set_request_context({"request_id": request_id, "terminal": None, "user": None})
        try:
            response = self.get_response(request)
            response["X-Request-ID"] = request_id
            # API responses can contain identity, guest, booking, stay, HR, or
            # financial data. Mark every API outcome non-storable so private
            # browser caches and shared intermediaries cannot replay it across
            # sessions. Django admin pages also carry privileged account data.
            path = request.path_info or ""
            if path.startswith("/api/") or path in {"/django-admin", "/django-admin/"} or path.startswith("/django-admin/"):
                response["Cache-Control"] = "private, no-store, max-age=0, must-revalidate"
                response["Pragma"] = "no-cache"
                response["Expires"] = "0"
                response["Surrogate-Control"] = "no-store"
            return response
        finally:
            # Restore the prior context even when a view or exception handler fails.
            reset_request_context(token)
