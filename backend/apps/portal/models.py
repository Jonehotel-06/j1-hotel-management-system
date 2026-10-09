"""Verified-email guest portal access records.

Raw magic-link and session secrets are never stored.  The portal deliberately
uses its own short-lived, scoped session model instead of treating a booking
reference or legacy booking access token as a general identity credential.
"""
from django.db import models
from django.utils import timezone

from apps.core.models import TimeStampedModel


class PortalAccessChallenge(TimeStampedModel):
    class Purpose(models.TextChoices):
        LOGIN = "LOGIN", "Portal sign in"

    email = models.EmailField(db_index=True)
    purpose = models.CharField(max_length=30, choices=Purpose.choices, default=Purpose.LOGIN)
    # SHA-256 digest of an unguessable link token.  A unique digest makes a
    # consume retry unambiguous without persisting the bearer secret itself.
    token_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField(db_index=True)
    consumed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    request_ip = models.GenericIPAddressField(null=True, blank=True)
    request_user_agent = models.CharField(max_length=255, blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["email", "expires_at"]),
            models.Index(fields=["email", "consumed_at"]),
        ]

    @property
    def is_consumable(self):
        return self.consumed_at is None and self.expires_at > timezone.now()

    def __str__(self):
        return f"Portal challenge for {self.email} ({self.purpose})"


class PortalSession(TimeStampedModel):
    class AuthenticationMethod(models.TextChoices):
        EMAIL_MAGIC_LINK = "EMAIL_MAGIC_LINK", "Verified email magic link"

    email = models.EmailField(db_index=True)
    # SHA-256 digest of the opaque short-lived session bearer.  The raw value
    # is returned exactly once from consume and should be held only in memory
    # by a cross-origin portal client until a cookie-based refresh flow is
    # deliberately introduced for a compatible deployment topology.
    session_hash = models.CharField(max_length=64, unique=True)
    authentication_method = models.CharField(
        max_length=40,
        choices=AuthenticationMethod.choices,
        default=AuthenticationMethod.EMAIL_MAGIC_LINK,
    )
    challenge = models.ForeignKey(
        PortalAccessChallenge,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="sessions",
    )
    issued_at = models.DateTimeField(default=timezone.now, db_index=True)
    last_used_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(db_index=True)
    revoked_at = models.DateTimeField(null=True, blank=True, db_index=True)
    revoked_reason = models.CharField(max_length=255, blank=True, default="")
    device_label = models.CharField(max_length=120, blank=True, default="")
    request_ip = models.GenericIPAddressField(null=True, blank=True)
    request_user_agent = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["-issued_at"]
        indexes = [
            models.Index(fields=["email", "expires_at"]),
            models.Index(fields=["email", "revoked_at"]),
        ]

    @property
    def is_active(self):
        return self.revoked_at is None and self.expires_at > timezone.now()

    def __str__(self):
        return f"Portal session for {self.email} issued {self.issued_at:%Y-%m-%d %H:%M}"


class GuestPortalInvitation(TimeStampedModel):
    """One automatic portal invitation per stay, created after a successful check-in.

    The row is the idempotency key (unique stay) and links to the EmailLog that
    recorded the real provider outcome, so the status shown to staff is never a
    guess. The invitation carries no sign-in token: guests still sign in through
    the existing one-time magic-link flow.
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", "Queued"
        SENT = "SENT", "Sent"
        FAILED = "FAILED", "Not delivered"
        NO_EMAIL = "NO_EMAIL", "No email address on file"

    stay = models.OneToOneField("stays.Stay", on_delete=models.PROTECT, related_name="portal_invitation")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING, db_index=True)
    # Needed to deliver or resend. Staff responses show only a masked form.
    recipient_email = models.EmailField(blank=True, default="")
    email_log = models.ForeignKey(
        "notifications.EmailLog", null=True, blank=True, on_delete=models.SET_NULL, related_name="portal_invitations",
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    last_attempt_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_error_code = models.CharField(max_length=40, blank=True, default="")

    class Meta:
        ordering = ["-created_at", "-pk"]
        indexes = [models.Index(fields=["status", "last_attempt_at"])]

    def __str__(self):
        return f"Portal invitation for stay {self.stay_id}: {self.status}"
