"""Make the persisted audit-log grant match the enforced policy (audit N9).

The audit-log endpoint is administrator-only, but migration
``0014_seed_department_capabilities`` had seeded ``audit.view`` onto MANAGER,
GENERAL_MANAGER, ACCOUNTS_MANAGER and SECURITY. Every one of those roles could
see "View audit records" in the capability matrix while the API answered 403 —
a grant that means nothing, which is exactly the inconsistency N9 reports.

``apps.accounts.capabilities`` now grants ``audit.view`` to ADMIN only. Editing
that module does not rewrite rows already seeded into a deployed database, so
this migration reconciles the stored grants with the module: ADMIN keeps the
grant, the four non-admin roles lose it, and the capability row itself stays
active so the matrix still documents what the grant means.

Reversal restores the historical seed shape (grants for the four roles).
"""
from django.db import migrations

NON_ADMIN_AUDIT_ROLES = ("MANAGER", "GENERAL_MANAGER", "ACCOUNTS_MANAGER", "SECURITY")


def restrict_audit_view_to_admin(apps, schema_editor):
    Capability = apps.get_model("accounts", "Capability")
    RoleCapability = apps.get_model("accounts", "RoleCapability")

    capability, _ = Capability.objects.update_or_create(
        code="audit.view",
        defaults={
            "name": "View audit records",
            "category": "Security",
            "description": "View immutable audit history. Administrator-only: audit records "
                           "cover every role's actions, including administrative ones.",
            "is_active": True,
        },
    )
    RoleCapability.objects.filter(
        role__in=NON_ADMIN_AUDIT_ROLES, capability=capability
    ).delete()
    RoleCapability.objects.get_or_create(role="ADMIN", capability=capability)


def restore_seeded_audit_grants(apps, schema_editor):
    Capability = apps.get_model("accounts", "Capability")
    RoleCapability = apps.get_model("accounts", "RoleCapability")

    capability = Capability.objects.filter(code="audit.view").first()
    if capability is None:
        return
    for role in NON_ADMIN_AUDIT_ROLES:
        RoleCapability.objects.get_or_create(role=role, capability=capability)


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0018_workstation_receptionist_desktop_key"),
    ]

    operations = [
        migrations.RunPython(restrict_audit_view_to_admin, restore_seeded_audit_grants),
    ]
