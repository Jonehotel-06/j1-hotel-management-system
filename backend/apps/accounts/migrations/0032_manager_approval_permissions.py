"""Batch 07: grant the Manager the five defined approval permissions.

The Manager (``User.Role.MANAGER``) must be able to decide:

* leave requests (``leave.approve``);
* stock-count variances (``inventory.adjust.approve``);
* purchase orders awaiting approval (``procurement.approve``);
* payroll runs (``payroll.approve``);
* expenses awaiting approval (``expense.approve``).

The persisted ``RoleCapability`` rows are the authorization source, so this
migration idempotently ensures exactly these five grants exist for MANAGER.
It grants nothing else, revokes nothing (existing MANAGER, ADMIN and
GENERAL_MANAGER authority is preserved byte-for-byte), and touches no other
role: department staff gain no approval authority from this change.

The reverse removes only the grants this run created, so rolling back can
never revoke a grant the seed or an administrator put in place.
"""
from django.db import migrations

ACTION = "MANAGER_APPROVAL_PERMISSIONS_0032"

MANAGER_APPROVAL_CAPABILITIES = (
    "leave.approve",
    "inventory.adjust.approve",
    "procurement.approve",
    "payroll.approve",
    "expense.approve",
)


def forwards(apps, schema_editor):
    Capability = apps.get_model("accounts", "Capability")
    RoleCapability = apps.get_model("accounts", "RoleCapability")
    AuditLog = apps.get_model("audit", "AuditLog")
    db = schema_editor.connection.alias
    created_pks = []
    for code in MANAGER_APPROVAL_CAPABILITIES:
        cap = Capability.objects.using(db).filter(code=code, is_active=True).first()
        if cap is None:
            continue
        grant, created = RoleCapability.objects.using(db).get_or_create(role="MANAGER", capability=cap)
        if created:
            created_pks.append(grant.pk)
    AuditLog.objects.using(db).create(
        action=ACTION,
        object_type="accounts.rolecapability",
        object_id=",".join(str(pk) for pk in created_pks),
        metadata={"role": "MANAGER", "capabilities": list(MANAGER_APPROVAL_CAPABILITIES), "created_pks": created_pks},
    )


def backwards(apps, schema_editor):
    RoleCapability = apps.get_model("accounts", "RoleCapability")
    AuditLog = apps.get_model("audit", "AuditLog")
    db = schema_editor.connection.alias
    marker = AuditLog.objects.using(db).filter(action=ACTION).order_by("-pk").first()
    created_pks = []
    if marker:
        created_pks = [pk for pk in (marker.metadata or {}).get("created_pks", []) if pk]
    if created_pks:
        RoleCapability.objects.using(db).filter(
            pk__in=created_pks, role="MANAGER", capability__code__in=MANAGER_APPROVAL_CAPABILITIES
        ).delete()
    AuditLog.objects.using(db).create(
        action=ACTION + "_REVERSED",
        object_type="accounts.rolecapability",
        object_id=",".join(str(pk) for pk in created_pks),
        metadata={"role": "MANAGER", "removed_pks": created_pks},
    )


class Migration(migrations.Migration):
    dependencies = [("accounts", "0031_procurement_inventory_access"), ("audit", "0003_utf8mb4_auditlog_table")]
    operations = [migrations.RunPython(forwards, backwards)]
