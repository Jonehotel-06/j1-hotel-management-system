# apps/accounts/models.py
"""Custom user model — email is the sole authentication identifier."""
import secrets

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.db import models

from apps.core.models import TimeStampedModel
from apps.core.validators import validate_image_upload


class UserManager(BaseUserManager):
    use_in_migrations = True

    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError("An email address is required.")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("role", User.Role.ADMIN)
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")
        return self.create_user(email, password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin, TimeStampedModel):
    """One account type; capabilities are governed by `role` (server-side)."""

    class Role(models.TextChoices):
        # Existing roles stay byte-for-byte compatible with current JWT/API
        # contracts and historical audit/migration rows. Active staff creation
        # and assignment offer ONLY the 11 approved staff roles (plus ADMIN for
        # the hotel owner/superuser and GUEST for public customer accounts); see
        # ``apps.accounts.departments``.
        ADMIN = "ADMIN", "Hotel Owner / Administrator"
        MANAGER = "MANAGER", "Manager"  # retired title → GENERAL_MANAGER
        RECEPTIONIST = "RECEPTIONIST", "Receptionist"
        # Retired for new assignments: resolves to WAITER / Waiters/Waitresses.
        CASHIER = "CASHIER", "Cashier"
        HOUSEKEEPING = "HOUSEKEEPING", "Housekeepers"
        MAINTENANCE = "MAINTENANCE", "Maintenance"  # retired → HOUSEKEEPING
        INVENTORY_CLERK = "INVENTORY_CLERK", "Inventory Clerk"  # retired → STOREKEEPER
        FRONT_DESK_SUPERVISOR = "FRONT_DESK_SUPERVISOR", "Front Desk Supervisor"  # retired → RECEPTIONIST
        GENERAL_MANAGER = "GENERAL_MANAGER", "General Manager"
        RESTAURANT_MANAGER = "RESTAURANT_MANAGER", "Restaurant Manager"  # retired → WAITER
        WAITER = "WAITER", "Waiters/Waitresses"
        BAR_MANAGER = "BAR_MANAGER", "Bar Manager"  # retired → WAITER
        BARTENDER = "BARTENDER", "Bartender"  # retired → WAITER
        KITCHEN_MANAGER = "KITCHEN_MANAGER", "Kitchen Manager"  # retired → CHEF
        CHEF = "CHEF", "Chefs"
        HOUSEKEEPING_MANAGER = "HOUSEKEEPING_MANAGER", "Housekeeping Manager"  # retired → HOUSEKEEPING
        HOUSEKEEPER = "HOUSEKEEPER", "Housekeeper"  # retired → HOUSEKEEPING
        MAINTENANCE_TECHNICIAN = "MAINTENANCE_TECHNICIAN", "Maintenance Technician"  # retired → HOUSEKEEPING
        ACCOUNTS_MANAGER = "ACCOUNTS_MANAGER", "Accounts Manager"  # retired → ACCOUNTANT
        ACCOUNTANT = "ACCOUNTANT", "Accountant"
        HR_MANAGER = "HR_MANAGER", "HR Manager"  # retired → PROCUREMENT_OFFICER
        HR_OFFICER = "HR_OFFICER", "HR Officer"  # retired → PROCUREMENT_OFFICER
        SECURITY = "SECURITY", "Security"
        PROCUREMENT_OFFICER = "PROCUREMENT_OFFICER", "Admin Manager/Procurement"
        STOREKEEPER = "STOREKEEPER", "Store Keeper"
        PORTER = "PORTER", "Porters"
        KITCHEN_ASSISTANT = "KITCHEN_ASSISTANT", "Kitchen Assistant"
        GUEST = "GUEST", "Guest"

    email = models.EmailField(unique=True, db_index=True)
    first_name = models.CharField(max_length=100)
    middle_name = models.CharField(max_length=100, blank=True, default="")
    last_name = models.CharField(max_length=100)
    phone = models.CharField(max_length=20, blank=True, default="")
    role = models.CharField(max_length=32, choices=Role.choices, default=Role.RECEPTIONIST, db_index=True)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)  # Django admin access only
    email_verified = models.BooleanField(default=False)
    must_change_password = models.BooleanField(default=False)
    profile_image = models.ImageField(
        upload_to="profiles/%Y/%m/", null=True, blank=True, validators=[validate_image_upload]
    )
    date_joined = models.DateTimeField(auto_now_add=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["first_name", "last_name"]

    class Meta:
        ordering = ["-date_joined"]

    def __str__(self):
        return f"{self.full_name} <{self.email}>"

    @property
    def full_name(self):
        parts = [p.strip() for p in (self.first_name, getattr(self, "middle_name", ""), self.last_name) if p and p.strip()]
        return " ".join(parts) or self.email

    @property
    def department(self) -> str:
        """This account's single department context (see ``accounts.departments``).

        Stored records only: the role and, when present, the staff profile's
        department. A request may never choose this value.
        """
        from .departments import department_for_user

        return department_for_user(self)

    @property
    def department_role(self) -> str:
        """The department's ONE standard role for this account."""
        from .departments import canonical_role

        return canonical_role(self.role)

    @property
    def is_admin(self):
        return self.role == self.Role.ADMIN

    @property
    def is_manager_or_admin(self):
        # GENERAL_MANAGER is manager-equivalent: the capability catalogue grants
        # it MANAGER's full set, including ``reports.financial.view``. Keeping
        # the property in step with ``apps.core.permissions.MANAGER_ROLES`` is
        # what lets the general manager see the financial half of the dashboard
        # the frontend routes them to.
        return self.role in (self.Role.ADMIN, self.Role.MANAGER, self.Role.GENERAL_MANAGER)

    @property
    def is_staff_member(self):
        """Operational staff, including the additive capability-era roles.

        Legacy broad endpoint permission classes intentionally remain scoped to
        ADMIN/MANAGER/RECEPTIONIST until they are migrated individually. New
        operational endpoints use named capabilities instead.
        """
        return self.role in (role for role in self.Role.values if role != self.Role.GUEST)

    def refresh_from_db(self, using=None, fields=None, from_queryset=None):
        super().refresh_from_db(using=using, fields=fields, from_queryset=from_queryset)
        if hasattr(self, "_jone_capability_codes"):
            delattr(self, "_jone_capability_codes")


def _new_workstation_reference():
    return "wks-" + secrets.token_urlsafe(18)


class Workstation(TimeStampedModel):
    """Optional workstation attribution; never an authentication factor."""

    class Department(models.TextChoices):
        SHARED = "SHARED", "Shared"
        MANAGEMENT = "MANAGEMENT", "General Manager"
        ACCOUNTS = "ACCOUNTS", "Accountant"
        PROCUREMENT = "PROCUREMENT", "Admin Manager/Procurement"
        KITCHEN = "KITCHEN", "Chefs"
        KITCHEN_ASSISTANT = "KITCHEN_ASSISTANT", "Kitchen Assistant"
        RESTAURANT = "RESTAURANT", "Waiters/Waitresses"
        PORTER = "PORTER", "Porters"
        FRONT_DESK = "FRONT_DESK", "Receptionist"
        HOUSEKEEPING = "HOUSEKEEPING", "Housekeepers"
        INVENTORY = "INVENTORY", "Store Keeper"
        SECURITY = "SECURITY", "Security"

    reference = models.CharField(
        max_length=48,
        unique=True,
        default=_new_workstation_reference,
        editable=False,
    )
    name = models.CharField(max_length=120)
    department = models.CharField(max_length=24, choices=Department.choices, default=Department.SHARED, db_index=True)
    location = models.CharField(max_length=120, blank=True, default="")
    is_active = models.BooleanField(default=True, db_index=True)
    last_seen_at = models.DateTimeField(null=True, blank=True, db_index=True)
    # Receptionist Desktop enrolment. Only a SHA-256 digest of the one-time
    # desktop key is stored; the raw key is shown once when issued. A front
    # desk workstation with a key is the only place operational staff (other
    # than management) can sign in. See apps/accounts/desktop_policy.py.
    sign_in_key_hash = models.CharField(max_length=64, blank=True, default="", db_index=True)
    sign_in_key_issued_at = models.DateTimeField(null=True, blank=True)
    # Receptionist Desktop enrolment, second route. An administrator registers
    # the network address the desk actually reaches the API from; a staff
    # sign-in (or session refresh) arriving from that address IS the desk, so
    # the browser needs no key. One exact address or a CIDR network — a whole
    # subnet also admits every phone and guest device sharing it, so the
    # registration UI spells that out before anything is saved.
    ip_address = models.CharField(max_length=64, blank=True, default="", db_index=True)
    desktop_ip_registered_at = models.DateTimeField(null=True, blank=True)
    current_staff = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reported_current_workstations",
        help_text="Informational account reported by this client; not verified session ownership.",
    )
    created_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="workstations_created"
    )
    updated_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="workstations_updated"
    )

    class Meta:
        ordering = ["department", "name", "pk"]
        indexes = [models.Index(fields=["is_active", "department"])]

    def __str__(self):
        return f"{self.name} ({self.department})"


