"""Transactional service-request lifecycle with append-only operational evidence."""
from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.capabilities import has_capability
from apps.accounts.models import User
from apps.stays.models import StayRoom

from ..models import ServiceRequest, ServiceRequestEvent

_DEFAULT_TEAM = {
    ServiceRequest.Category.HOUSEKEEPING: ServiceRequest.OwnerTeam.HOUSEKEEPING,
    ServiceRequest.Category.MAINTENANCE: ServiceRequest.OwnerTeam.MAINTENANCE,
    ServiceRequest.Category.ROOM_SERVICE: ServiceRequest.OwnerTeam.FOOD_BEVERAGE,
    ServiceRequest.Category.AMENITY: ServiceRequest.OwnerTeam.HOUSEKEEPING,
    ServiceRequest.Category.TRANSPORT: ServiceRequest.OwnerTeam.FRONT_DESK,
    ServiceRequest.Category.BILLING: ServiceRequest.OwnerTeam.FRONT_DESK,
    ServiceRequest.Category.GENERAL: ServiceRequest.OwnerTeam.FRONT_DESK,
}
_SLA = {
    ServiceRequest.Priority.LOW: timedelta(hours=24),
    ServiceRequest.Priority.NORMAL: timedelta(hours=4),
    ServiceRequest.Priority.HIGH: timedelta(hours=1),
    ServiceRequest.Priority.URGENT: timedelta(minutes=15),
}
_TEAM_FOR_ROLE = {
    User.Role.HOUSEKEEPING: ServiceRequest.OwnerTeam.HOUSEKEEPING,
    User.Role.MAINTENANCE: ServiceRequest.OwnerTeam.MAINTENANCE,
}
_TERMINAL_STATUSES = {ServiceRequest.Status.CLOSED, ServiceRequest.Status.CANCELLED}
_ALLOWED_TRANSITIONS = {
    ServiceRequest.Status.OPEN: {
        ServiceRequest.Status.ACKNOWLEDGED,
        ServiceRequest.Status.IN_PROGRESS,
        ServiceRequest.Status.ESCALATED,
        ServiceRequest.Status.CANCELLED,
    },
    ServiceRequest.Status.ACKNOWLEDGED: {
        ServiceRequest.Status.IN_PROGRESS,
        ServiceRequest.Status.ESCALATED,
        ServiceRequest.Status.CANCELLED,
    },
    ServiceRequest.Status.IN_PROGRESS: {
        ServiceRequest.Status.RESOLVED,
        ServiceRequest.Status.ESCALATED,
    },
    ServiceRequest.Status.ESCALATED: {
        ServiceRequest.Status.ACKNOWLEDGED,
        ServiceRequest.Status.IN_PROGRESS,
        ServiceRequest.Status.RESOLVED,
        ServiceRequest.Status.CANCELLED,
    },
    ServiceRequest.Status.RESOLVED: {
        ServiceRequest.Status.CLOSED,
        ServiceRequest.Status.IN_PROGRESS,
    },
}


def _reference() -> str:
    return f"SRQ-{timezone.now():%Y%m%d}-{secrets.token_hex(6).upper()}"


def scoped_idempotency_key(*, scope: str, raw_key: str | None) -> str | None:
    """Hash client keys within an authenticated requester scope before storage."""
    raw_key = str(raw_key or "").strip()
    if not raw_key:
        return None
    if len(raw_key) > 128:
        raise ValidationError({"idempotency_key": "Idempotency key must be at most 128 characters."})
    digest = hashlib.sha256(f"{scope}:{raw_key}".encode("utf-8")).hexdigest()
    return f"service-request:{digest}"


def _actor_label(actor, fallback="") -> str:
    return (getattr(actor, "full_name", "") or getattr(actor, "email", "") or fallback or "").strip()[:160]


def _create_event(*, request, event_type, actor=None, actor_label="", message="", guest_visible=False,
                  previous_status="", new_status="", details=None):
    return ServiceRequestEvent.objects.create(
        request=request,
        type=event_type,
        actor=actor,
        actor_label=_actor_label(actor, actor_label),
        message=(message or "").strip(),
        guest_visible=bool(guest_visible),
        previous_status=previous_status or "",
        new_status=new_status or "",
        details=dict(details or {}),
    )


