"""ONE standard role per department — the single source of truth.

APPROVED 11-DEPARTMENT / 11-ROLE ARCHITECTURE
=============================================
Active staff departments and assignable staff roles are consolidated to the
exact 11 owner-approved operational teams (each with ONE standard staff role):

1. General Manager           (``MANAGEMENT``         ↔ ``GENERAL_MANAGER``)
2. Accountant                (``ACCOUNTS``           ↔ ``ACCOUNTANT``)
3. Admin Manager/Procurement (``PROCUREMENT``        ↔ ``PROCUREMENT_OFFICER``)
4. Chefs                     (``KITCHEN``            ↔ ``CHEF``)
5. Kitchen Assistant         (``KITCHEN_ASSISTANT``  ↔ ``KITCHEN_ASSISTANT``)
6. Waiters/Waitresses        (``RESTAURANT``         ↔ ``WAITER``)
7. Porters                   (``PORTER``             ↔ ``PORTER``)
8. Receptionist              (``FRONT_DESK``         ↔ ``RECEPTIONIST``)
9. Housekeepers              (``HOUSEKEEPING``       ↔ ``HOUSEKEEPING``)
10. Store Keeper             (``INVENTORY``          ↔ ``STOREKEEPER``)
11. Security                 (``SECURITY``           ↔ ``SECURITY``)

System non-departmental accounts:
* ``ADMIN`` — Hotel Owner / System Superuser (global oversight, non-employee).
* ``GUEST`` — Public customer booking account (not a staff role).

CONSOLIDATION RULES
===================
* Former HR staff/departments (``HR``, ``HR_OFFICER``, ``HR_MANAGER``) resolve
  to **Admin Manager/Procurement** (``PROCUREMENT`` / ``PROCUREMENT_OFFICER``).
* Former Maintenance staff/departments (``MAINTENANCE``, ``MAINTENANCE_TECHNICIAN``)
  resolve to **Housekeepers** (``HOUSEKEEPING`` / ``HOUSEKEEPING``), while
  preserving the Maintenance work-order module and ``Room.Status.MAINTENANCE``.
* Former Bar and Cashier staff/departments (``BAR``, ``BARTENDER``,
  ``BAR_MANAGER``, ``CASHIER``) resolve to **Waiters/Waitresses**
  (``RESTAURANT`` / ``WAITER``), while preserving POS Bar mode and menu areas.
* Former intra-department hierarchy titles (``MANAGER``, ``FRONT_DESK_SUPERVISOR``,
  ``RESTAURANT_MANAGER``, ``KITCHEN_MANAGER``, ``HOUSEKEEPING_MANAGER``,
  ``HOUSEKEEPER``, ``ACCOUNTS_MANAGER``, ``INVENTORY_CLERK``) resolve to their
  department's single approved role.
* Historical ``User.Role`` constants remain defined in ``accounts.models`` so
  audit logs, JWT history, and reversible migrations never break; they are
  strictly refused on new or updated assignments via ``assert_assignable_role``.
"""
from __future__ import annotations

import re

from .models import User, Workstation

#: The 11 approved operational departments and each department's ONE standard role.
DEPARTMENT_ROLES: dict[str, str] = {
    Workstation.Department.MANAGEMENT: User.Role.GENERAL_MANAGER,
    Workstation.Department.ACCOUNTS: User.Role.ACCOUNTANT,
    Workstation.Department.PROCUREMENT: User.Role.PROCUREMENT_OFFICER,
    Workstation.Department.KITCHEN: User.Role.CHEF,
    Workstation.Department.KITCHEN_ASSISTANT: User.Role.KITCHEN_ASSISTANT,
    Workstation.Department.RESTAURANT: User.Role.WAITER,
    Workstation.Department.PORTER: User.Role.PORTER,
    Workstation.Department.FRONT_DESK: User.Role.RECEPTIONIST,
    Workstation.Department.HOUSEKEEPING: User.Role.HOUSEKEEPING,
    Workstation.Department.INVENTORY: User.Role.STOREKEEPER,
    Workstation.Department.SECURITY: User.Role.SECURITY,
}

