"""Magic-link challenge and short-lived opaque session services for the portal."""
from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed

from apps.audit.services import log_action
from apps.bookings.models import Guest, RoomOccupant
from apps.core.email_design import render_notice_email
from apps.core.emails import queue_email

from .eligibility import eligible_stay_ids_for_email, guest_emails_for_stay, has_active_portal_stay
from .models import PortalAccessChallenge, PortalSession


INVALID_PORTAL_CREDENTIAL_MESSAGE = "This portal sign-in link or session is invalid or has expired."
PORTAL_ACCESS_ENDED_MESSAGE = "Portal access ended because checkout is complete."
# DRF auth-failure code; the API envelope surfaces it as PORTAL_ACCESS_ENDED.
PORTAL_ACCESS_ENDED_CODE = "portal_access_ended"
# Stored in PortalSession.revoked_reason when checkout ends access, so a later
# request can explain the outcome instead of reporting a generic failure.
CHECKOUT_REVOKE_REASON = "Checkout complete"


def normalize_portal_email(email: str) -> str:
    """Canonical portal lookup key while preserving Django's email validation."""
    return str(email or "").strip().lower()


def hash_secret(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def portal_identity_exists(email: str) -> bool:
    """Whether this address is a guest (contact or room occupant) of any booking.

    Kept for callers that need identity rather than eligibility. Portal access
    decisions use ``has_active_portal_stay`` (see ``eligibility.py``), which
    additionally requires a current in-house stay.
    """
    email = normalize_portal_email(email)
    if not email:
        return False
    if Guest.objects.filter(email__iexact=email).exists():
        return True
    return RoomOccupant.objects.filter(email__iexact=email).exists()


def _client_ip(request):
    if request is None:
        return None
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip() or None
    return request.META.get("REMOTE_ADDR") or None


def _user_agent(request) -> str:
    if request is None:
        return ""
    return str(request.META.get("HTTP_USER_AGENT", ""))[:255]


def _setting_int(name: str, default: int, *, minimum: int = 1, maximum: int = 24 * 365):
    try:
        return max(minimum, min(int(getattr(settings, name, default)), maximum))
    except (TypeError, ValueError):  # defensive: configuration must not weaken auth
        return default


def _challenge_ttl() -> timedelta:
    return timedelta(minutes=_setting_int("PORTAL_CHALLENGE_MINUTES", 15, maximum=60))


def session_idle_ttl() -> timedelta:
    """Sliding idle timeout: a returning guest stays signed in while active."""
    return timedelta(hours=_setting_int("PORTAL_SESSION_IDLE_HOURS", 168, maximum=24 * 90))


def session_absolute_ttl() -> timedelta:
    """Hard cap on one session's life, regardless of activity."""
    return timedelta(hours=_setting_int("PORTAL_SESSION_MAX_HOURS", 720, maximum=24 * 365))


def _challenge_limit() -> int:
    return _setting_int("PORTAL_CHALLENGES_PER_EMAIL_PER_HOUR", 5, maximum=20)


def _portal_frontend_url() -> str:
    # This value is public deployment configuration, never a secret. A portal
    # may share FRONTEND_URL initially or live at a separately configured host.
    return (getattr(settings, "PORTAL_FRONTEND_URL", "") or settings.FRONTEND_URL).rstrip("/")


def portal_magic_link(token: str) -> str:
    return f"{_portal_frontend_url()}/portal/login.html?{urlencode({'token': token})}"


def _send_magic_link(*, email: str, token: str):
    """Compose the generic, non-booking-specific portal sign-in email."""
    link = portal_magic_link(token)
    text_body, html_body = render_notice_email(
        category="Guest portal access",
        title="Sign in to your guest portal",
        greeting="Hello,",
        paragraphs=[
            "Use the secure link below to view reservations and permitted guest services. "
            "For your protection, it can be used only once and expires shortly.",
        ],
        cta_label="Open Guest Portal",
        cta_url=link,
        footnote=(
            "If you did not request this link, you can ignore this email. "
            "No portal access is granted until the link is opened."
        ),
        preheader="Your secure J-ONE HOTEL & LODGE guest portal sign-in link.",
    )
    queue_email(
        subject="Your secure J-ONE HOTEL & LODGE guest portal link",
        message=text_body,
        html_message=html_body,
        recipients=[email],
        kind="GENERIC",
    )


def request_access_challenge(*, email: str, request=None) -> None:
    """Create/send a one-use challenge without exposing whether a guest exists.

    Unknown addresses receive exactly the same HTTP response but do not produce
    a challenge or send email.  The per-email window is an additional defence
    alongside DRF's per-client throttle and applies even when callers rotate
    IP addresses.
    """
    email = normalize_portal_email(email)
    now = timezone.now()
    # Only a verified, currently in-house guest may receive a link. Proof of
    # ownership still happens on consume; this only decides whether one is minted.
    if not has_active_portal_stay(email):
        return

    window_start = now - timedelta(hours=1)
    with transaction.atomic():
        recent_count = PortalAccessChallenge.objects.filter(
            email=email, created_at__gte=window_start
        ).count()
        if recent_count >= _challenge_limit():
            return

        # A newer link supersedes any outstanding one for this address. This
        # bounds the attack surface and makes the latest email unambiguous.
        PortalAccessChallenge.objects.filter(
            Q(expires_at__isnull=True) | Q(expires_at__gt=now),
            email=email,
            consumed_at__isnull=True,
        ).update(expires_at=now)
        token = secrets.token_urlsafe(32)
        challenge = PortalAccessChallenge.objects.create(
            email=email,
            purpose=PortalAccessChallenge.Purpose.LOGIN,
            token_hash=hash_secret(token),
            # No elapsed-time expiry: the link stays usable (once) for as long
            # as the address has an in-house stay. Checkout ends it.
            expires_at=None,
            request_ip=_client_ip(request),
            request_user_agent=_user_agent(request),
        )
        log_action(
            actor=None,
            action="PORTAL_ACCESS_CHALLENGE_CREATED",
            instance=challenge,
            metadata={"purpose": challenge.purpose},
            request=request,
            summary="Guest portal sign-in challenge requested",
        )
        # No raw token is written to a database row, log, or audit event. The
        # on-commit callback avoids an email that points to a rolled-back row.
        transaction.on_commit(lambda: _send_magic_link(email=email, token=token))


def consume_access_challenge(*, raw_token: str, request=None):
    """Consume a one-use magic link and issue an opaque short-lived session."""
    token_hash = hash_secret(raw_token)
    now = timezone.now()
    with transaction.atomic():
        try:
            challenge = PortalAccessChallenge.objects.select_for_update().get(token_hash=token_hash)
        except PortalAccessChallenge.DoesNotExist as exc:
            raise AuthenticationFailed(INVALID_PORTAL_CREDENTIAL_MESSAGE) from exc
        if not challenge.is_consumable:
            raise AuthenticationFailed(INVALID_PORTAL_CREDENTIAL_MESSAGE)

        # Do not mint a session if the guest is no longer in house (checked out,
        # cancelled, or the stay was released) since the email was sent.
        if not has_active_portal_stay(challenge.email):
            challenge.consumed_at = now
            challenge.save(update_fields=["consumed_at", "updated_at"])
            if _email_has_checked_out_stay(challenge.email):
                raise AuthenticationFailed(PORTAL_ACCESS_ENDED_MESSAGE, code=PORTAL_ACCESS_ENDED_CODE)
            raise AuthenticationFailed(INVALID_PORTAL_CREDENTIAL_MESSAGE)

        challenge.consumed_at = now
        challenge.save(update_fields=["consumed_at", "updated_at"])
        raw_session_token = secrets.token_urlsafe(32)
        session = PortalSession.objects.create(
            email=challenge.email,
            session_hash=hash_secret(raw_session_token),
            authentication_method=PortalSession.AuthenticationMethod.EMAIL_MAGIC_LINK,
            challenge=challenge,
            issued_at=now,
            last_used_at=now,
            expires_at=min(now + session_idle_ttl(), now + session_absolute_ttl()),
            device_label=_user_agent(request)[:120],
            request_ip=_client_ip(request),
            request_user_agent=_user_agent(request),
        )
        log_action(
            actor=None,
            action="PORTAL_SESSION_CREATED",
            instance=session,
            metadata={"authentication_method": session.authentication_method},
            request=request,
            summary="Verified guest portal session created",
        )
    return session, raw_session_token


def _email_has_checked_out_stay(email: str) -> bool:
    """Whether this address was a guest of a stay that has checked out.

    Used only to choose the explicit "checkout is complete" message; it never
    grants access.
    """
    from apps.stays.models import Stay

    email = normalize_portal_email(email)
    return Stay.objects.filter(
        Q(guest__email__iexact=email)
        | Q(booking__room_assignments__occupants__email__iexact=email),
        status=Stay.Status.CHECKED_OUT,
    ).exists()


def session_for_token(raw_token: str) -> PortalSession:
    """Resolve a session only while its guest is still eligible for the portal.

    Every request re-checks live stay state, so checkout, cancellation or a
    released room takes effect immediately, whatever the session row says.
    """
    token_hash = hash_secret(raw_token)
    try:
        session = PortalSession.objects.get(session_hash=token_hash)
    except PortalSession.DoesNotExist as exc:
        raise AuthenticationFailed(INVALID_PORTAL_CREDENTIAL_MESSAGE) from exc

    now = timezone.now()
    if session.revoked_at is not None:
        if session.revoked_reason == CHECKOUT_REVOKE_REASON:
            raise AuthenticationFailed(PORTAL_ACCESS_ENDED_MESSAGE, code=PORTAL_ACCESS_ENDED_CODE)
        raise AuthenticationFailed(INVALID_PORTAL_CREDENTIAL_MESSAGE)
    if session.expires_at <= now or session.issued_at + session_absolute_ttl() <= now:
        raise AuthenticationFailed(INVALID_PORTAL_CREDENTIAL_MESSAGE)

    if not has_active_portal_stay(session.email):
        if _email_has_checked_out_stay(session.email):
            raise AuthenticationFailed(PORTAL_ACCESS_ENDED_MESSAGE, code=PORTAL_ACCESS_ENDED_CODE)
        raise AuthenticationFailed(INVALID_PORTAL_CREDENTIAL_MESSAGE)

    # Sliding idle expiry, bounded by the absolute cap. Writes are throttled so
    # ordinary portal requests do not write on every call.
    touch_after = timedelta(minutes=_setting_int("PORTAL_SESSION_TOUCH_MINUTES", 15, maximum=60))
    if session.last_used_at <= now - touch_after:
        new_expiry = min(now + session_idle_ttl(), session.issued_at + session_absolute_ttl())
        PortalSession.objects.filter(pk=session.pk, revoked_at__isnull=True).update(
            last_used_at=now, expires_at=new_expiry
        )
        session.last_used_at = now
        session.expires_at = new_expiry
    return session


def end_portal_access_for_checked_out_stay(stay) -> None:
    """Revoke portal access that depended on a stay that has just checked out.

    Only addresses that no longer have ANY eligible stay are affected, so a guest
    with another in-house stay keeps access to that stay. Runs inside the
    checkout transaction: if checkout rolls back, this rolls back too. Access is
    enforced per request regardless (see ``session_for_token``), so this call is
    defence in depth that makes revocation visible in the data as well.
    """
    now = timezone.now()
    for email in guest_emails_for_stay(stay):
        if eligible_stay_ids_for_email(email):
            continue
        PortalAccessChallenge.objects.filter(
            Q(expires_at__isnull=True) | Q(expires_at__gt=now),
            email__iexact=email,
            consumed_at__isnull=True,
        ).update(expires_at=now)
        for session in PortalSession.objects.filter(email__iexact=email, revoked_at__isnull=True):
            revoke_session(session, reason=CHECKOUT_REVOKE_REASON)


def revoke_session(session: PortalSession, *, reason: str = "Signed out", request=None) -> PortalSession:
    """Revoke exactly this portal session; idempotent for logout retries."""
    now = timezone.now()
    with transaction.atomic():
        session = PortalSession.objects.select_for_update().get(pk=session.pk)
        if session.revoked_at is None:
            session.revoked_at = now
            session.revoked_reason = str(reason or "Signed out")[:255]
            session.save(update_fields=["revoked_at", "revoked_reason", "updated_at"])
            log_action(
                actor=None,
                action="PORTAL_SESSION_REVOKED",
                instance=session,
                metadata={"reason": session.revoked_reason},
                request=request,
                summary="Guest portal session revoked",
            )
    return session
