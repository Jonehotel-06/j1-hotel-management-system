"""Verify finance 0022 on an isolated migration-verification database only.

Use DATABASE_URL pointing to a *copy* of an isolated test database whose filename
contains 'migration-verification'. Never run against a production database.
This intentionally alters policy configuration in that expendable database.
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")


def main():
    import django
    django.setup()
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor
    from apps.finance.models import FinancialControlPolicy

    name = str(connection.settings_dict["NAME"])
    if connection.vendor != "sqlite" or "migration-verification" not in Path(name).name:
        raise RuntimeError("Use an isolated SQLite migration-verification database copy only.")

    def evidence():
        # Compare historical record content, not only counts; no financial or
        # operational records may be rewritten by a configuration migration.
        tables = ["finance_financialtransaction", "finance_folioposting",
                  "inventory_stockmovement", "bookings_booking",
                  "guest_services_servicerequest"]
        import hashlib
        with connection.cursor() as cursor:
            data = {}
            for table in tables:
                cursor.execute(f'SELECT * FROM "{table}" ORDER BY id')
                rows = cursor.fetchall()
                data[table] = {"rows": len(rows), "sha256": hashlib.sha256(repr(rows).encode()).hexdigest()}
            return data

    def migrate_to(number):
        MigrationExecutor(connection).migrate([("finance", number)])

    original = evidence()
    migrate_to("0021_guest_collection_idempotency")
    FinancialControlPolicy.objects.all().delete()
    migrate_to("0022_initialize_missing_control_policy")
    row = FinancialControlPolicy.objects.get()
    assert row.is_active and row.require_separate_approver
    assert str(row.effective_from) == "1970-01-01"
    assert row.refund_manager_approval_threshold == 0
    assert row.expense_manager_approval_threshold == 0
    assert row.discount_manager_approval_threshold == 0
    assert row.cash_variance_manager_approval_threshold == 1000
    results = [{"name": "Upgrade from finance 0021 with empty configuration", "status": "PASSED"}]

    original_id = row.pk
    migrate_to("0021_guest_collection_idempotency")
    assert FinancialControlPolicy.objects.get(pk=original_id)
    results.append({"name": "Reverse migration does not delete restored configuration", "status": "PASSED"})
    row.name = "Isolated management configuration"
    row.is_active = False
    row.expense_manager_approval_threshold = 75
    row.save()
    before = list(FinancialControlPolicy.objects.values())
    migrate_to("0022_initialize_missing_control_policy")
    assert list(FinancialControlPolicy.objects.values()) == before
    results.append({"name": "Existing inactive management configuration remains unchanged", "status": "PASSED"})
    assert evidence() == original
    results.append({"name": "Historical transaction, posting, stock, booking and request content unchanged", "status": "PASSED"})
    print(json.dumps({"checks": results, "history": original}, indent=2))


if __name__ == "__main__":
    main()