def _hash_device_token(raw_token: str) -> str:
    """SHA-256 digest of a device token. The raw token is never stored."""
    import hashlib

    return hashlib.sha256(str(raw_token or "").encode("utf-8")).hexdigest()


class FrontDeskDevice(TimeStampedModel):
    """A staff device approved to be the Receptionist Desktop, independent of its IP.

    WHY A DEVICE RECORD EXISTS
    ==========================
    The Receptionist Desktop used to be identified by its network address: an
    administrator registered the address the desk reaches the API from, and a
    sign-in or token refresh arriving from that address was accepted. That works
    until the address changes — a power cut reboots the router, the ISP hands
    out a new lease, the desk moves to another floor — and then:

    * sign-in is refused because the request no longer arrives from the
      registered address, and
    * every staff session dies at its next token refresh for the same reason,
      because the refresh path enforces the same rule.

    The address was never the desk. The desk is a particular BROWSER on a
    particular machine, used by particular staff. This model records that
    browser as a device:

    * ``token_hash`` — SHA-256 of a random 256-bit token the browser keeps. The
      raw token is returned once, at enrolment, and never again: it is not in
      the database, not in the API, and not in any log.
    * ``device_label`` — what the browser called itself, for an administrator
      reviewing the list.
    * ``last_ip_address`` / ``ip_addresses`` — METADATA ONLY. Every distinct
      address the device has been seen from, so a change is visible and
      reviewable. An address is never required to match, and a new address never
      silently rewrites the workstation's registered address.
    * ``staff`` — the account that enrolled the device. A device credential is
      bound to that account, so a token copied to another machine cannot be
      replayed by a different user, and revoking the staff account revokes the
      device.

    SECURITY PROPERTIES (unchanged or strengthened, never weakened)
    ==============================================================
    * The device must belong to an ACTIVE ``FRONT_DESK`` workstation
      (``workstation``, ``PROTECT``); deactivating that workstation deauthorises
      every device enrolled for it on the next request.
    * The staff account must still be active and still hold an operational role.
    * Revocation is immediate (``is_active=False`` / ``revoked_at``) and audited.
    * A stolen token cannot be used from a different staff account.
    * The workstation is still required to exist and be active, so a hotel that
      retires its front desk retires its devices with it.
    * Nothing here disables the address pin. When an administrator HAS pinned a
      desk address, the pin still applies exactly as before; when the device
      proves itself and the saved pin is unreachable (a platform proxy address),
      the device credential is what admits the browser (see
      ``desktop_policy.device_credential_overrides_unusable_ip_pin``).
    """

    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    workstation = models.ForeignKey(
        Workstation,
        on_delete=models.PROTECT,
        related_name="front_desk_devices",
        help_text="The active Front Desk workstation this device stands at.",
    )
    staff = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="front_desk_devices",
        help_text="The account that enrolled the device. A token only works for this account.",
    )
    device_label = models.CharField(max_length=160, blank=True, default="")
    is_active = models.BooleanField(default=True, db_index=True)
    last_seen_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_ip_address = models.CharField(max_length=64, blank=True, default="")
    #: Every distinct address this device has presented, oldest first. Written by
    #: ``desktop_policy.record_device_seen``; capped so the row stays small.
    ip_addresses = models.JSONField(default=list, blank=True)
    first_enrolled_ip = models.CharField(max_length=64, blank=True, default="")
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="revoked_front_desk_devices",
    )
    revoked_reason = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["-last_seen_at", "-id"]
        indexes = [
            models.Index(fields=["is_active", "workstation"]),
        ]

    def __str__(self):
        return f"Front desk device {self.device_label or self.pk} (workstation {self.workstation_id})"

    def save(self, *args, **kwargs):
        """Give a not-yet-approved device a placeholder digest.

        ``token_hash`` is UNIQUE, so several pending rows cannot all carry the
        empty string. A pending row gets a random ``pending:<hex>`` value that
        is not a SHA-256 digest and therefore can never be produced by hashing
        a presented token — the row exists for an administrator to review and
        resolves nothing until ``issue_device_token`` replaces it.
        """
        if not self.token_hash:
            import uuid

            self.token_hash = f"pending:{uuid.uuid4().hex}"
        super().save(*args, **kwargs)

    # -- token helpers (kept on the model so no caller can store a raw token) --
    @staticmethod
    def hash_token(raw_token: str) -> str:
        return _hash_device_token(raw_token)

    def matches(self, raw_token: str) -> bool:
        return bool(self.token_hash) and self.token_hash == _hash_device_token(raw_token)


