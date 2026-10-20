"""Attach a safe correlation id and initialize per-request audit context."""
import re
import secrets

from django.conf import settings
from django.http.request import split_domain_port, validate_host

from .client_ip import is_trusted_proxy
from .request_context import reset_request_context, set_request_context

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,80}$")
_HEALTH_PATHS = frozenset({"/api/health/", "/api/health/live/", "/api/health/ready/"})


def _first_concrete_allowed_host(allowed_hosts) -> str:
    for entry in allowed_hosts or ():
        candidate = str(entry or "").strip().lstrip(".")
        if candidate and candidate != "*":
            return candidate
    return ""


class RequestContextMiddleware:
    """Correlate API/audit rows without trusting arbitrary header values."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path_info or ""
        # Orchestrators (Docker HEALTHCHECK, Railway, Fly.io, Render, K8s/ALB)
        # often probe /api/health/* from a trusted loopback/private address
        # using an internal IP or platform probe hostname while ALLOWED_HOSTS
        # stays restricted to public API domains. Rewrite HTTP_HOST strictly
        # for read-only health probes from trusted peers only.
        if (
            path in _HEALTH_PATHS
            and request.method in {"GET", "HEAD"}
            and is_trusted_proxy(request.META.get("REMOTE_ADDR", ""))
        ):
            allowed = list(getattr(settings, "ALLOWED_HOSTS", ()) or ())
            raw_host = request.META.get("HTTP_HOST") or ""
            domain, _ = split_domain_port(raw_host)
            if allowed and (not domain or not validate_host(domain, allowed)):
                fallback = _first_concrete_allowed_host(allowed)
                if fallback:
                    request.META["HTTP_HOST"] = fallback

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
            if path.startswith("/api/") or path in {"/django-admin", "/django-admin/"} or path.startswith("/django-admin/"):
                response["Cache-Control"] = "private, no-store, max-age=0, must-revalidate"
                response["Pragma"] = "no-cache"
                response["Expires"] = "0"
                response["Surrogate-Control"] = "no-store"
            return response
        finally:
            # Restore the prior context even when a view or exception handler fails.
            reset_request_context(token)
