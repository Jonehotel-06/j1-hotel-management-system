"""Grant finance control-policy management to managers/admins."""
from django.db import migrations


def seed_finance_controls_capability(apps, schema_editor):
    Capability = apps.get_model("accounts", "Capability")
    RoleCapability = apps.get_model("accounts", "RoleCapability")
    capability, _ = Capability.objects.get_or_create(
        code="finance.controls.manage",
        defaults={
            "name": "Manage finance controls",
            "category": "Finance",
            "description": "Create effective-dated financial control policies.",
            "is_active": True,
        },
    )
    for role in ("ADMIN", "MANAGER"):
        RoleCapability.objects.get_or_create(role=role, capability=capability)


class Migration(migrations.Migration):
    dependencies = [("accounts", "0004_pos_capabilities")]
    operations = [migrations.RunPython(seed_finance_controls_capability, migrations.RunPython.noop)]
