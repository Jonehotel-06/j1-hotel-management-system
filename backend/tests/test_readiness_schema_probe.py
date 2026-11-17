"""The readiness probe must distinguish an outage from an out-of-date schema.

Before this repair, ``/api/health/ready/`` reported ``{"database": "up"}`` on a
deployment whose schema was months behind the code. The orchestrator was happy,
the load balancer kept sending traffic, and eleven screens answered 503 — while
``SELECT 1`` (all the probe checked) kept succeeding.

The probe now also reports the schema state and names the exact missing
table/column, and ``?strict=1`` turns drift into a 503 for operators who want
the deployment gate to fail.
"""
from __future__ import annotations

import json
from unittest import mock

from django.test import TestCase


class ReadinessProbeTests(TestCase):
    def test_liveness_touches_nothing(self):
        response = self.client.get("/api/health/live/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content)["status"], "ok")

    def test_default_body_is_the_established_contract(self):
        """Existing orchestrator probes must keep getting exactly this body."""
        response = self.client.get("/api/health/ready/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            json.loads(response.content),
            {"success": True, "status": "ok", "database": "up"},
        )

    def test_schema_detail_is_available_on_request_and_current(self):
        response = self.client.get("/api/health/ready/?schema=1")
        self.assertEqual(response.status_code, 200)
        body = json.loads(response.content)
        self.assertTrue(body["success"])
        self.assertEqual(body["database"], "up")
        self.assertIn("schema", body)
        self.assertTrue(body["schema"]["ok"], body["schema"])

    def test_schema_query_names_the_drift_without_failing_the_probe(self):
        drift = {
            "ok": False,
            "checked_models": 149,
            "checked_tables": 149,
            "missing_tables": ["inventory_stockunit"],
            "missing_columns": [("inventory_stocklocation", "is_reception")],
            "error": "",
        }
        with mock.patch("apps.core.health.schema_drift_report", return_value=drift), \
             mock.patch("apps.core.health.pending_migration_count", return_value=2):
            response = self.client.get("/api/health/ready/?schema=1")
        # Still serving: a partial deploy must not remove every replica.
        self.assertEqual(response.status_code, 200)
        body = json.loads(response.content)
        self.assertEqual(body["database"], "up")
        self.assertFalse(body["schema"]["ok"])
        self.assertIn("inventory_stockunit", body["schema"]["missing_tables"])
        self.assertIn("inventory_stocklocation.is_reception",
                      body["schema"]["missing_columns"])
        self.assertEqual(body["schema"]["pending_migrations"], 2)

    def test_strict_mode_turns_drift_into_a_503(self):
        drift = {
            "ok": False, "checked_models": 149, "checked_tables": 149,
            "missing_tables": ["inventory_stockunit"], "missing_columns": [], "error": "",
        }
        with mock.patch("apps.core.health.schema_drift_report", return_value=drift), \
             mock.patch("apps.core.health.pending_migration_count", return_value=1):
            response = self.client.get("/api/health/ready/?strict=1")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(json.loads(response.content)["status"], "schema-out-of-date")

    def test_a_dead_database_is_still_a_503(self):
        with mock.patch("apps.core.views._database_is_available", return_value=False):
            response = self.client.get("/api/health/ready/")
        self.assertEqual(response.status_code, 503)
        body = json.loads(response.content)
        self.assertFalse(body["success"])
        self.assertEqual(body["database"], "down")

    def test_legacy_health_endpoint_keeps_its_contract(self):
        response = self.client.get("/api/health/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(json.loads(response.content)["success"])

    def test_a_broken_schema_check_never_breaks_the_probe(self):
        with mock.patch("apps.core.health.schema_drift_report", side_effect=RuntimeError("boom")):
            response = self.client.get("/api/health/ready/?schema=1")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(json.loads(response.content)["success"])


class ApiRevisionHeaderTests(TestCase):
    def test_revision_is_echoed_on_api_responses_only(self):
        with self.settings(API_REVISION="2026-11-17.1"):
            response = self.client.get("/api/health/live/")
        self.assertEqual(response["X-API-Revision"], "2026-11-17.1")

    def test_no_header_when_unset(self):
        with self.settings(API_REVISION=""):
            response = self.client.get("/api/health/live/")
        self.assertNotIn("X-API-Revision", response)
