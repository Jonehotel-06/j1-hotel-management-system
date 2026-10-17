"""Seed dispatch/inspection grants for housekeeping and maintenance workflows."""
from django.db import migrations


def seed_operations_dispatch_capabilities(apps, schema_editor):
    Capability = apps.get_model("accounts", "Capability")
    RoleCapability = apps.get_model("accounts", "RoleCapability")
    definitions = {
        "housekeeping.task.assign": ("Dispatch housekeeping tasks", "Operations", "Assign and inspect housekeeping tasks."),
        "maintenance.work_order.assign": ("Dispatch maintenance work orders", "Operations", "Assign and verify maintenance work orders."),
    }
    for code, (name, category, description) in definitions.items():
        capability, _ = Capability.objects.get_or_create(
            code=code,
            defaults={"name": name, "category": category, "description": description, "is_active": True},
        )
        for role in ("ADMIN", "MANAGER", "RECEPTIONIST"):
            RoleCapability.objects.get_or_create(role=role, capability=capability)
    # Reception owns front-desk dispatch in the approved matrix, so it must be
    # able to list/create the operational records it dispatches as well.
    for code in ("housekeeping.task.manage", "maintenance.work_order.manage"):
        capability = Capability.objects.filter(code=code).first()
        if capability:
            RoleCapability.objects.get_or_create(role="RECEPTIONIST", capability=capability)


class Migration(migrations.Migration):
    dependencies = [("accounts", "0007_guest_request_assign_capability")]
    operations = [migrations.RunPython(seed_operations_dispatch_capabilities, migrations.RunPython.noop)]
