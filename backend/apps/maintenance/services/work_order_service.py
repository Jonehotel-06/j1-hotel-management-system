"""Transactional maintenance workflow and explicit availability-impact controls."""
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

from ..models import MaintenanceEvent, MaintenanceWorkOrder, RoomOutage

_SLA = {
    MaintenanceWorkOrder.Priority.LOW: timedelta(hours=48),
    MaintenanceWorkOrder.Priority.NORMAL: timedelta(hours=12),
    MaintenanceWorkOrder.Priority.HIGH: timedelta(hours=2),
    MaintenanceWorkOrder.Priority.URGENT: timedelta(minutes=30),
}
_TERMINAL = {MaintenanceWorkOrder.Status.COMPLETED, MaintenanceWorkOrder.Status.CANCELLED}
_TRANSITIONS = {
    MaintenanceWorkOrder.Status.OPEN: {MaintenanceWorkOrder.Status.TRIAGED, MaintenanceWorkOrder.Status.CANCELLED},
    MaintenanceWorkOrder.Status.TRIAGED: {MaintenanceWorkOrder.Status.ASSIGNED, MaintenanceWorkOrder.Status.CANCELLED},
    MaintenanceWorkOrder.Status.ASSIGNED: {MaintenanceWorkOrder.Status.IN_PROGRESS, MaintenanceWorkOrder.Status.CANCELLED},
    MaintenanceWorkOrder.Status.IN_PROGRESS: {MaintenanceWorkOrder.Status.PENDING_PARTS, MaintenanceWorkOrder.Status.READY_FOR_VERIFICATION},
    MaintenanceWorkOrder.Status.PENDING_PARTS: {MaintenanceWorkOrder.Status.IN_PROGRESS, MaintenanceWorkOrder.Status.CANCELLED},
    MaintenanceWorkOrder.Status.READY_FOR_VERIFICATION: {MaintenanceWorkOrder.Status.COMPLETED, MaintenanceWorkOrder.Status.IN_PROGRESS},
}


def _reference():
    return f"MNT-{timezone.now():%Y%m%d}-{secrets.token_hex(6).upper()}"


def scoped_work_order_key(*, scope, raw_key):
    raw_key = str(raw_key or "").strip()
    if not raw_key:
        return None
    if len(raw_key) > 128:
        raise ValidationError({"idempotency_key": "Idempotency key must be at most 128 characters."})
    return "maintenance:" + hashlib.sha256(f"{scope}:{raw_key}".encode()).hexdigest()


def _event(*, work_order, event_type, actor=None, message="", previous_status="", new_status="", details=None):
    return MaintenanceEvent.objects.create(
        work_order=work_order, type=event_type, actor=actor, message=(message or "").strip(),
        previous_status=previous_status or "", new_status=new_status or "", details=dict(details or {}),
    )


def _is_dispatcher(actor):
    return bool(actor and has_capability(actor, "maintenance.work_order.assign"))


def _can_work(work_order, actor):
    return bool(actor and (_is_dispatcher(actor) or work_order.assigned_to_id == actor.pk))


def _validate_assignee(assignee):
    if assignee is None:
        return
    if not assignee.is_active or assignee.role != User.Role.MAINTENANCE:
        raise ValidationError("A maintenance work order must be assigned to an active maintenance user.")


def _validate_context(*, room=None, service_request=None):
    if service_request and service_request.room_id and room and service_request.room_id != room.pk:
        raise ValidationError("The work-order room must match its linked service request.")
    if service_request and service_request.category != "MAINTENANCE":
        raise ValidationError("Only a maintenance service request may be linked to a maintenance work order.")


@transaction.atomic
def create_work_order(
    *, category, summary, priority=MaintenanceWorkOrder.Priority.NORMAL, room=None,
    service_request=None, description="", due_at=None, assigned_to=None, actor=None, source_key=None,
):
    if category not in MaintenanceWorkOrder.Category.values:
        raise ValidationError({"category": "Invalid maintenance category."})
    if priority not in MaintenanceWorkOrder.Priority.values:
        raise ValidationError({"priority": "Invalid maintenance priority."})
    summary = str(summary or "").strip()
    description = str(description or "").strip()
    if not summary:
        raise ValidationError({"summary": "A work-order summary is required."})
    if len(summary) > 255 or len(description) > 5000:
        raise ValidationError("Work-order summary/description exceeds its permitted length.")
    _validate_context(room=room, service_request=service_request)
    _validate_assignee(assigned_to)
    if service_request:
        existing_link = MaintenanceWorkOrder.objects.select_for_update().filter(service_request=service_request).first()
        if existing_link:
            return existing_link, False
    source_key = str(source_key or "").strip() or None
    if source_key:
        existing = MaintenanceWorkOrder.objects.select_for_update().filter(source_key=source_key).first()
        if existing:
            return existing, False
    now = timezone.now()
    if due_at is not None and due_at <= now:
        raise ValidationError({"due_at": "Due time must be in the future."})
    initial_status = MaintenanceWorkOrder.Status.ASSIGNED if assigned_to else MaintenanceWorkOrder.Status.OPEN
    for _ in range(8):
        try:
            with transaction.atomic():
                work_order = MaintenanceWorkOrder.objects.create(
                    reference=_reference(), source_key=source_key, room=room, service_request=service_request,
                    category=category, priority=priority, status=initial_status, summary=summary,
                    description=description, due_at=due_at or now + _SLA[priority], reported_by=actor,
                    assigned_to=assigned_to,
                )
            break
        except IntegrityError:
            if source_key:
                existing = MaintenanceWorkOrder.objects.select_for_update().filter(source_key=source_key).first()
                if existing:
                    return existing, False
    else:
        raise ValidationError("Could not allocate a unique maintenance reference; please retry.")
    _event(work_order=work_order, event_type=MaintenanceEvent.Type.CREATED, actor=actor, new_status=work_order.status,
           details={"category": category, "priority": priority})
    if assigned_to:
        _event(work_order=work_order, event_type=MaintenanceEvent.Type.ASSIGNED, actor=actor,
               previous_status=MaintenanceWorkOrder.Status.OPEN, new_status=work_order.status,
               details={"assigned_to_id": assigned_to.pk, "assigned_to": assigned_to.full_name})
    return work_order, True


