"""Capability catalogue and least-privilege role resolution.

The persisted ``RoleCapability`` rows are the authorization source. JWT claims,
terminal identifiers, department labels, and browser checks are never security
boundaries. Legacy endpoint role allowlists intentionally remain narrower until
individual APIs are migrated and tested.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - imports only for static analysis
    from .models import User


# Capability codes are stable API/security identifiers, not display labels.
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
    ("payment.read", "Read payment ledger", "Finance", "Review payment and refund records."),
    ("payment.capture", "Capture collections", "Finance", "Record permitted tender collections."),
    ("payment.receipt.send", "Send guest receipts", "Finance", "Send a verified booking receipt to its guest."),
    ("payment.refund.request", "Request refunds", "Finance", "Request a refund, void, or controlled discount."),
    ("payment.refund.approve", "Approve sensitive money actions", "Finance", "Approve policy-controlled refunds, voids, and discounts."),
    ("cash_session.open", "Open cash sessions", "Cashier", "Open the user's permitted cash session."),
    ("cash_session.close", "Close cash sessions", "Cashier", "Close the user's permitted cash session."),
    ("expense.manage", "Manage expenses", "Finance", "Create, submit, and post permitted expense records."),
    ("expense.approve", "Approve expenses", "Finance", "Independently approve or reject controlled expense requests."),
    ("pos.order.manage", "Manage general POS orders", "POS", "Manage permitted room-service, restaurant, takeaway, and order records."),
    ("pos.menu.manage", "Manage all POS menus", "POS", "Create and manage every POS menu category, item, and modifier."),
    ("restaurant.menu.manage", "Manage restaurant menu", "Restaurant", "Create and manage restaurant-category menu items."),
    ("restaurant.order.manage", "Manage restaurant orders", "Restaurant", "Create and progress restaurant orders."),
    ("restaurant.table.manage", "Manage restaurant tables", "Restaurant", "Maintain table records and table-service sessions."),
    ("bar.order.manage", "Manage bar orders", "Bar", "Create and progress bar orders."),
    ("bar.menu.manage", "Manage bar menu", "Bar", "Maintain bar-category menu items."),
    ("kitchen.queue.view", "View production queues", "Kitchen", "View the user's assigned kitchen or bar production queue."),
    ("kitchen.ticket.manage", "Progress production tickets", "Kitchen", "Start, mark ready, or complete assigned production tickets."),
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
    ("payroll.view", "View payroll", "Payroll", "View payroll run and compensation records."),
    ("payroll.manage", "Prepare payroll", "Payroll", "Maintain compensation records and prepare payroll runs."),
    ("payroll.approve", "Approve payroll", "Payroll", "Independently approve a submitted payroll run."),
    ("payroll.rules.manage", "Propose statutory payroll rules", "Payroll", "Create immutable effective-dated statutory rules for independent review."),
    ("payroll.rules.review", "Review statutory payroll rules", "Payroll", "Independently review and approve statutory payroll rules against legal sources."),
    ("reports.financial.view", "View financial reports", "Reports", "View financial reporting and exports."),
    ("audit.view", "View audit records", "Security", "View immutable audit history."),
    ("terminal.manage", "Manage workstation registry", "Security", "Register, label, and deactivate informational workstations."),
    ("service_qr.manage", "Manage guest service QR links", "Guest services", "Create and revoke signed room/table service links."),
    ("settings.manage", "Manage hotel settings", "Administration", "Manage hotel-wide configuration."),
)

ALL_CAPABILITY_CODES = frozenset(item[0] for item in CAPABILITY_DEFINITIONS)

# Global compatibility matrix. New department roles receive only their
# operational surface; legacy API allowlists are not widened by these grants.
ROLE_CAPABILITY_CODES = {
    "ADMIN": ALL_CAPABILITY_CODES,
    "MANAGER": frozenset({
        "account.view", "booking.read", "booking.manage", "stay.check_in", "stay.check_out",
        "folio.view", "folio.charge.post", "finance.controls.manage", "payment.read", "payment.capture",
        "payment.receipt.send", "payment.refund.request", "payment.refund.approve", "cash_session.open",
        "cash_session.close", "expense.manage", "expense.approve",
        "pos.order.manage", "pos.menu.manage", "restaurant.menu.manage", "restaurant.order.manage", "restaurant.table.manage",
        "bar.order.manage", "bar.menu.manage", "kitchen.queue.view", "kitchen.ticket.manage",
        "guest_request.manage", "guest_request.assign", "housekeeping.task.manage", "housekeeping.task.assign",
        "maintenance.work_order.manage", "maintenance.work_order.assign", "inventory.manage", "inventory.adjust.approve",
        "procurement.manage", "procurement.approve", "staff.profile.manage", "shift.manage", "attendance.clock",
        "attendance.manage", "leave.request", "leave.approve", "payroll.view", "payroll.manage", "payroll.approve",
        "payroll.rules.manage", "payroll.rules.review", "reports.financial.view", "terminal.manage", "service_qr.manage",
    }),
    "RECEPTIONIST": frozenset({
        "booking.read", "booking.manage", "stay.check_in", "stay.check_out", "folio.view", "folio.charge.post",
        "payment.read", "payment.capture", "payment.receipt.send", "payment.refund.request",
        "guest_request.manage", "guest_request.assign", "housekeeping.task.manage", "housekeeping.task.assign",
        "maintenance.work_order.manage", "maintenance.work_order.assign", "attendance.clock", "leave.request",
    }),
    "CASHIER": frozenset({
        "folio.charge.post", "payment.capture", "payment.refund.request", "cash_session.open",
        "cash_session.close", "expense.manage", "pos.order.manage", "attendance.clock", "leave.request",
    }),
    "HOUSEKEEPING": frozenset({
        "housekeeping.task.manage", "guest_request.manage", "attendance.clock", "leave.request",
    }),
    "MAINTENANCE": frozenset({
        "maintenance.work_order.manage", "guest_request.manage", "attendance.clock", "leave.request",
    }),
    "INVENTORY_CLERK": frozenset({"inventory.manage", "procurement.manage", "attendance.clock", "leave.request"}),
    "FRONT_DESK_SUPERVISOR": frozenset({
        "booking.read", "booking.manage", "stay.check_in", "stay.check_out", "folio.view", "folio.charge.post",
        "payment.read", "payment.capture", "payment.receipt.send", "payment.refund.request",
        "guest_request.manage", "guest_request.assign",
        "housekeeping.task.manage", "housekeeping.task.assign", "maintenance.work_order.manage",
        "maintenance.work_order.assign", "attendance.clock", "attendance.manage", "leave.request",
    }),
    "GENERAL_MANAGER": frozenset({
        "account.view", "booking.read", "booking.manage", "stay.check_in", "stay.check_out", "folio.view",
        "folio.charge.post", "finance.controls.manage", "payment.read", "payment.capture", "payment.receipt.send",
        "payment.refund.request", "payment.refund.approve", "cash_session.open", "cash_session.close",
        "expense.manage", "expense.approve",
        "pos.order.manage", "pos.menu.manage", "restaurant.menu.manage", "restaurant.order.manage", "restaurant.table.manage",
        "bar.order.manage", "bar.menu.manage", "kitchen.queue.view", "kitchen.ticket.manage", "guest_request.manage",
        "guest_request.assign", "housekeeping.task.manage", "housekeeping.task.assign", "maintenance.work_order.manage",
        "maintenance.work_order.assign", "inventory.manage", "inventory.adjust.approve", "procurement.manage",
        "procurement.approve", "staff.profile.manage", "shift.manage", "attendance.clock", "attendance.manage",
        "leave.request", "leave.approve", "payroll.view", "payroll.manage", "payroll.approve",
        "payroll.rules.manage", "payroll.rules.review", "reports.financial.view", "terminal.manage", "service_qr.manage",
    }),
    "RESTAURANT_MANAGER": frozenset({
        "restaurant.order.manage", "restaurant.table.manage", "restaurant.menu.manage", "payment.capture",
        "kitchen.queue.view", "attendance.clock", "attendance.manage", "shift.manage", "leave.request",
    }),
    "WAITER": frozenset({
        "restaurant.order.manage", "payment.capture", "guest_request.manage", "attendance.clock", "leave.request",
    }),
    "BAR_MANAGER": frozenset({
        "bar.order.manage", "bar.menu.manage", "payment.capture", "kitchen.queue.view", "attendance.clock",
        "attendance.manage", "shift.manage", "leave.request",
    }),
    "BARTENDER": frozenset({
        "bar.order.manage", "payment.capture", "guest_request.manage", "attendance.clock", "leave.request",
    }),
    "KITCHEN_MANAGER": frozenset({
        "kitchen.queue.view", "kitchen.ticket.manage", "attendance.clock", "attendance.manage", "shift.manage",
        "leave.request",
    }),
    "CHEF": frozenset({"kitchen.queue.view", "kitchen.ticket.manage", "attendance.clock", "leave.request"}),
    "HOUSEKEEPING_MANAGER": frozenset({
        "housekeeping.task.manage", "housekeeping.task.assign", "guest_request.manage", "guest_request.assign",
        "attendance.clock", "attendance.manage", "shift.manage", "leave.request",
    }),
    "HOUSEKEEPER": frozenset({
        "housekeeping.task.manage", "guest_request.manage", "attendance.clock", "leave.request",
    }),
    "MAINTENANCE_TECHNICIAN": frozenset({
        "maintenance.work_order.manage", "guest_request.manage", "attendance.clock", "leave.request",
    }),
    "ACCOUNTS_MANAGER": frozenset({
        "account.view", "folio.view", "folio.charge.post", "finance.controls.manage", "payment.read", "payment.capture",
        "payment.refund.request", "payment.refund.approve", "cash_session.open", "cash_session.close",
        "expense.manage", "expense.approve", "payroll.view", "payroll.manage", "payroll.approve",
        "payroll.rules.manage", "payroll.rules.review", "reports.financial.view",
    }),
    "ACCOUNTANT": frozenset({
        "folio.view", "folio.charge.post", "payment.read", "payment.capture", "payment.refund.request", "expense.manage",
        "payroll.view", "payroll.manage", "reports.financial.view", "attendance.clock", "leave.request",
    }),
    "HR_MANAGER": frozenset({
        "staff.profile.manage", "shift.manage", "attendance.clock", "attendance.manage", "leave.request",
        "leave.approve", "payroll.view", "payroll.manage", "payroll.approve", "payroll.rules.manage",
    }),
    "SECURITY": frozenset({"guest_request.manage", "attendance.clock"}),
    "PROCUREMENT_OFFICER": frozenset({"procurement.manage", "attendance.clock", "leave.request"}),
    "STOREKEEPER": frozenset({"inventory.manage", "procurement.manage", "attendance.clock", "leave.request"}),
    "PORTER": frozenset({"guest_request.manage", "attendance.clock", "leave.request"}),
    "KITCHEN_ASSISTANT": frozenset({"kitchen.queue.view", "kitchen.ticket.manage", "attendance.clock", "leave.request"}),
    "GUEST": frozenset(),
}

# Deliberately separate from legacy broad endpoint role allowlists.
OPERATIONAL_STAFF_ROLES = frozenset(role for role in ROLE_CAPABILITY_CODES if role != "GUEST")

# ---------------------------------------------------------------------------
# Role compatibility mapping (audit N8)
#
# The accounts migration history left TWO housekeeping worker roles in
# ``User.Role`` with IDENTICAL capability grants:
#
#   HOUSEKEEPING — the original role code, referenced by early workflow code
#   HOUSEKEEPER  — added with the department capability seed
#
# The housekeeping workflow hard-coded ``User.Role.HOUSEKEEPING``, so a real
# HOUSEKEEPER account could not be assigned a task (HTTP 400), could not claim
# one, and saw an empty task list — while the capability matrix claimed both
# roles were equivalent.
#
# Both identifiers must remain valid (existing accounts and JWT/API consumers
# already reference them), so this module is the SINGLE place that maps the
# historical alias onto the canonical operational role. Workflow code must test
# membership here rather than comparing against one literal role code. A future
# migration can retire HOUSEKEEPER by rewriting the accounts and deleting the
# alias from this mapping alone.
# ---------------------------------------------------------------------------
CANONICAL_HOUSEKEEPING_ROLE = "HOUSEKEEPING"
HOUSEKEEPING_ROLE_ALIASES = frozenset({"HOUSEKEEPER"})
HOUSEKEEPING_WORKER_ROLES = frozenset({CANONICAL_HOUSEKEEPING_ROLE}) | HOUSEKEEPING_ROLE_ALIASES

# The same shape for the maintenance worker pair (MAINTENANCE / MAINTENANCE_TECHNICIAN),
# so the identical latent defect cannot appear in the maintenance workflow.
CANONICAL_MAINTENANCE_ROLE = "MAINTENANCE"
MAINTENANCE_ROLE_ALIASES = frozenset({"MAINTENANCE_TECHNICIAN"})
MAINTENANCE_WORKER_ROLES = frozenset({CANONICAL_MAINTENANCE_ROLE}) | MAINTENANCE_ROLE_ALIASES


def is_housekeeping_worker(user) -> bool:
    """True for an account that may be assigned and work housekeeping tasks."""
    return getattr(user, "role", None) in HOUSEKEEPING_WORKER_ROLES


def is_maintenance_worker(user) -> bool:
    """True for an account that may be assigned and work maintenance orders."""
    return getattr(user, "role", None) in MAINTENANCE_WORKER_ROLES


def capability_codes_for_user(user: "User") -> frozenset[str]:
    """Return active capability codes resolved from persisted server grants."""
    if not user or not getattr(user, "is_authenticated", False) or not getattr(user, "is_active", False):
        return frozenset()
    cached = getattr(user, "_jone_capability_codes", None)
    if cached is not None:
        return cached

    from .models import Capability, RoleCapability, User, UserCapabilityOverride

    if user.role == User.Role.ADMIN:
        codes = frozenset(Capability.objects.filter(is_active=True).values_list("code", flat=True))
    else:
        role_codes = set(
            RoleCapability.objects.filter(role=user.role, capability__is_active=True)
            .values_list("capability__code", flat=True)
        )
        if not role_codes and user.role in ROLE_CAPABILITY_CODES:
            role_codes = set(ROLE_CAPABILITY_CODES[user.role])
        for effect, cap_code in (
            UserCapabilityOverride.objects.filter(user=user, capability__is_active=True)
            .values_list("effect", "capability__code")
        ):
            if effect == UserCapabilityOverride.Effect.GRANT:
                role_codes.add(cap_code)
            elif effect == UserCapabilityOverride.Effect.REVOKE:
                role_codes.discard(cap_code)
        codes = frozenset(role_codes)
    user._jone_capability_codes = codes
    return codes


def has_capability(user: "User", capability: str) -> bool:
    """Return whether an active authenticated user holds a named capability."""
    return str(capability or "") in capability_codes_for_user(user)
