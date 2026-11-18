"""Batch 6 — the container entrypoint applies migrations safely.

Docker itself is not available inside this test environment, so the tests
verify the entrypoint contract directly (the same file the image runs) plus
Django's own migration state. Each assertion maps to one of the deployment
safety rules.
"""
import os
import re
import subprocess
from pathlib import Path

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

ENTRYPOINT = Path(__file__).resolve().parent.parent / "docker" / "docker-cmd.sh"
COMPOSE = Path(__file__).resolve().parent.parent.parent / "compose.yaml"


class EntrypointContractTests(SimpleTestCase):
    def setUp(self):
        self.script = ENTRYPOINT.read_text(encoding="utf-8")
        # Strip comment lines: the rules are asserted on EXECUTABLE lines so a
        # comment can never make a forbidden command look present or absent.
        self.code = "\n".join(
            line for line in self.script.splitlines()
            if not line.lstrip().startswith("#")
        )

    def test_entrypoint_is_executable_shell(self):
        self.assertTrue(ENTRYPOINT.exists())
        self.assertTrue(os.access(ENTRYPOINT, os.X_OK), "entrypoint must be executable")

    def test_migrations_run_on_startup_for_the_web_role(self):
        self.assertIn("manage.py migrate --noinput", self.code)
        # Migrations are applied before Gunicorn is exec'd.
        self.assertLess(self.code.index("run_migrations"), self.code.index("gunicorn config.wsgi"))

    def test_script_fails_fast_so_a_failed_migration_prevents_startup(self):
        self.assertTrue(self.script.startswith("#!/bin/sh"))
        self.assertIn("set -eu", self.code)
        self.assertIn("Refusing to start the application server", self.script)
        self.assertIn("exit 1", self.code)

    def test_never_runs_destructive_management_commands(self):
        for forbidden in ("makemigrations", "flush", "loaddata", "dropdb", "reset_db"):
            self.assertNotIn(forbidden, self.code,
                             f"the entrypoint must never run {forbidden} in production")

    def test_waits_for_the_database_before_migrating(self):
        self.assertIn("wait_for_database", self.code)
        self.assertLess(self.code.index("wait_for_database"), self.code.index("manage.py migrate"))
        self.assertIn("--noinput", self.code)

    def test_only_the_web_role_migrates_so_replicas_cannot_race(self):
        """worker/beat branches must not call run_migrations."""
        worker = self.code[self.code.index("    worker)"):self.code.index("    beat|scheduler)")]
        beat = self.code[self.code.index("    beat|scheduler)"):self.code.index("    web|api)")]
        for branch in (worker, beat):
            self.assertNotIn("migrate", branch.replace("--noinput", ""))
        web = self.code[self.code.index("    web|api)"):]
        self.assertIn("run_migrations", web)

    def test_migrations_can_be_disabled_for_external_release_jobs(self):
        self.assertIn("RUN_MIGRATIONS", self.code)

    def test_database_credentials_are_never_echoed(self):
        for leak in ("DB_PASSWORD", "DATABASE_URL", "$DB_USER", "$DB_HOST"):
            self.assertNotIn(f'echo "{leak}', self.code)
            self.assertNotIn(f"echo ${leak}", self.code)
        self.assertNotIn("printenv", self.code)


class MigrationStateTests(TestCase):
    """Every app is fully migrated with the new migrations applied cleanly."""

    def test_no_pending_migrations_anywhere(self):
        out = subprocess.run(
            ["python", "manage.py", "makemigrations", "--check", "--dry-run"],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True, text=True,
            env={**os.environ, "DJANGO_SETTINGS_MODULE": "config.settings.development"},
        )
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("No changes detected", out.stdout)

    def test_event_locations_are_seeded_by_migration(self):
        from apps.events.models import EventLocation
        codes = set(EventLocation.objects.values_list("code", flat=True))
        self.assertEqual(codes, {"INSIDE_BAR", "OUTSIDE_BAR", "HOTEL_COMPOUND"})

    def test_pos_tender_accepts_paystack(self):
        from apps.pos.models import PosTender
        self.assertIn("PAYSTACK", PosTender.Method.values)


class ComposeContractTests(SimpleTestCase):
    def test_api_service_waits_for_a_healthy_database(self):
        text = COMPOSE.read_text(encoding="utf-8")
        self.assertIn("SELECT 1;", text)
        self.assertIn("healthcheck", text)
        self.assertIn("depends_on", text)
        self.assertIn("service_healthy", text)

    def test_worker_does_not_start_before_the_api_is_healthy(self):
        text = COMPOSE.read_text(encoding="utf-8")
        # The worker must not race the web container's migration run.
        worker_block = text[text.index("worker:"):] if "worker:" in text else ""
        self.assertIn("api", worker_block)
