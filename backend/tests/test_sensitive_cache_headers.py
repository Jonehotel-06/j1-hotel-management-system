"""Prevent browser and intermediary caches from retaining dynamic API data."""
from tests.base import BaseAPITestCase
from tests.factories import make_staff


class SensitiveResponseCacheHeaderTests(BaseAPITestCase):
    def assert_no_store(self, response):
        cache_control = response.get("Cache-Control", "").lower()
        self.assertIn("no-store", cache_control)
        self.assertIn("private", cache_control)
        self.assertEqual(response.get("Pragma"), "no-cache")
        self.assertEqual(response.get("Expires"), "0")
        self.assertEqual(response.get("Surrogate-Control"), "no-store")

    def test_public_and_authenticated_api_responses_are_never_stored(self):
        public = self.client.get("/api/health/live/")
        self.assertEqual(public.status_code, 200, public.content)
        self.assert_no_store(public)

        # Error responses can still contain account-state details and need the
        # same policy as successful JSON responses.
        unauthorized = self.client.get("/api/auth/profile/")
        self.assertEqual(unauthorized.status_code, 401, unauthorized.content)
        self.assert_no_store(unauthorized)

        staff = make_staff("cache.headers@staff.dev")
        self.auth(staff)
        private = self.client.get("/api/auth/capabilities/")
        self.assertEqual(private.status_code, 200, private.content)
        self.assert_no_store(private)

    def test_django_admin_redirects_are_never_stored(self):
        response = self.client.get("/django-admin/")
        self.assertEqual(response.status_code, 302)
        self.assert_no_store(response)
