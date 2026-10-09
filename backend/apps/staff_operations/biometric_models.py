"""Biometric fingerprint records and temporary workstation sign-in credentials.

Standards:
- Conforms to ISO/IEC 19794-2 and ANSI/NIST-ITL minutiae template structures.
- Stores secure minutiae representations (x, y, angle, type, quality) or normalized feature vectors.
- Raw biometric images are NEVER stored or transmitted.
- Templates are retention-bound and cleared upon revocation/expiry.
- Successful verification can issue temporary, scoped workstation sign-in credentials.
"""
from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.core.models import TimeStampedModel


class FingerprintTemplate(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Awaiting approval"
        ACTIVE = "ACTIVE", "Active"
        REJECTED = "REJECTED", "Rejected"
        REVOKED = "REVOKED", "Revoked"
        EXPIRED = "EXPIRED", "Expired"

    class FingerPosition(models.TextChoices):
        UNKNOWN = "UNKNOWN", "Unknown / unspecified"
        RIGHT_THUMB = "RIGHT_THUMB", "Right thumb"
        RIGHT_INDEX = "RIGHT_INDEX", "Right index"
        RIGHT_MIDDLE = "RIGHT_MIDDLE", "Right middle"
        RIGHT_RING = "RIGHT_RING", "Right ring"
        RIGHT_LITTLE = "RIGHT_LITTLE", "Right little"
        LEFT_THUMB = "LEFT_THUMB", "Left thumb"
        LEFT_INDEX = "LEFT_INDEX", "Left index"
        LEFT_MIDDLE = "LEFT_MIDDLE", "Left middle"
        LEFT_RING = "LEFT_RING", "Left ring"
        LEFT_LITTLE = "LEFT_LITTLE", "Left little"

    staff = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="fingerprint_templates")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING, db_index=True)
    finger_position = models.CharField(max_length=20, choices=FingerPosition.choices, default=FingerPosition.RIGHT_INDEX)
    minutiae_data = models.JSONField(default=list, blank=True)
    template_format = models.CharField(max_length=40, default="ISO_19794_2_MINUTIAE")
    requested_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="fingerprint_template_decisions")
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    decision_note = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        ordering = ["-requested_at", "-id"]
        indexes = [models.Index(fields=["staff", "status"], name="fptpl_staff_status_idx")]
        constraints = [
            # At most one active template per staff member
            models.UniqueConstraint(fields=["staff"], condition=Q(status="ACTIVE"), name="one_active_fingerprint_template_per_staff"),
        ]

    def clear_minutiae(self):
        self.minutiae_data = []


class FingerprintVerificationAttempt(models.Model):
    staff = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="fingerprint_verification_attempts")
    action = models.CharField(max_length=30)
    outcome = models.CharField(max_length=30, db_index=True)
    match_score = models.FloatField(null=True, blank=True)
    source_key = models.CharField(max_length=160, unique=True)
    workstation_reference = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]


class TemporaryWorkstationCredential(TimeStampedModel):
    """Temporary login credential minted upon genuine biometric verification."""
    staff = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="temp_workstation_credentials")
    workstation_reference = models.CharField(max_length=64, db_index=True)
    credential_token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    expires_at = models.DateTimeField(db_index=True)
    is_used = models.BooleanField(default=False, db_index=True)
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
