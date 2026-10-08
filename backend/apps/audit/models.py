# apps/audit/models.py
"""Immutable audit trail for sensitive administrative and financial actions."""
from django.conf import settings
from django.db import models


class AuditLog(models.Model):
    """Append-only record. Rows are never updated or deleted via the API or
    Django admin (deleted there only to mirror API behaviour)."""

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_events",
    )
    action = models.CharField(max_length=60, db_index=True)
    object_type = models.CharField(max_length=120, db_index=True, blank=True, default="")
    object_id = models.CharField(max_length=64, blank=True, default="")
    summary = models.CharField(max_length=255, blank=True, default="")
    changes = models.JSONField(default=dict, blank=True)   # {"field": [old, new]}
    metadata = models.JSONField(default=dict, blank=True)  # extra context (refs, amounts)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["object_type", "object_id"])]

    def __str__(self):
        actor = self.actor.email if self.actor else "system"
        return f"{self.action} by {actor} @ {self.created_at:%Y-%m-%d %H:%M:%S}"
