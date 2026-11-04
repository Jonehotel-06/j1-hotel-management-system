"""Repair deployments where an applied foundation migration left no configuration.

Use only the existing model defaults, never replace management configuration.
Reverse is deliberately a no-op: approved transactions retain policy provenance.
"""
from datetime import date
from django.db import migrations


def initialize_missing_policy(apps, schema_editor):
    Policy = apps.get_model("finance", "FinancialControlPolicy")
    policies = Policy.objects.using(schema_editor.connection.alias)
    if not policies.exists():
        policies.create(
            name="Default financial controls", effective_from=date(1970, 1, 1),
            notes="Existing conservative model defaults restored by finance 0022; management must review configuration.",
        )


class Migration(migrations.Migration):
    dependencies = [("finance", "0021_guest_collection_idempotency")]
    operations = [migrations.RunPython(initialize_missing_policy, migrations.RunPython.noop)]
