"""Controlled addition — guest-amenity and complementary-breakfast capabilities.

Three new capability codes are seeded and granted to the roles that legitimately
need them, following the existing migration pattern (0034):

* ``guest_amenity.allocate`` — Store Keeper allocates basic guest-amenity stock
  (soap, toothbrush, toothpaste, tissue) into the Reception Department's stock.
* ``guest_amenity.issue`` — Receptionist issues those amenities to checking-in
  guests from the Reception allocation (NOT Main Store).
* ``breakfast.issue`` — Receptionist records a complementary breakfast served to
  an eligible lodged guest (no folio charge, no inventory movement).

Nothing is revoked. The reverse deletes only grants created by this migration.
"""
from django.db import migrations

ACTION = "GUEST_AMENITY_BREAKFAST_CAPABILITY_0037"
CAPABILITY_DEFINITIONS = (
    ("guest_amenity.allocate", "Allocate guest amenities", "Inventory",
     "Allocate basic guest-amenity stock (soap, toothbrush, toothpaste, tissue) into the Reception Department's stock."),
    ("guest_amenity.issue", "Issue guest amenities", "Reception",
     "Issue basic guest amenities to checking-in guests from the Reception Department's own allocation."),
    ("breakfast.issue", "Record complementary breakfast", "Reception",
     "Record a complementary breakfast served to an eligible lodged guest, with no folio charge and no inventory movement."),
)
ROLE_GRANTS = {
    "STOREKEEPER": ("guest_amenity.allocate",),
    "GENERAL_MANAGER": ("guest_amenity.allocate",),
    "MANAGER": ("guest_amenity.allocate",),
    "RECEPTIONIST": ("guest_amenity.issue", "breakfast.issue"),
}


def forwards(apps, schema_editor):
    Capability = apps.get_model("accounts", "Capability")
    RoleCapability = apps.get_model("accounts", "RoleCapability")
    AuditLog = apps.get_model("audit", "AuditLog")
    db = schema_editor.connection.alias

    capability_objs = {}
    for code, name, category, description in CAPABILITY_DEFINITIONS:
        capability, _created = Capability.objects.using(db).get_or_create(
            code=code,
            defaults={"name": name, "category": category, "description": description, "is_active": True},
        )
        capability_objs[code] = capability

    created_pks = []
    for role, codes in ROLE_GRANTS.items():
        for code in codes:
            grant, created = RoleCapability.objects.using(db).get_or_create(
                role=role, capability=capability_objs[code]
            )
            if created:
                created_pks.append(grant.pk)

    AuditLog.objects.using(db).create(
        action=ACTION,
        object_type="accounts.rolecapability",
        object_id=",".join(str(pk) for pk in created_pks),
        metadata={"capabilities": [c[0] for c in CAPABILITY_DEFINITIONS], "created_pks": created_pks},
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
    dependencies = [("accounts", "0036_front_desk_device_credential")]

    operations = [migrations.RunPython(forwards, backwards)]
