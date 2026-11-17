"""Report whether every model in the project has its table and columns.

Why this exists
---------------
The production incident behind this repair was a *deployment ordering* fault,
not a database fault: the application code expected
``inventory_stocklocation.is_reception``, ``inventory_stockunit`` and
``guest_services_minimartcategory`` but the Northflank MySQL database had never
had those migrations applied. Every affected endpoint raised
``ProgrammingError`` ("no such column ..."), which the API edge reported as
"the database is temporarily unavailable" (HTTP 503) — a transient-sounding
message for a permanent state, which is why the same failures kept coming back
after every "retry".

``migrate --check`` answers "is there a migration FILE I have not applied?".
It cannot answer "does the live schema actually match the models?" — a database
can have every migration recorded as applied while a table is still missing
(an interrupted migration, a restored partial dump, a manually dropped column).
This command answers that second question directly and exits non-zero, so it is
safe to run:

    * as a pre-flight gate in the deployment pipeline (after ``migrate``),
    * inside a readiness probe (``python manage.py check_schema``),
    * by an operator who is staring at a 500/503 on one screen.

It performs only read-only introspection. It never writes, migrates, or
changes data.
"""
from __future__ import annotations

from django.apps import apps
from django.core.management.base import BaseCommand
from apps.core.health import schema_drift_report


class Command(BaseCommand):
    help = (
        "Verify that every model table and column exists in the connected "
        "database (detects unapplied migrations / restored partial schemas). "
        "Read-only. Exits 1 when the schema is behind the code."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--database",
            default="default",
            help="Database alias to inspect (default: default).",
        )
        parser.add_argument(
            "--quiet",
            action="store_true",
            help="Print nothing when the schema is current.",
        )

    def handle(self, *args, **options):
        alias = options["database"]
        report = schema_drift_report(alias=alias, apps_registry=apps)

        if report["ok"]:
            if not options["quiet"]:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Schema OK on '{alias}': {report['checked_models']} model(s), "
                        f"{report['checked_tables']} table(s) verified."
                    )
                )
            return

        self.stderr.write(
            self.style.ERROR(
                f"SCHEMA DRIFT on '{alias}': {len(report['missing_tables'])} missing "
                f"table(s), {len(report['missing_columns'])} missing column(s). "
                "The deployed code expects a schema this database does not have."
            )
        )
        for table in report["missing_tables"]:
            self.stderr.write(f"  missing table : {table}")
        for table, column in report["missing_columns"]:
            self.stderr.write(f"  missing column: {table}.{column}")

        self.stderr.write("")
        self.stderr.write(
            self.style.WARNING(
                "Fix: run `python manage.py migrate --noinput` against this "
                "database (take a backup first), then re-run this command. "
                "No data is modified by this check."
            )
        )
        raise SystemExit(1)
