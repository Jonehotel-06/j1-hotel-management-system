"""Capability catalogue and server-side resolution for operational roles.

The legacy ``User.role`` field remains the compatibility source for existing
public/staff endpoints.  New operational features authorize named capabilities
through the persisted role-to-capability matrix in this module; browser role
claims and UI visibility are never authorization inputs.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - imports only for static analysis
    from .models import User


# Keep codes stable: they are API/security contracts, not display labels.
CAPABILITY_DEFINITIONS = (
    ("account.view", "View staff accounts", "Identity", "View permitted staff account information."),
    ("account.manage", "Manage staff accounts", "Identity", "Create and change staff roles/accounts."),
    ("booking.read", "View reservations", "Reservations", "Read reservation and guest operational details."),
    ("booking.manage", "Manage reservations", "Reservations", "Create and manage permitted reservations."),
    ("stay.check_in", "Check in guests", "Stays", "Perform a validated guest check-in."),
    ("stay.check_out", "Check out guests", "Stays", "Perform a validated guest check-out."),
    ("folio.view", "View folios", "Finance", "View permitted guest folios and balances."),
    ("folio.charge.post", "Post normal folio charges", "Finance", "Post permitted normal charges to an open folio."),
    ("finance.controls.manage", "Manage finance controls", "Finance", "Create effective-dated financial control policies."),
    ("payment.capture", "Capture collections", "Finance", "Record permitted tender collections."),
    ("payment.refund.request", "Request refunds", "Finance", "Request a refund, void, or controlled discount."),
    ("payment.refund.approve", "Approve sensitive money actions", "Finance", "Approve policy-controlled refunds, voids, and discounts."),
    ("cash_session.open", "Open cash sessions", "Cashier", "Open the user's permitted cash session."),
    ("cash_session.close", "Close cash sessions", "Cashier", "Close the user's permitted cash session."),
    ("expense.manage", "Manage expenses", "Finance", "Create, submit, and post permitted expense records."),
    ("expense.approve", "Approve expenses", "Finance", "Independently approve or reject controlled expense requests."),
    ("pos.order.manage", "Manage POS orders", "POS", "Create and progress permitted POS or room-service orders."),
    ("pos.menu.manage", "Manage POS menu", "POS", "Create and manage POS categories, items, and modifiers."),
    ("guest_request.manage", "Manage guest requests", "Guest services", "Create or progress permitted guest requests."),
    ("guest_request.assign", "Dispatch guest requests", "Guest services", "Assign or re-route guest service requests."),
    ("housekeeping.task.manage", "Manage housekeeping tasks", "Operations", "Work permitted housekeeping tasks."),
    ("housekeeping.task.assign", "Dispatch housekeeping tasks", "Operations", "Assign and inspect housekeeping tasks."),
    ("maintenance.work_order.manage", "Manage maintenance work orders", "Operations", "Work permitted maintenance orders."),
    ("maintenance.work_order.assign", "Dispatch maintenance work orders", "Operations", "Assign and verify maintenance work orders."),
    ("inventory.manage", "Manage inventory", "Inventory", "Create permitted inventory movements and counts."),
    ("inventory.adjust.approve", "Approve inventory adjustments", "Inventory", "Approve count variances before stock adjustments post."),
    ("procurement.manage", "Manage procurement", "Inventory", "Create and receive permitted procurement records."),
    ("procurement.approve", "Approve procurement", "Inventory", "Approve purchase orders requested by another user."),
    ("staff.profile.manage", "Manage staff operational profiles", "Staff operations", "Create and maintain non-sensitive staff operational profiles."),
    ("shift.manage", "Manage staff shifts", "Staff operations", "Create, schedule, and cancel staff shifts."),
    ("attendance.clock", "Clock attendance", "Staff operations", "Record the user's own source-keyed attendance events."),
    ("attendance.manage", "Manage attendance", "Staff operations", "View workforce attendance records and resolve permitted operations."),
    ("leave.request", "Request leave", "Staff operations", "Submit and cancel the user's permitted leave requests."),
    ("leave.approve", "Approve leave", "Staff operations", "Review leave requests for other staff members."),
    ("reports.financial.view", "View financial reports", "Reports", "View financial reporting and exports."),
    ("audit.view", "View audit records", "Security", "View immutable audit history."),
    ("settings.manage", "Manage hotel settings", "Administration", "Manage hotel-wide configuration."),
)

ALL_CAPABILITY_CODES = frozenset(item[0] for item in CAPABILITY_DEFINITIONS)

# Initial global role matrix.  It maps the pre-existing roles without changing
# their current endpoint rules, then adds the operational roles needed by later
# vertical slices.  New granular endpoints use this matrix via RoleCapability.
ROLE_CAPABILITY_CODES = {
    "ADMIN": ALL_CAPABILITY_CODES,
    "MANAGER": frozenset({
        "account.view", "booking.read", "booking.manage", "stay.check_in", "stay.check_out",
        "folio.view", "folio.charge.post", "finance.controls.manage", "payment.capture", "payment.refund.request",
        "payment.refund.approve", "cash_session.open", "cash_session.close", "expense.manage", "expense.approve", "pos.order.manage", "pos.menu.manage",
        "guest_request.manage", "guest_request.assign", "housekeeping.task.manage", "housekeeping.task.assign",
        "maintenance.work_order.manage", "maintenance.work_order.assign",
        "inventory.manage", "inventory.adjust.approve", "procurement.manage", "procurement.approve",
        "staff.profile.manage", "shift.manage", "attendance.clock", "attendance.manage", "leave.request", "leave.approve",
        "reports.financial.view", "audit.view",
    }),
    "RECEPTIONIST": frozenset({
        "booking.read", "booking.manage", "stay.check_in", "stay.check_out", "folio.view",
        "folio.charge.post", "payment.capture", "payment.refund.request", "guest_request.manage", "guest_request.assign",
        "housekeeping.task.manage", "housekeeping.task.assign",
        "maintenance.work_order.manage", "maintenance.work_order.assign", "attendance.clock", "leave.request",
    }),
    "CASHIER": frozenset({
        "booking.read", "folio.charge.post", "payment.capture",
        "payment.refund.request", "cash_session.open", "cash_session.close", "expense.manage", "pos.order.manage", "attendance.clock", "leave.request",
    }),
    "HOUSEKEEPING": frozenset({"booking.read", "housekeeping.task.manage", "guest_request.manage", "attendance.clock", "leave.request"}),
    "MAINTENANCE": frozenset({"booking.read", "maintenance.work_order.manage", "guest_request.manage", "attendance.clock", "leave.request"}),
    "INVENTORY_CLERK": frozenset({"inventory.manage", "procurement.manage", "attendance.clock", "leave.request"}),
    "GUEST": frozenset(),
}

# Existing broad permission classes deliberately retain their old allowlist
# until each endpoint is migrated to capability checks.  This narrower list is
# used only to tell whether a role is an operational staff role in new code.
OPERATIONAL_STAFF_ROLES = frozenset(role for role in ROLE_CAPABILITY_CODES if role != "GUEST")


def capability_codes_for_user(user: "User") -> frozenset[str]:
    """Return active capability codes for ``user`` with per-request caching.

    A user instance is request-local under DRF authentication. Caching the
    resolved set on it means a page making several capability checks performs
    at most one role-matrix query, not one query per permission class.
    """
    if not user or not getattr(user, "is_authenticated", False) or not getattr(user, "is_active", False):
        return frozenset()
    cached = getattr(user, "_jone_capability_codes", None)
    if cached is not None:
        return cached

    # Import lazily: this module is referenced from model and migration code.
    from .models import Capability, RoleCapability, User

    if user.role == User.Role.ADMIN:
        codes = frozenset(Capability.objects.filter(is_active=True).values_list("code", flat=True))
    else:
        codes = frozenset(
            RoleCapability.objects.filter(role=user.role, capability__is_active=True)
            .values_list("capability__code", flat=True)
        )
    user._jone_capability_codes = codes
    return codes


def has_capability(user: "User", capability: str) -> bool:
    """Return whether an active authenticated user holds a named capability."""
    return str(capability or "") in capability_codes_for_user(user)
