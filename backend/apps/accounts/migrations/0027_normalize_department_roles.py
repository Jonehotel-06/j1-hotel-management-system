"""Move existing accounts onto their department's ONE standard role.

WHAT THIS MIGRATION DOES, AND IN WHAT ORDER
===========================================
1. Seeds the ``HR_OFFICER`` capability rows (the HR department's single standard
   role, added with the one-role-per-department model).
2. For every account still holding a retired departmental title
   (``BAR_MANAGER``, ``RESTAURANT_MANAGER``, ``KITCHEN_MANAGER``,
   ``KITCHEN_ASSISTANT``, ``HOUSEKEEPING_MANAGER``, ``HOUSEKEEPER``,
   ``MAINTENANCE_TECHNICIAN``, ``ACCOUNTS_MANAGER``, ``HR_MANAGER``,
   ``FRONT_DESK_SUPERVISOR``, ``INVENTORY_CLERK``):
   a. computes the capabilities the account actually holds today from the
      persisted ``RoleCapability`` rows;
   b. writes an explicit, reasoned ``UserCapabilityOverride`` GRANT for every one
      of those capabilities the department's standard role does not already
      hold — so nobody loses access to work they can do now, and each extra
      right becomes individually visible and reviewable;
   c. sets ``role`` to the department's standard role;
   d. writes an append-only ``AuditLog`` row recording the change.
3. Touches nothing else. No capability is granted to a department that did not
   already hold it somewhere in that department, no other role's grants change,
   and no operational record, order, stock or financial row is read or written.

SAFETY
======
* Idempotent — re-running finds no account on a retired title and does nothing.
* Additive evidence only — no ``RoleCapability`` row is deleted, so an auditor can
  still see what each historical title granted, and the API still resolves a
  legacy token's role through ``apps.accounts.departments.canonical_role``.
* Reversible in substance — the reverse operation restores the stored title from
  the audit trail. The capability grants it added are left in place on purpose:
  deleting a grant during a rollback could silently remove a permission somebody
  is using in production, which is worse than an extra visible grant. An
  operator who wants them gone can revoke them individually (they are now
  explicit rows, not an invisible title).
"""
from django.db import migrations

HR_OFFICER = "HR_OFFICER"

HR_OFFICER_CAPABILITIES = (
    "staff.profile.manage",
    "staff.view",
    "staff.manage",
    "shift.manage",
    "attendance.clock",
    "attendance.manage",
    "leave.request",
    "payroll.view",
    "payroll.manage",
)

HR_DEPARTMENT_CODE = "HR"

RETIRED_DEPARTMENTAL_ROLES = {
    "FRONT_DESK_SUPERVISOR": "RECEPTIONIST",
    "BAR_MANAGER": "BARTENDER",
    "RESTAURANT_MANAGER": "WAITER",
    "KITCHEN_MANAGER": "CHEF",
    "KITCHEN_ASSISTANT": "CHEF",
    "HOUSEKEEPING_MANAGER": "HOUSEKEEPING",
    "HOUSEKEEPER": "HOUSEKEEPING",
    "MAINTENANCE_TECHNICIAN": "MAINTENANCE",
    "ACCOUNTS_MANAGER": "ACCOUNTANT",
    "HR_MANAGER": "HR_OFFICER",
    "INVENTORY_CLERK": "STOREKEEPER",
}

DEPARTMENT_OF_ROLE = {
    "RECEPTIONIST": "FRONT_DESK",
    "CASHIER": "CASHIER",
    "BARTENDER": "BAR",
    "WAITER": "RESTAURANT",
    "CHEF": "KITCHEN",
    "HOUSEKEEPING": "HOUSEKEEPING",
    "MAINTENANCE": "MAINTENANCE",
    "STOREKEEPER": "INVENTORY",
    "PROCUREMENT_OFFICER": "PROCUREMENT",
    "ACCOUNTANT": "ACCOUNTS",
    "HR_OFFICER": "HR",
    "SECURITY": "SECURITY",
    "PORTER": "PORTER",
    "MANAGER": "MANAGEMENT",
}

#: Capabilities whose authority must stay individually visible. The override
#: reason says so, so an owner reviewing staff permissions sees them first.
SENSITIVE = {
    "payment.refund.approve",
    "expense.approve",
    "inventory.adjust.approve",
    "procurement.approve",
    "payroll.approve",
    "leave.approve",
    "attendance.manage",
    "shift.manage",
}

