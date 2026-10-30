"""Bar unopened-item returns, their stock effect, and eligible refunds.

THE BUSINESS CASE
=================
A guest orders twelve bottles at ₦2,000, drinks seven, and leaves five
unopened. The hotel must charge ₦14,000, put five bottles back on the bar
shelf, and hand back ₦10,000 if the guest already paid.

WHAT THIS MODULE DOES, IN ORDER
===============================
1. Locks the order and proves it is an *eligible* bar order (delivered/served,
   charged, not cancelled or voided, not already fully returned).
2. Validates each returned quantity against the quantity that has not already
   come back — the same bottle cannot be returned twice.
3. Values the returned quantity at the **actual net price the guest paid** for
   that line (the frozen line total split per unit), never at today's menu
   price, so a discounted or modified line cannot over-refund.
4. Puts the stock back through the inventory app's append-only ``POS_RETURN``
   movement, source-keyed on this return record.
5. Posts one balanced compensating ledger entry that takes the returned value
   off the guest's charge (a folio credit when the order was billed to a room).
6. Computes the refund due as ``net paid - final charge``, never more, and
   routes it through the controlled refund path: cash is only complete when the
   drawer pays it out; card/bank stays ``PROCESSING`` until a provider
   reference is confirmed; an amount at or above the configured policy
   threshold needs the existing maker-checker approval before any money moves.
7. Publishes the final charge, the amount already paid, the outstanding
   balance and the refund state, and writes append-only audit evidence.

WHAT IT DELIBERATELY DOES NOT DO
================================
* It never edits an order line, a posted charge, a tender or a historical
  receipt. ``PosOrder.total_amount`` stays frozen; the reduction accumulates in
  ``PosOrder.returned_amount`` and is published as ``net_total``.
* It never deletes a stock movement. The returned bottles are a new inbound
  movement; the original consumption stays where it is.
* It never labels a refund successful that the hotel has not actually paid.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Mapping

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count, DecimalField, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.audit.services import log_action
from apps.core.utils import hotel_today
from apps.finance.models import CashMovement, CashSession, FinancialLine, FinancialTransaction, Folio, FolioPosting
from apps.finance.services import accounting
from apps.finance.services.approval_service import request_approval, requires_manager_approval
from apps.finance.services.ledger_service import create_posted_transaction
from apps.inventory.services.inventory_service import restock_returned_pos_items

from ..models import PosOrder, PosOrderEvent, PosOrderReturn, PosOrderReturnLine, PosTender

CENT = Decimal("0.01")
ZERO = Decimal("0.00")

#: Order modes that may record an unopened-item return. The owner asked for the
#: bar bottle workflow; a restaurant bottle of wine is the same physical event,
#: so RESTAURANT is allowed too, while a room-service tray that has already been
#: delivered to a guest room is not a bar-stock return.
RETURNABLE_MODES = frozenset({PosOrder.Mode.BAR, PosOrder.Mode.RESTAURANT})
#: Only a delivered/served order has physically consumed stock, so only a
#: delivered order can have unopened items handed back.
RETURNABLE_STATUSES = frozenset({PosOrder.Status.DELIVERED})


def _money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def captured_total(order) -> Decimal:
    """Money actually collected against this order (never a client figure)."""
    total = order.tenders.filter(status=PosTender.Status.CAPTURED).aggregate(
        total=Coalesce(Sum("amount"), Value(ZERO), output_field=DecimalField(max_digits=12, decimal_places=2))
    )["total"]
    return _money(total)


def returned_quantity_map(order) -> dict[int, int]:
    """``{order_line_id: already_returned_quantity}`` for one order."""
    rows = (
        PosOrderReturnLine.objects.filter(return_record__order=order)
        .values("order_line_id")
        .annotate(total=Sum("quantity_returned"))
    )
    return {row["order_line_id"]: int(row["total"] or 0) for row in rows}


def returned_amount_total(order) -> Decimal:
    total = PosOrderReturn.objects.filter(order=order).aggregate(
        total=Coalesce(Sum("returned_amount"), Value(ZERO), output_field=DecimalField(max_digits=12, decimal_places=2))
    )["total"]
    return _money(total)


def line_net_unit_price(line) -> Decimal:
    """The actual net price one unit of this line charged the guest.

    ``PosOrderLine.line_total`` already includes the line's modifiers and is the
    frozen amount the guest was billed, so dividing it by the sold quantity is
    the only valuation that cannot over-refund a discounted or modified line.
    """
    quantity = max(int(line.quantity or 0), 1)
    return (_money(line.line_total) / quantity).quantize(CENT, rounding=ROUND_HALF_UP)


def line_net_amount_for(line, quantity: int, *, already_returned_amount: Decimal) -> Decimal:
    """Net money that ``quantity`` more returned units of ``line`` give back.

    Proportional to the frozen line total, and capped so the cumulative returned
    money of a line can never exceed what the line actually charged.
    """
    sold = max(int(line.quantity or 0), 1)
    quantity = int(quantity or 0)
    if quantity <= 0:
        raise ValidationError({"lines": "A returned quantity must be greater than zero."})
    if quantity >= sold:
        amount = _money(line.line_total)
    else:
        amount = (_money(line.line_total) * quantity / sold).quantize(CENT, rounding=ROUND_HALF_UP)
    remaining = max(_money(line.line_total) - _money(already_returned_amount), ZERO)
    return min(amount, remaining)


def _eligible_queryset():
    return (
        PosOrder.objects.filter(status__in=RETURNABLE_STATUSES, mode__in=RETURNABLE_MODES)
        .exclude(charge_transaction__isnull=True)
        .select_related("stay", "folio", "created_by", "table_session__table", "charge_transaction")
        .annotate(
            paid_total=Coalesce(
                Sum("tenders__amount", filter=Q(tenders__status=PosTender.Status.CAPTURED)),
                Value(ZERO),
                output_field=DecimalField(max_digits=12, decimal_places=2),
            ),
            return_count=Count("returns", distinct=True),
            line_count=Count("lines", distinct=True),
        )
        .order_by("-delivered_at", "-created_at", "-pk")
    )


def eligible_orders(*, search="", order_reference="", unsettled_only=False, limit=200):
    """Bounded list of bar orders that may still have unopened items returned."""
    queryset = _eligible_queryset()
    search = str(search or "").strip()
    if search:
        if len(search) > 100:
            raise ValidationError({"search": "Search is limited to 100 characters."})
        queryset = queryset.filter(
            Q(reference__icontains=search)
            | Q(guest_name__icontains=search)
            | Q(table_number__icontains=search)
            | Q(created_by__email__icontains=search)
        )
    reference = str(order_reference or "").strip()
    if reference:
        queryset = queryset.filter(reference=reference)
    if unsettled_only:
        queryset = queryset.filter(
            settlement_status__in=[PosOrder.SettlementStatus.UNPAID, PosOrder.SettlementStatus.PARTIALLY_PAID]
        )
    return queryset[: max(1, min(int(limit or 200), 500))]


def return_preview(order) -> dict:
    """Everything the return modal shows before anything is recorded."""
    if not isinstance(order, PosOrder):
        order = PosOrder.objects.get(pk=order.pk)
    already = returned_quantity_map(order)
    returned_lines = {
        row.order_line_id: row
        for row in PosOrderReturnLine.objects.filter(return_record__order=order)
    }
    already_amount_by_line: dict[int, Decimal] = {}
    for line_id, row in returned_lines.items():
        already_amount_by_line[line_id] = already_amount_by_line.get(line_id, ZERO) + _money(row.line_net_amount)
    paid = captured_total(order)
    lines = []
    for line in order.lines.order_by("pk"):
        fixed = int(already.get(line.pk, 0))
        returnable = max(int(line.quantity) - fixed, 0)
        lines.append(
            {
                "order_line_id": line.pk,
                "item_name": line.item_name,
                "item_sku": line.item_sku,
                "quantity_sold": int(line.quantity),
                "quantity_returned": fixed,
                "quantity_returnable": returnable,
                "unit_net_price": str(line_net_unit_price(line)),
                "line_net_total": str(_money(line.line_total)),
                "returnable_value": str(
                    line_net_amount_for(
                        line, returnable, already_returned_amount=already_amount_by_line.get(line.pk, ZERO)
                    )
                ),
            }
        )
    returned_total = returned_amount_total(order)
    net_total = _money(order.total_amount) - returned_total
    return {
        "order_reference": order.reference,
        "order_id": order.pk,
        "status": order.status,
        "mode": order.mode,
        "settlement_status": order.settlement_status,
        "guest_name": order.guest_name,
        "table_number": order.table_number,
        "staff_email": order.created_by.email if order.created_by_id else "",
        "currency": order.currency,
        "original_total": str(_money(order.total_amount)),
        "returned_amount": str(returned_total),
        "final_total": str(_money(net_total)),
        "amount_paid": str(paid),
        "outstanding_balance": str(max(_money(net_total) - paid, ZERO)),
        "refund_due": str(min(returned_total, max(paid - _money(net_total), ZERO))),
        "returns": [
            {
                "reference": record.reference,
                "created_at": record.created_at,
                "returned_amount": str(record.returned_amount),
                "refund_due": str(record.refund_due),
                "refund_method": record.refund_method,
                "refund_status": record.refund_status,
                "refund_reference": record.refund_reference,
                "recorded_by_email": record.recorded_by.email if record.recorded_by_id else "",
            }
            for record in order.returns.select_related("recorded_by").order_by("-created_at")
        ],
        "lines": lines,
    }


def _validate_datestamp(order):
    if order.status not in RETURNABLE_STATUSES:
        raise ValidationError(
            f"Order {order.reference} is {order.get_status_display().lower()}; unopened items can only be "
            "returned from a delivered/served order."
        )
    if order.mode not in RETURNABLE_MODES:
        raise ValidationError(
            f"Order {order.reference} is a {order.get_mode_display().lower()} order; unopened-item returns "
            "are recorded against bar and restaurant service."
        )
    if order.charge_transaction_id is None:
        raise ValidationError(
            f"Order {order.reference} has no posted charge yet, so there is no bill to correct. "
            "Deliver (charge) the order first."
        )


def _recompute_settlement(order, *, paid: Decimal, refunded: Decimal) -> str:
    """Publish the settlement status that follows from net charge vs money in."""
    net_total = _money(order.net_total)
    if net_total <= ZERO:
        status = PosOrder.SettlementStatus.REFUNDED if paid > ZERO else PosOrder.SettlementStatus.PAID
    elif paid >= net_total:
        status = (
            PosOrder.SettlementStatus.PARTIALLY_REFUNDED
            if refunded > ZERO
            else PosOrder.SettlementStatus.PAID
        )
    elif paid > ZERO:
        status = PosOrder.SettlementStatus.PARTIALLY_PAID
    else:
        status = PosOrder.SettlementStatus.UNPAID
    if status != order.settlement_status:
        order.settlement_status = status
        order.save(update_fields=["settlement_status", "updated_at"])
    return status


def _post_return_credit(*, record, order, amount: Decimal, actor) -> FinancialTransaction:
    """Take the returned value off the guest's charge, once."""
    if amount <= ZERO:
        return None
    folio = None
    if order.folio_id:
        folio = Folio.objects.select_for_update().get(pk=order.folio_id)
    lines = [
        {
            "account_code": accounting.FOOD_BEVERAGE_REVENUE,
            "direction": FinancialLine.Direction.DEBIT,
            "amount": amount,
            "description": f"Unopened items returned {record.reference}",
        },
        {
            "account_code": accounting.ACCOUNTS_RECEIVABLE,
            "direction": FinancialLine.Direction.CREDIT,
            "amount": amount,
            "folio": folio,
            "description": f"Credit for returned items on {order.reference}",
        },
    ]
    postings = []
    if folio is not None:
        postings.append(
            {
                "line_index": 1,
                "folio": folio,
                "kind": FolioPosting.Kind.ADJUSTMENT,
                "effect": FolioPosting.Effect.CREDIT,
                "amount": amount,
                "description": f"Unopened items returned on {order.reference}",
                "source_reference": record.reference,
            }
        )
    source_key = f"pos-return-credit:{record.reference}"
    financial_transaction, _created = create_posted_transaction(
        transaction_type=FinancialTransaction.Type.ADJUSTMENT,
        source_key=source_key,
        idempotency_key=source_key,
        actor=actor,
        source_reference=order.reference,
        external_reference=record.reference,
        narrative=f"Unopened items returned against POS order {order.reference}",
        metadata={"pos_order_id": order.pk, "pos_order_return_id": record.pk, "amount": str(amount)},
        currency=order.currency,
        business_date=hotel_today(),
        lines=lines,
        postings=postings,
    )
    record.credit_transaction = financial_transaction
    record.save(update_fields=["credit_transaction", "updated_at"])
    return financial_transaction