#: Historical departmental titles retired for NEW assignments, mapped to the
#: single approved role of the consolidated department they belong to.
RETIRED_DEPARTMENTAL_ROLES: dict[str, str] = {
    User.Role.MANAGER: User.Role.GENERAL_MANAGER,
    User.Role.FRONT_DESK_SUPERVISOR: User.Role.RECEPTIONIST,
    User.Role.RESTAURANT_MANAGER: User.Role.WAITER,
    User.Role.BAR_MANAGER: User.Role.WAITER,
    User.Role.BARTENDER: User.Role.WAITER,
    # Cashier was a standalone department/role; duties belong to Waiters/Waitresses.
    User.Role.CASHIER: User.Role.WAITER,
    User.Role.KITCHEN_MANAGER: User.Role.CHEF,
    User.Role.HOUSEKEEPING_MANAGER: User.Role.HOUSEKEEPING,
    User.Role.HOUSEKEEPER: User.Role.HOUSEKEEPING,
    User.Role.MAINTENANCE: User.Role.HOUSEKEEPING,
    User.Role.MAINTENANCE_TECHNICIAN: User.Role.HOUSEKEEPING,
    User.Role.ACCOUNTS_MANAGER: User.Role.ACCOUNTANT,
    User.Role.HR_MANAGER: User.Role.PROCUREMENT_OFFICER,
    User.Role.HR_OFFICER: User.Role.PROCUREMENT_OFFICER,
    User.Role.INVENTORY_CLERK: User.Role.STOREKEEPER,
}

#: Historical labels for removed standalone departments. Stored rows normalize
#: to their consolidated approved department for read/compatibility checks,
#: while new assignments must select the approved department explicitly.
REMOVED_CASHIER_DEPARTMENT_CODES = frozenset({"CASHIER", "CASHIER_TILL"})
REMOVED_BAR_DEPARTMENT_CODES = frozenset({"BAR", "BAR_SERVICE"})
REMOVED_MAINTENANCE_DEPARTMENT_CODES = frozenset({"MAINTENANCE"})
REMOVED_HR_DEPARTMENT_CODES = frozenset({"HR", "HUMAN_RESOURCES"})

REMOVED_STANDALONE_DEPARTMENT_MESSAGES: dict[str, str] = {
    "CASHIER": (
        "The Cashier department was merged into Restaurants; choose RESTAURANT "
        "and assign the existing WAITER role."
    ),
    "CASHIER_TILL": (
        "The Cashier department was merged into Restaurants; choose RESTAURANT "
        "and assign the existing WAITER role."
    ),
    "BAR": (
        "The standalone Bar department was consolidated into Waiters/Waitresses; "
        "choose RESTAURANT and assign the WAITER role."
    ),
    "BAR_SERVICE": (
        "The standalone Bar department was consolidated into Waiters/Waitresses; "
        "choose RESTAURANT and assign the WAITER role."
    ),
    "MAINTENANCE": (
        "The standalone Maintenance department was consolidated into Housekeepers; "
        "choose HOUSEKEEPING and assign the HOUSEKEEPING role."
    ),
    "HR": (
        "The standalone Human Resources department was consolidated into "
        "Admin Manager/Procurement; choose PROCUREMENT and assign the PROCUREMENT_OFFICER role."
    ),
    "HUMAN_RESOURCES": (
        "The standalone Human Resources department was consolidated into "
        "Admin Manager/Procurement; choose PROCUREMENT and assign the PROCUREMENT_OFFICER role."
    ),
}


