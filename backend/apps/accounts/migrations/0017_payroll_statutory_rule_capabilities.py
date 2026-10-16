"""Separate proposal and independent review access for statutory payroll rules."""
from django.db import migrations


CAPABILITIES = (
    ("payroll.rules.manage", "Propose statutory payroll rules", "Payroll", "Create immutable effective-dated statutory rules for independent review."),
    ("payroll.rules.review", "Review statutory payroll rules", "Payroll", "Independently review and approve statutory payroll rules against legal sources."),
)
MANAGE_ROLES = ("MANAGER", "GENERAL_MANAGER", "ACCOUNTS_MANAGER", "HR_MANAGER")
REVIEW_ROLES = ("MANAGER", "GENERAL_MANAGER", "ACCOUNTS_MANAGER")


def apply_payroll_rule_grants(apps, schema_editor):
    Capability = apps.get_model("accounts", "Capability")
    RoleCapability = apps.get_model("accounts", "RoleCapability")
    by_code = {}
    for code, name, category, description in CAPABILITIES:
        capability, _ = Capability.objects.update_or_create(
            code=code,
            defaults={"name": name, "category": category, "description": description, "is_active": True},
        )
        by_code[code] = capability
    for role in MANAGE_ROLES:
        RoleCapability.objects.get_or_create(role=role, capability=by_code["payroll.rules.manage"])
    for role in REVIEW_ROLES:
        RoleCapability.objects.get_or_create(role=role, capability=by_code["payroll.rules.review"])


def reverse_payroll_rule_grants(apps, schema_editor):
    Capability = apps.get_model("accounts", "Capability")
    RoleCapability = apps.get_model("accounts", "RoleCapability")
    RoleCapability.objects.filter(capability__code__in=[row[0] for row in CAPABILITIES]).delete()
    Capability.objects.filter(code__in=[row[0] for row in CAPABILITIES]).delete()


class Migration(migrations.Migration):
    dependencies = [("accounts", "0016_payment_read_and_booking_scope")]

    operations = [migrations.RunPython(apply_payroll_rule_grants, reverse_payroll_rule_grants)]
