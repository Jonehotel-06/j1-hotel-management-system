"""Structured, drillable reconciliation checks for the Accounting night audit.

The Accounting screen's "N unresolved room or billing checks" line used to be
the length of one undifferentiated ``discrepancies`` list built inside
``night_audit_service.daily_reconciliation``. Three things were wrong with
that list as a worklist:

1. it mixed cash-drawer checks (``CASH_VARIANCE``, ``DRAWER_STILL_OPEN``) in
   with room and billing checks, so the sentence named the wrong subject and
   the number could not be trusted to mean what it said;
2. it was rendered as a bullet list of ``reference · detail``, so an accountant
   could see that something was flagged but not the record, the amount, the
   current status, why it was flagged, or what to do next;
3. it had no notion of a decision: a check that an accountant had already
   investigated, and a check whose underlying condition had since cleared,
   looked exactly like a fresh unexplained discrepancy.

This module is the one place those checks are computed. It returns one record
per condition with a stable ``check_id``, the object it belongs to (room,
folio, stay, drawer, purchase order), the amount where one exists, the current
derived status, an explicit "why it is flagged" sentence, and the recommended
next action. Review decisions live in ``ReconciliationCheckReview`` (append
only) and are joined onto the live conditions here, so the count always
reflects the underlying records:

* the condition still holds and nobody has reviewed it  -> ``OPEN``
* the condition still holds but was acknowledged         -> ``ACKNOWLEDGED``
* the condition still holds but was dismissed with a
  reason (auditor said "not a discrepancy")             -> ``DISMISSED``
* a review exists but the condition has since cleared    -> ``RESOLVED``

Nothing in this module posts, mutates or revalues a financial record. A review
row is documentation attached to a check; it never edits the ledger, the folio,
the drawer or the payment it is about.
"""
from __future__ import annotations

import hashlib
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q, Sum

from apps.accounts.capabilities import has_capability
from apps.core.utils import hotel_date_bounds, hotel_today, money

from ..models import (
    CashMovement,
    CashSession,
    Folio,
    FolioPosting,
    ReconciliationCheckReview,
)

CENT = Decimal("0.01")
ZERO = Decimal("0.00")

CATEGORY_ROOM = "ROOM"
CATEGORY_BILLING = "BILLING"
CATEGORY_CASH = "CASH_DRAWER"
CATEGORY_PROCUREMENT = "PROCUREMENT"

SEVERITY_ERROR = "ERROR"
SEVERITY_WARNING = "WARNING"
SEVERITY_INFO = "INFO"

STATUS_OPEN = "OPEN"
STATUS_ACKNOWLEDGED = "ACKNOWLEDGED"
STATUS_DISMISSED = "DISMISSED"
STATUS_RESOLVED = "RESOLVED"

DECISION_ACKNOWLEDGE = "ACKNOWLEDGED"
DECISION_DISMISS = "DISMISSED"
DECISION_ESCALATE = "ESCALATED"
REVIEW_DECISIONS = (DECISION_ACKNOWLEDGE, DECISION_DISMISS, DECISION_ESCALATE)

#: Subject of each check type, so the UI never has to invent a column heading
#: and the API contract stays stable.
CHECK_TYPES = {
    "CASH_VARIANCE": {
        "category": CATEGORY_CASH, "severity": SEVERITY_ERROR,
        "label": "Cash drawer variance",
        "subject": "Cash drawer",
    },
    "DRAWER_STILL_OPEN": {
        "category": CATEGORY_CASH, "severity": SEVERITY_ERROR,
        "label": "Cash drawer still open",
        "subject": "Cash drawer",
    },
    "ROOM_OCCUPIED_NOT_IN_HOUSE": {
        "category": CATEGORY_ROOM, "severity": SEVERITY_ERROR,
        "label": "Room held by a checked-in booking with no in-house stay",
        "subject": "Room",
    },
    "ROOM_ARRIVAL_NOT_CHECKED_IN": {
        "category": CATEGORY_ROOM, "severity": SEVERITY_INFO,
        "label": "Arrival still pending (room held, guest not checked in)",
        "subject": "Room",
    },
    "STAY_WITHOUT_ROOM": {
        "category": CATEGORY_ROOM, "severity": SEVERITY_ERROR,
        "label": "In-house stay with no room allocated",
        "subject": "Stay",
    },
    "IN_HOUSE_NO_FOLIO": {
        "category": CATEGORY_BILLING, "severity": SEVERITY_ERROR,
        "label": "In-house stay with no folio",
        "subject": "Stay",
    },
    "FOLIO_ON_HOLD": {
        "category": CATEGORY_BILLING, "severity": SEVERITY_WARNING,
        "label": "Folio on hold while the guest is in house",
        "subject": "Folio",
    },
    "FOLIO_MULTIPLE_OPEN": {
        "category": CATEGORY_BILLING, "severity": SEVERITY_WARNING,
        "label": "More than one open folio on one stay",
        "subject": "Folio",
    },
    "FOLIO_BALANCE_DUE": {
        "category": CATEGORY_BILLING, "severity": SEVERITY_WARNING,
        "label": "In-house folio with a balance to settle",
        "subject": "Folio",
    },
    "PO_RECEIVED_NOT_BILLED": {
        "category": CATEGORY_PROCUREMENT, "severity": SEVERITY_WARNING,
        "label": "Goods received with no supplier payable raised",
        "subject": "Purchase order",
    },
    "PO_PAYABLE_EXCEEDS_RECEIVED": {
        "category": CATEGORY_PROCUREMENT, "severity": SEVERITY_ERROR,
        "label": "Supplier payable larger than the value received",
        "subject": "Purchase order",
    },
}