def _raw_department_token(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", str(value or "").strip().upper()).strip("_")


def is_removed_cashier_department(value: str) -> bool:
    """True for a stored or submitted label of the removed Cashier department."""
    return _raw_department_token(value) in REMOVED_CASHIER_DEPARTMENT_CODES


def removed_department_message(value: str) -> str:
    """Return consolidation guidance when ``value`` names a retired standalone department."""
    return REMOVED_STANDALONE_DEPARTMENT_MESSAGES.get(_raw_department_token(value), "")


def is_removed_standalone_department(value: str) -> bool:
    """True when ``value`` names any retired standalone staff department."""
    return bool(removed_department_message(value))


#: Non-departmental global system owner role (Hotel Owner / Administrator).
GLOBAL_ROLES = frozenset({User.Role.ADMIN})

#: Roles that carry hotel-wide cross-department operational oversight when their
#: own persisted account context is valid.
CROSS_DEPARTMENT_OVERSIGHT_ROLES = frozenset({User.Role.ADMIN, User.Role.GENERAL_MANAGER})

#: Every role that may be assigned to an account in the consolidated system:
#: the 11 approved staff roles + ADMIN (owner) + GUEST (customer).
ASSIGNABLE_ROLES = frozenset(set(DEPARTMENT_ROLES.values()) | GLOBAL_ROLES | {User.Role.GUEST})

#: Sensitive maker-checker approvals that must never be handed out merely by
#: holding an operational job title.
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
    """Resolve a historical departmental title to its approved department role."""
    role = str(role or "")
    return RETIRED_DEPARTMENTAL_ROLES.get(role, role)


def is_retired_role(role: str) -> bool:
    return str(role or "") in RETIRED_DEPARTMENTAL_ROLES


def department_for_role(role: str) -> str:
    """Which department a role belongs to (``""`` for non-departmental global roles)."""
    canonical = canonical_role(role)
    if canonical in GLOBAL_ROLES:
        return ""
    return ROLE_TO_DEPARTMENT.get(canonical, "")


def _normalize_department_code(value: str) -> str:
    """Normalize persisted department codes and established display aliases."""
    raw = _raw_department_token(value)
    aliases = {
        # 1. General Manager
        "MANAGEMENT": Workstation.Department.MANAGEMENT,
        "GENERAL_MANAGER": Workstation.Department.MANAGEMENT,
        "EXECUTIVE_MANAGEMENT": Workstation.Department.MANAGEMENT,
        "ADMIN": Workstation.Department.MANAGEMENT,
        "ADMINISTRATION": Workstation.Department.MANAGEMENT,
        # 2. Accountant
        "ACCOUNTS": Workstation.Department.ACCOUNTS,
        "ACCOUNTANT": Workstation.Department.ACCOUNTS,
        "ACCOUNTS_FINANCE": Workstation.Department.ACCOUNTS,
        "ACCOUNTS_AND_FINANCE": Workstation.Department.ACCOUNTS,
        "FINANCE": Workstation.Department.ACCOUNTS,
        "ACCOUNTING": Workstation.Department.ACCOUNTS,
        # 3. Admin Manager/Procurement (includes consolidated HR)
        "PROCUREMENT": Workstation.Department.PROCUREMENT,
        "ADMIN_PROCUREMENT": Workstation.Department.PROCUREMENT,
        "ADMIN_MANAGER_PROCUREMENT": Workstation.Department.PROCUREMENT,
        "ADMINISTRATION_PROCUREMENT": Workstation.Department.PROCUREMENT,
        "ADMINISTRATION_AND_PROCUREMENT": Workstation.Department.PROCUREMENT,
        "ADMIN_MANAGEMENT_PROCUREMENT": Workstation.Department.PROCUREMENT,
        "HR": Workstation.Department.PROCUREMENT,
        "HUMAN_RESOURCES": Workstation.Department.PROCUREMENT,
        # 4. Chefs
        "KITCHEN": Workstation.Department.KITCHEN,
        "CHEFS": Workstation.Department.KITCHEN,
        "CHEF": Workstation.Department.KITCHEN,
        "KITCHEN_AND_RESTAURANT": Workstation.Department.KITCHEN,
        "KITCHEN_CULINARY_PRODUCTION": Workstation.Department.KITCHEN,
        # 5. Kitchen Assistant
        "KITCHEN_ASSISTANT": Workstation.Department.KITCHEN_ASSISTANT,
        "KITCHEN_ASSISTANTS": Workstation.Department.KITCHEN_ASSISTANT,
        # 6. Waiters/Waitresses (includes consolidated Bar & Cashier)
        "RESTAURANT": Workstation.Department.RESTAURANT,
        "WAITERS_WAITRESSES": Workstation.Department.RESTAURANT,
        "WAITER": Workstation.Department.RESTAURANT,
        "WAITERS": Workstation.Department.RESTAURANT,
        "WAITRESSES": Workstation.Department.RESTAURANT,
        "RESTAURANT_BAR": Workstation.Department.RESTAURANT,
        "RESTAURANT_BAR_SERVICE": Workstation.Department.RESTAURANT,
        "RESTAURANT_AND_BAR_SERVICE": Workstation.Department.RESTAURANT,
        "RESTAURANT_SERVICE": Workstation.Department.RESTAURANT,
        "FOOD_AND_BEVERAGE": Workstation.Department.RESTAURANT,
        "F_AND_B": Workstation.Department.RESTAURANT,
        "BAR": Workstation.Department.RESTAURANT,
        "BAR_SERVICE": Workstation.Department.RESTAURANT,
        "CASHIER": Workstation.Department.RESTAURANT,
        "CASHIER_TILL": Workstation.Department.RESTAURANT,
        # 7. Porters
        "PORTER": Workstation.Department.PORTER,
        "PORTERS": Workstation.Department.PORTER,
        "PORTERING": Workstation.Department.PORTER,
        # 8. Receptionist
        "FRONT_DESK": Workstation.Department.FRONT_DESK,
        "FRONTDESK": Workstation.Department.FRONT_DESK,
        "RECEPTIONIST": Workstation.Department.FRONT_DESK,
        "RECEPTION": Workstation.Department.FRONT_DESK,
        "FRONT_OFFICE": Workstation.Department.FRONT_DESK,
        "FRONT_DESK_AND_RECEPTION": Workstation.Department.FRONT_DESK,
        "FRONT_DESK_RECEPTION": Workstation.Department.FRONT_DESK,
        # 9. Housekeepers (includes consolidated Maintenance)
        "HOUSEKEEPING": Workstation.Department.HOUSEKEEPING,
        "HOUSEKEEPERS": Workstation.Department.HOUSEKEEPING,
        "HOUSEKEEPER": Workstation.Department.HOUSEKEEPING,
        "MAINTENANCE": Workstation.Department.HOUSEKEEPING,
        # 10. Store Keeper
        "INVENTORY": Workstation.Department.INVENTORY,
        "STORE_KEEPER": Workstation.Department.INVENTORY,
        "STOREKEEPER": Workstation.Department.INVENTORY,
        "STORES": Workstation.Department.INVENTORY,
        "STORE": Workstation.Department.INVENTORY,
        "STORES_INVENTORY": Workstation.Department.INVENTORY,
        "STORES_AND_INVENTORY": Workstation.Department.INVENTORY,
        "INVENTORY_STORES": Workstation.Department.INVENTORY,
        # 11. Security
        "SECURITY": Workstation.Department.SECURITY,
        # Non-departmental owner marker
        "ADMINISTRATOR": "",
        "HOTEL_OWNER_ADMINISTRATOR": "",
    }
    if raw in aliases:
        return aliases[raw]
    if raw in DEPARTMENT_ROLES:
        return raw
    for role, label in User.Role.choices:
        if raw == _raw_department_token(label):
            return department_for_role(role)
    return raw


def _profile_department(user) -> tuple[str, bool]:
    """Return the normalized persisted profile department and its validity."""
    try:
        profile = user.staff_profile
    except Exception:  # noqa: BLE001 - a missing profile is normal
        return "", True
    text_value = str(getattr(profile, "department", "") or "").strip()
    ref = getattr(profile, "department_ref", None)
    ref_value = str(getattr(ref, "code", "") or "").strip() if ref else ""
    text_code = _normalize_department_code(text_value) if text_value else ""
    ref_code = _normalize_department_code(ref_value) if ref_value else ""
    if text_value and not text_code:
        return "", False
    if ref_value and not ref_code:
        return "", False
    if text_code and ref_code and text_code != ref_code:
        return "", False
    return ref_code or text_code, True


def department_for_user(user) -> str:
    """Resolve department from stored identity and fail closed on conflicts."""
    if user is None:
        return ""
    role = str(getattr(user, "role", "") or "")
    if role in GLOBAL_ROLES:
        return ""
    from_role = department_for_role(role)
    if not from_role:
        return ""
    profile_code, valid = _profile_department(user)
    if not valid:
        return ""
    if profile_code and profile_code != from_role:
        # Compatibility for pre-migration KITCHEN_ASSISTANT profiles that stored
        # KITCHEN while KITCHEN_ASSISTANT was temporarily grouped under Kitchen.
        if role == User.Role.KITCHEN_ASSISTANT and profile_code == Workstation.Department.KITCHEN:
            return from_role
        return ""
    return from_role


def department_context_is_valid(user) -> bool:
    """Whether an account's persisted role/profile identity is internally safe."""
    if user is None:
        return False
    role = str(getattr(user, "role", "") or "")
    if role in GLOBAL_ROLES or role == User.Role.GUEST:
        return True
    return bool(department_for_role(role) and department_for_user(user))


def validate_role_department(role: str, department: str = "", *, department_ref: str = "") -> str:
    """Validate the one canonical role against optional submitted department data."""
    role = str(role or "").strip().upper()
    if role in RETIRED_DEPARTMENTAL_ROLES:
        assert_assignable_role(role)
    if role not in ASSIGNABLE_ROLES or role == User.Role.GUEST:
        raise ValueError(f"'{role}' is not an assignable staff role.")
    expected = department_for_role(role)
    supplied = [str(value or "").strip() for value in (department, department_ref) if str(value or "").strip()]
    for value in supplied:
        msg = removed_department_message(value)
        if msg:
            raise ValueError(msg)
    normalized = [_normalize_department_code(value) for value in supplied]
    if any(not code for code in normalized):
        raise ValueError("Choose a configured department.")
    if role in GLOBAL_ROLES:
        if any(code not in {Workstation.Department.MANAGEMENT} for code in normalized):
            raise ValueError(f"{role} is a global oversight role and cannot be assigned to an operational department.")
        return Workstation.Department.MANAGEMENT if normalized else ""
    if not expected:
        raise ValueError(f"'{role}' has no configured operational department.")
    for code in normalized:
        if code != expected:
            raise ValueError(
                f"A {role} account belongs to {expected}; the request cannot place it in {code}."
            )
    return expected


def department_of_role(role: str) -> str:
    """Alias kept for callers that read better as 'the department of this role'."""
    return department_for_role(role)


def is_cross_department(user, department: str) -> bool:
    """True when ``department`` is not this account's authorized department."""
    if user is None:
        return True
    role = str(getattr(user, "role", "") or "")
    if role in GLOBAL_ROLES:
        return False
    target = _normalize_department_code(department)
    if not target or target not in DEPARTMENT_ROLES:
        return True
    own = department_for_user(user)
    if not own:
        return True
    if role in CROSS_DEPARTMENT_OVERSIGHT_ROLES:
        return False
    return own != target


class RetiredDepartmentalRole(ValueError):
    """Raised when a new assignment uses a retired departmental title."""


def assert_assignable_role(role: str) -> str:
    """Return the canonical role for an assignment, or refuse a retired title."""
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
    """``[(role, label)]`` for staff-account forms: the 11 approved staff roles + ADMIN + GUEST."""
    choices = []
    for department, role in DEPARTMENT_ROLES.items():
        label = dict(Workstation.Department.choices).get(department, department)
        choices.append((role, label))
    for role in sorted(GLOBAL_ROLES):
        choices.append((role, f"Global · {User.Role(role).label}"))
    choices.append((User.Role.GUEST, "Guest"))
    return choices
