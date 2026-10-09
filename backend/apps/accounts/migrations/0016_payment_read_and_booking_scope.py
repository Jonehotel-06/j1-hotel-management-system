"""Separate payment-ledger access from booking access, and narrow booking reads.

Payment/receipt surfaces now use explicit capabilities. Operational roles which
work through assigned queues (cashier, housekeeping, maintenance) must not gain
full guest booking/receipt access as an accidental consequence of being staff.
"""
from django.db import migrations


CAPABILITIES = (
    ("payment.read", "Read payment ledger", "Finance", "Review payment and refund records."),
    ("payment.receipt.send", "Send guest receipts", "Finance", "Send a verified booking receipt to its guest."),
)
PAYMENT_READ_ROLES = (
    "MANAGER",
    "RECEPTIONIST",
    "FRONT_DESK_SUPERVISOR",
    "GENERAL_MANAGER",
    "ACCOUNTS_MANAGER",
    "ACCOUNTANT",
)
RECEIPT_SEND_ROLES = (
    "MANAGER",
    "RECEPTIONIST",
    "FRONT_DESK_SUPERVISOR",
    "GENERAL_MANAGER",
)
BOOKING_READ_REMOVALS = ("CASHIER", "HOUSEKEEPING", "MAINTENANCE")


def apply_security_grants(apps, schema_editor):
    Capability = apps.get_model("accounts", "Capability")
    RoleCapability = apps.get_model("accounts", "RoleCapability")

    by_code = {}
    for code, name, category, description in CAPABILITIES:
        capability, _ = Capability.objects.update_or_create(
            code=code,
            defaults={
                "name": name,
                "category": category,
                "description": description,
                "is_active": True,
            },
        )
        by_code[code] = capability

    for role in PAYMENT_READ_ROLES:
        RoleCapability.objects.get_or_create(role=role, capability=by_code["payment.read"])
    for role in RECEIPT_SEND_ROLES:
        RoleCapability.objects.get_or_create(role=role, capability=by_code["payment.receipt.send"])

    RoleCapability.objects.filter(
        role__in=BOOKING_READ_REMOVALS,
        capability__code="booking.read",
    ).delete()


def reverse_security_grants(apps, schema_editor):
    Capability = apps.get_model("accounts", "Capability")
    RoleCapability = apps.get_model("accounts", "RoleCapability")

    RoleCapability.objects.filter(capability__code__in=[row[0] for row in CAPABILITIES]).delete()
    Capability.objects.filter(code__in=[row[0] for row in CAPABILITIES]).delete()

    booking_read = Capability.objects.filter(code="booking.read").first()
    if booking_read:
        for role in BOOKING_READ_REMOVALS:
            RoleCapability.objects.get_or_create(role=role, capability=booking_read)


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0015_restaurant_menu_area_capability"),
    ]

    operations = [
        migrations.RunPython(apply_security_grants, reverse_security_grants),
    ]
