"""Define the staff-record capabilities the staff APIs already enforce (audit N1).

``apps.staff_operations`` gates supervisor fingerprint management on
``staff.view`` (GET ``/api/admin/staff-operations/fingerprint/staff/<id>/``) and
on ``attendance.manage`` OR ``staff.manage`` (POST, and
``fingerprint_service`` revocation). Neither code was ever defined in
``apps.accounts.capabilities`` and therefore no ``Capability`` row existed.

``capability_codes_for_user`` resolves non-administrator grants from persisted
``RoleCapability`` rows and resolves ADMIN from persisted ``Capability`` rows, so
a code that was never defined can never be held by ANY account — not even an
administrator. Staff profile → "Enroll / Re-Enroll Fingerprint" therefore
answered 403 "You do not have the required permission for this action." for
every role while leave, shifts and payroll on the same page kept working.

This migration defines the two capabilities and grants them to the roles that
already own the staff-record surface (the same roles that hold
``staff.profile.manage`` and are admitted by the front-end staff pages). It does
NOT widen any existing gate: ``POST`` enrolment already accepted
``attendance.manage`` (held by exactly these roles) and the ordinary workforce
roles receive neither new code. The persisted rows are what make ADMIN's
dynamic "all active capabilities" resolution include the new codes.

Reversal removes the grants and deactivates the capability rows, restoring the
pre-fix state exactly.
"""
from django.db import migrations

STAFF_VIEW_ROLES = ("ADMIN", "MANAGER", "GENERAL_MANAGER", "HR_MANAGER")

CAPABILITIES = (
    {
        "code": "staff.view",
        "name": "View staff records",
        "category": "Staff operations",
        "description": (
            "View permitted staff operational records, including biometric enrollment "
            "and attendance status."
        ),
    },
    {
        "code": "staff.manage",
        "name": "Manage staff records",
        "category": "Staff operations",
        "description": (
            "Record permitted staff operational changes, including another staff member's "
            "biometric enrollment or revocation."
        ),
    },
)


def add_staff_record_capabilities(apps, schema_editor):
    Capability = apps.get_model("accounts", "Capability")
    RoleCapability = apps.get_model("accounts", "RoleCapability")

    for definition in CAPABILITIES:
        capability, _ = Capability.objects.update_or_create(
            code=definition["code"],
            defaults={
                "name": definition["name"],
                "category": definition["category"],
                "description": definition["description"],
                "is_active": True,
            },
        )
        for role in STAFF_VIEW_ROLES:
            RoleCapability.objects.get_or_create(role=role, capability=capability)


def remove_staff_record_capabilities(apps, schema_editor):
    Capability = apps.get_model("accounts", "Capability")
    RoleCapability = apps.get_model("accounts", "RoleCapability")

    codes = [definition["code"] for definition in CAPABILITIES]
    RoleCapability.objects.filter(capability__code__in=codes).delete()
    Capability.objects.filter(code__in=codes).update(is_active=False)


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0020_user_middle_name_user_must_change_password_and_more"),
    ]

    operations = [
        migrations.RunPython(add_staff_record_capabilities, remove_staff_record_capabilities),
    ]
