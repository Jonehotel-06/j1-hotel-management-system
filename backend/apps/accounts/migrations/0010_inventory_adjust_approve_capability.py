"""Seed manager/admin approval for stock-count variance adjustments."""
from django.db import migrations


def seed_inventory_adjust_approval(apps, schema_editor):
    Capability = apps.get_model("accounts", "Capability")
    RoleCapability = apps.get_model("accounts", "RoleCapability")
    capability, _ = Capability.objects.get_or_create(
        code="inventory.adjust.approve",
        defaults={
            "name": "Approve inventory adjustments",
            "category": "Inventory",
            "description": "Approve count variances before stock adjustments post.",
            "is_active": True,
        },
    )
    for role in ("ADMIN", "MANAGER"):
        RoleCapability.objects.get_or_create(role=role, capability=capability)


class Migration(migrations.Migration):
    dependencies = [("accounts", "0009_procurement_approve_capability")]
    operations = [migrations.RunPython(seed_inventory_adjust_approval, migrations.RunPython.noop)]
