from django.db import migrations

ACTION = "PROCUREMENT_INVENTORY_GRANT_0031"


def forwards(apps, schema_editor):
    Capability = apps.get_model("accounts", "Capability")
    RoleCapability = apps.get_model("accounts", "RoleCapability")
    AuditLog = apps.get_model("audit", "AuditLog")
    db = schema_editor.connection.alias
    cap = Capability.objects.using(db).get(code="inventory.manage")
    grant, created = RoleCapability.objects.using(db).get_or_create(role="PROCUREMENT_OFFICER", capability=cap)
    AuditLog.objects.using(db).create(action=ACTION, object_type="accounts.rolecapability", object_id=str(grant.pk), metadata={"created": created})


def backwards(apps, schema_editor):
    RoleCapability = apps.get_model("accounts", "RoleCapability")
    AuditLog = apps.get_model("audit", "AuditLog")
    db = schema_editor.connection.alias
    marker = AuditLog.objects.using(db).filter(action=ACTION).order_by("-pk").first()
    if marker and marker.metadata.get("created"):
        RoleCapability.objects.using(db).filter(pk=marker.object_id, role="PROCUREMENT_OFFICER", capability__code="inventory.manage").delete()
    AuditLog.objects.using(db).create(action=ACTION + "_REVERSED", object_type="accounts.rolecapability", object_id=marker.object_id if marker else "")


class Migration(migrations.Migration):
    dependencies = [("accounts", "0030_consolidate_approved_departments_and_roles"), ("audit", "0003_utf8mb4_auditlog_table")]
    operations = [migrations.RunPython(forwards, backwards)]
