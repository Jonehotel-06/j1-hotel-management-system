"""Transactional housekeeping task workflow and controlled room-clean projection."""
from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.capabilities import has_capability
from apps.accounts.models import User
from apps.rooms.models import Room
from apps.stays.models import StayRoom

from ..models import HousekeepingTask, HousekeepingTaskEvent

_SLA = {
    HousekeepingTask.Priority.LOW: timedelta(hours=24),
    HousekeepingTask.Priority.NORMAL: timedelta(hours=8),
    HousekeepingTask.Priority.HIGH: timedelta(hours=2),
    HousekeepingTask.Priority.URGENT: timedelta(minutes=30),
}
_TERMINAL = {HousekeepingTask.Status.COMPLETED, HousekeepingTask.Status.CANCELLED}
_TRANSITIONS = {
    HousekeepingTask.Status.OPEN: {HousekeepingTask.Status.ASSIGNED, HousekeepingTask.Status.CANCELLED},
    HousekeepingTask.Status.ASSIGNED: {HousekeepingTask.Status.ACCEPTED, HousekeepingTask.Status.CANCELLED},
    HousekeepingTask.Status.ACCEPTED: {HousekeepingTask.Status.IN_PROGRESS, HousekeepingTask.Status.BLOCKED, HousekeepingTask.Status.CANCELLED},
    HousekeepingTask.Status.IN_PROGRESS: {HousekeepingTask.Status.READY_FOR_INSPECTION, HousekeepingTask.Status.BLOCKED},
    HousekeepingTask.Status.BLOCKED: {HousekeepingTask.Status.IN_PROGRESS, HousekeepingTask.Status.CANCELLED},
    HousekeepingTask.Status.READY_FOR_INSPECTION: {HousekeepingTask.Status.COMPLETED, HousekeepingTask.Status.IN_PROGRESS},
}


def _reference():
    return f"HKT-{timezone.now():%Y%m%d}-{secrets.token_hex(6).upper()}"


def scoped_task_key(*, scope, raw_key):
    raw_key = str(raw_key or "").strip()
    if not raw_key:
        return None
    if len(raw_key) > 128:
        raise ValidationError({"idempotency_key": "Idempotency key must be at most 128 characters."})
    return "housekeeping:" + hashlib.sha256(f"{scope}:{raw_key}".encode()).hexdigest()


def _event(*, task, event_type, actor=None, message="", previous_status="", new_status="", details=None):
    return HousekeepingTaskEvent.objects.create(
        task=task, type=event_type, actor=actor, message=(message or "").strip(),
        previous_status=previous_status or "", new_status=new_status or "", details=dict(details or {}),
    )


def _is_dispatcher(actor):
    return bool(actor and has_capability(actor, "housekeeping.task.assign"))


def _can_work(task, actor):
    return bool(actor and (_is_dispatcher(actor) or task.assigned_to_id == actor.pk))


def _validate_assignee(assignee):
    if assignee is None:
        return
    if not assignee.is_active or assignee.role != User.Role.HOUSEKEEPING:
        raise ValidationError("A housekeeping task must be assigned to an active housekeeping user.")


def _validate_context(*, room, stay=None, service_request=None):
    if stay and not StayRoom.objects.filter(stay=stay, room=room).exists():
        raise ValidationError("The room is not assigned to the supplied stay.")
    if service_request:
        if service_request.room_id and service_request.room_id != room.pk:
            raise ValidationError("The housekeeping task room must match its linked service request.")
        if service_request.stay_id and stay and service_request.stay_id != stay.pk:
            raise ValidationError("The housekeeping task stay must match its linked service request.")


def _set_room_cleaning_projection(*, task, target_status):
    """Only workflow transitions update the operational clean/dirty projection."""
    room = Room.objects.select_for_update().get(pk=task.room_id)
    desired = None
    if target_status == HousekeepingTask.Status.IN_PROGRESS:
        desired = Room.HousekeepingStatus.CLEANING
    elif target_status == HousekeepingTask.Status.COMPLETED:
        desired = Room.HousekeepingStatus.CLEAN
    if desired and room.housekeeping_status != desired:
        room.housekeeping_status = desired
        room.save(update_fields=["housekeeping_status", "updated_at"])


