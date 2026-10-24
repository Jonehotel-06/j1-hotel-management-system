"""Seed the unopened-item return capabilities and their role grants.

The bar-return workflow added two capability atoms:

``pos.return.record``
    record untouched, unopened items a guest hands back against a delivered
    bar/restaurant order (stock returns, bill correction, refund obligation);
``pos.return.refund``
    actually pay that obligation out (cash drawer, card/bank confirmation),
    still inside the existing maker-checker money controls.

As with every other capability, the *persisted* ``RoleCapability`` rows are the
authorization source — ``capability_codes_for_user`` never reads the Python
catalogue at request time — so an existing database needs this migration, not
just the catalogue edit.

Grants are deliberately narrow and mirror the existing least privilege:

* ``pos.return.record``  — the roles that already run bar/restaurant service,
  plus the cashier seat that handles the money, plus management;
* ``pos.return.refund``  — the roles that already hold ``payment.capture`` and
  run a drawer, plus management. A bartender or waiter can record the return
  but cannot pay money out of a cashier's drawer.

Reversal removes only the rows this migration added.
"""
from django.db import migrations

CAPABILITIES = (
    (
        "pos.return.record",
        "Record unopened-item returns",
        "POS",
        "Record untouched, unopened items returned by a guest against a delivered bar/restaurant order.",
    ),
    (
        "pos.return.refund",
        "Pay out return refunds",
        "POS",
        "Complete the refund owed when unopened items are returned, within the configured money controls.",
    ),
)

RECORD_ROLES = ("ADMIN", "MANAGER", "GENERAL_MANAGER", "BAR_MANAGER", "BARTENDER",
                "RESTAURANT_MANAGER", "WAITER", "CASHIER")
REFUND_ROLES = ("ADMIN", "MANAGER", "GENERAL_MANAGER", "BAR_MANAGER", "RESTAURANT_MANAGER", "CASHIER")


def seed(apps, schema_editor):
    Capability = apps.get_model("accounts", "Capability")
    RoleCapability = apps.get_model("accounts", "RoleCapability")
    by_code = {}
    for code, name, category, description in CAPABILITIES:
        capability, _ = Capability.objects.update_or_create(
            code=code, defaults={"name": name, "category": category, "description": description, "is_active": True}
        )
        by_code[code] = capability
    for role in RECORD_ROLES:
        RoleCapability.objects.get_or_create(role=role, capability=by_code["pos.return.record"])
    for role in REFUND_ROLES:
        RoleCapability.objects.get_or_create(role=role, capability=by_code["pos.return.refund"])


def unseed(apps, schema_editor):
    RoleCapability = apps.get_model("accounts", "RoleCapability")
    Capability = apps.get_model("accounts", "Capability")
    RoleCapability.objects.filter(capability__code__in=[code for code, *_ in CAPABILITIES]).delete()
    Capability.objects.filter(code__in=[code for code, *_ in CAPABILITIES]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0024_accounts_manager_attendance_clock"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