def _refund_asset_account(method: str) -> str:
    if method == PosOrderReturn.RefundMethod.CASH:
        return accounting.CASH_ON_HAND
    if method == PosOrderReturn.RefundMethod.CARD:
        return accounting.CARD_CLEARING
    return accounting.BANK_CLEARING


def _settle_refund(*, record, order, actor, method: str, cash_session=None, provider_reference="") -> FinancialTransaction:
    """Post the money-back entry for a return refund, exactly once."""
    if record.refund_transaction_id or record.refund_status == PosOrderReturn.RefundStatus.COMPLETED:
        return record.refund_transaction
    amount = _money(record.refund_due)
    if amount <= ZERO:
        raise ValidationError("This return has no refundable balance to pay out.")
    session = None
    if method == PosOrderReturn.RefundMethod.CASH:
        if cash_session is None:
            raise ValidationError({"refund_method": "A cash refund requires the open cashier session that pays it out."})
        session = CashSession.objects.select_for_update().get(pk=cash_session.pk)
        if session.status != CashSession.Status.OPEN:
            raise ValidationError({"refund_method": "The cashier session for this refund is not open."})
        if session.expected_cash < amount:
            raise ValidationError({
                "refund_method": (
                    f"The drawer holds {session.expected_cash} but the refund is {amount}; "
                    "record the refund after a safe drop or through a manager-authorised payout."
                )
            })
    elif method not in PosOrderReturn.RefundMethod.values or method == PosOrderReturn.RefundMethod.NONE:
        raise ValidationError({"refund_method": "Choose how the guest is refunded."})

    source_key = f"pos-return-refund:{record.reference}"
    financial_transaction, _created = create_posted_transaction(
        transaction_type=FinancialTransaction.Type.REFUND,
        source_key=source_key,
        idempotency_key=source_key,
        actor=actor,
        source_reference=order.reference,
        external_reference=provider_reference or record.reference,
        narrative=f"Refund for unopened items returned on {order.reference}",
        metadata={
            "pos_order_id": order.pk,
            "pos_order_return_id": record.pk,
            "method": method,
            "provider_reference": provider_reference,
        },
        currency=order.currency,
        business_date=hotel_today(),
        lines=[
            {
                "account_code": accounting.ACCOUNTS_RECEIVABLE,
                "direction": FinancialLine.Direction.DEBIT,
                "amount": amount,
                "description": f"Refund due to guest {record.reference}",
            },
            {
                "account_code": _refund_asset_account(method),
                "direction": FinancialLine.Direction.CREDIT,
                "amount": amount,
                "description": f"{method.title()} refund {record.reference}",
            },
        ],
    )
    if session is not None:
        CashMovement.objects.create(
            cash_session=session,
            transaction=financial_transaction,
            type=CashMovement.Type.REFUND,
            amount=amount,
            currency=order.currency,
            actor=actor,
            source_reference=record.reference,
            notes=f"Unopened items returned on {order.reference}",
        )
        session.expected_cash = (_money(session.expected_cash) - amount).quantize(CENT)
        session.save(update_fields=["expected_cash", "updated_at"])
    record.refund_transaction = financial_transaction
    record.refund_method = method
    record.refund_status = PosOrderReturn.RefundStatus.COMPLETED
    record.refund_reference = (provider_reference or financial_transaction.reference)[:120]
    record.refund_recorded_by = actor
    record.refund_recorded_at = timezone.now()
    record.refund_cash_session = session
    record.refund_failure_reason = ""
    record.save(
        update_fields=[
            "refund_transaction", "refund_method", "refund_status", "refund_reference",
            "refund_recorded_by", "refund_recorded_at", "refund_cash_session",
            "refund_failure_reason", "updated_at",
        ]
    )
    PosOrderEvent.objects.create(
        order=order,
        type=PosOrderEvent.Type.RETURN_REFUND,
        actor=actor,
        details={
            "return_reference": record.reference,
            "amount": str(amount),
            "method": method,
            "financial_transaction": financial_transaction.reference,
        },
    )
    return financial_transaction


