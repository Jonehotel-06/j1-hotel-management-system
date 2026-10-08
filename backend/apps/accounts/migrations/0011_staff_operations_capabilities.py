"""Seed workforce scheduling, attendance, and leave capabilities."""
from django.db import migrations


CAPABILITIES = (
    ("staff.profile.manage", "Manage staff operational profiles", "Staff operations", "Create and maintain non-sensitive staff operational profiles."),
    ("shift.manage", "Manage staff shifts", "Staff operations", "Create, schedule, and cancel staff shifts."),
    ("attendance.clock", "Clock attendance", "Staff operations", "Record the user's own source-keyed attendance events."),
    ("attendance.manage", "Manage attendance", "Staff operations", "View workforce attendance records and resolve permitted operations."),
    ("leave.request", "Request leave", "Staff operations", "Submit and cancel the user's permitted leave requests."),
    ("leave.approve", "Approve leave", "Staff operations", "Review leave requests for other staff members."),
)


def seed_staff_operations_capabilities(apps, schema_editor):
    Capability = apps.get_model("accounts", "Capability")
    RoleCapability = apps.get_model("accounts", "RoleCapability")
    capabilities = {}
    for code, name, category, description in CAPABILITIES:
        capabilities[code], _ = Capability.objects.get_or_create(
            code=code,
            defaults={"name": name, "category": category, "description": description, "is_active": True},
        )
    manager_codes = tuple(capabilities)
    self_service_codes = ("attendance.clock", "leave.request")
    for role in ("ADMIN", "MANAGER"):
        for code in manager_codes:
            RoleCapability.objects.get_or_create(role=role, capability=capabilities[code])
    for role in ("RECEPTIONIST", "CASHIER", "HOUSEKEEPING", "MAINTENANCE", "INVENTORY_CLERK"):
        for code in self_service_codes:
            RoleCapability.objects.get_or_create(role=role, capability=capabilities[code])


class Migration(migrations.Migration):
    dependencies = [("accounts", "0010_inventory_adjust_approve_capability")]
    operations = [migrations.RunPython(seed_staff_operations_capabilities, migrations.RunPython.noop)]
