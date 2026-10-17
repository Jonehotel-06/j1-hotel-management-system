"""Retire statutory-payroll capabilities after PAYE/pension removal.

The payroll product no longer proposes or reviews government tax/pension
rules.  Removing persisted grants (and per-user overrides) ensures stale rows
cannot keep the retired surface alive through capability resolution.  The
capability rows are deactivated rather than deleted so historical audit
references remain intelligible; reversal restores the former grants.
"""
from django.db import migrations


CODES = ("payroll.rules.manage", "payroll.rules.review")
RESTORE_GRANTS = {
    "payroll.rules.manage": ("MANAGER", "GENERAL_MANAGER", "ACCOUNTS_MANAGER", "HR_MANAGER"),
    "payroll.rules.review": ("MANAGER", "GENERAL_MANAGER", "ACCOUNTS_MANAGER"),
}
DEFINITIONS = {
    "payroll.rules.manage": (
        "Propose statutory payroll rules", "Payroll",
        "Create immutable effective-dated statutory rules for independent review.",
    ),
    "payroll.rules.review": (
        "Review statutory payroll rules", "Payroll",
        "Independently review and approve statutory payroll rules against legal sources.",
    ),
}


def remove_statutory_payroll_capabilities(apps, schema_editor):
    Capability = apps.get_model("accounts", "Capability")
    RoleCapability = apps.get_model("accounts", "RoleCapability")
    UserCapabilityOverride = apps.get_model("accounts", "UserCapabilityOverride")

    capabilities = Capability.objects.filter(code__in=CODES)
    RoleCapability.objects.filter(capability__in=capabilities).delete()
    UserCapabilityOverride.objects.filter(capability__in=capabilities).delete()
    capabilities.update(is_active=False)


def restore_statutory_payroll_capabilities(apps, schema_editor):
    Capability = apps.get_model("accounts", "Capability")
    RoleCapability = apps.get_model("accounts", "RoleCapability")

    for code in CODES:
        name, category, description = DEFINITIONS[code]
        capability, _ = Capability.objects.update_or_create(
            code=code,
            defaults={
                "name": name,
                "category": category,
                "description": description,
                "is_active": True,
            },
        )
        for role in RESTORE_GRANTS[code]:
            RoleCapability.objects.get_or_create(role=role, capability=capability)


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0021_staff_records_capabilities"),
    ]

    operations = [
        migrations.RunPython(
            remove_statutory_payroll_capabilities,
            restore_statutory_payroll_capabilities,
        ),
    ]