def _dec(value) -> Decimal:
    return Decimal(value or 0).quantize(CENT)


def make_check_id(*, business_date, check_type: str, object_reference: str) -> str:
    """Stable identifier for one condition on one business date.

    Deliberately derived from (date, type, object) and nothing else: the same
    unresolved drawer on the same night keeps the same id across requests, which
    is what lets an acknowledgement stick to the record it was written about.
    """
    raw = f"{business_date.isoformat()}|{check_type}|{object_reference}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def _check(
    *, business_date, check_type, object_reference, amount=None, room="", folio="",
    stay="", booking="", reference="", status="", why="", action="", extra=None,
):
    meta = CHECK_TYPES[check_type]
    return {
        "check_id": make_check_id(business_date=business_date, check_type=check_type, object_reference=object_reference),
        "business_date": business_date.isoformat(),
        "category": meta["category"],
        "category_label": {
            CATEGORY_ROOM: "Room", CATEGORY_BILLING: "Billing",
            CATEGORY_CASH: "Cash drawer", CATEGORY_PROCUREMENT: "Procurement / payables",
        }[meta["category"]],
        "type": check_type,
        "type_label": meta["label"],
        "subject": meta["subject"],
        "severity": meta["severity"],
        "object_reference": object_reference,
        "room": room or "",
        "folio": folio or "",
        "stay": stay or "",
        "booking": booking or "",
        "reference": reference or object_reference,
        "amount": money(amount) if amount is not None else None,
        "currency": "NGN",
        "record_status": status or "",
        "why_flagged": why,
        "recommended_action": action,
        "details": dict(extra or {}),
        "status": STATUS_OPEN,
        "reviewed_by": "",
        "reviewed_at": None,
        "review_note": "",
        "review_decision": "",
    }


# --------------------------------------------------------------------------
# Cash drawer checks
# --------------------------------------------------------------------------
def _cash_checks(business_date):
    checks = []
    sessions = (
        CashSession.objects.filter(business_date=business_date)
        .select_related("cashier")
        .order_by("opened_at", "pk")
    )
    drops = {
        row["cash_session_id"]: _dec(row["total"])
        for row in CashMovement.objects.filter(
            cash_session__in=sessions, type=CashMovement.Type.SAFE_DROP
        ).values("cash_session_id").annotate(total=Sum("amount"))
    }
    drawer_rows = []
    today = hotel_today()
    for session in sessions:
        counted = _dec(session.counted_cash) if session.counted_cash is not None else None
        variance = _dec(session.variance) if session.variance is not None else None
        cashier = (session.cashier.full_name or session.cashier.email) if session.cashier_id else ""
        drawer_rows.append({
            "reference": session.reference,
            "cashier": cashier,
            "location": session.location,
            "status": session.status,
            "opening_float": money(session.opening_float),
            "safe_drops": money(drops.get(session.pk, ZERO)),
            "expected_cash": money(session.expected_cash),
            "counted_cash": money(counted) if counted is not None else None,
            "variance": money(variance) if variance is not None else None,
        })
        if variance is not None and variance != ZERO:
            direction = "more" if variance > ZERO else "less"
            checks.append(_check(
                business_date=business_date, check_type="CASH_VARIANCE",
                object_reference=session.reference, amount=variance,
                reference=session.reference, status=session.status,
                why=(
                    f"Drawer counted {money(counted)} against expected {money(session.expected_cash)}: "
                    f"{money(abs(variance))} {direction} cash than the recorded tenders and pay-outs account for."
                ),
                action=(
                    "Investigate the till slip trail, then close the drawer through the controlled "
                    "cash-variance review in Cash management. Do not adjust the count here."
                ),
                extra={"cashier": cashier, "expected_cash": money(session.expected_cash),
                       "counted_cash": money(counted)},
            ))
        if session.status == CashSession.Status.OPEN and session.business_date < today:
            checks.append(_check(
                business_date=business_date, check_type="DRAWER_STILL_OPEN",
                object_reference=session.reference,
                reference=session.reference, status=session.status,
                why="The cash session for this past business date was never closed, so its cash is still unverified.",
                action="Ask the cashier to count and close the drawer; the variance review follows the close.",
                extra={"cashier": cashier},
            ))
    return checks, drawer_rows