@transaction.atomic
def record_unopened_return(
    *,
    order,
    lines: Iterable[Mapping],
    actor,
    reason="",
    notes="",
    idempotency_key=None,
    restock_location=None,
    received_by="",
    refund_method=PosOrderReturn.RefundMethod.NONE,
    cash_session=None,
) -> tuple[PosOrderReturn, bool]:
    """Record untouched, unopened bottles coming back from a guest.

    ``lines`` is ``[{"order_line_id": int, "quantity_returned": int}, ...]``.
    Returns ``(return_record, created)``. A repeated request with the same
    ``idempotency_key`` returns the original record and changes nothing.
    """
    if actor is None or not getattr(actor, "is_authenticated", False):
        raise PermissionDenied("An authenticated staff member must record the return.")
    reason = str(reason or "").strip()
    if len(reason) > 255:
        raise ValidationError({"reason": "Keep the reason under 255 characters."})
    notes = str(notes or "").strip()[:2000]
    idempotency_key = str(idempotency_key or "").strip() or None
    refund_method = str(refund_method or PosOrderReturn.RefundMethod.NONE).upper()
    if refund_method not in PosOrderReturn.RefundMethod.values:
        raise ValidationError({"refund_method": "Choose a valid refund method."})

    order = (
        PosOrder.objects.select_for_update()
        .select_related("folio", "charge_transaction", "stay", "created_by")
        .get(pk=order.pk)
    )
    if idempotency_key:
        existing = PosOrderReturn.objects.select_for_update().filter(idempotency_key=idempotency_key).first()
        if existing:
            if existing.order_id != order.pk:
                raise ValidationError(
                    {"idempotency_key": "This key was already used for a different order's return."}
                )
            return existing, False
    _validate_datestamp(order)

    raw_lines = []
    for raw in lines or ():
        try:
            order_line_id = int(raw.get("order_line_id") if "order_line_id" in raw else raw.get("line_id"))
            quantity = int(raw.get("quantity_returned") if "quantity_returned" in raw else raw.get("quantity"))
        except (TypeError, ValueError) as exc:
            raise ValidationError({"lines": "Each returned line needs an integer order_line_id and quantity."}) from exc
        if quantity <= 0:
            raise ValidationError({"lines": "A returned quantity must be greater than zero."})
        if order_line_id in {row["order_line_id"] for row in raw_lines}:
            raise ValidationError({"lines": "Each order line may only appear once per return."})
        raw_lines.append({"order_line_id": order_line_id, "quantity": quantity})
    if not raw_lines:
        raise ValidationError({"lines": "Select at least one unopened item to return."})

    order_lines = {line.pk: line for line in order.lines.select_related("menu_item").filter(
        pk__in=[row["order_line_id"] for row in raw_lines]
    )}
    missing = sorted({row["order_line_id"] for row in raw_lines} - set(order_lines))
    if missing:
        raise ValidationError({"lines": f"These order lines do not belong to {order.reference}: {missing}."})

    already_qty = returned_quantity_map(order)
    already_amount: dict[int, Decimal] = {}
    for row in PosOrderReturnLine.objects.filter(return_record__order=order):
        already_amount[row.order_line_id] = already_amount.get(row.order_line_id, ZERO) + _money(row.line_net_amount)

    planned = []
    returned_total = ZERO
    for row in raw_lines:
        line = order_lines[row["order_line_id"]]
        fixed = int(already_qty.get(line.pk, 0))
        returnable = int(line.quantity) - fixed
        if row["quantity"] > returnable:
            raise ValidationError({
                "lines": (
                    f"{line.item_name}: {line.quantity} sold, {fixed} already returned, so at most "
                    f"{returnable} can come back."
                )
            })
        amount = line_net_amount_for(
            line, row["quantity"], already_returned_amount=already_amount.get(line.pk, ZERO)
        )
        returned_total += amount
        planned.append({"line": line, "quantity": row["quantity"], "amount": amount})

    paid = captured_total(order)
    final_total = _money(order.total_amount) - (returned_amount_total(order) + returned_total)
    record = PosOrderReturn.objects.create(
        idempotency_key=idempotency_key,
        order=order,
        reason=reason,
        notes=notes,
        restock_location=restock_location,
        original_total=_money(order.total_amount),
        returned_amount=returned_total,
        final_total=final_total,
        amount_paid=paid,
        refund_due=max(min(returned_total, paid - final_total), ZERO),
        refund_method=refund_method if refund_method else PosOrderReturn.RefundMethod.NONE,
        refund_status=PosOrderReturn.RefundStatus.NOT_DUE,
        recorded_by=actor,
        received_by=str(received_by or "")[:120],
        metadata={
            "mode": order.mode,
            "settlement_status_before": order.settlement_status,
            "lines": [
                {
                    "order_line_id": row["line"].pk,
                    "item_name": row["line"].item_name,
                    "quantity_returned": row["quantity"],
                    "line_net_amount": str(row["amount"]),
                    "unit_net_price": str(line_net_unit_price(row["line"])),
                }
                for row in planned
            ],
        },
    )

    # 1. Put the stock back (append-only POS_RETURN movements, one per
    #    item/location, keyed on this return record).
    from .food_service import record_prepared_returns
    food_line_ids = record_prepared_returns(order=order, planned=planned, actor=actor, return_reference=record.reference)
    restock_quantities = {row["line"].pk: row["quantity"] for row in planned if row["line"].pk not in food_line_ids}
    if restock_quantities:
        movements, aggregates, per_line_base, skipped_reason = restock_returned_pos_items(
            order=order,
            line_quantities=restock_quantities,
            actor=actor,
            return_reference=record.reference,
            reason=reason or f"Unopened items returned for {order.reference}",
            location=restock_location,
        )
    else:
        movements, aggregates, per_line_base = [], {}, {}
        skipped_reason = "Returned prepared food discarded; no prepared portions or raw ingredients restocked."
    for row in planned:
        PosOrderReturnLine.objects.create(
            return_record=record,
            order_line=row["line"],
            quantity_returned=row["quantity"],
            item_name=row["line"].item_name,
            item_sku=row["line"].item_sku,
            unit_net_price=line_net_unit_price(row["line"]),
            line_net_amount=row["amount"],
            restock_quantity=per_line_base.get(row["line"].pk, Decimal("0.0000")),
        )
    metadata = dict(record.metadata or {})
    metadata.update(
        {
            "stock_movements": [movement.reference for movement in movements],
            "restock_aggregates": {f"{item_id}@{location_id}": str(qty) for (item_id, location_id), qty in aggregates.items()},
            "restock_skipped_reason": skipped_reason,
        }
    )

    # 2. Take the returned value off the bill with one compensating entry.
    credit = _post_return_credit(record=record, order=order, amount=returned_total, actor=actor)
    if credit is not None:
        metadata["credit_transaction"] = credit.reference

    order.returned_amount = (returned_amount_total(order)).quantize(CENT)
    order.save(update_fields=["returned_amount", "updated_at"])

    # 3. Route the refund through the controlled path.
    refund_due = _money(record.refund_due)
    if refund_due <= ZERO:
        record.refund_status = PosOrderReturn.RefundStatus.NOT_DUE
        record.refund_method = PosOrderReturn.RefundMethod.NONE
        record.save(update_fields=["refund_status", "refund_method", "updated_at"])
        refunded = ZERO
    else:
        refunded = _route_refund(
            record=record, order=order, actor=actor, refund_due=refund_due,
            refund_method=refund_method, cash_session=cash_session,
        )
    record.metadata = metadata
    record.save(update_fields=["metadata", "updated_at"])

    # 4. Publish the resulting state on the order and the audit trail.
    _recompute_settlement(order, paid=paid, refunded=refunded if refund_due > ZERO else ZERO)
    PosOrderEvent.objects.create(
        order=order,
        type=PosOrderEvent.Type.RETURN_RECORDED,
        actor=actor,
        details={
            "return_reference": record.reference,
            "returned_amount": str(returned_total),
            "final_total": str(final_total),
            "refund_due": str(record.refund_due),
            "refund_status": record.refund_status,
            "restocked_movements": [movement.reference for movement in movements],
            "restock_skipped_reason": skipped_reason,
        },
    )
    log_action(
        actor=actor,
        action="POS_RETURN_RECORDED",
        instance=record,
        metadata={
            "order_reference": order.reference,
            "returned_amount": str(returned_total),
            "final_total": str(final_total),
            "refund_due": str(record.refund_due),
            "refund_status": record.refund_status,
        },
        summary=f"Recorded unopened-item return {record.reference} against {order.reference}",
    )
    return record, True


