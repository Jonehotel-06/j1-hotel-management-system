"""Seed the constrained guest-service dispatcher grant."""
from django.db import migrations


def seed_guest_request_assign_capability(apps, schema_editor):
    Capability = apps.get_model("accounts", "Capability")
    RoleCapability = apps.get_model("accounts", "RoleCapability")
    capability, _ = Capability.objects.get_or_create(
        code="guest_request.assign",
        defaults={
            "name": "Dispatch guest requests",
            "category": "Guest services",
            "description": "Assign or re-route guest service requests.",
            "is_active": True,
        },
    )
    for role in ("ADMIN", "MANAGER", "RECEPTIONIST"):
        RoleCapability.objects.get_or_create(role=role, capability=capability)


class Migration(migrations.Migration):
    dependencies = [("accounts", "0006_restrict_cashier_folio_access")]
    operations = [migrations.RunPython(seed_guest_request_assign_capability, migrations.RunPython.noop)]
