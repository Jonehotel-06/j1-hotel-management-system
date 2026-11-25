"""J-ONE reader-agent records (native Windows fingerprint agent).

WHY THIS MODULE EXISTS
======================
The DigitalPersona U.are.U 4500 is a USB device that the browser cannot open
(proprietary encrypted image pipeline, vendor driver -- no WebUSB/WebHID
interface). Staff-level attendance therefore needs a native component on the
reception PC. That component is the J-ONE Reader Agent
(``workstation_agent/jone_reader_agent.py``): it owns the sensor, runs the
vendor matcher (``dpfj_identify``) locally, and never sends fingerprint images
or templates to the browser.

These models are the server side of that contract:

``ReaderAgent``
    One provisioned agent per reception computer. Holds the HMAC key the API
    uses to authenticate the agent's requests (the agent proves possession of
    the key; the API never trusts a browser-supplied "success" flag).

``ReaderAgentSession``
    The single-use challenge the browser asks the API for *before* a biometric
    action. Bound to the operator, the action, the workstation and (for 1:1
    verification) the staff member, with a hard expiry. This is what makes a
    captured agent response impossible to replay.

``ReaderAgentEvent``
    Append-only log of every signed agent call (nonce uniqueness gives replay
    protection) plus the identity/score the agent measured.

``ReaderAgentTemplate``
    The enrolled template produced by the vendor SDK. An FMD (ISO/IEC
    19794-2:2005 Fingerprint Minutiae Data record, ~1 KB) -- never a raw
    image. Enrolment keeps the existing maker/checker rule: PENDING until a
    second authorised person approves it.

ISOLATION NOTE
--------------
Nothing here modifies the existing ``FingerprintTemplate`` /
``FingerprintDevice`` / ``FingerprintDeviceEvent`` behaviour. The legacy
minutiae-JSON enrolment path keeps working untouched; this module is additive
(new tables only, migration 0018) so an existing production database is not
rewritten and no existing record changes.
"""
from __future__ import annotations

import hashlib
import secrets

from django.conf import settings
from django.db import models

from apps.core.models import TimeStampedModel


def _new_agent_reference() -> str:
    return "RAG-" + secrets.token_hex(6).upper()


def _new_session_token_hash() -> str:
    return hashlib.sha256(secrets.token_bytes(32)).hexdigest()


class ReaderAgent(TimeStampedModel):
    """A provisioned J-ONE Reader Agent bound to one workstation and reader."""

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        REVOKED = "REVOKED", "Revoked"

    class DriverMode(models.TextChoices):
        LEGACY_SDK = "LEGACY_SDK", "DigitalPersona legacy driver + U.are.U SDK"
        WBF = "WBF", "Windows Biometric Framework (Windows Hello) driver"
        UNKNOWN = "UNKNOWN", "Unknown"

    reference = models.CharField(max_length=32, unique=True, default=_new_agent_reference, editable=False)
    label = models.CharField(max_length=120)
    workstation = models.ForeignKey(
        "accounts.Workstation", on_delete=models.PROTECT, related_name="reader_agents"
    )
    device_serial = models.CharField(max_length=80, blank=True, default="", db_index=True)
    model_name = models.CharField(max_length=80, blank=True, default="")
    firmware = models.CharField(max_length=40, blank=True, default="")
    sdk_version = models.CharField(max_length=40, blank=True, default="")
    driver_mode = models.CharField(max_length=12, choices=DriverMode.choices, default=DriverMode.UNKNOWN)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.ACTIVE, db_index=True)
    # HMAC-SHA256 key. Provisioned once, shown once, rotated on demand. The
    # agent stores its copy DPAPI-protected (per-machine, current user).
    signing_key = models.CharField(max_length=128, editable=False)
    provisioned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="reader_agents_provisioned",
    )
    last_seen_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_heartbeat = models.JSONField(default=dict, blank=True)
    last_error = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["label", "reference"]
        indexes = [models.Index(fields=["workstation", "status"], name="rag_ws_status_idx")]

    def __str__(self) -> str:
        return f"{self.reference} · {self.label}"

    def rotate_signing_key(self) -> str:
        self.signing_key = secrets.token_hex(32)
        self.save(update_fields=["signing_key", "updated_at"])
        return self.signing_key

    @property
    def is_active(self) -> bool:
        return self.status == self.Status.ACTIVE


