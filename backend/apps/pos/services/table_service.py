"""Transactional restaurant-table registry and service-session workflow."""
from __future__ import annotations

import hashlib
import json

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.audit.services import log_action
from apps.core.exceptions import RestaurantTableSessionConflictError

from ..models import (
    PosOrder,
    RestaurantTable,
    RestaurantTableSession,
    RestaurantTableSessionEvent,
)


ACTIVE_ORDER_STATUSES = (
    PosOrder.Status.DRAFT,
    PosOrder.Status.SUBMITTED,
    PosOrder.Status.PREPARING,
    PosOrder.Status.READY,
)
# Statuses that represent real guest spending for a table session. Cancelled and
# voided orders are excluded so a table's displayed totals never claim spending
# that did not happen (or that was reversed).
COUNTED_ORDER_STATUSES = (
    PosOrder.Status.DRAFT,
    PosOrder.Status.SUBMITTED,
    PosOrder.Status.PREPARING,
    PosOrder.Status.READY,
    PosOrder.Status.DELIVERED,
)
UNSETTLED_STATUSES = (
    PosOrder.SettlementStatus.UNPAID,
    PosOrder.SettlementStatus.PARTIALLY_PAID,
)


def _fingerprint(*, table_id: int, covers: int, notes: str) -> str:
    payload = {"table_id": int(table_id), "covers": int(covers), "notes": (notes or "").strip()}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _same_key(existing: RestaurantTableSession, fingerprint: str) -> RestaurantTableSession:
    if existing.idempotency_fingerprint != fingerprint:
        raise ValidationError({"idempotency_key": "This key was already used for a different table-session request."})
    return existing


@transaction.atomic
def open_table_session(*, actor, table_id: int, covers: int, idempotency_key: str, notes: str = ""):
    """Open one table session exactly once, with a portable unique-active guard."""
    key = str(idempotency_key or "").strip()
    if not key:
        raise ValidationError({"idempotency_key": "An idempotency key is required."})
    fingerprint = _fingerprint(table_id=table_id, covers=covers, notes=notes)
    existing = RestaurantTableSession.objects.select_for_update().filter(idempotency_key=key).first()
    if existing:
        return _same_key(existing, fingerprint), False

    table = RestaurantTable.objects.select_for_update().filter(pk=table_id).first()
    if table is None:
        raise ValidationError({"table_id": "The configured restaurant table does not exist."})
    if not table.is_active:
        raise RestaurantTableSessionConflictError("An inactive table cannot be opened for service.")
    if not 1 <= int(covers) <= table.seats:
        raise ValidationError({"covers": f"Covers must be between one and this table's capacity of {table.seats}."})

    open_session = RestaurantTableSession.objects.select_for_update().filter(
        active_table_id=table.pk, status=RestaurantTableSession.Status.OPEN
    ).first()
    if open_session:
        raise RestaurantTableSessionConflictError(
            f"Table {table.code} already has open service session {open_session.reference}."
        )

    try:
        # Keep the unique-constraint failure inside a savepoint so the outer
        # transaction can return a typed conflict instead of masking a race.
        with transaction.atomic():
            session = RestaurantTableSession.objects.create(
                table=table,
                active_table=table,
                covers=int(covers),
                opened_by=actor,
                idempotency_key=key,
                idempotency_fingerprint=fingerprint,
                notes=(notes or "").strip()[:500],
            )
    except IntegrityError as exc:
        existing = RestaurantTableSession.objects.select_for_update().filter(idempotency_key=key).first()
        if existing:
            return _same_key(existing, fingerprint), False
        if RestaurantTableSession.objects.select_for_update().filter(
            active_table_id=table.pk, status=RestaurantTableSession.Status.OPEN
        ).exists():
            raise RestaurantTableSessionConflictError(
                f"Table {table.code} was opened in another request. Refresh the table list."
            ) from exc
        raise

    RestaurantTableSessionEvent.objects.create(
        table_session=session,
        type=RestaurantTableSessionEvent.Type.OPENED,
        actor=actor,
        details={"table_code": table.code, "covers": session.covers},
    )
    log_action(
        actor=actor,
        action="RESTAURANT_TABLE_SESSION_OPENED",
        instance=session,
        metadata={"reference": session.reference, "table_code": table.code, "covers": session.covers},
    )
    return session, True