@transaction.atomic
def assign_work_order(*, work_order, actor, assignee=None, due_at=None, note=""):
    if not _is_dispatcher(actor):
        raise PermissionDenied("This user cannot dispatch maintenance work orders.")
    work_order = MaintenanceWorkOrder.objects.select_for_update().get(pk=work_order.pk)
    if work_order.status in _TERMINAL:
        raise ValidationError("A completed or cancelled work order cannot be reassigned.")
    _validate_assignee(assignee)
    if due_at is not None and due_at <= timezone.now():
        raise ValidationError({"due_at": "Due time must be in the future."})
    if assignee is None and work_order.status != MaintenanceWorkOrder.Status.OPEN:
        raise ValidationError("Only an unstarted open work order may be unassigned.")
    previous = work_order.status
    work_order.assigned_to = assignee
    if due_at is not None: work_order.due_at = due_at
    if assignee and work_order.status in {MaintenanceWorkOrder.Status.OPEN, MaintenanceWorkOrder.Status.TRIAGED}:
        work_order.status = MaintenanceWorkOrder.Status.ASSIGNED
    work_order.save(update_fields=["assigned_to", "due_at", "status", "updated_at"])
    _event(work_order=work_order, event_type=MaintenanceEvent.Type.ASSIGNED, actor=actor, message=note,
           previous_status=previous, new_status=work_order.status,
           details={"assigned_to_id": assignee.pk if assignee else None, "assigned_to": assignee.full_name if assignee else ""})
    return work_order


@transaction.atomic
def claim_work_order(*, work_order, actor):
    work_order = MaintenanceWorkOrder.objects.select_for_update().get(pk=work_order.pk)
    if work_order.status in _TERMINAL:
        raise ValidationError("A completed or cancelled work order cannot be claimed.")
    if work_order.assigned_to_id:
        if work_order.assigned_to_id == actor.pk: return work_order
        raise ValidationError("Work order is already assigned to another user.")
    if actor.role != User.Role.MAINTENANCE:
        raise PermissionDenied("Only a maintenance user may claim an unassigned work order.")
    _validate_assignee(actor)
    previous = work_order.status
    work_order.assigned_to = actor
    work_order.status = MaintenanceWorkOrder.Status.ASSIGNED
    work_order.save(update_fields=["assigned_to", "status", "updated_at"])
    _event(work_order=work_order, event_type=MaintenanceEvent.Type.CLAIMED, actor=actor,
           previous_status=previous, new_status=work_order.status, details={"assigned_to_id": actor.pk})
    return work_order


@transaction.atomic
def transition_work_order(*, work_order, target_status, actor, note=""):
    work_order = MaintenanceWorkOrder.objects.select_for_update().get(pk=work_order.pk)
    target_status = str(target_status or "").upper()
    if target_status not in MaintenanceWorkOrder.Status.values:
        raise ValidationError({"status": "Invalid maintenance status."})
    if target_status == work_order.status: return work_order
    if target_status not in _TRANSITIONS.get(work_order.status, set()):
        raise ValidationError(f"Work order {work_order.reference} cannot transition from {work_order.status} to {target_status}.")
    if not _can_work(work_order, actor):
        raise PermissionDenied("Only the assigned technician or dispatcher can progress this work order.")
    if target_status in {MaintenanceWorkOrder.Status.TRIAGED, MaintenanceWorkOrder.Status.COMPLETED} and not _is_dispatcher(actor):
        raise PermissionDenied("A dispatcher must triage or verify completion of a maintenance work order.")
    if target_status == MaintenanceWorkOrder.Status.COMPLETED and hasattr(work_order, "room_outage") and work_order.room_outage.status == RoomOutage.Status.ACTIVE:
        raise ValidationError("Clear the active room outage before completing this work order.")
    previous = work_order.status; now = timezone.now(); work_order.status = target_status
    fields = ["status", "updated_at"]
    event_type = MaintenanceEvent.Type.STATUS_CHANGED
    if target_status == MaintenanceWorkOrder.Status.TRIAGED:
        work_order.triaged_at = now; work_order.triaged_by = actor; fields += ["triaged_at", "triaged_by"]; event_type = MaintenanceEvent.Type.TRIAGED
    elif target_status == MaintenanceWorkOrder.Status.COMPLETED:
        work_order.completed_at = now; work_order.completed_by = actor; fields += ["completed_at", "completed_by"]; event_type = MaintenanceEvent.Type.VERIFIED
    work_order.save(update_fields=fields)
    _event(work_order=work_order, event_type=event_type, actor=actor, message=note, previous_status=previous, new_status=target_status)
    return work_order


