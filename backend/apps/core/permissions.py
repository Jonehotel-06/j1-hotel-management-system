# apps/core/permissions.py
"""Role-based permission classes.

Roles live on the User model and are ALWAYS evaluated server-side; a role
claim from the frontend is never trusted.
"""
from rest_framework.permissions import SAFE_METHODS, BasePermission

from apps.accounts.capabilities import has_capability


# Broad legacy endpoint rules. Do not add new operational roles here: doing so
# would silently grant a Cashier/Housekeeping/Maintenance account access to old
# staff-wide endpoints before each one has been migrated to a capability check.
STAFF_ROLES = ("ADMIN", "MANAGER", "RECEPTIONIST")
#: GENERAL_MANAGER is manager-equivalent: ``ROLE_CAPABILITY_CODES`` grants it the
#: same 50 capabilities as MANAGER (booking/refund approval, financial reports,
#: payroll, staff records, terminal management). It was omitted from this tuple
#: while being routed to ``index.html``, ``reports.html``, ``offers.html``,
#: ``rooms.html`` and ``staff-profile.html`` by the frontend, so every one of
#: those pages answered 403 to the person running the hotel. Adding it here does
#: not grant a capability the catalogue withholds; it makes enforcement agree
#: with the grants the role already has.
MANAGER_ROLES = ("ADMIN", "MANAGER", "GENERAL_MANAGER")

#: Read allowlist for the legacy configuration endpoints (amenities, facilities,
#: gallery, guest discounts, offers, policies, room types, rooms). These are the
#: same read-only records a RECEPTIONIST already sees; FRONT_DESK_SUPERVISOR
#: supervises that desk and is routed to ``rooms.html``. Writes remain
#: ``MANAGER_ROLES`` — no operational worker gains a mutation.
LEGACY_READ_ROLES = STAFF_ROLES + ("FRONT_DESK_SUPERVISOR", "GENERAL_MANAGER")


def _active(user):
    if not (user and user.is_authenticated and user.is_active):
        return False
    # The role/profile pair is trusted identity, not display metadata. A
    # conflicting stored department fails closed across the existing permission
    # classes as well as capability-gated views. Global oversight roles retain
    # their established policy in department_context_is_valid().
    if getattr(user, "is_staff_member", False):
        from apps.accounts.departments import department_context_is_valid

        if not department_context_is_valid(user):
            return False
    return True


class HasCapability(BasePermission):
    """Authorize a new operational endpoint by its server-side capability.

    Set ``required_capability`` (or a tuple/list in
    ``required_capabilities``) on the view.  Multiple values require all
    capabilities unless the view sets ``require_any_capability = True``.
    """

    message = "You do not have the required permission for this action."

    def has_permission(self, request, view):
        if not _active(request.user):
            return False
        required = getattr(view, "required_capabilities", None)
        if required is None:
            required = getattr(view, "required_capability", "")
        if isinstance(required, str):
            required = (required,)
        required = tuple(value for value in (required or ()) if value)
        if not required:
            # A misconfigured capability-protected endpoint must fail closed.
            return False
        resolved = [has_capability(request.user, code) for code in required]
        return any(resolved) if getattr(view, "require_any_capability", False) else all(resolved)


class IsStaffRole(BasePermission):
    """Any authenticated staff member (admin, manager, receptionist)."""

    message = "Staff access is required."

    def has_permission(self, request, view):
        return _active(request.user) and request.user.role in STAFF_ROLES


#: Roles whose landing page is the hotel dashboard.
#:
#: ``frontend/js/dashboard.js`` (PAGE_ACCESS) sends exactly these roles to
#: ``index.html`` and renders the link for them. The API gate used to be
#: ``STAFF_ROLES`` alone, so a General Manager and a Front Desk Supervisor were
#: shown the dashboard, were routed to it after sign-in, and then received 403
#: from ``/api/admin/dashboard/`` — a dashboard that never loads. The union
#: below is the same promise the frontend makes and nothing wider: the four
#: remaining operational roles (housekeeping, kitchen, stores, portering) keep
#: their own department screens and are still refused here.
DASHBOARD_ROLES = STAFF_ROLES + ("FRONT_DESK_SUPERVISOR", "GENERAL_MANAGER")


class IsDashboardViewer(BasePermission):
    """The roles the frontend actually routes to the main dashboard."""

    message = "Staff access is required."

    def has_permission(self, request, view):
        return _active(request.user) and request.user.role in DASHBOARD_ROLES


class IsManagerOrAdmin(BasePermission):
    """Operational managers and administrators."""

    message = "Manager or administrator access is required."

    def has_permission(self, request, view):
        return _active(request.user) and request.user.role in MANAGER_ROLES


