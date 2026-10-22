"""Give the accounts manager the self-service attendance grant every peer has.

``ROLE_CAPABILITY_CODES["ACCOUNTS_MANAGER"]`` omitted ``attendance.clock``. Every
other staff role — including ACCOUNTANT, which shares the same finance surface —
holds it. ``capability_codes_for_user`` resolves a non-administrator's codes from
persisted ``RoleCapability`` rows, so editing the catalogue alone would not have
repaired an existing database: the missing row is the defect.

Effects of the omission, measured by the audit's role probe:

* ``GET /api/admin/staff-operations/attendance/`` answered 403 to an accounts
  manager, although ``frontend/js/dashboard.js`` routes the role to
  ``workforce.html``.
* ``POST /api/admin/staff-operations/attendance/clock/`` likewise.
* The reception terminal's "open a record, then issue a code" path calls
  ``clock_attendance`` with the staff member as *actor*; the capability check
  therefore failed and surfaced the misleading identity message "Attendance
  clocking is limited to the active staff member" to an employee who was
  standing at the terminal in person.

This migration adds exactly one ``RoleCapability`` row. It grants no management
capability, no override, and nothing to any other role; reversal removes the
row and restores the previous state. The definition row for ``attendance.clock``
already exists (seeded with the capability catalogue); it is only created here
if a database predates that seed.
"""
from django.db import migrations

ROLE = "ACCOUNTS_MANAGER"
CODE = "attendance.clock"
DEFINITION = {
    "name": "Clock attendance",
    "category": "Staff operations",
    "description": "Record the user's own source-keyed attendance events.",
}


def grant_self_clock(apps, schema_editor):
    Capability = apps.get_model("accounts", "Capability")
    RoleCapability = apps.get_model("accounts", "RoleCapability")

    capability, _ = Capability.objects.get_or_create(code=CODE, defaults=DEFINITION)
    RoleCapability.objects.get_or_create(role=ROLE, capability=capability)


def revoke_self_clock(apps, schema_editor):
    RoleCapability = apps.get_model("accounts", "RoleCapability")
    RoleCapability.objects.filter(role=ROLE, capability__code=CODE).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0023_workstation_registered_desktop_ip"),
    ]

    operations = [
        migrations.RunPython(grant_self_clock, revoke_self_clock),
    ]