def _route_refund(*, record, order, actor, refund_due: Decimal, refund_method: str, cash_session) -> Decimal:
    """Decide whether money may leave now, and hand it to the right workflow."""
    from apps.accounts.capabilities import has_capability

    record.refund_method = refund_method or PosOrderReturn.RefundMethod.NONE
    try:
        approval_needed = requires_manager_approval(request_type="REFUND", amount=refund_due)
    except ValidationError:
        # No effective control policy configured in this database: fall back to
        # the stricter reading and require the approval capability instead of
        # silently paying money out.
        approval_needed = True
    if approval_needed:
        approval, _created = request_approval(
            request_type="REFUND",
            amount=refund_due,
            requester=actor,
            request_key=f"pos-return-refund:{record.reference}",
            source_reference=order.reference,
            currency=order.currency,
            reason=(record.reason or "Unopened items returned")[:255],
            metadata={"pos_order_return": record.reference, "pos_order": order.reference},
        )
        record.approval = approval
        record.refund_status = PosOrderReturn.RefundStatus.AWAITING_APPROVAL
        record.save(update_fields=["approval", "refund_status", "refund_method", "updated_at"])
        return ZERO
    if record.refund_method in {PosOrderReturn.RefundMethod.CARD, PosOrderReturn.RefundMethod.BANK_TRANSFER}:
        # Nothing is claimed successful yet, and no capability is needed to
        # *raise* the obligation: the money leaves only when the provider or
        # the bank confirms it through ``confirm_return_refund`` (which does
        # require ``pos.return.refund``).
        record.refund_status = PosOrderReturn.RefundStatus.PROCESSING
        record.save(update_fields=["refund_status", "refund_method", "updated_at"])
        return ZERO
    if not has_capability(actor, "pos.return.refund"):
        record.refund_status = PosOrderReturn.RefundStatus.PENDING
        record.save(update_fields=["refund_status", "refund_method", "updated_at"])
        return ZERO
    if record.refund_method == PosOrderReturn.RefundMethod.CASH:
        if cash_session is None:
            record.refund_status = PosOrderReturn.RefundStatus.PENDING
            record.save(update_fields=["refund_status", "refund_method", "updated_at"])
            return ZERO
        _settle_refund(record=record, order=order, actor=actor, method=record.refund_method, cash_session=cash_session)
        return refund_due
    # No method chosen: the obligation is real and outstanding, not paid.
    record.refund_status = PosOrderReturn.RefundStatus.PENDING
    record.save(update_fields=["refund_status", "refund_method", "updated_at"])
    return ZERO