# --------------------------------------------------------------------------
# Room and billing checks
# --------------------------------------------------------------------------
def _stay_room(stay):
    for occupancy in stay.stay_rooms.all():
        if occupancy.released_at is None:
            return occupancy.room
    return None


def _room_and_billing_checks(business_date):
    from apps.bookings.models import Booking, BookingRoom
    from apps.bookings.services.availability import blocking_booking_q
    from apps.rooms.models import Room
    from apps.stays.models import Stay

    checks = []
    occupied = {
        row["room_id"]: row["booking_id"]
        for row in BookingRoom.objects.filter(
            booking__check_in__lte=business_date, booking__check_out__gt=business_date
        ).filter(blocking_booking_q(prefix="booking")).values("room_id", "booking_id")
    }
    booking_status = {
        row["id"]: row["status"]
        for row in Booking.objects.filter(pk__in=set(occupied.values())).values("id", "status")
    }

    in_house = list(
        Stay.objects.filter(status=Stay.Status.IN_HOUSE)
        .select_related("booking", "guest")
        .prefetch_related("stay_rooms__room")
    )
    in_house_room_ids = set()
    for stay in in_house:
        room = _stay_room(stay)
        if room is None:
            checks.append(_check(
                business_date=business_date, check_type="STAY_WITHOUT_ROOM",
                object_reference=stay.reference,
                stay=stay.reference,
                booking=stay.booking.booking_reference if stay.booking_id else "",
                status=stay.status,
                why=(
                    "The stay is IN_HOUSE but no unreleased room allocation exists, so tonight's "
                    "accommodation revenue cannot be matched to a physical room."
                ),
                action="Front desk re-allocates the room on the stay (a room move is recorded, never overwritten).",
            ))
            continue
        in_house_room_ids.add(room.pk)

    # A room held tonight by a booking whose guest is already checked in but has
    # no in-house stay is the genuine discrepancy. A booking that is merely
    # CONFIRMED and due to arrive today is NOT: the guest simply has not reached
    # the desk yet, and flagging it produced "unresolved" checks every afternoon.
    for room_id in sorted(set(occupied) - in_house_room_ids):
        booking_id = occupied[room_id]
        status = booking_status.get(booking_id, "")
        room = Room.objects.filter(pk=room_id).first()
        room_number = getattr(room, "room_number", str(room_id))
        booking_ref = (
            Booking.objects.filter(pk=booking_id).values_list("booking_reference", flat=True).first() or ""
        )
        if status == Booking.Status.CHECKED_IN:
            checks.append(_check(
                business_date=business_date, check_type="ROOM_OCCUPIED_NOT_IN_HOUSE",
                object_reference=room_number, room=room_number, booking=booking_ref, status=status,
                why=(
                    "The booking is CHECKED_IN and holds this room tonight, but no in-house stay "
                    "record exists, so nobody is being billed for the room."
                ),
                action=(
                    "Front desk checks the stay in (or corrects the booking status if the guest never "
                    "arrived). No financial figure is changed from Accounting."
                ),
            ))
        else:
            checks.append(_check(
                business_date=business_date, check_type="ROOM_ARRIVAL_NOT_CHECKED_IN",
                object_reference=room_number, room=room_number, booking=booking_ref, status=status,
                why=(
                    "A blocking booking holds this room for tonight and the guest has not been checked "
                    "in yet. This is the normal state before arrival, not a discrepancy."
                ),
                action="No action required. It becomes a room check only if the arrival is missed or the booking is left open.",
            ))

    # ---- folios -----------------------------------------------------------
    in_house_ids = [stay.pk for stay in in_house]
    live_folios = list(
        Folio.objects.filter(
            stay__in=in_house_ids,
            status__in=[Folio.Status.OPEN, Folio.Status.ON_HOLD],
        ).values("id", "reference", "stay_id", "status")
    ) if in_house_ids else []
    folios_by_stay = {}
    for row in live_folios:
        folios_by_stay.setdefault(row["stay_id"], []).append(row)

    balances = {}
    if live_folios:
        balances = {
            row["folio_id"]: (Decimal(row["debits"] or 0) - Decimal(row["credits"] or 0)).quantize(CENT)
            for row in FolioPosting.objects.filter(folio_id__in=[row["id"] for row in live_folios])
            .values("folio_id")
            .annotate(
                debits=Sum("amount", filter=Q(effect=FolioPosting.Effect.DEBIT)),
                credits=Sum("amount", filter=Q(effect=FolioPosting.Effect.CREDIT)),
            )
        }

    unbilled = []
    for stay in in_house:
        folios = folios_by_stay.get(stay.pk, [])
        guest = stay.guest.full_name if stay.guest_id else ""
        room = _stay_room(stay)
        room_number = room.room_number if room is not None else ""
        if not folios:
            checks.append(_check(
                business_date=business_date, check_type="IN_HOUSE_NO_FOLIO",
                object_reference=stay.reference, stay=stay.reference, room=room_number,
                booking=stay.booking.booking_reference if stay.booking_id else "",
                status=stay.status,
                why=(
                    "The stay is in house but has no OPEN or ON_HOLD folio, so charges have nowhere "
                    "to be posted and tonight's revenue cannot be billed."
                ),
                action="Open the main folio for the stay through the normal check-in/folio route.",
            ))
            continue
        if len(folios) > 1:
            checks.append(_check(
                business_date=business_date, check_type="FOLIO_MULTIPLE_OPEN",
                object_reference=stay.reference, stay=stay.reference, room=room_number,
                status="; ".join(sorted(row["status"] for row in folios)),
                why=(
                    "This stay carries " + str(len(folios)) + " live folios "
                    "(" + ", ".join(sorted(row["reference"] for row in folios)) + "), so a balance "
                    "read from one of them understates or overstates what the guest owes."
                ),
                action="Accounts manager consolidates the intended main folio and closes the incidental one.",
                extra={"folios": [row["reference"] for row in folios]},
            ))
        on_hold = [row for row in folios if row["status"] == Folio.Status.ON_HOLD]
        if on_hold and not any(row["status"] == Folio.Status.OPEN for row in folios):
            checks.append(_check(
                business_date=business_date, check_type="FOLIO_ON_HOLD",
                object_reference=on_hold[0]["reference"], stay=stay.reference, room=room_number,
                folio=on_hold[0]["reference"], status=on_hold[0]["status"],
                why=(
                    "The only folio on this in-house stay is ON_HOLD, so charges are being held "
                    "rather than billed while the guest occupies the room."
                ),
                action="Release the hold (or record why it must stay on hold) so tonight's charges can post.",
            ))

        for row in folios:
            balance = balances.get(row["id"], ZERO)
            if row["status"] == Folio.Status.OPEN and balance > ZERO:
                unbilled.append({
                    "stay_reference": stay.reference,
                    "guest": guest,
                    "room": room_number,
                    "folio_reference": row["reference"],
                    "folio_balance": money(balance),
                })
                checks.append(_check(
                    business_date=business_date, check_type="FOLIO_BALANCE_DUE",
                    object_reference=row["reference"], amount=balance,
                    folio=row["reference"], stay=stay.reference, room=room_number,
                    status=row["status"],
                    why=(
                        f"Posts to this in-house folio total {money(balance)} more in charges than in "
                        "payments, so the guest owes that amount at check-out."
                    ),
                    action=(
                        "Collect at check-out through Payments, or confirm the balance is covered by an "
                        "approved credit arrangement. Expected during a stay; nothing is written off here."
                    ),
                    extra={"guest": guest},
                ))
    return checks, unbilled, len(in_house)


