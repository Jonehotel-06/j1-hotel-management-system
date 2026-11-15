"""Management tier — guest-amenity issuance and breakfast recording (0038).

Migration 0037 introduced the guest-amenity/breakfast capabilities and granted
``guest_amenity.allocate`` to GENERAL_MANAGER and MANAGER, but not
``guest_amenity.issue`` or ``breakfast.issue``. The consequence was concrete and
reported from the floor:

* a general manager, covering the front desk for an hour, could not issue a
  soap/toothbrush to a checking-in guest or record a complementary breakfast —
  the endpoints answered 403 while the rest of the front desk's work was
  available to them;
* ``ROLE_CAPABILITY_CODES`` in ``apps/accounts/capabilities.py`` did not even
  list “guest_amenity.allocate“ for the management tier, so the matrix an
  auditor reads disagreed with the rows 0037 had written. That drift is fixed in
  the same change.

This migration brings the persisted grants in line with the maintained matrix:
the management tier (GENERAL_MANAGER, MANAGER) receives the two issuance codes it
was missing, and re-asserts ``guest_amenity.allocate`` (``get_or_create``: rows
0037 already created are left untouched). **Nothing is revoked.** The front desk
keeps ``guest_amenity.issue``/``breakfast.issue``, the store keeper keeps
``guest_amenity.allocate``, and no other department is added.

The reverse deletes only the grants this migration actually created, so rolling
back returns the database to exactly the state 0037 left it in.
"""
from django.db import migrations

ACTION = "MANAGEMENT_TIER_GUEST_AMENITY_BREAKFAST_0038"
ROLE_GRANTS = {
    "GENERAL_MANAGER": ("guest_amenity.allocate", "guest_amenity.issue", "breakfast.issue"),
    "MANAGER": ("guest_amenity.allocate", "guest_amenity.issue", "breakfast.issue"),
}


def forwards(apps, schema_editor):
    Capability = apps.get_model("accounts", "Capability")
    RoleCapability = apps.get_model("accounts", "RoleCapability")
    AuditLog = apps.get_model("audit", "AuditLog")
    db = schema_editor.connection.alias

    created_pks = []
    for role, codes in ROLE_GRANTS.items():
        for code in codes:
            capability = Capability.objects.using(db).filter(code=code).first()
            if capability is None:
                # A database that never ran 0037 must not be half-granted here:
                # the capability rows themselves are 0037's responsibility.
                continue
            grant, created = RoleCapability.objects.using(db).get_or_create(
                role=role, capability=capability
            )
            if created:
                created_pks.append(grant.pk)

    AuditLog.objects.using(db).create(
        action=ACTION,
        object_type="accounts.rolecapability",
        object_id=",".join(str(pk) for pk in created_pks),
        metadata={
            "roles": sorted(ROLE_GRANTS),
            "capabilities": sorted({code for codes in ROLE_GRANTS.values() for code in codes}),
            "created_pks": created_pks,
            "note": "management tier covers the guest-amenity / breakfast station; nothing revoked",
        },
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
    dependencies = [("accounts", "0037_guest_amenity_breakfast_capability")]

    operations = [migrations.RunPython(forwards, backwards)]
