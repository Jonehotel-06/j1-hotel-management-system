"""Add granular POS menu capability without widening legacy staff access."""
from django.db import migrations


def seed_pos_capability(apps, schema_editor):
    Capability = apps.get_model("accounts", "Capability")
    RoleCapability = apps.get_model("accounts", "RoleCapability")
    capability, _ = Capability.objects.get_or_create(
        code="pos.menu.manage",
        defaults={
            "name": "Manage POS menu",
            "category": "POS",
            "description": "Create and manage POS categories, items, and modifiers.",
            "is_active": True,
        },
    )
    # Managers/admins own menu/catalog policy. ADMIN resolves all active
    # capabilities dynamically, but storing an explicit grant keeps data
    # exports/matrix inspection complete and is harmless.
    for role in ("ADMIN", "MANAGER"):
        RoleCapability.objects.get_or_create(role=role, capability=capability)


class Migration(migrations.Migration):
    dependencies = [("accounts", "0003_capability_and_portal_foundation")]

    operations = [migrations.RunPython(seed_pos_capability, migrations.RunPython.noop)]