class ReaderAgentSession(TimeStampedModel):
    """Browser-requested, single-use challenge for one biometric operation."""

    class Status(models.TextChoices):
        PENDING = "PENDING", "Waiting for the reader agent"
        DONE = "DONE", "Completed"
        FAILED = "FAILED", "Failed"
        EXPIRED = "EXPIRED", "Expired"

    class Action(models.TextChoices):
        CLOCK_IN = "CLOCK_IN", "Clock in"
        CLOCK_OUT = "CLOCK_OUT", "Clock out"
        BREAK_START = "BREAK_START", "Start break"
        BREAK_END = "BREAK_END", "End break"
        VERIFY = "VERIFY", "Verify identity only"
        ENROLL = "ENROLL", "Enrol fingerprint"

    token_hash = models.CharField(max_length=64, unique=True, default=_new_session_token_hash, editable=False)
    action = models.CharField(max_length=16, choices=Action.choices)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True)
    # 1:1 target for VERIFY/ENROLL; blank means 1:N identification (kiosk).
    staff = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="reader_agent_sessions",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="reader_agent_sessions_requested",
    )
    agent = models.ForeignKey(ReaderAgent, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="sessions")
    workstation_reference = models.CharField(max_length=48, blank=True, default="")
    finger_position = models.CharField(max_length=20, blank=True, default="RIGHT_INDEX")
    expires_at = models.DateTimeField(db_index=True)
    consumed_at = models.DateTimeField(null=True, blank=True)
    result = models.JSONField(default=dict, blank=True)
    error_code = models.CharField(max_length=40, blank=True, default="")
    error_message = models.CharField(max_length=300, blank=True, default="")

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["status", "expires_at"], name="rag_sess_status_idx")]

    def __str__(self) -> str:
        return f"{self.action} · {self.status}"


class ReaderAgentEvent(models.Model):
    """Append-only audit of signed agent calls; unique nonce = replay refusal."""

    class Kind(models.TextChoices):
        HEARTBEAT = "HEARTBEAT", "Heartbeat / status"
        GALLERY = "GALLERY", "Template gallery sync"
        IDENTIFY = "IDENTIFY", "Identification / verification"
        ENROLL = "ENROLL", "Enrolment capture"
        ERROR = "ERROR", "Rejected call"

    agent = models.ForeignKey(ReaderAgent, on_delete=models.PROTECT, related_name="events")
    kind = models.CharField(max_length=12, choices=Kind.choices, db_index=True)
    nonce = models.CharField(max_length=80, unique=True)
    outcome = models.CharField(max_length=40, blank=True, default="")
    staff = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="reader_agent_events",
    )
    match_score = models.FloatField(null=True, blank=True)
    session = models.ForeignKey(ReaderAgentSession, null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="events")
    detail = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["agent", "created_at"], name="rag_event_agent_idx")]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValueError("Reader agent events are append-only and cannot be updated.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):  # pragma: no cover - defensive
        raise ValueError("Reader agent events are append-only and cannot be deleted.")

    def __str__(self) -> str:
        return f"{self.agent.reference} · {self.kind} · {self.outcome}"


class ReaderAgentTemplate(TimeStampedModel):
    """An enrolled vendor template (FMD) for one staff member."""

    class Status(models.TextChoices):
        PENDING = "PENDING", "Awaiting approval"
        ACTIVE = "ACTIVE", "Active"
        REJECTED = "REJECTED", "Rejected"
        REVOKED = "REVOKED", "Revoked"
        EXPIRED = "EXPIRED", "Expired"

    staff = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                              related_name="reader_agent_templates")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True)
    finger_position = models.CharField(max_length=20, default="RIGHT_INDEX")
    # Base64 of the ISO/IEC 19794-2:2005 FMD produced by dpfj. No images.
    fmd = models.TextField()
    fmd_size = models.PositiveIntegerField(default=0)
    views = models.PositiveSmallIntegerField(default=0)
    template_format = models.CharField(max_length=40, default="ISO_19794_2_2005_FMD")
    enrolled_by_agent = models.ForeignKey(ReaderAgent, null=True, blank=True, on_delete=models.SET_NULL,
                                          related_name="templates")
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="reader_agent_templates_requested",
    )
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="reader_agent_templates_decided",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField(max_length=200, blank=True, default="")
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["staff", "status"], name="rag_tpl_staff_status_idx")]
        constraints = [
            models.UniqueConstraint(fields=["staff"], condition=models.Q(status="ACTIVE"),
                                    name="one_active_reader_agent_template_per_staff"),
        ]

    def __str__(self) -> str:
        return f"{self.staff_id} · {self.finger_position} · {self.status}"