@transaction.atomic
def create_housekeeping_task(
    *, room, task_type, summary, priority=HousekeepingTask.Priority.NORMAL, stay=None,
    service_request=None, detail="", due_at=None, assigned_to=None, actor=None, source_key=None,
):
    if task_type not in HousekeepingTask.Type.values:
        raise ValidationError({"type": "Invalid housekeeping task type."})
    if priority not in HousekeepingTask.Priority.values:
        raise ValidationError({"priority": "Invalid housekeeping priority."})
    summary = str(summary or "").strip()
    if not summary:
        raise ValidationError({"summary": "A task summary is required."})
    if len(summary) > 255 or len(str(detail or "")) > 5000:
        raise ValidationError("Task summary/detail exceeds its permitted length.")
    _validate_context(room=room, stay=stay, service_request=service_request)
    _validate_assignee(assigned_to)
    if service_request:
        existing_link = HousekeepingTask.objects.select_for_update().filter(service_request=service_request).first()
        if existing_link:
            return existing_link, False
    source_key = str(source_key or "").strip() or None
    if source_key:
        existing = HousekeepingTask.objects.select_for_update().filter(source_key=source_key).first()
        if existing:
            return existing, False
    now = timezone.now()
    if due_at is not None and due_at <= now:
        raise ValidationError({"due_at": "Due time must be in the future."})
    for _ in range(8):
        try:
            with transaction.atomic():
                task = HousekeepingTask.objects.create(
                    reference=_reference(), source_key=source_key, room=room, stay=stay,
                    service_request=service_request, type=task_type, priority=priority,
                    status=HousekeepingTask.Status.ASSIGNED if assigned_to else HousekeepingTask.Status.OPEN,
                    summary=summary, detail=str(detail or "").strip(), due_at=due_at or now + _SLA[priority],
                    assigned_to=assigned_to, created_by=actor,
                )
            break
        except IntegrityError:
            if source_key:
                existing = HousekeepingTask.objects.select_for_update().filter(source_key=source_key).first()
                if existing:
                    return existing, False
    else:
        raise ValidationError("Could not allocate a unique housekeeping task reference; please retry.")
    _event(task=task, event_type=HousekeepingTaskEvent.Type.CREATED, actor=actor, new_status=task.status,
           details={"type": task.type, "priority": task.priority})
    if assigned_to:
        _event(task=task, event_type=HousekeepingTaskEvent.Type.ASSIGNED, actor=actor,
               previous_status=HousekeepingTask.Status.OPEN, new_status=task.status,
               details={"assigned_to_id": assigned_to.pk, "assigned_to": assigned_to.full_name})
    return task, True


@transaction.atomic
def ensure_checkout_cleaning_task(*, stay, room, actor=None):
    """Create the idempotent checkout-clean task after a real room release."""
    source_key = f"checkout-clean:{stay.reference}:{room.pk}"
    return create_housekeeping_task(
        room=room, stay=stay, task_type=HousekeepingTask.Type.CHECKOUT_CLEAN,
        priority=HousekeepingTask.Priority.HIGH,
        summary=f"Checkout clean — Room {room.room_number}",
        detail=f"Room released from stay {stay.reference}.", actor=actor, source_key=source_key,
    )


@transaction.atomic
def assign_housekeeping_task(*, task, actor, assignee=None, due_at=None, note=""):
    if not _is_dispatcher(actor):
        raise PermissionDenied("This user cannot dispatch housekeeping tasks.")
    task = HousekeepingTask.objects.select_for_update().get(pk=task.pk)
    if task.status in _TERMINAL:
        raise ValidationError("A completed or cancelled task cannot be reassigned.")
    _validate_assignee(assignee)
    if due_at is not None and due_at <= timezone.now():
        raise ValidationError({"due_at": "Due time must be in the future."})
    if assignee is None and task.status != HousekeepingTask.Status.OPEN:
        raise ValidationError("Only an unstarted open task may be unassigned.")
    previous = task.status
    task.assigned_to = assignee
    if due_at is not None:
        task.due_at = due_at
    if assignee and task.status == HousekeepingTask.Status.OPEN:
        task.status = HousekeepingTask.Status.ASSIGNED
    task.save(update_fields=["assigned_to", "due_at", "status", "updated_at"])
    _event(task=task, event_type=HousekeepingTaskEvent.Type.ASSIGNED, actor=actor, message=note,
           previous_status=previous, new_status=task.status,
           details={"assigned_to_id": assignee.pk if assignee else None, "assigned_to": assignee.full_name if assignee else ""})
    return task


