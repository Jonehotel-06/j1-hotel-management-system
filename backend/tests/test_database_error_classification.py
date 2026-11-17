"""The API edge must not call a broken deployment "the database is unavailable".

Reported symptom
----------------
Eleven unrelated admin endpoints (inventory items/units/locations, Mini Mart
products/categories/sales, amenities stock/issues, breakfast config, reports
approvals, room types and the booking-extension quote) all answered::

    HTTP 503  {"code": "DATABASE_UNAVAILABLE",
               "message": "The database is temporarily unavailable. Please
                           try again."}

They shared no feature code, only the exception handler. The database was in
fact reachable and serving every other endpoint; the code simply expected
columns and tables that the deployed schema did not have yet
(``inventory_stocklocation.is_reception``, ``inventory_stockunit``,
``guest_services_minimartcategory``).

Django raises ``ProgrammingError`` for that, and because
``ProgrammingError`` subclasses ``DatabaseError`` — and the handler tested
``isinstance(exc, DatabaseError)`` before anything else — a permanent schema
fault was dressed up as a transient outage. Staff retried forever.

What these tests pin down
-------------------------
1. A schema error is reported as 500 DATABASE_SCHEMA_ERROR, not 503.
2. A genuine connectivity failure is still 503 DATABASE_UNAVAILABLE.
3. An integrity violation stays a 500 and is never reported as an outage.
4. Response bodies never leak SQL, table or column names.
5. The readiness probe surfaces the missing table/column by name.
6. ``check_schema`` exits non-zero for drift and zero when current.
"""
from __future__ import annotations

import io
from unittest import mock

from django.db import DataError
from django.db.utils import IntegrityError, OperationalError, ProgrammingError
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIRequestFactory, force_authenticate
from rest_framework.views import APIView

from apps.accounts.models import User
from apps.core.exception_handler import jone_exception_handler
from apps.core.health import schema_drift_report


def _call(exc, *, path="/api/admin/inventory/units/"):
    """Run the real exception handler exactly as DRF would."""
    request = APIRequestFactory().get(path)
    view = APIView()
    return jone_exception_handler(exc, {"request": request, "view": view})


class DatabaseErrorClassificationTests(SimpleTestCase):
    def test_missing_column_is_a_schema_error_not_an_outage(self):
        exc = ProgrammingError("(1054, \"Unknown column 'inventory_stocklocation.is_reception' in 'field list'\")")
        response = _call(exc)
        self.assertEqual(response.status_code, 500, response.data)
        self.assertEqual(response.data["code"], "DATABASE_SCHEMA_ERROR")
        self.assertEqual(response.data["category"], "DATABASE")
        self.assertFalse(response.data["success"])
        # The operator-facing remedy is named; no SQL is disclosed.
        self.assertIn("migration", response.data["message"].lower())
        body = str(response.data)
        self.assertNotIn("is_reception", body)
        self.assertNotIn("Unknown column", body)
        self.assertNotIn("1054", body)

    def test_missing_table_is_a_schema_error(self):
        exc = ProgrammingError('(1146, "Table \'jone.inventory_stockunit\' doesn\'t exist")')
        response = _call(exc)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.data["code"], "DATABASE_SCHEMA_ERROR")
        self.assertNotIn("inventory_stockunit", str(response.data))

    def test_connectivity_failure_is_still_a_503_outage(self):
        response = _call(OperationalError("(2003, \"Can't connect to MySQL server on 'db' (111)\")"))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["code"], "DATABASE_UNAVAILABLE")
        self.assertNotIn("MySQL", str(response.data))

    def test_deadlock_or_lock_timeout_is_still_a_503_outage(self):
        response = _call(OperationalError("(1205, 'Lock wait timeout exceeded; try restarting transaction')"))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["code"], "DATABASE_UNAVAILABLE")

    def test_integrity_error_is_never_reported_as_an_outage(self):
        response = _call(IntegrityError("duplicate key value violates unique constraint"))
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.data["code"], "DATABASE_INTEGRITY_ERROR")
        self.assertNotIn("constraint", str(response.data))

    def test_data_error_stays_a_400_validation_error(self):
        response = _call(DataError("value too long for type character varying(20)"))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "VALIDATION_ERROR")

    def test_every_error_envelope_carries_a_request_id_for_log_correlation(self):
        response = _call(ProgrammingError("no such column: x"))
        self.assertTrue(response.data.get("request_id"))
        self.assertEqual(response["X-Request-ID"], response.data["request_id"])

    # -- engine-specific drift detection ---------------------------------- #
    # The class alone is NOT enough to tell drift from an outage: MySQL raises
    # ProgrammingError for a missing table/column, but SQLite raises
    # OperationalError — the very class it uses for "server has gone away".
    # Class-only detection therefore reported schema drift as a 503 outage on
    # SQLite (found by pointing the repaired API at a database with
    # inventory.0017 and guest_services.0010 unapplied). These pin both engines.

    def test_sqlite_missing_column_is_drift_not_an_outage(self):
        response = _call(OperationalError("no such column: inventory_stocklocation.is_reception"))
        self.assertEqual(response.status_code, 500, response.data)
        self.assertEqual(response.data["code"], "DATABASE_SCHEMA_ERROR")
        self.assertNotIn("is_reception", str(response.data))

    def test_sqlite_missing_table_is_drift_not_an_outage(self):
        response = _call(OperationalError("no such table: guest_services_minimartcategory"))
        self.assertEqual(response.status_code, 500, response.data)
        self.assertEqual(response.data["code"], "DATABASE_SCHEMA_ERROR")
        self.assertNotIn("guest_services_minimartcategory", str(response.data))

    def test_mysql_missing_table_is_drift_not_an_outage(self):
        exc = OperationalError('(1146, "Table \'jone.inventory_stockunit\' doesn\'t exist")')
        response = _call(exc)
        self.assertEqual(response.status_code, 500, response.data)
        self.assertEqual(response.data["code"], "DATABASE_SCHEMA_ERROR")

    def test_postgres_missing_relation_is_drift(self):
        exc = OperationalError('relation "inventory_stockunit" does not exist')
        response = _call(exc)
        self.assertEqual(response.status_code, 500, response.data)
        self.assertEqual(response.data["code"], "DATABASE_SCHEMA_ERROR")

    def test_real_outages_never_look_like_drift(self):
        """The narrowness matters more than the match: no false positives."""
        outage_messages = [
            "(2003, \"Can't connect to MySQL server on 'db' (111)\")",
            "(2006, 'MySQL server has gone away')",
            "(2013, 'Lost connection to MySQL server during query')",
            "(1205, 'Lock wait timeout exceeded; try restarting transaction')",
            "(1213, 'Deadlock found when trying to get lock; try restarting transaction')",
            "database table is locked",
            "database is locked",
            "(1040, 'Too many connections')",
        ]
        for message in outage_messages:
            with self.subTest(message=message):
                response = _call(OperationalError(message))
                self.assertEqual(response.status_code, 503, response.data)
                self.assertEqual(response.data["code"], "DATABASE_UNAVAILABLE")