def _validate_context(*, guest, stay=None, room=None):
    if stay and stay.guest_id != guest.pk:
        raise ValidationError("The service-request stay must belong to the selected guest.")
    if room and stay:
        # A request remains tied to an actual historic/current occupancy, not a
        # caller-provided room number. Released rooms are valid for a post-stay
        # follow-up, while active occupancy is used for current portal requests.
        if not StayRoom.objects.filter(stay=stay, room=room).exists():
            raise ValidationError("The service-request room is not assigned to the selected stay.")


def _validate_assignee(assignee):
    if assignee is None:
        return
    if not assignee.is_active or not assignee.is_staff_member:
        raise ValidationError("Service requests can only be assigned to an active operational staff user.")


def _insert_service_request(*, idempotency_key=None, **fields):
    """Create a request safely under concurrent browser retries/reference races."""
    for _ in range(8):
        try:
            # A savepoint keeps the outer business transaction usable after a
            # competing request wins the idempotency-key race.
            with transaction.atomic():
                return ServiceRequest.objects.create(reference=_reference(), idempotency_key=idempotency_key, **fields), True
        except IntegrityError:
            if idempotency_key:
                existing = ServiceRequest.objects.select_for_update().filter(idempotency_key=idempotency_key).first()
                if existing:
                    return existing, False
            # A reference collision is extraordinarily unlikely, but retry it
            # rather than exposing it as a failed guest submission.
    raise ValidationError("Could not allocate a unique service-request reference; please retry.")


def _can_dispatch(actor) -> bool:
    return bool(actor and has_capability(actor, "guest_request.assign"))


def _can_work(request, actor) -> bool:
    return bool(actor and (_can_dispatch(actor) or request.assigned_to_id == actor.pk))


@transaction.atomic
def create_service_request(
    *,
    guest,
    category: str,
    priority: str = ServiceRequest.Priority.NORMAL,
    channel: str,
    summary: str,
    detail: str = "",
    stay=None,
    room=None,
    owner_team: str | None = None,
    assigned_to=None,
    due_at=None,
    actor=None,
    portal_email="",
    idempotency_key: str | None = None,
) -> tuple[ServiceRequest, bool]:
    """Create a request with a stable retry key and immutable creation event."""
    if category not in ServiceRequest.Category.values:
        raise ValidationError({"category": "Invalid service-request category."})
    if priority not in ServiceRequest.Priority.values:
        raise ValidationError({"priority": "Invalid service-request priority."})
    if channel not in ServiceRequest.Channel.values:
        raise ValidationError({"channel": "Invalid service-request channel."})
    if owner_team and owner_team not in ServiceRequest.OwnerTeam.values:
        raise ValidationError({"owner_team": "Invalid owner team."})
    summary = str(summary or "").strip()
    if not summary:
        raise ValidationError({"summary": "A concise request summary is required."})
    if len(summary) > 255:
        raise ValidationError({"summary": "Summary must be at most 255 characters."})
    detail = str(detail or "").strip()
    if len(detail) > 5000:
        raise ValidationError({"detail": "Detail must be at most 5000 characters."})
    _validate_context(guest=guest, stay=stay, room=room)
    _validate_assignee(assigned_to)
    if idempotency_key:
        existing = ServiceRequest.objects.select_for_update().filter(idempotency_key=idempotency_key).first()
        if existing:
            if existing.guest_id != guest.pk:
                # Digest collision/cross-scope reuse must not disclose another
                # guest's request even though a SHA-256 collision is implausible.
                raise ValidationError({"idempotency_key": "This retry key is not valid for this guest."})
            return existing, False
    now = timezone.now()
    if due_at is not None and due_at <= now:
        raise ValidationError({"due_at": "Due time must be in the future."})
    request, created = _insert_service_request(
        idempotency_key=idempotency_key,
        guest=guest,
        stay=stay,
        room=room,
        category=category,
        priority=priority,
        channel=channel,
        owner_team=owner_team or _DEFAULT_TEAM[category],
        assigned_to=assigned_to,
        summary=summary,
        detail=detail,
        due_at=due_at or (now + _SLA[priority]),
        created_by=actor,
        portal_email=(portal_email or "")[:254],
    )
    if not created:
        if request.guest_id != guest.pk:
            raise ValidationError({"idempotency_key": "This retry key is not valid for this guest."})
        return request, False
    _create_event(
        request=request,
        event_type=ServiceRequestEvent.Type.CREATED,
        actor=actor,
        actor_label="Guest" if not actor else "",
        message=detail,
        guest_visible=True,
        new_status=request.status,
        details={"channel": channel, "category": category, "priority": priority},
    )
    if assigned_to:
        _create_event(
            request=request,
            event_type=ServiceRequestEvent.Type.ASSIGNED,
            actor=actor,
            message="",
            guest_visible=False,
            details={"assigned_to_id": assigned_to.pk, "assigned_to": assigned_to.full_name},
        )
    return request, True


