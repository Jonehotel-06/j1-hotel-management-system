"""Seed ``sign_in.any_device`` — management authority as a capability.

WHY
===
"Who may sign in from any device instead of only from an enrolled front-desk
desktop" used to be a hard-coded role list inside ``desktop_policy``. With one
standard role per department (``apps.accounts.departments``) a departmental
title no longer exists to carry that freedom, so the freedom must be expressed
as what it actually is: an authority that can be granted explicitly, reviewed,
and revoked — ``sign_in.any_device``.

Grants seeded here are exactly the accounts that were already exempt: the three
global management roles. Nobody gains device freedom that did not have it, and
nobody loses it. Anybody else receives it only as an individually granted,
reasoned ``UserCapabilityOverride`` (audited), never by being given a bigger
title.

Reversal removes only the rows this migration added.
"""
from django.db import migrations

CAPABILITY = (
    "sign_in.any_device",
    "Sign in from any device",
    "Identity",
    "Sign in, refresh a session and use operational screens from any device instead of only "
    "from an enrolled front-desk desktop.",
)

EXEMPT_ROLES = ("ADMIN", "MANAGER", "GENERAL_MANAGER")


def seed(apps, schema_editor):
    Capability = apps.get_model("accounts", "Capability")
    RoleCapability = apps.get_model("accounts", "RoleCapability")
    code, name, category, description = CAPABILITY
    capability, _ = Capability.objects.update_or_create(
        code=code,
        defaults={"name": name, "category": category, "description": description, "is_active": True},
    )
    for role in EXEMPT_ROLES:
        RoleCapability.objects.get_or_create(role=role, capability=capability)


def unseed(apps, schema_editor):
    Capability = apps.get_model("accounts", "Capability")
    Capability.objects.filter(code=CAPABILITY[0]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0027_normalize_department_roles"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