class SchemaDriftDetectionTests(TestCase):
    def test_drift_report_is_clean_for_a_fully_migrated_database(self):
        report = schema_drift_report()
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["missing_tables"], [])
        self.assertEqual(report["missing_columns"], [])
        self.assertGreater(report["checked_models"], 100)

    def test_drift_report_names_the_missing_column(self):
        """A dropped column must be reported by table.column, not swallowed."""
        from django.db import connection

        real_introspection = connection.introspection

        class PartialIntrospection:
            def __getattr__(self, name):
                return getattr(real_introspection, name)

            def get_table_description(self, cursor, table):
                described = real_introspection.get_table_description(cursor, table)
                if table == "inventory_stocklocation":
                    return [c for c in described if c.name != "is_reception"]
                return described

        with mock.patch.object(connection, "introspection", PartialIntrospection()):
            report = schema_drift_report()
        self.assertFalse(report["ok"])
        self.assertIn(("inventory_stocklocation", "is_reception"), report["missing_columns"])

    def test_drift_report_names_a_missing_table(self):
        from django.db import connection

        real_introspection = connection.introspection

        class HiddenTableIntrospection:
            def __getattr__(self, name):
                return getattr(real_introspection, name)

            def table_names(self, *args, **kwargs):
                names = real_introspection.table_names(*args, **kwargs)
                return [n for n in names if n != "inventory_stockunit"]

        with mock.patch.object(connection, "introspection", HiddenTableIntrospection()):
            report = schema_drift_report()
        self.assertFalse(report["ok"])
        self.assertIn("inventory_stockunit", report["missing_tables"])


class CheckSchemaCommandTests(TestCase):
    def test_command_exits_zero_on_a_current_schema(self):
        from django.core.management import call_command

        out = io.StringIO()
        call_command("check_schema", stdout=out)
        self.assertIn("Schema OK", out.getvalue())

    def test_command_exits_non_zero_and_names_the_drift(self):
        from django.core.management import call_command

        drift = {
            "ok": False,
            "checked_models": 10,
            "checked_tables": 10,
            "missing_tables": ["inventory_stockunit"],
            "missing_columns": [("inventory_stocklocation", "is_reception")],
            "error": "",
        }
        err = io.StringIO()
        with mock.patch(
            "apps.core.management.commands.check_schema.schema_drift_report",
            return_value=drift,
        ):
            with self.assertRaises(SystemExit) as raised:
                call_command("check_schema", stderr=err)
        self.assertEqual(raised.exception.code, 1)
        output = err.getvalue()
        self.assertIn("inventory_stockunit", output)
        self.assertIn("inventory_stocklocation.is_reception", output)
        self.assertIn("migrate --noinput", output)