@transaction.atomic
def confirm_return_refund(
    *, record, actor, refund_method="", provider_reference="", succeeded=True,
    failure_reason="", cash_session=None,
) -> PosOrderReturn:
    """Pay out (or fail) a return refund, idempotently.

    Nothing here labels money successful without evidence: a card/bank refund
    must arrive with the provider's reference, and a cash refund must come from
    an open drawer that actually holds the amount.
    """
    from apps.finance.models import ApprovalRequest

    record = PosOrderReturn.objects.select_for_update().select_related("order").get(pk=record.pk)
    if record.refund_status == PosOrderReturn.RefundStatus.COMPLETED:
        return record
    order = PosOrder.objects.select_for_update().select_related("folio").get(pk=record.order_id)
    if _money(record.refund_due) <= ZERO:
        raise ValidationError("This return has no refundable balance.")
    if record.approval_id and record.refund_status == PosOrderReturn.RefundStatus.AWAITING_APPROVAL:
        approval = ApprovalRequest.objects.filter(pk=record.approval_id).first()
        if approval is None or approval.status != ApprovalRequest.Status.APPROVED:
            raise ValidationError(
                "This refund still needs a manager's approval before any money is paid out."
            )
    method = str(refund_method or record.refund_method or "").upper()
    if not succeeded:
        record.refund_status = PosOrderReturn.RefundStatus.FAILED
        record.refund_failure_reason = str(failure_reason or "The refund could not be completed.")[:500]
        record.refund_method = method if method in PosOrderReturn.RefundMethod.values else record.refund_method
        record.save(
            update_fields=["refund_status", "refund_failure_reason", "refund_method", "updated_at"]
        )
        log_action(
            actor=actor,
            action="POS_RETURN_REFUND_FAILED",
            instance=record,
            metadata={
                "order_reference": order.reference,
                "reason": record.refund_failure_reason,
                "refund_due": str(record.refund_due),
            },
            summary=f"Refund for {record.reference} recorded as failed",
        )
        return record
    if method not in {
        PosOrderReturn.RefundMethod.CASH,
        PosOrderReturn.RefundMethod.CARD,
        PosOrderReturn.RefundMethod.BANK_TRANSFER,
    }:
        raise ValidationError({"refund_method": "Choose cash, card, or bank transfer to pay this refund."})
    if method != PosOrderReturn.RefundMethod.CASH and not str(provider_reference or "").strip():
        raise ValidationError({
            "provider_reference": (
                "A card or bank refund needs the provider/bank confirmation reference. Do not mark it "
                "successful before the provider confirms it."
            )
        })
    _settle_refund(
        record=record,
        order=order,
        actor=actor,
        method=method,
        cash_session=cash_session,
        provider_reference=str(provider_reference or "").strip(),
    )
    paid = captured_total(order)
    refunded = _money(record.refund_due)
    _recompute_settlement(order, paid=paid, refunded=refunded)
    log_action(
        actor=actor,
        action="POS_RETURN_REFUND_RECORDED",
        instance=record,
        metadata={
            "order_reference": order.reference,
            "refund_due": str(record.refund_due),
            "refund_method": record.refund_method,
            "refund_reference": record.refund_reference,
        },
        summary=f"Refunded {record.refund_due} for unopened items on {order.reference}",
    )
    return record


def return_queryset():
    """Bounded, filterable projection of posted unopened-item returns."""
    return PosOrderReturn.objects.select_related(
        "order", "recorded_by", "refund_recorded_by", "approval", "restock_location"
    ).prefetch_related("lines")


def returns_for_report(*, start=None, end=None):
    """Aggregate returned items for the reporting layer (no new source of truth)."""
    queryset = PosOrderReturn.objects.all()
    if start:
        queryset = queryset.filter(created_at__date__gte=start)
    if end:
        queryset = queryset.filter(created_at__date__lte=end)
    return queryset.aggregate(
        returns=Count("pk"),
        returned_value=Coalesce(Sum("returned_amount"), Value(ZERO), output_field=DecimalField(max_digits=12, decimal_places=2)),
        refunded_value=Coalesce(
            Sum("refund_due", filter=Q(refund_status=PosOrderReturn.RefundStatus.COMPLETED)),
            Value(ZERO),
            output_field=DecimalField(max_digits=12, decimal_places=2),
        ),
    )
