# apps/core/management/commands/check_database_charset.py
"""Report MySQL character sets for the configured database (read-only).

The 2026-10-18 production incident began with MySQL error 1366: a
database created outside utf8mb4 could not store the "→" in a staff
notification message. Application settings now force a utf8mb4
*connection* and migrations convert the notification/audit tables,
but whether every OTHER pre-existing table already uses utf8mb4 is a
fact about the deployed database that code cannot know in advance.

This command makes that fact visible before anyone converts anything:

    python manage.py check_database_charset

It prints the connection charset Django is configured with, the
database default, and every table whose default collation is NOT
utf8mb4. It changes nothing. Exit status is non-zero when a MySQL
database still has non-utf8mb4 tables, so deployment checks can gate
on it. Converting the remaining tables (after a backup, sized with
this report) is a deliberate, owner-approved operation — see the
deployment notes — not something a booting web service should do
blindly to ~100 tables.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import connection


class Command(BaseCommand):
    help = "Report MySQL database/table charsets and flag non-utf8mb4 tables (read-only)."

    def handle(self, *args, **options):
        self.stdout.write(self.style.MIGRATE_HEADING("Database charset"))
        self.stdout.write(f"  backend : {connection.vendor}")
        options = connection.settings_dict.get("OPTIONS") or {}
        self.stdout.write(
            f"  configured connection charset: {options.get('charset') or '(not set — server default)'}"
        )
        if connection.vendor != "mysql":
            self.stdout.write(
                "  Non-MySQL backend: Django stores text as Unicode here; "
                "no charset conversion applies."
            )
            return

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT DEFAULT_CHARACTER_SET_NAME, DEFAULT_COLLATION_NAME "
                "FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = DATABASE()"
            )
            row = cursor.fetchone()
            if row:
                self.stdout.write(f"  database default: {row[0]} / {row[1]}")
            cursor.execute(
                "SELECT TABLE_NAME, TABLE_COLLATION FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE' "
                "ORDER BY TABLE_NAME"
            )
            tables = cursor.fetchall()

        non_utf8mb4 = [
            (name, collation or "(database default)")
            for name, collation in tables
            if not str(collation or "").startswith("utf8mb4")
        ]
        self.stdout.write(f"  tables checked: {len(tables)}")
        if not non_utf8mb4:
            self.stdout.write(self.style.SUCCESS("  All tables use a utf8mb4 collation."))
            return
        self.stdout.write(
            self.style.WARNING(
                f"  {len(non_utf8mb4)} table(s) are NOT utf8mb4. Unicode text "
                "(arrows, ₦, accented names, emoji) can fail (MySQL 1366) when "
                "stored through a utf8mb4 connection into these tables:"
            )
        )
        for name, collation in non_utf8mb4:
            self.stdout.write(f"    - {name}: {collation}")
        raise CommandError(
            "Non-utf8mb4 tables remain. Back up first, then convert the listed "
            "tables with: ALTER TABLE <name> CONVERT TO CHARACTER SET utf8mb4 "
            "COLLATE utf8mb4_unicode_ci;"
        )
