"""Seed controlled expense workflow capabilities."""
from django.db import migrations


CAPABILITIES = (
    ("expense.manage", "Manage expenses", "Finance", "Create, submit, and post permitted expense records."),
    ("expense.approve", "Approve expenses", "Finance", "Independently approve or reject controlled expense requests."),
)


def seed_expense_capabilities(apps, schema_editor):
    Capability = apps.get_model("accounts", "Capability")
    RoleCapability = apps.get_model("accounts", "RoleCapability")
    capabilities = {}
    for code, name, category, description in CAPABILITIES:
        capabilities[code], _ = Capability.objects.get_or_create(
            code=code,
            defaults={"name": name, "category": category, "description": description, "is_active": True},
        )
    for role in ("ADMIN", "MANAGER"):
        for capability in capabilities.values():
            RoleCapability.objects.get_or_create(role=role, capability=capability)
    RoleCapability.objects.get_or_create(role="CASHIER", capability=capabilities["expense.manage"])


class Migration(migrations.Migration):
    dependencies = [("accounts", "0011_staff_operations_capabilities")]
    operations = [migrations.RunPython(seed_expense_capabilities, migrations.RunPython.noop)]