class Capability(TimeStampedModel):
    """A server-side permission atom exposed to staff clients as a capability.

    Codes are stable API/security identifiers (for example ``payment.capture``)
    and are intentionally separate from human-facing display labels.
    """

    code = models.CharField(max_length=80, unique=True)
    name = models.CharField(max_length=120)
    category = models.CharField(max_length=60, db_index=True)
    description = models.CharField(max_length=255, blank=True, default="")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["category", "code"]

    def __str__(self):
        return self.code


class RoleCapability(TimeStampedModel):
    """Persisted global role → capability grant.

    The initial data migration seeds the compatibility matrix from
    ``accounts.capabilities``.  Keeping it in the database makes future
    controlled role configuration possible without making client role claims
    authoritative.
    """

    role = models.CharField(max_length=32, choices=User.Role.choices, db_index=True)
    capability = models.ForeignKey(Capability, on_delete=models.PROTECT, related_name="role_grants")

    class Meta:
        ordering = ["role", "capability__code"]
        constraints = [
            models.UniqueConstraint(fields=["role", "capability"], name="unique_role_capability_grant"),
        ]

    def __str__(self):
        return f"{self.role} → {self.capability.code}"


class UserCapabilityOverride(TimeStampedModel):
    """Per-user capability grant or revocation controlled by an Administrator."""

    class Effect(models.TextChoices):
        GRANT = "GRANT", "Grant capability"
        REVOKE = "REVOKE", "Revoke capability"

    user = models.ForeignKey("accounts.User", on_delete=models.CASCADE, related_name="capability_overrides")
    capability = models.ForeignKey(Capability, on_delete=models.CASCADE, related_name="user_overrides")
    effect = models.CharField(max_length=12, choices=Effect.choices, default=Effect.GRANT, db_index=True)
    reason = models.CharField(max_length=255, blank=True, default="")
    updated_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="capability_overrides_authored"
    )

    class Meta:
        ordering = ["user_id", "capability__code"]
        constraints = [
            models.UniqueConstraint(fields=["user", "capability"], name="unique_user_capability_override"),
        ]

    def __str__(self):
        return f"{self.user.email} · {self.effect} · {self.capability.code}"