def lock_open_table_session(reference: str) -> RestaurantTableSession:
    """Resolve and lock an active session within the caller's atomic order write."""
    session = (
        RestaurantTableSession.objects.select_for_update()
        .select_related("table")
        .filter(reference=reference)
        .first()
    )
    if session is None:
        raise ValidationError({"table_session_reference": "The restaurant table session was not found."})
    if (
        session.status != RestaurantTableSession.Status.OPEN
        or session.active_table_id != session.table_id
        or not session.table.is_active
    ):
        raise RestaurantTableSessionConflictError("This restaurant table session is no longer open for new orders.")
    return session


@transaction.atomic
def transfer_table_session(*, reference: str, destination_table_id: int, actor):
    """Move an open dining session without recreating orders or tickets.

    The destination must be active and unoccupied. Repeating the same request
    is a harmless no-op; merges are deliberately not implicit. Table rows are
    locked in stable order before session rows to keep opposing transfers from
    deadlocking each other.
    """
    observed = RestaurantTableSession.objects.filter(reference=reference).first()
    if observed is None:
        raise ValidationError({"reference": "Restaurant table session not found."})
    try:
        destination_id = int(destination_table_id)
    except (TypeError, ValueError) as exc:
        raise ValidationError({"destination_table_id": "Choose a valid destination table."}) from exc

    observed_source_id = observed.table_id
    if destination_id == observed_source_id:
        was_transferred_here = RestaurantTableSessionEvent.objects.filter(
            table_session=observed, type=RestaurantTableSessionEvent.Type.TRANSFERRED,
            details__to_table_id=destination_id,
        ).exists()
        if was_transferred_here:
            return observed, False
        raise ValidationError({"destination_table_id": "A table cannot be transferred to itself."})

    # Lock both table rows in stable order before locking the session. This
    # matches table opening's lock order and prevents opposite-direction
    # transfers from holding one session while waiting on each other's table.
    locked = {row.pk: row for row in RestaurantTable.objects.select_for_update().filter(
        pk__in=sorted({observed_source_id, destination_id})
    ).order_by("pk")}
    source = locked.get(observed_source_id)
    destination = locked.get(destination_id)
    if source is None:
        raise ValidationError({"reference": "The source table no longer exists."})
    if destination is None:
        raise ValidationError({"destination_table_id": "The destination table does not exist."})

    session = (
        RestaurantTableSession.objects.select_for_update()
        .select_related("table")
        .filter(reference=reference)
        .first()
    )
    if session is None:
        raise ValidationError({"reference": "Restaurant table session not found."})
    if session.table_id != observed_source_id:
        # If the earlier request committed to this same destination while this
        # call waited for its locks, treat this request as an idempotent retry.
        already_moved_here = (
            session.table_id == destination_id
            and RestaurantTableSessionEvent.objects.filter(
                table_session=session, type=RestaurantTableSessionEvent.Type.TRANSFERRED,
                details__to_table_id=destination_id,
            ).exists()
        )
        if already_moved_here:
            return session, False
        raise RestaurantTableSessionConflictError("The table session changed while this transfer was being prepared. Refresh and retry.")
    if session.status != RestaurantTableSession.Status.OPEN or session.active_table_id != session.table_id:
        raise RestaurantTableSessionConflictError("Only an open table session can be transferred.")
    if not source.is_active:
        raise RestaurantTableSessionConflictError("The source table is no longer active.")
    if not destination.is_active:
        raise RestaurantTableSessionConflictError("An inactive table cannot receive a service session.")
    if destination.seats < session.covers:
        raise ValidationError({"destination_table_id": "The destination table does not have enough seats for this session."})

    existing = RestaurantTableSession.objects.select_for_update().filter(
        active_table_id=destination.pk, status=RestaurantTableSession.Status.OPEN
    ).exclude(pk=session.pk).first()
    if existing:
        raise RestaurantTableSessionConflictError(
            f"Destination table {destination.code} is occupied. Close its session or use a separately authorized merge workflow."
        )

    try:
        # Keep the database's unique-active-table guard authoritative even
        # when two requests race on engines with weaker row-lock semantics.
        with transaction.atomic():
            order_count = PosOrder.objects.filter(table_session=session).count()
            # The event is written first in this same savepoint so the model's
            # persistence guard can prove a table_id change is authorized.
            RestaurantTableSessionEvent.objects.create(
                table_session=session,
                type=RestaurantTableSessionEvent.Type.TRANSFERRED,
                actor=actor,
                details={"from_table_id": source.pk, "from_table_code": source.code,
                         "to_table_id": destination.pk, "to_table_code": destination.code,
                         "order_count": order_count},
            )
            session.table = destination
            session.active_table = destination
            session.save(update_fields=["table", "active_table", "updated_at"])
            log_action(
                actor=actor,
                action="RESTAURANT_TABLE_SESSION_TRANSFERRED",
                instance=session,
                metadata={"reference": session.reference, "from_table_code": source.code,
                          "to_table_code": destination.code, "order_count": order_count},
            )
    except IntegrityError as exc:
        raise RestaurantTableSessionConflictError(
            "Another request occupied the destination table first. Refresh the table list."
        ) from exc
    return session, True


