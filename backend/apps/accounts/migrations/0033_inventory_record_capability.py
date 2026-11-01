"""Task 09 — restrict MANUAL stock recording to Store Keeper, Admin and GM.

Before this migration the hotel had ONE inventory authority,
``inventory.manage``, which meant two different things at once:

* *using* the inventory workspace — reading balances, movements, counts,
  recipes and consumption hand-offs; and
* *recording* stock — creating items, opening balances, receipts, issues,
  returns, transfers, replenishments, balance adjustments and stock counts.

Because those were the same code, every role that needed to SEE stock also
silently held the right to rewrite it. The Store Keeper is the hotel's stock
custodian; Admin and the General Manager oversee them. Nobody else may record
stock.

This migration therefore:

1. creates the ``inventory.record`` capability row (an idempotent get_or_create
   — the catalogue in ``apps.accounts.capabilities`` already defines it); and
2. grants it to exactly ``STOREKEEPER``, ``ADMIN`` and ``GENERAL_MANAGER``.

Nothing is revoked. In particular:

* ``inventory.manage`` is untouched, so every department keeps the stock
  VISIBILITY it has today;
* ``procurement.manage``, POS, kitchen, housekeeping and payroll flows are
  untouched;
* historical audit rows are untouched.

The reverse removes only the grants this run created (tracked by audit-row
primary keys), so a rollback can never revoke a grant an administrator added
by hand.
"""
from django.db import migrations

ACTION = "INVENTORY_RECORD_CAPABILITY_0033"

#: The only roles allowed to record stock manually (task 09, section A).
#:
#: ``INVENTORY_CLERK`` and ``MANAGER`` are the RETIRED titles for, respectively,
#: Store Keeper and General Manager (see ``apps.accounts.departments``). They
#: are still present on historical accounts that have not been re-assigned yet,
#: and they are the same two people doing the same job — so they keep the same
#: authority until the account is normalised. No NEW assignment may use them.
INVENTORY_RECORDING_ROLES = (
    "STOREKEEPER",
    "ADMIN",
    "GENERAL_MANAGER",
    "INVENTORY_CLERK",  # retired title → STOREKEEPER
    "MANAGER",           # retired title → GENERAL_MANAGER
)

CAPABILITY_CODE = "inventory.record"


def forwards(apps, schema_editor):
    Capability = apps.get_model("accounts", "Capability")
    RoleCapability = apps.get_model("accounts", "RoleCapability")
    AuditLog = apps.get_model("audit", "AuditLog")
    db = schema_editor.connection.alias

    capability, _created = Capability.objects.using(db).get_or_create(
        code=CAPABILITY_CODE,
        defaults={
            "name": "Record stock",
            "category": "Inventory",
            "description": (
                "Create stock items, opening stock, receipts, issues, returns, transfers, "
                "replenishments, adjustments and stock counts."
            ),
            "is_active": True,
        },
    )

    created_pks = []
    for role in INVENTORY_RECORDING_ROLES:
        grant, created = RoleCapability.objects.using(db).get_or_create(role=role, capability=capability)
        if created:
            created_pks.append(grant.pk)

    AuditLog.objects.using(db).create(
        action=ACTION,
        object_type="accounts.rolecapability",
        object_id=",".join(str(pk) for pk in created_pks),
        metadata={
            "capability": CAPABILITY_CODE,
            "roles": list(INVENTORY_RECORDING_ROLES),
            "created_pks": created_pks,
        },
    )


def backwards(apps, schema_editor):
    RoleCapability = apps.get_model("accounts", "RoleCapability")
    Capability = apps.get_model("accounts", "Capability")
    AuditLog = apps.get_model("audit", "AuditLog")
    db = schema_editor.connection.alias

    marker = AuditLog.objects.using(db).filter(action=ACTION).order_by("-pk").first()
    created_pks = []
    if marker:
        created_pks = [pk for pk in (marker.metadata or {}).get("created_pks", []) if pk]
    if created_pks:
        RoleCapability.objects.using(db).filter(
            pk__in=created_pks, role__in=INVENTORY_RECORDING_ROLES, capability__code=CAPABILITY_CODE
        ).delete()

    # Only remove the capability row if no role still references it.
    if not RoleCapability.objects.using(db).filter(capability__code=CAPABILITY_CODE).exists():
        Capability.objects.using(db).filter(code=CAPABILITY_CODE).delete()

    AuditLog.objects.using(db).create(
        action=ACTION + "_REVERSED",
        object_type="accounts.rolecapability",
        object_id=",".join(str(pk) for pk in created_pks),
        metadata={"capability": CAPABILITY_CODE, "removed_pks": created_pks},
    )


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0032_manager_approval_permissions"),
        ("audit", "0003_utf8mb4_auditlog_table"),
    ]
    operations = [migrations.RunPython(forwards, backwards)]
