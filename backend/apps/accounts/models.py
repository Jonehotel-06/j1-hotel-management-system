# apps/accounts/models.py
"""Custom user model — email is the sole authentication identifier."""
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
        # contracts. The operational roles are additive and are authorized by
        # the persisted capability matrix rather than browser-provided claims.
        ADMIN = "ADMIN", "Administrator"
        MANAGER = "MANAGER", "Manager"
        RECEPTIONIST = "RECEPTIONIST", "Receptionist"
        CASHIER = "CASHIER", "Cashier"
        HOUSEKEEPING = "HOUSEKEEPING", "Housekeeping"
        MAINTENANCE = "MAINTENANCE", "Maintenance"
        INVENTORY_CLERK = "INVENTORY_CLERK", "Inventory Clerk"
        GUEST = "GUEST", "Guest"

    email = models.EmailField(unique=True, db_index=True)
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    phone = models.CharField(max_length=20, blank=True, default="")
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.RECEPTIONIST, db_index=True)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)  # Django admin access only
    email_verified = models.BooleanField(default=False)
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
        return f"{self.first_name} {self.last_name}".strip() or self.email

    @property
    def is_admin(self):
        return self.role == self.Role.ADMIN

    @property
    def is_manager_or_admin(self):
        return self.role in (self.Role.ADMIN, self.Role.MANAGER)

    @property
    def is_staff_member(self):
        """Operational staff, including the additive capability-era roles.

        Legacy broad endpoint permission classes intentionally remain scoped to
        ADMIN/MANAGER/RECEPTIONIST until they are migrated individually. New
        operational endpoints use named capabilities instead.
        """
        return self.role in (
            self.Role.ADMIN,
            self.Role.MANAGER,
            self.Role.RECEPTIONIST,
            self.Role.CASHIER,
            self.Role.HOUSEKEEPING,
            self.Role.MAINTENANCE,
            self.Role.INVENTORY_CLERK,
        )


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

    role = models.CharField(max_length=20, choices=User.Role.choices, db_index=True)
    capability = models.ForeignKey(Capability, on_delete=models.PROTECT, related_name="role_grants")

    class Meta:
        ordering = ["role", "capability__code"]
        constraints = [
            models.UniqueConstraint(fields=["role", "capability"], name="unique_role_capability_grant"),
        ]

    def __str__(self):
        return f"{self.role} → {self.capability.code}"