# --------------------------------------------------------------------------
# Procurement <-> payable checks
# --------------------------------------------------------------------------
def _procurement_checks(business_date):
    """Only for the current hotel date: payables are point-in-time, not daily.

    A purchase order that has received goods but carries no payable is a live
    condition of the accounting records, not an event of one business day, so it
    is only reported against today's date. Reporting it against every past date
    would invent history that the ledger does not have.
    """
    from apps.finance.models import Expense
    from apps.inventory.models import PurchaseOrder

    if business_date != hotel_today():
        return []

    checks = []
    po_statuses = (
        PurchaseOrder.Status.APPROVED,
        PurchaseOrder.Status.ORDERED,
        PurchaseOrder.Status.PARTIALLY_RECEIVED,
        PurchaseOrder.Status.RECEIVED,
        PurchaseOrder.Status.PAID,
    )
    orders = (
        PurchaseOrder.objects.filter(status__in=po_statuses)
        .select_related("supplier")
        .prefetch_related("lines", "goods_receipts__lines")
    )
    # Two questions, kept apart on purpose:
    # * has a payable document been raised at all (any status but rejected/voided)?
    # * has any of it actually reached the ledger (submitted/approved/posted)?
    # A drafted payable is work in progress, not cost in the books, so it does
    # not silence the "goods received, nothing on the ledger" condition — but the
    # sentence the accountant reads must say which of the two is true.
    raised_documents = {}
    ledger_payables = {}
    for expense in Expense.objects.filter(purchase_order__isnull=False).exclude(
        status__in=[Expense.Status.REJECTED, Expense.Status.VOIDED],
    ).select_related("purchase_order", "settlement_transaction"):
        raised_documents.setdefault(expense.purchase_order_id, []).append(expense)
        if expense.status in {Expense.Status.SUBMITTED, Expense.Status.APPROVED, Expense.Status.POSTED}:
            ledger_payables.setdefault(expense.purchase_order_id, []).append(expense)

    for po in orders:
        received = sum(
            (Decimal(line.quantity_received) * Decimal(line.unit_cost)
             for receipt in po.goods_receipts.all() for line in receipt.lines.all()),
            ZERO,
        ).quantize(CENT)
        if received <= ZERO:
            continue
        payables = ledger_payables.get(po.pk, [])
        documents = raised_documents.get(po.pk, [])
        raised = sum((Decimal(expense.amount) for expense in payables), ZERO).quantize(CENT)
        if not payables and not documents:
            checks.append(_check(
                business_date=business_date, check_type="PO_RECEIVED_NOT_BILLED",
                object_reference=po.reference, amount=received,
                reference=po.reference, status=po.status,
                why=(
                    f"Goods worth {money(received)} have been received against this order, but no "
                    "supplier payable (expense on account) has been raised, so the cost is not in the "
                    "ledger and the supplier is not in the payables list."
                ),
                action=(
                    "Accounting raises the supplier payable from the order in Supplier accounts "
                    "(the amount is capped at the value actually received)."
                ),
                extra={"supplier": po.supplier.name if po.supplier_id else "", "received_value": money(received)},
            ))
        elif not payables and documents:
            draft = documents[0]
            checks.append(_check(
                business_date=business_date, check_type="PO_RECEIVED_NOT_BILLED",
                object_reference=po.reference, amount=received,
                reference=po.reference, status=po.status,
                why=(
                    f"Goods worth {money(received)} have been received and payable {draft.reference} has "
                    f"been raised, but it is still {draft.status} — the cost is not in the ledger yet."
                ),
                action="Submit, approve and post the payable so the supplier liability reaches the books.",
                extra={"supplier": po.supplier.name if po.supplier_id else "", "received_value": money(received),
                       "payable_document": draft.reference, "payable_status": draft.status},
            ))
        elif received - raised > ZERO:
            checks.append(_check(
                business_date=business_date, check_type="PO_RECEIVED_NOT_BILLED",
                object_reference=po.reference, amount=received - raised,
                reference=po.reference, status=po.status,
                why=(
                    f"Goods worth {money(received)} have been received but only {money(raised)} has been "
                    f"billed to the supplier, leaving {money(received - raised)} of cost outside the ledger."
                ),
                action=(
                    "Usually a further delivery arrived after the payable was raised. Decide with the "
                    "accounts manager how the remainder is billed (see the procurement payable note in "
                    "the accounting agent report) — it is not silently absorbed here."
                ),
                extra={"supplier": po.supplier.name if po.supplier_id else "", "raised": money(raised),
                       "received_value": money(received)},
            ))
        if payables and raised - received > ZERO:
            checks.append(_check(
                business_date=business_date, check_type="PO_PAYABLE_EXCEEDS_RECEIVED",
                object_reference=po.reference, amount=raised - received,
                reference=po.reference, status=po.status,
                why=(
                    f"Payables of {money(raised)} have been raised against goods received worth "
                    f"{money(received)} — {money(raised - received)} more than the hotel has taken delivery of."
                ),
                action=(
                    "Check the receipt record first. If the delivery was not fully receipted, receipt the "
                    "balance; otherwise raise a correction through the approved expense/reversal route."
                ),
                extra={"supplier": po.supplier.name if po.supplier_id else "", "raised": money(raised),
                       "received_value": money(received)},
            ))
    return checks


