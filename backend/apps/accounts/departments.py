"""ONE standard role per department — the single source of truth.

THE OWNER'S RULE
================
"Each department must have exactly one standard department role, with no higher
or lower staff role hierarchy inside that department."

WHY THIS MODULE EXISTS
======================
The role catalogue grew one role per *job title*, which is not the same thing as
one role per *department*. That produced two parallel hierarchies:

* Bar:            ``BAR_MANAGER``      → ``BARTENDER``
* Restaurant:     ``RESTAURANT_MANAGER`` → ``WAITER``
* Kitchen:        ``KITCHEN_MANAGER``  → ``CHEF`` / ``KITCHEN_ASSISTANT``
* Housekeeping:   ``HOUSEKEEPING_MANAGER`` → ``HOUSEKEEPER`` / ``HOUSEKEEPING``
* Front desk:     ``FRONT_DESK_SUPERVISOR`` → ``RECEPTIONIST``
* Accounts:       ``ACCOUNTS_MANAGER`` → ``ACCOUNTANT``
* Human resources: ``HR_MANAGER`` → (no worker role at all)
* Stores:         ``INVENTORY_CLERK`` (a second identifier for ``STOREKEEPER``)

A hierarchy inside a department is exactly what the owner asked to remove: two
members of the bar team doing the same work, one of whom happens to hold a
title, must not have two different permission surfaces. The role is the
DEPARTMENT; what an individual may additionally do (approve a refund above a
threshold, review a stock count) is authorized *explicitly and separately* by a
capability grant that is audited — and, for money movements, still requires a
second person (the existing maker-checker controls).

WHAT THIS MODULE DOES NOT DO
============================
* It never grants one department another department's capabilities. A single
  role per department is emphatically not "every role may do everything".
* It never touches ``ADMIN`` or ``GENERAL_MANAGER``: those are global oversight
  roles, not rungs on a departmental ladder, and their policy is unchanged.
* It never renames or deletes a stored ``User.Role`` value. Existing accounts,
  audit rows, migrations and API consumers that reference a historical code keep
  working; the codes below are *retired for new assignments* and resolve to the
  department's canonical role.

ROLLOUT
=======
* ``canonical_role()``            — resolve any historical code to its department role.
* ``department_for_role()``       — which department a role belongs to.
* ``assert_assignable_role()``    — refuse a retired departmental code on a new or
                                    changed assignment (the API and Django admin
                                    both call it), so no new hierarchy can be created.
* ``apps.accounts.migrations.0026_normalize_department_roles`` — moves existing
  accounts onto the canonical role and preserves any capability the person
  already held as an explicit, reasoned ``UserCapabilityOverride`` grant.
"""
from __future__ import annotations

from .models import User, Workstation

#: The department's ONE standard role. Every operational department the hotel
#: runs appears exactly once, and every entry points at a single role code.
DEPARTMENT_ROLES: dict[str, str] = {
    Workstation.Department.FRONT_DESK: User.Role.RECEPTIONIST,
    Workstation.Department.CASHIER: User.Role.CASHIER,
    Workstation.Department.BAR: User.Role.BARTENDER,
    Workstation.Department.RESTAURANT: User.Role.WAITER,
    Workstation.Department.KITCHEN: User.Role.CHEF,
    Workstation.Department.HOUSEKEEPING: User.Role.HOUSEKEEPING,
    Workstation.Department.MAINTENANCE: User.Role.MAINTENANCE,
    Workstation.Department.INVENTORY: User.Role.STOREKEEPER,
    Workstation.Department.PROCUREMENT: User.Role.PROCUREMENT_OFFICER,
    Workstation.Department.ACCOUNTS: User.Role.ACCOUNTANT,
    Workstation.Department.HR: User.Role.HR_OFFICER,
    Workstation.Department.SECURITY: User.Role.SECURITY,
    Workstation.Department.PORTER: User.Role.PORTER,
    Workstation.Department.MANAGEMENT: User.Role.MANAGER,
}

#: Historical departmental titles retired for NEW assignments, mapped to the
#: single standard role of the department they belonged to. Existing accounts
#: and historical records keep their stored value (nothing is rewritten by
#: merely importing this module); the migration and the assignment boundary
#: move people onto the canonical role with their capabilities preserved.
RETIRED_DEPARTMENTAL_ROLES: dict[str, str] = {
    User.Role.FRONT_DESK_SUPERVISOR: User.Role.RECEPTIONIST,
    User.Role.BAR_MANAGER: User.Role.BARTENDER,
    User.Role.RESTAURANT_MANAGER: User.Role.WAITER,
    User.Role.KITCHEN_MANAGER: User.Role.CHEF,
    User.Role.KITCHEN_ASSISTANT: User.Role.CHEF,
    User.Role.HOUSEKEEPING_MANAGER: User.Role.HOUSEKEEPING,
    User.Role.HOUSEKEEPER: User.Role.HOUSEKEEPING,
    User.Role.MAINTENANCE_TECHNICIAN: User.Role.MAINTENANCE,
    User.Role.ACCOUNTS_MANAGER: User.Role.ACCOUNTANT,
    User.Role.HR_MANAGER: User.Role.HR_OFFICER,
    User.Role.INVENTORY_CLERK: User.Role.STOREKEEPER,
}

