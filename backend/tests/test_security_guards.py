"""Security guard coverage for the audit's SEC-005 gap and SEC-02's policy.

The audit recorded SEC-005 ("rate limiting, refresh-token revocation, CSRF and
dependency scanning") as NOT TESTED, and SEC-002/SEC-003 as failing before the
repair. Refresh-token revocation is covered in ``tests/test_auth.py``
(``test_token_refresh_rotates_and_old_refresh_is_blacklisted`` and
``test_logout_blacklists_refresh_token``); this module covers the rest:

* rate limiting actually refuses the request that exceeds the budget (429) and
  does so per scope rather than as a global block;
* the API cannot be driven by a session cookie, so there is no cookie-session
  CSRF surface to exploit;
* the production settings refuse a wildcard CORS origin instead of trusting it.

The last group are configuration guards: they read the settings source rather
than importing ``config.settings.production`` (which deliberately refuses to
boot without a real database and secret key). They pin the policy; they do not
prove what the deployed host serves, which remains unverified.
"""

import pathlib
import re
from datetime import timedelta

from django.conf import settings
from django.test import SimpleTestCase
from rest_framework.throttling import SimpleRateThrottle

from apps.accounts.models import User

from .base import BaseAPITestCase
from .factories import make_staff

BACKEND = pathlib.Path(__file__).resolve().parent.parent


class ThrottleEnforcementTests(BaseAPITestCase):
    def test_login_is_rate_limited_once_the_budget_is_spent(self):
        """The 6th sign-in attempt inside the window is refused with 429.

        The login scope is 5/min. BaseAPITestCase clears the cache per test, so
        the counter starts clean and the assertion is deterministic.
        """
        url = "/api/auth/login/"
        payload = {"email": "throttle-probe@example.invalid", "password": "not-a-real-password"}
        codes = [self.client.post(url, payload, format="json").status_code for _ in range(6)]
        self.assertEqual(codes[:5], [401] * 5, codes)
        self.assertEqual(codes[5], 429, codes)
        throttled = self.client.post(url, payload, format="json")
        self.assertEqual(throttled.status_code, 429)
        # A client can act on the refusal: the wait is advertised.
        self.assertIn("Retry-After", throttled.headers)

    def test_exhausting_one_scope_does_not_block_other_endpoints(self):
        """Throttles are per scope, not a global lockout for an IP."""
        payload = {"email": "throttle-scope@example.invalid", "password": "not-a-real-password"}
        for _ in range(6):
            self.client.post("/api/auth/login/", payload, format="json")
        self.assertEqual(
            self.client.post("/api/auth/login/", payload, format="json").status_code,
            429,
            "the login scope must be exhausted before this assertion means anything",
        )
        # Public content on a different scope keeps working.
        self.assertNotEqual(self.client.get("/api/hotel/").status_code, 429)

    def test_the_configured_rate_is_what_is_enforced(self):
        """A tighter configured rate takes effect - the limit is not hard-coded.

        ``SimpleRateThrottle`` snapshots ``api_settings.DEFAULT_THROTTLE_RATES``
        into a class attribute at import time, so ``override_settings`` cannot
        reach it; the test mutates that mapping directly and restores it.
        """
        rates = SimpleRateThrottle.THROTTLE_RATES
        original = rates["login"]
        rates["login"] = "2/min"
        try:
            payload = {"email": "throttle-rate@example.invalid", "password": "not-a-real-password"}
            codes = [self.client.post("/api/auth/login/", payload, format="json").status_code for _ in range(3)]
            self.assertEqual(codes, [401, 401, 429], codes)
        finally:
            rates["login"] = original


class SessionCookieIsNotAnApiCredentialTests(BaseAPITestCase):
    """No session authenticator is configured, so a cookie cannot drive the API.

    This is why the API has no cookie-session CSRF surface: every state-changing
    endpoint requires a bearer token that a cross-site form post cannot supply.
    """

    def setUp(self):
        super().setUp()
        self.manager = make_staff("cookie-manager@staff.dev", role=User.Role.MANAGER)

    def test_a_django_session_cookie_does_not_authenticate_an_api_read(self):
        self.client.force_login(self.manager)          # sets a real session cookie
        response = self.client.get("/api/admin/bookings/")
        self.assertIn(response.status_code, (401, 403), response.content)

    def test_a_django_session_cookie_does_not_authenticate_an_api_write(self):
        self.client.force_login(self.manager)
        response = self.client.post("/api/admin/rooms/", {"number": "COOKIE-1"}, format="json")
        self.assertIn(response.status_code, (401, 403), response.content)

    def test_a_bearer_token_does_authenticate_the_same_call(self):
        """The negative result above is not just "the endpoint is broken"."""
        self.auth(self.manager)
        self.assertEqual(self.client.get("/api/admin/bookings/").status_code, 200)