# --------------------------------------------------------------------------
# Review state
# --------------------------------------------------------------------------
def _apply_reviews(checks, *, business_date):
    """Join the latest append-only review decision onto each live check."""
    by_id = {check["check_id"]: check for check in checks}
    if by_id:
        reviews = (
            ReconciliationCheckReview.objects.filter(check_id__in=list(by_id))
            .select_related("actor")
            .order_by("created_at", "pk")
        )
        latest = {}
        for review in reviews:
            latest[review.check_id] = review  # ordered, so the last write wins
        for check_id, review in latest.items():
            check = by_id[check_id]
            check["review_decision"] = review.decision
            check["reviewed_by"] = review.actor.email if review.actor_id else ""
            check["reviewed_at"] = review.created_at.isoformat()
            check["review_note"] = review.note
            if review.decision == DECISION_DISMISS:
                check["status"] = STATUS_DISMISSED
            elif review.decision == DECISION_ACKNOWLEDGE:
                check["status"] = STATUS_ACKNOWLEDGED
            else:
                check["status"] = STATUS_OPEN

    # Reviews on this business date whose condition has since cleared. They are
    # reported as RESOLVED rather than silently disappearing, so an accountant
    # can see that a check they were working on is gone and why.
    resolved = []
    live_ids = set(by_id)
    for review in (
        ReconciliationCheckReview.objects.filter(business_date=business_date)
        .exclude(check_id__in=list(live_ids))
        .select_related("actor")
        .order_by("-created_at", "-pk")[:50]
    ):
        resolved.append({
            "check_id": review.check_id,
            "business_date": review.business_date.isoformat(),
            "category": review.category,
            "type": review.check_type,
            "object_reference": review.object_reference,
            "amount": money(review.amount) if review.amount is not None else None,
            "status": STATUS_RESOLVED,
            "review_decision": review.decision,
            "reviewed_by": review.actor.email if review.actor_id else "",
            "reviewed_at": review.created_at.isoformat(),
            "review_note": review.note,
            "why_flagged": "The condition this review was written about no longer holds for this business date.",
        })
    return resolved