@transaction.atomic
def claim_housekeeping_task(*, task, actor):
    task = HousekeepingTask.objects.select_for_update().get(pk=task.pk)
    if task.status in _TERMINAL:
        raise ValidationError("A completed or cancelled task cannot be claimed.")
    if task.assigned_to_id:
        if task.assigned_to_id == actor.pk:
            return task
        raise ValidationError("Task is already assigned to another user.")
    if actor.role != User.Role.HOUSEKEEPING and not _is_dispatcher(actor):
        raise PermissionDenied("Only a housekeeping user may claim an unassigned task.")
    _validate_assignee(actor if actor.role == User.Role.HOUSEKEEPING else None)
    if actor.role != User.Role.HOUSEKEEPING:
        raise ValidationError("A dispatcher must assign the task to an active housekeeping user.")
    previous = task.status
    task.assigned_to = actor
    if task.status == HousekeepingTask.Status.OPEN:
        task.status = HousekeepingTask.Status.ASSIGNED
    task.save(update_fields=["assigned_to", "status", "updated_at"])
    _event(task=task, event_type=HousekeepingTaskEvent.Type.CLAIMED, actor=actor,
           previous_status=previous, new_status=task.status, details={"assigned_to_id": actor.pk})
    return task


@transaction.atomic
def transition_housekeeping_task(*, task, target_status, actor, note=""):
    task = HousekeepingTask.objects.select_for_update().get(pk=task.pk)
    target_status = str(target_status or "").upper()
    if target_status not in HousekeepingTask.Status.values:
        raise ValidationError({"status": "Invalid housekeeping task status."})
    if target_status == task.status:
        return task
    if target_status not in _TRANSITIONS.get(task.status, set()):
        raise ValidationError(f"Task {task.reference} cannot transition from {task.status} to {target_status}.")
    if not _can_work(task, actor):
        raise PermissionDenied("Only the assigned housekeeper or dispatcher can progress this task.")
    if target_status == HousekeepingTask.Status.COMPLETED and not _is_dispatcher(actor):
        raise PermissionDenied("A dispatcher must inspect and complete a housekeeping task.")
    previous = task.status
    now = timezone.now()
    task.status = target_status
    fields = ["status", "updated_at"]
    if target_status == HousekeepingTask.Status.ACCEPTED:
        task.accepted_at = now; fields.append("accepted_at")
    elif target_status == HousekeepingTask.Status.IN_PROGRESS:
        task.started_at = now; fields.append("started_at")
    elif target_status == HousekeepingTask.Status.READY_FOR_INSPECTION:
        task.ready_for_inspection_at = now; fields.append("ready_for_inspection_at")
    elif target_status == HousekeepingTask.Status.COMPLETED:
        task.completed_at = now; task.inspected_at = now; task.inspected_by = actor
        fields += ["completed_at", "inspected_at", "inspected_by"]
    task.save(update_fields=fields)
    _set_room_cleaning_projection(task=task, target_status=target_status)
    _event(task=task,
           event_type=HousekeepingTaskEvent.Type.INSPECTED if target_status == HousekeepingTask.Status.COMPLETED else HousekeepingTaskEvent.Type.STATUS_CHANGED,
           actor=actor, message=note, previous_status=previous, new_status=target_status)
    return task


@transaction.atomic
def add_housekeeping_comment(*, task, actor, message):
    task = HousekeepingTask.objects.select_for_update().get(pk=task.pk)
    if not _can_work(task, actor):
        raise PermissionDenied("Only the assigned housekeeper or dispatcher can comment on this task.")
    message = str(message or "").strip()
    if not message:
        raise ValidationError({"message": "A comment is required."})
    if len(message) > 5000:
        raise ValidationError({"message": "Comment must be at most 5000 characters."})
    return _event(task=task, event_type=HousekeepingTaskEvent.Type.COMMENT, actor=actor, message=message)


def housekeeping_queryset_for_user(actor):
    if _is_dispatcher(actor):
        return HousekeepingTask.objects.all()
    if getattr(actor, "role", "") == User.Role.HOUSEKEEPING:
        from django.db.models import Q
        return HousekeepingTask.objects.filter(Q(assigned_to=actor) | Q(assigned_to__isnull=True))
    return HousekeepingTask.objects.none()
