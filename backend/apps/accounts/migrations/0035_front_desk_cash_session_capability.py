"""Grant the front desk its own cash drawer (cash_session.open / close).

WHY THIS MIGRATION EXISTS
=========================
``RECEPTIONIST`` and ``FRONT_DESK_SUPERVISOR`` hold ``payment.capture``, so both
roles take money at the counter. Every cash collection in the system requires
the ACTING cashier's own open ``CashSession``:

* ``finance.settle_outstanding_balance`` (check-out settlement),
* ``guest_services.settle_walk_in_laundry`` (laundry),
* ``pos.capture_direct_tender`` (POS tender).

Neither role could create or close that session, and the only screen that
offered it was the POS console - which is not part of the front-desk workflow.
The result was that a receptionist could not take a cash payment for anything,
and the operator reported it as "receptionists cannot open cash sessions".

The grants added here are strictly the cashier's OWN drawer:

* ``CashSessionListView`` scopes a caller without ``reports.financial.view`` to
  ``cashier=request.user``, so a receptionist can never read another cashier's
  session;
* ``cash_session_service.open_cash_session`` / ``close_cash_session`` refuse to
  touch a session that belongs to somebody else;
* nothing about finance reporting, variance review, refund approval or
  reconciliation is granted, and no existing grant is revoked.

The reverse deletes ONLY the grants this migration created, tracked through an
audit marker, so a later deliberate revocation is never undone by a rollback of
this migration.
"""
from django.db import migrations

ACTION = "FRONT_DESK_CASH_SESSION_CAPABILITY_0035"
CAPABILITY_CODES = ("cash_session.open", "cash_session.close")
ROLES = ("RECEPTIONIST", "FRONT_DESK_SUPERVISOR")


def forwards(apps, schema_editor):
    Capability = apps.get_model("accounts", "Capability")
    RoleCapability = apps.get_model("accounts", "RoleCapability")
    AuditLog = apps.get_model("audit", "AuditLog")
    db = schema_editor.connection.alias

    created_pks = []
    missing_capabilities = []
    for code in CAPABILITY_CODES:
        capability = Capability.objects.using(db).filter(code=code).first()
        if capability is None:
            # The catalogue rows are seeded by 0003/0014; record rather than
            # invent one, so a bad database is visible instead of silently
            # producing an inert grant.
            missing_capabilities.append(code)
            continue
        for role in ROLES:
            grant, created = RoleCapability.objects.using(db).get_or_create(
                role=role, capability=capability,
            )
            if created:
                created_pks.append(grant.pk)

    AuditLog.objects.using(db).create(
        action=ACTION,
        object_type="accounts.rolecapability",
        object_id=",".join(str(pk) for pk in created_pks),
        metadata={
            "capabilities": list(CAPABILITY_CODES),
            "roles": list(ROLES),
            "created_pks": created_pks,
            "missing_capabilities": missing_capabilities,
            "reason": "Front desk must be able to open and close its own cash drawer.",
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
    dependencies = [("accounts", "0034_expense_request_capability")]

    operations = [migrations.RunPython(forwards, backwards)]