class SettingsPolicyGuardTests(BaseAPITestCase):
    """Static guards over the settings that harden a deployed environment."""

    def test_production_refuses_a_wildcard_cors_origin(self):
        source = (BACKEND / "config" / "settings" / "production.py").read_text()
        self.assertIn("CORS_ALLOW_ALL_ORIGINS = False", source)
        # Both the environment override and a literal '*' in the allow-list are
        # refused at boot rather than trusted (audit SEC-02).
        self.assertRegex(source, r"if config\(\"CORS_ALLOW_ALL_ORIGINS\"[\s\S]{0,120}raise RuntimeError")
        self.assertIn('if "*" in CORS_ALLOWED_ORIGINS:', source)
        wildcard_guard = source.split('if "*" in CORS_ALLOWED_ORIGINS:', 1)[1]
        self.assertIn("raise RuntimeError", wildcard_guard[:200])

    def test_production_requires_https_and_pins_browser_security_headers(self):
        source = (BACKEND / "config" / "settings" / "production.py").read_text()
        for requirement in (
            "SECURE_SSL_REDIRECT",
            "SECURE_HSTS_SECONDS",
            "SECURE_CONTENT_TYPE_NOSNIFF",
            "SESSION_COOKIE_SECURE",
            "CSRF_COOKIE_SECURE",
            "X_FRAME_OPTIONS",
        ):
            self.assertIn(requirement, source, f"{requirement} must stay configured in production")

    def test_no_session_authenticator_is_enabled(self):
        source = (BACKEND / "config" / "settings" / "base.py").read_text()
        block = re.search(r"DEFAULT_AUTHENTICATION_CLASSES[\s\S]{0,400}?\),\s*\n\s*\"DEFAULT_PERMISSION_CLASSES\"", source)
        self.assertIsNotNone(block, "the authentication classes must stay declared explicitly")
        self.assertNotIn("SessionAuthentication", block.group(0))
        self.assertNotIn("BasicAuthentication", block.group(0))

    def test_every_sensitive_throttle_scope_has_a_finite_rate(self):
        rates = settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]
        required = {
            "login", "register", "password_reset", "booking_create",
            "payment_init", "payment_verify", "portal_access_request", "portal_access_consume",
        }
        missing = sorted(scope for scope in required if not rates.get(scope))
        self.assertEqual(missing, [], "sensitive scopes must not be left unlimited")
        for scope, rate in rates.items():
            self.assertIsNotNone(rate, f"{scope} must have a concrete rate")
            self.assertRegex(rate, r"^\d+/(s|min|hour|day)$", f"{scope} rate {rate!r} must be finite")


class TokenPolicyGuardTests(SimpleTestCase):
    """SEC-005: revocation is only meaningful while tokens stay short-lived.

    Logout blacklists the refresh token (covered in `test_auth.py`), but a
    stateless access token cannot be revoked before it expires - so the access
    lifetime *is* the revocation window. These guards stop that window from being
    widened silently, and stop rotation/blacklisting from being switched off.
    """

    def test_refresh_rotation_and_blacklisting_stay_enabled(self):
        simplejwt = settings.SIMPLE_JWT
        self.assertTrue(simplejwt["ROTATE_REFRESH_TOKENS"], "refresh rotation was switched off")
        self.assertTrue(simplejwt["BLACKLIST_AFTER_ROTATION"], "rotated refresh tokens are no longer blacklisted")
        self.assertIn(
            "rest_framework_simplejwt.token_blacklist", settings.INSTALLED_APPS,
            "the blacklist app provides the storage logout depends on",
        )

    def test_token_lifetimes_stay_bounded(self):
        simplejwt = settings.SIMPLE_JWT
        access = simplejwt["ACCESS_TOKEN_LIFETIME"]
        refresh = simplejwt["REFRESH_TOKEN_LIFETIME"]
        self.assertLessEqual(
            access, timedelta(minutes=30),
            f"a {access} access token is a {access} revocation window - logout cannot end it sooner",
        )
        self.assertLessEqual(
            refresh, timedelta(days=7),
            f"a {refresh} refresh token outlives any plausible rotation policy",
        )
        self.assertLess(access, refresh)
