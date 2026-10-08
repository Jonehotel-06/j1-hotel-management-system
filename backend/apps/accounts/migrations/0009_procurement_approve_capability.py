"""Seed purchase-order maker-checker approval capability."""
from django.db import migrations


def seed_procurement_approve_capability(apps, schema_editor):
    Capability = apps.get_model("accounts", "Capability")
    RoleCapability = apps.get_model("accounts", "RoleCapability")
    capability, _ = Capability.objects.get_or_create(
        code="procurement.approve",
        defaults={
            "name": "Approve procurement",
            "category": "Inventory",
            "description": "Approve purchase orders requested by another user.",
            "is_active": True,
        },
    )
    for role in ("ADMIN", "MANAGER"):
        RoleCapability.objects.get_or_create(role=role, capability=capability)


class Migration(migrations.Migration):
    dependencies = [("accounts", "0008_operations_dispatch_capabilities")]
    operations = [migrations.RunPython(seed_procurement_approve_capability, migrations.RunPython.noop)]