#: Global oversight roles. These are NOT department roles and deliberately keep
#: their own policy: an administrator or general manager is not a rung above the
#: bar team, they are outside every department ladder.
GLOBAL_ROLES = frozenset({User.Role.ADMIN, User.Role.GENERAL_MANAGER})

#: Every role that may be assigned to an account after this change.
ASSIGNABLE_ROLES = frozenset(set(DEPARTMENT_ROLES.values()) | GLOBAL_ROLES | {User.Role.GUEST})

#: What a person may additionally do beyond their department role, granted
#: per-user and audited (``UserCapabilityOverride``). Listed here so the rule is
#: explicit rather than implied: these are the capabilities that must never be
#: handed out merely by holding a job title.
SENSITIVE_APPROVAL_CAPABILITIES = frozenset({
    "payment.refund.approve",
    "expense.approve",
    "inventory.adjust.approve",
    "procurement.approve",
    "payroll.approve",
    "leave.approve",
    "attendance.manage",
    "shift.manage",
})

ROLE_TO_DEPARTMENT: dict[str, str] = {
    role: department for department, role in DEPARTMENT_ROLES.items()
}


def canonical_role(role: str) -> str:
    """Resolve a historical departmental title to its department's one role."""
    role = str(role or "")
    return RETIRED_DEPARTMENTAL_ROLES.get(role, role)


def is_retired_role(role: str) -> bool:
    return str(role or "") in RETIRED_DEPARTMENTAL_ROLES


def department_for_role(role: str) -> str:
    """Which department a role belongs to (``""`` for global/oversight roles)."""
    canonical = canonical_role(role)
    if canonical in GLOBAL_ROLES:
        return ""
    return ROLE_TO_DEPARTMENT.get(canonical, "")


def department_for_user(user) -> str:
    """The department context of a staff account.

    A staff profile may record a department explicitly; when it does and that
    department has a canonical role, the ROLE must agree with it. When it does
    not, the role decides. Nothing here lets a request payload choose a
    department: this reads stored records only.
    """
    if user is None:
        return ""
    profile_department = ""
    try:
        profile_department = str(getattr(user.staff_profile, "department", "") or "")
    except Exception:  # noqa: BLE001 - no profile is a normal state
        profile_department = ""
    from_role = department_for_role(getattr(user, "role", ""))
    if profile_department:
        code = _normalize_department_code(profile_department)
        if code in DEPARTMENT_ROLES:
            return code
    return from_role


def _normalize_department_code(value: str) -> str:
    raw = str(value or "").strip().upper().replace("-", "_").replace(" ", "_").replace("&", "AND")
    aliases = {
        "FRONT_OFFICE": Workstation.Department.FRONT_DESK,
        "RECEPTION": Workstation.Department.FRONT_DESK,
        "FRONT_DESK_AND_RECEPTION": Workstation.Department.FRONT_DESK,
        "FOOD_AND_BEVERAGE": Workstation.Department.RESTAURANT,
        "F_AND_B": Workstation.Department.RESTAURANT,
        "KITCHEN_AND_RESTAURANT": Workstation.Department.KITCHEN,
        "STORES": Workstation.Department.INVENTORY,
        "STORE": Workstation.Department.INVENTORY,
        "FINANCE": Workstation.Department.ACCOUNTS,
        "HUMAN_RESOURCES": Workstation.Department.HR,
        "ADMIN": Workstation.Department.MANAGEMENT,
        "ADMINISTRATION": Workstation.Department.MANAGEMENT,
    }
    return aliases.get(raw, raw)


def department_of_role(role: str) -> str:
    """Alias kept for callers that read better as "the department of this role"."""
    return department_for_role(role)


def is_cross_department(user, department: str) -> bool:
    """True when ``department`` is not this account's own department.

    Cross-department duty is possible, but only as an explicitly audited
    capability grant — never because the caller sent a different department in
    the request body or URL.
    """
    own = department_for_user(user)
    target = _normalize_department_code(department)
    if not own or not target:
        return False
    return own != target


class RetiredDepartmentalRole(ValueError):
    """Raised when a new assignment uses a retired departmental title."""


def assert_assignable_role(role: str) -> str:
    """Return the canonical role for an assignment, or refuse it.

    Called by every staff-account write path. A retired departmental title is
    refused with an explanation and the department's one role, so an operator
    cannot recreate the hierarchy through the API or the Django admin.
    """
    role = str(role or "").strip().upper()
    if not role:
        return role
    if role in RETIRED_DEPARTMENTAL_ROLES:
        canonical = RETIRED_DEPARTMENTAL_ROLES[role]
        department = department_for_role(canonical)
        raise RetiredDepartmentalRole(
            f"'{role}' is a retired departmental title. Each department has ONE standard role; "
            f"assign '{canonical}' for the {department or 'department'} team instead, and grant any "
            "extra authority as an explicit, audited per-user capability."
        )
    return role


def department_role_choices():
    """``[(role, label)]`` for staff-account forms: one role per department."""
    choices = []
    for department, role in DEPARTMENT_ROLES.items():
        label = dict(Workstation.Department.choices).get(department, department)
        choices.append((role, f"{label} · {User.Role(role).label}"))
    for role in sorted(GLOBAL_ROLES):
        choices.append((role, f"Global · {User.Role(role).label}"))
    choices.append((User.Role.GUEST, "Guest"))
    return choices
