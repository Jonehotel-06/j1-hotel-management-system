# apps/portal/invitations.py
"""Automatic guest-portal invitation after a successful check-in.

Guarantees
- Idempotent: one GuestPortalInvitation per stay (unique FK). A repeated check-in
  signal, a race, or a retry cannot send a second automatic invitation.
- Non-blocking: scheduling runs on transaction commit, so a notification problem
  can never roll back or delay the check-in. Any delivery error is recorded on the
  invitation (FAILED + safe error code) and logged; it is never silently dropped.
- Honest status: the invitation status follows the EmailLog row written by the
  synchronous email pipeline (SENT / FAILED), not an assumed success.
- Staff fallback: status, a masked recipient, copy-ready text (no token), and a
  rate-limited resend.
"""
from __future__ import annotations

import functools
import logging
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.core.exceptions import JOneAPIError
from apps.core.emails import send_email_safe
from apps.notifications.models import EmailLog

from .models import GuestPortalInvitation
from .services import _portal_frontend_url

logger = logging.getLogger("apps")

INVITATION_KIND = "PORTAL_INVITATION"


class InvitationRecentlySentError(JOneAPIError):
    status_code = 409
    default_detail = "A portal invitation was sent recently. Please wait before sending another."
    default_code = "INVITATION_RECENTLY_SENT"


def resend_cooldown() -> timedelta:
    minutes = int(getattr(settings, "PORTAL_INVITATION_RESEND_COOLDOWN_MINUTES", 5))
    return timedelta(minutes=max(minutes, 0))


def portal_login_url() -> str:
    # No token and no guest data in the URL: the guest signs in with their email.
    return f"{_portal_frontend_url()}/portal/login.html"


def mask_email(email: str) -> str:
    local, _, domain = (email or "").partition("@")
    if not domain:
        return ""
    visible = local[:1] if len(local) <= 2 else local[:2]
    return f"{visible}{'*' * max(len(local) - len(visible), 1)}@{domain}"


def copy_text_for(invitation: GuestPortalInvitation, guest_first_name: str = "") -> str:
    greeting = f"Dear {guest_first_name}," if guest_first_name else "Dear guest,"
    return (
        f"{greeting}\n\nYou can manage your J-ONE Hotel & Lodge stay online: view your folio, "
        f"request room service and track your requests.\n\n"
        f"Open {portal_login_url()} and sign in with the email address you gave at check-in. "
        f"We will send a one-time sign-in link to that address.\n\nJ-ONE Hotel & Lodge"
    )


def schedule_portal_invitation(stay_id: int) -> None:
    """Call inside the check-in transaction. Work happens only after a successful commit."""
    transaction.on_commit(functools.partial(_create_and_deliver, stay_id, None))


def _create_and_deliver(stay_id: int, actor_id) -> GuestPortalInvitation | None:
    """Create the invitation once and deliver it. Never raises into the caller."""
    try:
        invitation, created = GuestPortalInvitation.objects.get_or_create(stay_id=stay_id)
    except IntegrityError:
        # A concurrent scheduler created it first; that invitation is authoritative.
        return GuestPortalInvitation.objects.filter(stay_id=stay_id).first()
    except Exception:
        logger.exception("Portal invitation record could not be created for stay %s", stay_id)
        return None
    if not created:
        return invitation  # idempotent: already queued, sent, failed or skipped
    return _deliver(invitation, actor_id=actor_id)


def _deliver(invitation: GuestPortalInvitation, *, actor_id=None) -> GuestPortalInvitation:
    stay = invitation.stay.__class__.objects.select_related("guest", "booking").get(pk=invitation.stay_id)
    email = (getattr(stay.guest, "email", "") or "").strip()
    invitation.attempts = invitation.attempts + 1
    invitation.last_attempt_at = timezone.now()
    if not email:
        invitation.status = GuestPortalInvitation.Status.NO_EMAIL
        invitation.last_error_code = "NO_EMAIL"
        invitation.save(update_fields=["status", "attempts", "last_attempt_at", "last_error_code", "updated_at"])
        return invitation
    invitation.recipient_email = email
    invitation.status = GuestPortalInvitation.Status.PENDING
    invitation.last_error_code = ""
    invitation.save(update_fields=["recipient_email", "status", "attempts", "last_attempt_at", "last_error_code", "updated_at"])
    first_name = (getattr(stay.guest, "first_name", "") or "").strip()
    try:
        log = send_email_safe(
            "Manage your stay with J-ONE Hotel & Lodge",
            copy_text_for(invitation, first_name),
            [email],
            kind=INVITATION_KIND,
            booking_reference=getattr(stay.booking, "booking_reference", "") or "",
            created_by=None,
        )
    except Exception:
        logger.exception("Portal invitation delivery raised unexpectedly for stay %s", stay.pk)
        invitation.status = GuestPortalInvitation.Status.FAILED
        invitation.last_error_code = "DELIVERY_EXCEPTION"
        invitation.save(update_fields=["status", "last_error_code", "updated_at"])
        return invitation
    invitation.email_log = log
    invitation.status = _status_from_log(log)
    invitation.last_error_code = "" if invitation.status == GuestPortalInvitation.Status.SENT else "PROVIDER_REJECTED"
    invitation.save(update_fields=["email_log", "status", "last_error_code", "updated_at"])
    logger.info("Portal invitation for stay %s: %s", stay.pk, invitation.status)
    return invitation


def _status_from_log(log) -> str:
    if log is None:
        return GuestPortalInvitation.Status.FAILED
    if log.status == EmailLog.Status.SENT:
        return GuestPortalInvitation.Status.SENT
    if log.status == EmailLog.Status.FAILED:
        return GuestPortalInvitation.Status.FAILED
    return GuestPortalInvitation.Status.PENDING


def sync_invitation_status(invitation: GuestPortalInvitation) -> GuestPortalInvitation:
    """Refresh status from the EmailLog row when delivery has finished since the last read."""
    if invitation.email_log_id:
        log = EmailLog.objects.filter(pk=invitation.email_log_id).only("status").first()
        current = _status_from_log(log)
        if current != invitation.status and current != GuestPortalInvitation.Status.PENDING:
            invitation.status = current
            invitation.save(update_fields=["status", "updated_at"])
    return invitation


def resend_portal_invitation(*, stay, actor):
    """Staff resend. Rate-limited per stay; delivery happens after the lock is released."""
    with transaction.atomic():
        invitation, _ = GuestPortalInvitation.objects.select_for_update().get_or_create(stay=stay)
        now = timezone.now()
        if invitation.last_attempt_at and invitation.status == GuestPortalInvitation.Status.SENT:
            if now - invitation.last_attempt_at < resend_cooldown():
                raise InvitationRecentlySentError()
        invitation.status = GuestPortalInvitation.Status.PENDING
        invitation.last_attempt_at = now
        invitation.save(update_fields=["status", "last_attempt_at", "updated_at"])
    return _deliver(invitation, actor_id=getattr(actor, "pk", None))