# ---------------------------------------------------------------------------
# Reservations
#
# The booking endpoints predate the capability matrix and were gated by the
# broad legacy `STAFF_ROLES` allowlist. The matrix grants `booking.read` /
# `booking.manage` to GENERAL_MANAGER and FRONT_DESK_SUPERVISOR (and the
# sidebar shows them the reservations workspace), yet the API answered 403
# (audit N10). These classes honour BOTH: the legacy allowlist keeps working
# exactly as before, and the persisted capability grant now means what the
# matrix and the UI have always claimed.
#
# Widening is deliberately limited to the capability in question — a role that
# does not hold `booking.read` (Cashier, Housekeeping, Maintenance, Storekeeper,
# Security, guests) is still refused, and sensitive operations keep their own
# stricter gates (reschedule remains MANAGER/ADMIN).
# ---------------------------------------------------------------------------
def _has_booking_grant(user, code: str) -> bool:
    if user.role in STAFF_ROLES:
        return True
    # A GUEST account must never reach the staff reservation surface, even if a
    # capability row were mis-seeded: the role check is the outer boundary.
    if user.role == "GUEST":
        return False
    return has_capability(user, code)


class BookingReadAccess(BasePermission):
    """Read reservations, guests and the derived booking surfaces."""

    message = "Reservation access is required."

    def has_permission(self, request, view):
        return _active(request.user) and _has_booking_grant(request.user, "booking.read")


class BookingManageAccess(BasePermission):
    """Change reservations (confirm, cancel, check in/out, no-show, assign)."""

    message = "Reservation management access is required."

    def has_permission(self, request, view):
        return _active(request.user) and _has_booking_grant(request.user, "booking.manage")


class BookingAccess(BasePermission):
    """Read for safe methods, manage for writes."""

    message = "Reservation access is required."

    def has_permission(self, request, view):
        permission = BookingReadAccess() if request.method in SAFE_METHODS else BookingManageAccess()
        return permission.has_permission(request, view)


class IsAdminRole(BasePermission):
    """Administrators only (user management, settings, audit logs)."""

    message = "Administrator access is required."

    def has_permission(self, request, view):
        return _active(request.user) and request.user.role == "ADMIN"


class IsStaffReadOnlyManagerWrite(BasePermission):
    """Read-only records for the desk, writes for managers.

    Read: the legacy staff tuple plus the front desk supervisor (the role that
    runs the desk and needs room inventory) and the manager-equivalent general
    manager. Write: managers and administrators only.
    """

    def has_permission(self, request, view):
        if not (_active(request.user) and request.user.role in LEGACY_READ_ROLES):
            return False
        if request.method in SAFE_METHODS:
            return True
        return request.user.role in MANAGER_ROLES


class CanViewStaffProfile(BasePermission):
    """Who may open another person's staff profile card.

    Any staff member may open their own card. Other cards are visible to accounts that
    hold ``staff.view`` — ADMIN holds the full catalogue, MANAGER and
    GENERAL_MANAGER are granted it, and so is HR_MANAGER, whose ``staff.html``
    and ``staff-profile.html`` pages the frontend already unlocks. Deciding on
    the capability rather than the legacy role tuple is what makes the two
    agree: the previous ADMIN/MANAGER/RECEPTIONIST check refused the HR manager
    and the general manager the very card their navigation linked to.

    Role claims from the client are never consulted; the row in the database
    decides. This is the READ half of staff management — every mutation still
    goes through :class:`IsAdminRole`.
    """

    message = "You do not have permission to view this staff profile."

    def has_permission(self, request, view):
        user = request.user
        if not (_active(user) and getattr(user, "is_staff_member", False)):
            return False
        if str(view.kwargs.get("pk") or "") == str(user.pk):
            return True
        return has_capability(user, "staff.view")


class IsOwnerOrStaff(BasePermission):
    """Object-level access: a guest may only touch their own records.

    Works with objects that expose ``guest.user`` (Booking, Payment) or a
    direct ``user`` attribute. Protects against IDOR (e.g. /api/bookings/2/).
    """

    message = "You do not have permission to access this resource."

    def has_permission(self, request, view):
        return _active(request.user)

    def has_object_permission(self, request, view, obj):
        user = request.user
        if user.role in STAFF_ROLES:
            return True
        guest = getattr(obj, "guest", None)
        if guest is not None:
            return guest.user_id == user.id
        obj_user = getattr(obj, "user", None)
        if obj_user is not None:
            return obj_user.id == user.id
        return False
