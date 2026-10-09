"""Facial-verification records for staff attendance.

Privacy rules enforced here and in ``services/face_service.py``:
* Only a numeric face descriptor (a 128-number vector) is stored, never an image.
* Descriptors are cleared as soon as a template is rejected, revoked or expired.
* Attempt rows record the outcome code and distance only, never the probe.
"""
from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.core.models import TimeStampedModel


class FaceTemplate(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Awaiting approval"
        ACTIVE = "ACTIVE", "Active"
        REJECTED = "REJECTED", "Rejected"
        REVOKED = "REVOKED", "Revoked"
        EXPIRED = "EXPIRED", "Expired"

    staff = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="face_templates")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING, db_index=True)
    descriptor = models.JSONField(default=list, blank=True)
    model_version = models.CharField(max_length=40, default="descriptor-128-v1")
    requested_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="face_template_decisions")
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    decision_note = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        ordering = ["-requested_at", "-id"]
        indexes = [models.Index(fields=["staff", "status"], name="facetpl_staff_status_idx")]
        constraints = [
            # Database-level guarantee: at most one ACTIVE template per staff member.
            models.UniqueConstraint(fields=["staff"], condition=Q(status="ACTIVE"), name="one_active_face_template_per_staff"),
        ]

    def clear_descriptor(self):
        self.descriptor = []


class FaceVerificationAttempt(models.Model):
    staff = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="face_verification_attempts")
    action = models.CharField(max_length=20)
    outcome = models.CharField(max_length=24, db_index=True)
    distance = models.FloatField(null=True, blank=True)
    source_key = models.CharField(max_length=160, unique=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
