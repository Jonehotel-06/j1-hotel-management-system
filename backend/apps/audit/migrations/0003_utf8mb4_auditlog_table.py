"""Convert the audit-log table to utf8mb4 on MySQL.

Audit summaries contain the same Unicode punctuation as notifications
(room moves are recorded as "room 201 → 203", date changes as
"2026-10-21→2026-10-23"). On a database created without utf8mb4 the
INSERT fails with MySQL 1366; the write helper now contains that
failure in a savepoint, but the audit row itself would still be lost.
Converting the one table this app owns preserves the audit trail.

No-op on non-MySQL backends. Existing rows are converted in place by
``CONVERT TO CHARACTER SET`` and preserved.
"""
from django.db import migrations


def _convert(schema_editor, charset, collation):
    if schema_editor.connection.vendor != "mysql":
        return
    schema_editor.execute(
        f"ALTER TABLE `audit_auditlog` CONVERT TO CHARACTER SET {charset} "
        f"COLLATE {collation}"
    )


def forwards(apps, schema_editor):
    _convert(schema_editor, "utf8mb4", "utf8mb4_unicode_ci")


def backwards(apps, schema_editor):
    _convert(schema_editor, "utf8mb3", "utf8mb3_unicode_ci")


class Migration(migrations.Migration):

    dependencies = [
        ("audit", "0002_auditlog_actor_department_auditlog_actor_role_and_more"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