def summarise(checks):
    """Count a check list by what each one actually is.

    Only ERROR conditions count as unresolved. Warnings (an in-house balance, a
    pending arrival, goods received not yet billed) are surfaced separately and
    never inflate the "unresolved" number, and dismissed checks are reported as
    dismissed instead of being deleted.
    """
    def count(predicate):
        return sum(1 for check in checks if predicate(check))

    unresolved = [check for check in checks if check["severity"] == SEVERITY_ERROR and check["status"] in {STATUS_OPEN, STATUS_ACKNOWLEDGED}]
    return {
        "total_conditions": len(checks),
        "unresolved": len(unresolved),
        "unresolved_room_and_billing": len([c for c in unresolved if c["category"] in {CATEGORY_ROOM, CATEGORY_BILLING}]),
        "unresolved_cash_drawer": len([c for c in unresolved if c["category"] == CATEGORY_CASH]),
        "unresolved_procurement": len([c for c in unresolved if c["category"] == CATEGORY_PROCUREMENT]),
        "attention_room_and_billing": count(lambda c: c["category"] in {CATEGORY_ROOM, CATEGORY_BILLING} and c["severity"] == SEVERITY_WARNING and c["status"] in {STATUS_OPEN, STATUS_ACKNOWLEDGED}),
        "attention_procurement": count(lambda c: c["category"] == CATEGORY_PROCUREMENT and c["severity"] == SEVERITY_WARNING and c["status"] in {STATUS_OPEN, STATUS_ACKNOWLEDGED}),
        "informational": count(lambda c: c["severity"] == SEVERITY_INFO and c["status"] in {STATUS_OPEN, STATUS_ACKNOWLEDGED}),
        "acknowledged": count(lambda c: c["status"] == STATUS_ACKNOWLEDGED),
        "dismissed": count(lambda c: c["status"] == STATUS_DISMISSED),
    }


