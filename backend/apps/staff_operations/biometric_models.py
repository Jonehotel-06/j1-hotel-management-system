"""Biometric fingerprint records and temporary workstation sign-in credentials.

Standards:
- Conforms to ISO/IEC 19794-2 and ANSI/NIST-ITL minutiae template structures.
- Stores secure minutiae representations (x, y, angle, type, quality) or normalized feature vectors.
- Raw biometric images are NEVER stored or transmitted.
- Templates are retention-bound and cleared upon revocation/expiry.
- Successful verification can issue temporary, scoped workstation sign-in credentials.
"""
from django.conf import settings
from django.core.exceptions import ValidationError
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


class FingerprintDevice(TimeStampedModel):
    """Physical fingerprint reader bound to a workstation.

    This is the software integration boundary for attendance hardware: a
    vendor SDK or local capture service on the workstation submits captures
    under this device's identity. Raw fingerprint images are never stored —
    only ISO/IEC 19794-2 minutiae probes are processed, matched, and logged
    as outcomes. Device rows carry status/availability for supervisors; the
    binding to a Workstation preserves the approved workstation restrictions.
    """

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active / available"
        DISABLED = "DISABLED", "Disabled by management"
        FAULT = "FAULT", "Fault reported"

    serial_number = models.CharField(max_length=80, unique=True, db_index=True)
    label = models.CharField(max_length=120)
    workstation = models.ForeignKey(
        "accounts.Workstation", on_delete=models.PROTECT, related_name="fingerprint_devices"
    )
    vendor = models.CharField(max_length=80, blank=True, default="")
    model_name = models.CharField(max_length=80, blank=True, default="")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.ACTIVE, db_index=True)
    last_seen_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_error = models.CharField(max_length=255, blank=True, default="")
    registered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="fingerprint_devices_registered",
    )
    notes = models.CharField(max_length=500, blank=True, default="")

    class Meta:
        ordering = ["serial_number"]

    def __str__(self):
        return f"{self.serial_number} · {self.label}"


class FingerprintDeviceEvent(models.Model):
    """Append-only capture event from a bound reader (mocked or physical).

    Records who the device identified, what happened and when — the audit
    history for attendance hardware. ``event_nonce`` makes replayed or
    duplicated submissions detectable; no raw fingerprint image is stored.
    """

    device = models.ForeignKey(
        FingerprintDevice, on_delete=models.PROTECT, related_name="events"
    )
    event_nonce = models.CharField(max_length=80)
    staff = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="fingerprint_device_events",
    )
    action = models.CharField(max_length=30, blank=True, default="")
    captured_at = models.DateTimeField()
    received_at = models.DateTimeField(auto_now_add=True, db_index=True)
    outcome = models.CharField(max_length=30, db_index=True)
    match_score = models.FloatField(null=True, blank=True)
    attendance_reference = models.CharField(max_length=64, blank=True, default="")
    source_key = models.CharField(max_length=160, unique=True)
    details = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-received_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["device", "event_nonce"], name="unique_fingerprint_device_event_nonce"
            )
        ]
        indexes = [models.Index(fields=["device", "received_at"])]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Fingerprint device events are append-only and cannot be updated.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Fingerprint device events are append-only and cannot be deleted.")

    def __str__(self):
        return f"{self.device.serial_number} · {self.event_nonce} · {self.outcome}"