HR_CAPABILITY_DEFINITIONS = (
    (
        "HR_OFFICER",
        "The HR department's single standard role: staff records, shifts, attendance "
        "administration, leave requests and payroll preparation, with approvals granted "
        "explicitly per person.",
    ),
)


def seed_hr_officer(apps, schema_editor):
    Capability = apps.get_model("accounts", "Capability")
    RoleCapability = apps.get_model("accounts", "RoleCapability")
    definitions = {
        "staff.profile.manage": ("Manage staff operational profiles", "Staff operations"),
        "staff.view": ("View staff records", "Staff operations"),
        "staff.manage": ("Manage staff records", "Staff operations"),
        "shift.manage": ("Manage staff shifts", "Staff operations"),
        "attendance.clock": ("Clock attendance", "Staff operations"),
        "attendance.manage": ("Manage attendance", "Staff operations"),
        "leave.request": ("Request leave", "Staff operations"),
        "payroll.view": ("View payroll", "Payroll"),
        "payroll.manage": ("Prepare payroll", "Payroll"),
    }
    for code in HR_OFFICER_CAPABILITIES:
        name, category = definitions[code]
        capability, _ = Capability.objects.get_or_create(
            code=code, defaults={"name": name, "category": category, "description": ""}
        )
        RoleCapability.objects.get_or_create(role=HR_OFFICER, capability=capability)


def normalize_accounts(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    RoleCapability = apps.get_model("accounts", "RoleCapability")
    UserCapabilityOverride = apps.get_model("accounts", "UserCapabilityOverride")
    Capability = apps.get_model("accounts", "Capability")
    AuditLog = apps.get_model("audit", "AuditLog")

    grants_by_role = {}
    for row in RoleCapability.objects.filter(capability__is_active=True).values(
        "role", "capability__code"
    ):
        grants_by_role.setdefault(row["role"], set()).add(row["capability__code"])

    capabilities = {capability.code: capability for capability in Capability.objects.all()}

    for account in User.objects.filter(role__in=list(RETIRED_DEPARTMENTAL_ROLES)).order_by("pk"):
        previous_role = account.role
        canonical = RETIRED_DEPARTMENTAL_ROLES[previous_role]
        department = DEPARTMENT_OF_ROLE.get(canonical, "")
        held = set(grants_by_role.get(previous_role, set()))
        canonical_grants = set(grants_by_role.get(canonical, set()))
        missing = sorted(code for code in held - canonical_grants if code in capabilities)
        for code in missing:
            sensitive = code in SENSITIVE
            reason = (
                f"Preserved from the retired departmental title {previous_role} during the "
                "one-role-per-department migration"
                + (" — this is a sensitive approval/administration right; review whether this "
                   "person still needs it" if sensitive else "")
            )
            UserCapabilityOverride.objects.get_or_create(
                user=account,
                capability=capabilities[code],
                defaults={
                    "effect": "GRANT",
                    "reason": reason[:255],
                },
            )
        account.role = canonical
        account.save(update_fields=["role"])
        AuditLog.objects.create(
            action="STAFF_DEPARTMENT_ROLE_NORMALIZED",
            object_type="accounts.user",
            object_id=str(account.pk),
            summary=(
                f"{previous_role} → {canonical} ({department or 'global'}): one standard role per department"
            )[:255],
            changes={"role": [previous_role, canonical]},
            metadata={
                "previous_role": previous_role,
                "canonical_role": canonical,
                "department": department,
                "preserved_capabilities": missing,
                "sensitive_capabilities": [code for code in missing if code in SENSITIVE],
                "migration": "accounts.0027_normalize_department_roles",
            },
        )


def restore_titles(apps, schema_editor):
    """Rollback: restore the stored title recorded in the audit trail."""
    User = apps.get_model("accounts", "User")
    AuditLog = apps.get_model("audit", "AuditLog")
    for row in AuditLog.objects.filter(action="STAFF_DEPARTMENT_ROLE_NORMALIZED").order_by("-pk"):
        previous_role = (row.changes or {}).get("role", [None, None])[0]
        canonical = (row.changes or {}).get("role", [None, None])[1]
        if not previous_role or not canonical:
            continue
        User.objects.filter(pk=row.object_id, role=canonical).update(role=previous_role)


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0026_alter_rolecapability_role_alter_user_role_and_more"),
        ("audit", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_hr_officer, migrations.RunPython.noop),
        migrations.RunPython(normalize_accounts, restore_titles),
    ]