@transaction.atomic
def assign_service_request(*, service_request, actor, assigned_to=None, owner_team=None, due_at=None, note=""):
    """Dispatch/re-route a request; only the named dispatcher capability may do so."""
    if not _can_dispatch(actor):
        raise PermissionDenied("This user cannot dispatch guest service requests.")
    request = ServiceRequest.objects.select_for_update().get(pk=service_request.pk)
    if request.status in _TERMINAL_STATUSES:
        raise ValidationError("A closed or cancelled request cannot be reassigned.")
    _validate_assignee(assigned_to)
    if owner_team and owner_team not in ServiceRequest.OwnerTeam.values:
        raise ValidationError({"owner_team": "Invalid owner team."})
    if due_at is not None and due_at <= timezone.now():
        raise ValidationError({"due_at": "Due time must be in the future."})
    changed = (
        request.assigned_to_id != getattr(assigned_to, "pk", None)
        or (owner_team and request.owner_team != owner_team)
        or (due_at is not None and request.due_at != due_at)
    )
    if not changed:
        return request
    request.assigned_to = assigned_to
    if owner_team:
        request.owner_team = owner_team
    if due_at is not None:
        request.due_at = due_at
    request.save(update_fields=["assigned_to", "owner_team", "due_at", "updated_at"])
    _create_event(
        request=request,
        event_type=ServiceRequestEvent.Type.ASSIGNED,
        actor=actor,
        message=note,
        guest_visible=False,
        details={
            "assigned_to_id": assigned_to.pk if assigned_to else None,
            "assigned_to": assigned_to.full_name if assigned_to else "",
            "owner_team": request.owner_team,
            "due_at": request.due_at.isoformat() if request.due_at else None,
        },
    )
    return request


@transaction.atomic
def claim_service_request(*, service_request, actor):
    """Let an eligible housekeeping/maintenance worker claim an unassigned task."""
    request = ServiceRequest.objects.select_for_update().get(pk=service_request.pk)
    if request.status in _TERMINAL_STATUSES:
        raise ValidationError("A closed or cancelled request cannot be claimed.")
    if request.assigned_to_id:
        if request.assigned_to_id == actor.pk:
            return request
        raise ValidationError("This request is already assigned to another staff user.")
    if _can_dispatch(actor):
        return assign_service_request(service_request=request, actor=actor, assigned_to=actor)
    if _TEAM_FOR_ROLE.get(actor.role) != request.owner_team:
        raise PermissionDenied("This unassigned request is not in the caller's permitted team queue.")
    _validate_assignee(actor)
    request.assigned_to = actor
    request.save(update_fields=["assigned_to", "updated_at"])
    _create_event(
        request=request,
        event_type=ServiceRequestEvent.Type.ASSIGNED,
        actor=actor,
        guest_visible=False,
        details={"assigned_to_id": actor.pk, "assigned_to": actor.full_name, "claimed": True},
    )
    return request


