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

    def test_head_probes_succeed_for_load_balancers_and_uptime_monitors(self):
        self.assertEqual(self.client.head("/api/health/live/").status_code, 200)
        self.assertEqual(self.client.head("/api/health/ready/").status_code, 200)
        self.assertEqual(self.client.head("/api/health/").status_code, 200)
        with patch("apps.core.views._database_is_available", return_value=False):
            self.assertEqual(self.client.head("/api/health/ready/").status_code, 503)

    def test_internal_http_health_probes_bypass_ssl_redirect_while_api_redirects(self):
        from django.test import override_settings

        with override_settings(
            SECURE_SSL_REDIRECT=True,
            SECURE_REDIRECT_EXEMPT=[r"^api/health/(?:live/|ready/)?$"],
        ):
            self.assertEqual(self.client.get("/api/health/live/").status_code, 200)
            self.assertEqual(self.client.get("/api/health/ready/").status_code, 200)
            self.assertEqual(self.client.get("/api/health/").status_code, 200)
            redirected = self.client.get("/api/")
            self.assertEqual(redirected.status_code, 301)
            self.assertTrue(redirected["Location"].startswith("https://"))

    def test_trusted_proxy_health_probe_allows_internal_host_without_relaxing_allowed_hosts(self):
        from django.test import override_settings

        with override_settings(ALLOWED_HOSTS=["api.jonehotel.test"]):
            # Internal orchestrator probe from trusted private network succeeds:
            probe = self.client.get(
                "/api/health/ready/",
                HTTP_HOST="healthcheck.internal:8000",
                REMOTE_ADDR="10.0.1.9",
            )
            self.assertEqual(probe.status_code, 200)

            # Non-health endpoints still reject unrecognized Host headers even via proxy:
            rejected_api = self.client.get(
                "/api/",
                HTTP_HOST="healthcheck.internal:8000",
                REMOTE_ADDR="10.0.1.9",
            )
            self.assertEqual(rejected_api.status_code, 400)

            # External untrusted peers cannot spoof Host on health probes either:
            rejected_external = self.client.get(
                "/api/health/ready/",
                HTTP_HOST="evil.example",
                REMOTE_ADDR="198.51.100.25",
            )
            self.assertEqual(rejected_external.status_code, 400)

    def test_mysql8_text_db_default_compat_emits_modify_column_only_for_oracle_mysql(self):
        from unittest.mock import MagicMock
        from django.db import models
        from django.db.backends.mysql.schema import DatabaseSchemaEditor

        old_field = models.TextField()
        old_field.set_attributes_from_name("body")
        new_field = models.TextField(db_default="")
        new_field.set_attributes_from_name("body")

        editor = DatabaseSchemaEditor.__new__(DatabaseSchemaEditor)
        editor.connection = MagicMock(
            mysql_is_mariadb=False,
            data_types={"TextField": "longtext"},
            data_type_check_constraints={},
        )
        editor.quote_name = lambda name: f"`{name}`"
        editor.db_default_sql = lambda field: ("(%s)", [""])

        sql, params = editor._alter_column_database_default_sql(
            None, old_field, new_field, drop=False
        )
        self.assertEqual(sql, "MODIFY COLUMN `body` longtext NOT NULL DEFAULT (%s)")
        self.assertEqual(params, [""])

        drop_sql, drop_params = editor._alter_column_database_default_sql(
            None, old_field, new_field, drop=True
        )
        self.assertEqual(drop_sql, "MODIFY COLUMN `body` longtext NOT NULL")
        self.assertEqual(drop_params, [])


