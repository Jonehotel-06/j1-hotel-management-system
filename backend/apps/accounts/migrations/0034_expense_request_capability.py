"""Controlled repair — narrow Store Keeper / Procurement expense handover right.

``expense.request`` creates a DRAFT handover ONLY. It does not grant
``expense.manage`` (posting), ``expense.approve`` (maker-checker), supplier
payment execution, or inventory adjustment approval. The Accountant prepares
and submits the draft; an authorised approver decides; posting stays in the
existing controlled expense service.

The Administrator resolves all catalogue capabilities by construction. This
migration seeds the capability row and grants it to the Store Keeper, the
Procurement Officer and the retired INVENTORY_CLERK title so historical
accounts keep working. Nothing is revoked. The reverse deletes only grants
created by this migration, tracked through an audit marker.
"""
from django.db import migrations

ACTION = "EXPENSE_REQUEST_CAPABILITY_0034"
CAPABILITY_CODE = "expense.request"
ROLES = ("STOREKEEPER", "PROCUREMENT_OFFICER", "INVENTORY_CLERK", "GENERAL_MANAGER")


def forwards(apps, schema_editor):
    Capability = apps.get_model("accounts", "Capability")
    RoleCapability = apps.get_model("accounts", "RoleCapability")
    AuditLog = apps.get_model("audit", "AuditLog")
    db = schema_editor.connection.alias

    capability, _created = Capability.objects.using(db).get_or_create(
        code=CAPABILITY_CODE,
        defaults={
            "name": "Request an expense handover",
            "category": "Finance",
            "description": (
                "Create a DRAFT Store Keeper / Procurement expense handover only. "
                "Does not approve, post or execute payment."
            ),
            "is_active": True,
        },
    )
    created_pks = []
    for role in ROLES:
        grant, created = RoleCapability.objects.using(db).get_or_create(role=role, capability=capability)
        if created:
            created_pks.append(grant.pk)
    AuditLog.objects.using(db).create(
        action=ACTION,
        object_type="accounts.rolecapability",
        object_id=",".join(str(pk) for pk in created_pks),
        metadata={"capability": CAPABILITY_CODE, "roles": list(ROLES), "created_pks": created_pks},
    )


def backwards(apps, schema_editor):
    RoleCapability = apps.get_model("accounts", "RoleCapability")
    AuditLog = apps.get_model("audit", "AuditLog")
    db = schema_editor.connection.alias
    marker = AuditLog.objects.using(db).filter(action=ACTION).order_by("-pk").first()
    created_pks = []
    if marker:
        raw = (marker.metadata or {}).get("created_pks") or []
        created_pks = [int(pk) for pk in raw if str(pk).isdigit()]
    if created_pks:
        RoleCapability.objects.using(db).filter(pk__in=created_pks).delete()
    if marker:
        marker.delete()


class Migration(migrations.Migration):
    dependencies = [("accounts", "0033_inventory_record_capability")]

    operations = [migrations.RunPython(forwards, backwards)]
