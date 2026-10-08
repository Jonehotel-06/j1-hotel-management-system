"""Regression tests for deployment-facing liveness and readiness probes."""
from unittest.mock import patch

from django.db import DatabaseError

from tests.base import BaseAPITestCase


class HealthProbeTests(BaseAPITestCase):
    def test_liveness_is_public_and_never_touches_the_database(self):
        with patch("apps.core.views.connection.cursor") as cursor:
            response = self.client.get("/api/health/live/")

        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(response.json(), {"success": True, "status": "ok"})
        cursor.assert_not_called()

    def test_readiness_checks_database_and_reports_up(self):
        response = self.client.get("/api/health/ready/")

        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(
            response.json(),
            {"success": True, "status": "ok", "database": "up"},
        )

    def test_readiness_returns_retryable_503_when_database_is_unavailable(self):
        with patch("apps.core.views.connection.cursor", side_effect=DatabaseError("unavailable")):
            response = self.client.get("/api/health/ready/")

        self.assertEqual(response.status_code, 503, response.json())
        self.assertEqual(
            response.json(),
            {"success": False, "status": "degraded", "database": "down"},
        )

    def test_legacy_health_endpoint_remains_a_readiness_alias(self):
        with patch("apps.core.views._database_is_available", return_value=False):
            response = self.client.get("/api/health/")

        self.assertEqual(response.status_code, 503, response.json())
        self.assertEqual(response.json()["database"], "down")

    def test_api_index_advertises_distinct_probe_endpoints(self):
        response = self.client.get("/api/")

        self.assertEqual(response.status_code, 200, response.json())
        data = response.json()["data"]
        self.assertEqual(data["liveness"], "/api/health/live/")
        self.assertEqual(data["readiness"], "/api/health/ready/")
