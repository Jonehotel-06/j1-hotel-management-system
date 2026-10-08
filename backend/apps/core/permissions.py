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
MANAGER_ROLES = ("ADMIN", "MANAGER")


def _active(user):
    return bool(user and user.is_authenticated and user.is_active)


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


class IsManagerOrAdmin(BasePermission):
    """Operational managers and administrators."""

    message = "Manager or administrator access is required."

    def has_permission(self, request, view):
        return _active(request.user) and request.user.role in MANAGER_ROLES


class IsAdminRole(BasePermission):
    """Administrators only (user management, settings, audit logs)."""

    message = "Administrator access is required."

    def has_permission(self, request, view):
        return _active(request.user) and request.user.role == "ADMIN"


class IsStaffReadOnlyManagerWrite(BasePermission):
    """All staff may read; only managers/admins may modify."""

    def has_permission(self, request, view):
        if not (_active(request.user) and request.user.role in STAFF_ROLES):
            return False
        if request.method in SAFE_METHODS:
            return True
        return request.user.role in MANAGER_ROLES


class CanViewStaffProfile(BasePermission):
    """Who may open another person's staff profile card.

    * ADMIN — every account (and may edit them via the users endpoints).
    * MANAGER — read-only view of every staff account.
    * RECEPTIONIST — their own profile only.

    Role claims from the client are never consulted; the row in the database
    decides. This is the READ half of staff management — every mutation still
    goes through :class:`IsAdminRole`.
    """

    message = "You do not have permission to view this staff profile."

    def has_permission(self, request, view):
        if not (_active(request.user) and request.user.role in STAFF_ROLES):
            return False
        if request.user.role in MANAGER_ROLES:
            return True
        # Receptionists may only ever look at themselves.
        return str(view.kwargs.get("pk") or "") == str(request.user.pk)


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