@transaction.atomic
def record_check_review(*, check_id, business_date, decision, note, actor):
    """Record an accountant's decision about one reconciliation check.

    This appends evidence; it never edits the record the check is about. A
    dismissal must carry a reason, because dismissing a discrepancy is a
    judgement that has to be readable later. Anyone who can manage expenses or
    approve them may review a check; a reviewer is never asked to be the person
    who caused it, because the review changes no money.
    """
    if not (has_capability(actor, "expense.manage") or has_capability(actor, "expense.approve")):
        raise PermissionDenied("This user cannot review reconciliation checks.")
    decision = str(decision or "").upper()
    if decision not in REVIEW_DECISIONS:
        raise ValidationError({"decision": "Choose ACKNOWLEDGED, DISMISSED or ESCALATED."})
    note = " ".join(str(note or "").split())
    if len(note) < 5:
        raise ValidationError({"note": "Record what you found and what you did (at least 5 characters)."})
    if len(note) > 2000:
        raise ValidationError({"note": "Keep the review note under 2000 characters."})
    check_id = str(check_id or "").strip()
    if not check_id:
        raise ValidationError({"check_id": "A reconciliation check is required."})

    collected = collect_checks(business_date=business_date, include_dismissed=True)
    match = next((row for row in collected["checks"] if row["check_id"] == check_id), None)
    if match is None:
        raise ValidationError({
            "check_id": (
                "This check is not part of the reconciliation pack for that business date. "
                "Reload the checks and try again."
            )
        })
    review = ReconciliationCheckReview.objects.create(
        check_id=check_id,
        business_date=business_date,
        category=match["category"],
        check_type=match["type"],
        object_reference=match["object_reference"],
        amount=Decimal(match["amount"]) if match["amount"] is not None else None,
        decision=decision,
        note=note,
        actor=actor,
        snapshot={"status": match["record_status"], "why_flagged": match["why_flagged"], "amount": match["amount"]},
    )
    # Reflect the decision on the returned check so the caller does not have to
    # re-read the pack to know what the record now says.
    match.update({
        "status": {
            DECISION_DISMISS: STATUS_DISMISSED,
            DECISION_ACKNOWLEDGE: STATUS_ACKNOWLEDGED,
        }.get(decision, STATUS_OPEN),
        "review_decision": decision,
        "reviewed_by": getattr(actor, "email", "") or "",
        "reviewed_at": review.created_at.isoformat(),
        "review_note": note,
    })
    return review, match


def collect_checks(*, business_date=None, include_dismissed=True):
    """Return the reconciliation checks for one business date, plus summary."""
    business_date = business_date or hotel_today()
    cash_checks, drawer_rows = _cash_checks(business_date)
    room_billing_checks, unbilled, in_house_count = _room_and_billing_checks(business_date)
    checks = cash_checks + room_billing_checks + _procurement_checks(business_date)
    resolved = _apply_reviews(checks, business_date=business_date)
    if not include_dismissed:
        checks = [check for check in checks if check["status"] != STATUS_DISMISSED]
    summary = summarise(checks)
    summary["business_date"] = business_date.isoformat()
    summary["resolved_since_review"] = len(resolved)
    # Same number the night audit reports as ``in_house_count``; computed once,
    # from the query the billing checks already ran.
    summary["in_house_count"] = in_house_count
    return {
        "business_date": business_date.isoformat(),
        "checks": checks,
        "resolved_reviews": resolved,
        "summary": summary,
        "cash_drawers": drawer_rows,
        "unbilled_in_house_folios": unbilled,
        "categories": [
            {"key": CATEGORY_ROOM, "label": "Room"},
            {"key": CATEGORY_BILLING, "label": "Billing"},
            {"key": CATEGORY_CASH, "label": "Cash drawer"},
            {"key": CATEGORY_PROCUREMENT, "label": "Procurement / payables"},
        ],
    }