@transaction.atomic
def start_room_outage(*, work_order, actor, outage_status=Room.Status.MAINTENANCE, reason=""):
    if not _is_dispatcher(actor):
        raise PermissionDenied("This user cannot place a room out of service.")
    work_order = MaintenanceWorkOrder.objects.select_for_update().select_related("room").get(pk=work_order.pk)
    if work_order.room_id is None:
        raise ValidationError("A room-specific work order is required before an outage can start.")
    if work_order.status in _TERMINAL:
        raise ValidationError("A completed or cancelled work order cannot create an outage.")
    if outage_status not in {Room.Status.MAINTENANCE, Room.Status.OUT_OF_SERVICE}:
        raise ValidationError({"outage_status": "Outage status must be MAINTENANCE or OUT_OF_SERVICE."})
    existing = RoomOutage.objects.select_for_update().filter(work_order=work_order).first()
    if existing:
        if existing.status == RoomOutage.Status.ACTIVE:
            return existing
        raise ValidationError("Create a new work order for a new outage after this outage was cleared.")
    room = Room.objects.select_for_update().get(pk=work_order.room_id)
    if room.status == Room.Status.OCCUPIED:
        raise ValidationError("An occupied room cannot be taken out of service; relocate the guest first.")
    outage = RoomOutage.objects.create(
        room=room, work_order=work_order, outage_status=outage_status, reason=(reason or "")[:500], created_by=actor,
    )
    room.status = outage_status
    room.save(update_fields=["status", "updated_at"])
    _event(work_order=work_order, event_type=MaintenanceEvent.Type.OUTAGE_STARTED, actor=actor, message=reason,
           details={"room_number": room.room_number, "outage_status": outage_status, "outage_id": outage.pk})
    return outage


@transaction.atomic
def clear_room_outage(*, outage, actor, note=""):
    if not _is_dispatcher(actor):
        raise PermissionDenied("This user cannot clear a room outage.")
    outage = RoomOutage.objects.select_for_update().select_related("work_order", "room").get(pk=outage.pk)
    if outage.status == RoomOutage.Status.CLEARED:
        return outage
    room = Room.objects.select_for_update().get(pk=outage.room_id)
    outage.status = RoomOutage.Status.CLEARED; outage.cleared_at = timezone.now(); outage.cleared_by = actor
    outage.save(update_fields=["status", "cleared_at", "cleared_by", "updated_at"])
    other_active = RoomOutage.objects.filter(room=room, status=RoomOutage.Status.ACTIVE).exclude(pk=outage.pk).exists()
    if not other_active and room.status in {Room.Status.MAINTENANCE, Room.Status.OUT_OF_SERVICE}:
        room.status = Room.Status.AVAILABLE
        room.save(update_fields=["status", "updated_at"])
    _event(work_order=outage.work_order, event_type=MaintenanceEvent.Type.OUTAGE_CLEARED, actor=actor, message=note,
           details={"outage_id": outage.pk, "room_number": room.room_number})
    return outage


@transaction.atomic
def add_maintenance_comment(*, work_order, actor, message):
    work_order = MaintenanceWorkOrder.objects.select_for_update().get(pk=work_order.pk)
    if not _can_work(work_order, actor):
        raise PermissionDenied("Only the assigned technician or dispatcher can comment on this work order.")
    message = str(message or "").strip()
    if not message: raise ValidationError({"message": "A comment is required."})
    if len(message) > 5000: raise ValidationError({"message": "Comment must be at most 5000 characters."})
    return _event(work_order=work_order, event_type=MaintenanceEvent.Type.COMMENT, actor=actor, message=message)


def maintenance_queryset_for_user(actor):
    if _is_dispatcher(actor): return MaintenanceWorkOrder.objects.all()
    if getattr(actor, "role", "") == User.Role.MAINTENANCE:
        from django.db.models import Q
        return MaintenanceWorkOrder.objects.filter(Q(assigned_to=actor) | Q(assigned_to__isnull=True))
    return MaintenanceWorkOrder.objects.none()