@transaction.atomic
def close_table_session(*, reference: str, actor, close_note: str = ""):
    """Close only after all attached orders are terminal and direct sales settled."""
    session = (
        RestaurantTableSession.objects.select_for_update()
        .select_related("table")
        .filter(reference=reference)
        .first()
    )
    if session is None:
        raise ValidationError({"reference": "Restaurant table session not found."})
    if session.status == RestaurantTableSession.Status.CLOSED:
        return session, False

    blocking_orders = PosOrder.objects.filter(table_session=session).filter(
        Q(status__in=ACTIVE_ORDER_STATUSES)
        | Q(status=PosOrder.Status.DELIVERED, settlement_status__in=UNSETTLED_STATUSES)
    )
    pending_count = blocking_orders.count()
    if pending_count:
        raise RestaurantTableSessionConflictError(
            f"Close blocked: {pending_count} order(s) are still in progress or have an unpaid balance. "
            "Finish, cancel, or settle every order first."
        )

    order_count = PosOrder.objects.filter(table_session=session).count()
    session.status = RestaurantTableSession.Status.CLOSED
    session.active_table = None
    session.closed_at = timezone.now()
    session.closed_by = actor
    session.close_note = (close_note or "").strip()[:500]
    session.save(update_fields=["status", "active_table", "closed_at", "closed_by", "close_note", "updated_at"])
    RestaurantTableSessionEvent.objects.create(
        table_session=session,
        type=RestaurantTableSessionEvent.Type.CLOSED,
        actor=actor,
        details={"table_code": session.table.code, "order_count": order_count},
    )
    log_action(
        actor=actor,
        action="RESTAURANT_TABLE_SESSION_CLOSED",
        instance=session,
        metadata={"reference": session.reference, "table_code": session.table.code, "order_count": order_count},
    )
    return session, True


def record_order_added(*, table_session: RestaurantTableSession, order: PosOrder, actor) -> None:
    RestaurantTableSessionEvent.objects.create(
        table_session=table_session,
        type=RestaurantTableSessionEvent.Type.ORDER_ADDED,
        order=order,
        actor=actor,
        details={"order_reference": order.reference, "amount": str(order.total_amount)},
    )
