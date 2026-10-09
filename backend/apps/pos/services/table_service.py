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
