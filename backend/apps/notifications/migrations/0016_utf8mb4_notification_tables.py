"""Convert the notification tables to utf8mb4 on MySQL.

Production failure, 2026-10-18::

    django.db.utils.OperationalError: (1366, "Incorrect string value:
    '\\xE2\\x86\\x92 20...' for column
    `...`.`notifications_notification`.`message` at row 1")

``\\xE2\\x86\\x92`` is the UTF-8 encoding of "→", which staff
notification messages contain ("... booked ..., 2026-10-21 →
2026-10-23"). The production database was created without utf8mb4, so
the column (and the tables that store rendered emails, whose subjects
and bodies contain "—", "·", "₦", ...) cannot store ordinary Unicode.

Scope is deliberately narrow — the two tables OWNED by this app whose
text demonstrably carries such characters. Converting a table with
``CONVERT TO CHARACTER SET`` rewrites every text column in it and
converts existing values from their current charset; rows are
preserved. It is a no-op on every non-MySQL backend and on MySQL
tables that are already utf8mb4 (the statement is still valid there).

Reversing converts back to utf8 (utf8mb3) — the narrowest charset that
still round-trips anything these columns could have held before; a
reverse is for migration-graph completeness, not a recommendation.
"""
from django.db import migrations


def _convert(schema_editor, charset, collation):
    if schema_editor.connection.vendor != "mysql":
        return
    for table in ("notifications_notification", "notifications_emaillog"):
        schema_editor.execute(
            f"ALTER TABLE `{table}` CONVERT TO CHARACTER SET {charset} "
            f"COLLATE {collation}"
        )


def forwards(apps, schema_editor):
    _convert(schema_editor, "utf8mb4", "utf8mb4_unicode_ci")


def backwards(apps, schema_editor):
    _convert(schema_editor, "utf8mb3", "utf8mb3_unicode_ci")


class Migration(migrations.Migration):

    dependencies = [
        ("notifications", "0015_alter_emaillog_kind"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