@transaction.atomic
def transition_service_request(*, service_request, target_status: str, actor, note="", guest_visible=True):
    """Advance the controlled lifecycle; workers may only work their own requests."""
    request = ServiceRequest.objects.select_for_update().get(pk=service_request.pk)
    target_status = str(target_status or "").upper()
    if target_status not in ServiceRequest.Status.values:
        raise ValidationError({"status": "Invalid service-request status."})
    if target_status == request.status:
        return request
    if target_status not in _ALLOWED_TRANSITIONS.get(request.status, set()):
        raise ValidationError(f"Request {request.reference} cannot transition from {request.status} to {target_status}.")
    if not _can_work(request, actor):
        raise PermissionDenied("Only the assigned worker or an authorized dispatcher can progress this request.")
    previous_status = request.status
    now = timezone.now()
    request.status = target_status
    update_fields = ["status", "updated_at"]
    if target_status == ServiceRequest.Status.ACKNOWLEDGED and request.acknowledged_at is None:
        request.acknowledged_at = now
        update_fields.append("acknowledged_at")
    if target_status == ServiceRequest.Status.RESOLVED:
        request.resolved_at = now
        update_fields.append("resolved_at")
    elif target_status == ServiceRequest.Status.IN_PROGRESS and previous_status == ServiceRequest.Status.RESOLVED:
        request.resolved_at = None
        update_fields.append("resolved_at")
    if target_status in _TERMINAL_STATUSES:
        request.closed_at = now
        update_fields.append("closed_at")
    request.save(update_fields=update_fields)
    event_type = (
        ServiceRequestEvent.Type.ACKNOWLEDGED if target_status == ServiceRequest.Status.ACKNOWLEDGED
        else ServiceRequestEvent.Type.ESCALATED if target_status == ServiceRequest.Status.ESCALATED
        else ServiceRequestEvent.Type.CANCELLED if target_status == ServiceRequest.Status.CANCELLED
        else ServiceRequestEvent.Type.STATUS_CHANGED
    )
    _create_event(
        request=request,
        event_type=event_type,
        actor=actor,
        message=note,
        guest_visible=guest_visible,
        previous_status=previous_status,
        new_status=target_status,
    )
    return request


@transaction.atomic
def add_service_request_comment(*, service_request, message, actor=None, actor_label="", guest_visible=False):
    """Add immutable discussion evidence after ownership/assignment checks."""
    request = ServiceRequest.objects.select_for_update().get(pk=service_request.pk)
    message = str(message or "").strip()
    if not message:
        raise ValidationError({"message": "A comment message is required."})
    if len(message) > 5000:
        raise ValidationError({"message": "Comment must be at most 5000 characters."})
    if actor and not _can_work(request, actor):
        raise PermissionDenied("Only the assigned worker or an authorized dispatcher can comment internally.")
    if request.status in _TERMINAL_STATUSES and actor is None:
        raise ValidationError("A closed or cancelled request cannot receive a guest comment.")
    return _create_event(
        request=request,
        event_type=ServiceRequestEvent.Type.COMMENT,
        actor=actor,
        actor_label=actor_label,
        message=message,
        guest_visible=guest_visible,
    )


@transaction.atomic
def cancel_service_request_by_guest(*, service_request, portal_email, note=""):
    """Allow a verified guest to cancel only a not-yet-worked request."""
    request = ServiceRequest.objects.select_for_update().select_related("guest").get(pk=service_request.pk)
    if request.guest.email.strip().lower() != str(portal_email or "").strip().lower():
        raise PermissionDenied("This request does not belong to the verified portal guest.")
    if request.status == ServiceRequest.Status.CANCELLED:
        return request
    if request.status not in {ServiceRequest.Status.OPEN, ServiceRequest.Status.ACKNOWLEDGED}:
        raise ValidationError("Only open or acknowledged requests can be cancelled by the guest.")
    previous_status = request.status
    request.status = ServiceRequest.Status.CANCELLED
    request.closed_at = timezone.now()
    request.save(update_fields=["status", "closed_at", "updated_at"])
    _create_event(
        request=request,
        event_type=ServiceRequestEvent.Type.CANCELLED,
        actor_label="Guest",
        message=note,
        guest_visible=True,
        previous_status=previous_status,
        new_status=ServiceRequest.Status.CANCELLED,
    )
    return request


def service_request_queryset_for_staff(actor):
    """Scope worker queues before serialization to avoid cross-team guest data."""
    from django.db.models import Q

    queryset = ServiceRequest.objects.all()
    if _can_dispatch(actor):
        return queryset
    team = _TEAM_FOR_ROLE.get(getattr(actor, "role", ""))
    if team:
        return queryset.filter(
            Q(created_by=actor)
            | (Q(owner_team=team) & (Q(assigned_to=actor) | Q(assigned_to__isnull=True)))
        )
    return queryset.filter(Q(assigned_to=actor) | Q(created_by=actor))
