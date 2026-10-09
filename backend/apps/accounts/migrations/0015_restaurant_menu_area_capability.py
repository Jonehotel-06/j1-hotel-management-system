"""Add a restaurant-scoped menu-management capability.

Department menu roles must not inherit the global POS menu permission: that
permission intentionally grants access to every service area's catalog.
"""
from django.db import migrations


CAPABILITY = (
    "restaurant.menu.manage",
    "Manage restaurant menu",
    "Restaurant",
    "Create and manage restaurant-category menu items.",
)
MANAGEMENT_ROLES = ("ADMIN", "MANAGER", "GENERAL_MANAGER", "RESTAURANT_MANAGER")


def grant_restaurant_menu_capability(apps, schema_editor):
    Capability = apps.get_model("accounts", "Capability")
    RoleCapability = apps.get_model("accounts", "RoleCapability")

    capability, _ = Capability.objects.update_or_create(
        code=CAPABILITY[0],
        defaults={
            "name": CAPABILITY[1],
            "category": CAPABILITY[2],
            "description": CAPABILITY[3],
            "is_active": True,
        },
    )
    for role in MANAGEMENT_ROLES:
        RoleCapability.objects.get_or_create(role=role, capability=capability)

    # Restaurant managers are department-scoped. The earlier seed gave them
    # pos.menu.manage, which would bypass all restaurant/bar catalog scoping.
    global_menu = Capability.objects.filter(code="pos.menu.manage").first()
    if global_menu:
        RoleCapability.objects.filter(role="RESTAURANT_MANAGER", capability=global_menu).delete()


def restore_legacy_restaurant_manager_menu_grant(apps, schema_editor):
    Capability = apps.get_model("accounts", "Capability")
    RoleCapability = apps.get_model("accounts", "RoleCapability")

    capability = Capability.objects.filter(code=CAPABILITY[0]).first()
    if capability:
        RoleCapability.objects.filter(capability=capability).delete()
        capability.delete()

    global_menu = Capability.objects.filter(code="pos.menu.manage").first()
    if global_menu:
        RoleCapability.objects.get_or_create(role="RESTAURANT_MANAGER", capability=global_menu)


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0014_seed_department_capabilities"),
    ]

    operations = [
        migrations.RunPython(
            grant_restaurant_menu_capability,
            restore_legacy_restaurant_manager_menu_grant,
        ),
    ]
