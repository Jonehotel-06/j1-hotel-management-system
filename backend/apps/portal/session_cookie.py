"""HttpOnly cookie transport for the guest-portal session.

The opaque session value is never readable by page JavaScript. The cookie is:

* ``HttpOnly`` (not exposed to scripts),
* ``Secure`` in production (HTTPS only),
* ``SameSite=Lax`` (not attached to cross-site subresource or form POSTs),
* scoped to ``/api/portal/`` so no other API path receives it,
* persistent (``Max-Age`` = the idle timeout), so closing and reopening the
  browser keeps a returning guest signed in. The server decides validity on every
  request, so the cookie lifetime never overrides checkout or revocation.

Cookie-authenticated, state-changing requests must also carry the
``X-Portal-Request: 1`` header (see ``authentication.py``), which a cross-site
form or simple request cannot send.
"""
from __future__ import annotations

from django.conf import settings

from .services import session_idle_ttl

SESSION_COOKIE_NAME = "jone_portal_session"
SESSION_COOKIE_PATH = "/api/portal/"
CSRF_HEADER_NAME = "X-Portal-Request"
CSRF_HEADER_VALUE = "1"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def cookie_secure() -> bool:
    return bool(getattr(settings, "PORTAL_COOKIE_SECURE", not settings.DEBUG))


def cookie_max_age() -> int:
    return int(session_idle_ttl().total_seconds())


def set_session_cookie(response, raw_token: str, *, max_age: int | None = None):
    response.set_cookie(
        SESSION_COOKIE_NAME,
        raw_token,
        max_age=max_age or cookie_max_age(),
        path=SESSION_COOKIE_PATH,
        secure=cookie_secure(),
        httponly=True,
        samesite="Lax",
    )
    return response


def clear_session_cookie(response):
    response.set_cookie(
        SESSION_COOKIE_NAME,
        "",
        max_age=0,
        expires="Thu, 01 Jan 1970 00:00:00 GMT",
        path=SESSION_COOKIE_PATH,
        secure=cookie_secure(),
        httponly=True,
        samesite="Lax",
    )
    return response
