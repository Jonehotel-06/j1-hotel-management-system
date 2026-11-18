"""Read-only staff reconciliation of existing collection and money-back evidence.

Booking payments/refunds retain their historical records (including those
which predate the journal bridge). All other activity is read from posted
journal tender lines. Do not call any posting, receipt or backfill service here.
"""
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation

from django.db.models import Case, DecimalField, Exists, F, OuterRef, Q, Subquery, Sum, Value, When
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Coalesce, Concat
from rest_framework.exceptions import ValidationError

from apps.bookings.models import BookingRoom
from apps.core.pagination import StandardPagination
from apps.core.utils import hotel_date_bounds, money
from apps.events.models import EventPayment
from apps.finance.models import FinancialLine, FinancialTransaction, FolioPosting, GuestCollectionKey, PaymentAllocation
from apps.finance.services import accounting
from apps.finance.services.payment_collection_service import COLLECTED_PAYMENT_STATUSES
from apps.guest_services.models import LaundryOrder
from apps.pos.models import MenuCategory, PosOrder, PosOrderReturn, PosTender
from apps.pos.services.order_service import order_outstanding_balance

from ..models import Payment, Refund

TX = FinancialTransaction.Type
POSTED = FinancialTransaction.Status.POSTED
ZERO = Decimal("0.00")
AMOUNT = DecimalField(max_digits=18, decimal_places=2)
TENDER_ACCOUNTS = (accounting.CASH_ON_HAND, accounting.BANK_CLEARING, accounting.CARD_CLEARING)


def _date_filters(queryset, params):
    dates = {}
    for key in ("date", "date_from", "date_to", "paid_on"):
        if params.get(key):
            try:
                dates[key] = date.fromisoformat(params[key])
            except ValueError as exc:
                raise ValidationError({key: "Use YYYY-MM-DD."}) from exc
    if date.max in dates.values() or dates.get("date_from", date.min) > dates.get("date_to", date.max):
        raise ValidationError({"date_to": "Enter a valid ending date on or after the starting date."})
    if "date_from" in dates:
        lo, _ = hotel_date_bounds(dates["date_from"], dates["date_from"])
        queryset = queryset.filter(feed_time__gte=lo)
    if "date_to" in dates:
        _, hi = hotel_date_bounds(dates["date_to"], dates["date_to"])
        queryset = queryset.filter(feed_time__lt=hi)
    for key in ("date", "paid_on"):
        if key in dates:
            lo, hi = hotel_date_bounds(dates[key], dates[key])
            queryset = queryset.filter(feed_time__gte=lo, feed_time__lt=hi)
    return queryset


def _branches(params):
    gross = Payment.objects.annotate(
        feed_time=Coalesce("paid_at", "created_at"), feed_kind=Value("payment"),
    )
    refunds = Refund.objects.filter(status=Refund.Status.PROCESSED, amount__gt=0).annotate(
        feed_time=Coalesce("processed_at", "updated_at"), feed_kind=Value("refund"),
        reference_label=Concat(Value("Refund #"), F("pk"), output_field=FinancialTransaction._meta.get_field("reference")),
    )
    posted_reversal = FinancialTransaction.objects.filter(reversal_of_id=OuterRef("pk"), type=TX.VOID, status=POSTED)
    operational = (
        (Q(type=TX.PAYMENT_COLLECTION) & ~Q(source_key__startswith="payment-collection:"))
        | (Q(type=TX.REFUND) & ~Q(source_key__startswith="refund:"))
    )
    reversals = Q(type=TX.VOID, reversal_of__type__in=(TX.PAYMENT_COLLECTION, TX.REFUND),
                  reversal_of__status__in=(POSTED, FinancialTransaction.Status.VOIDED))
    # Sum only liquid tender-account movements. A folio reallocation, draft,
    # waived receivable or credit note without a payout can never enter this feed.
    tender_delta = (FinancialLine.objects.filter(transaction_id=OuterRef("pk"), account_code__in=TENDER_ACCOUNTS)
                    .order_by().values("transaction_id").annotate(net=Sum(Case(
                        When(direction=FinancialLine.Direction.DEBIT, then=F("amount")),
                        default=-F("amount"), output_field=AMOUNT,
                    ))).values("net"))
    journal = (FinancialTransaction.objects.filter(operational | reversals)
               .filter(Q(status=POSTED) | Q(status=FinancialTransaction.Status.VOIDED) & Q(Exists(posted_reversal)))
               .annotate(feed_time=F("occurred_at"), feed_kind=Value("journal"),
                         signed=Subquery(tender_delta, output_field=AMOUNT))
               .filter(Q(type=TX.PAYMENT_COLLECTION, signed__gt=0) | Q(type=TX.REFUND, signed__lt=0)
                       | Q(type=TX.VOID, signed__lt=0) | Q(type=TX.VOID, signed__gt=0)))
    if method := (params.get("provider") or "").strip().upper():
        methods = ("POS", "CARD") if method in ("POS", "CARD") else (method,)
        gross = gross.filter(provider__in=methods)
        refunds = refunds.filter(payment__provider__in=methods)
        journal = journal.alias(feed_method=Coalesce(
            KeyTextTransform("method", "metadata"), KeyTextTransform("provider", "metadata"),
            KeyTextTransform("method", "reversal_of__metadata"), KeyTextTransform("provider", "reversal_of__metadata"),
        )).filter(feed_method__in=methods)
    if state := (params.get("status") or "").strip().upper():
        reversal = FinancialTransaction.objects.filter(type=TX.VOID, status=POSTED)
        booking_reversal = reversal.filter(reversal_of__type=TX.PAYMENT_COLLECTION,
            reversal_of__source_key=Concat(Value("payment-collection:"), OuterRef("reference")))
        refund_reversal = reversal.filter(reversal_of__type=TX.REFUND,
            reversal_of__source_key=Concat(Value("refund:"), OuterRef("pk"), output_field=FinancialTransaction._meta.get_field("source_key")))
        if state == "REVERSED":
            gross = gross.filter(Exists(booking_reversal), status__in=COLLECTED_PAYMENT_STATUSES)
            refunds = refunds.filter(Exists(refund_reversal))
            journal = journal.filter(Q(type=TX.VOID) | Q(Exists(posted_reversal)))
        else:
            gross = gross.filter(status=state).exclude(Exists(booking_reversal))
            refunds = refunds.exclude(Exists(refund_reversal)) if state == "PROCESSED" else refunds.none()
            journal = journal.filter({"SUCCESS": Q(type=TX.PAYMENT_COLLECTION), "PROCESSED": Q(type=TX.REFUND)}
                                     .get(state, Q(pk__in=[]))).exclude(Exists(posted_reversal))
    # Use scalar/evidence joins (or Exists for reverse many-to-one references),
    # so no order line, cash movement or payment allocation multiplies a row.
    term = " ".join((params.get("search") or "").split())[:120]
    for part in term.split():
        gross = gross.filter(
            Q(reference__icontains=part) | Q(transaction_id__icontains=part)
            | Q(metadata__external_payment_reference__icontains=part)
            | Q(booking__booking_reference__icontains=part) | Q(booking__guest__first_name__icontains=part)
            | Q(booking__guest__last_name__icontains=part) | Q(booking__guest__email__icontains=part)
            | Q(Exists(BookingRoom.objects.filter(
                booking_id=OuterRef("booking_id"), room__room_number__icontains=part,
            )))
        )
        refunds = refunds.filter(
            Q(payment__reference__icontains=part) | Q(booking__booking_reference__icontains=part)
            | Q(paystack_refund_reference__icontains=part) | Q(paystack_refund_id__icontains=part)
            | Q(booking__guest__first_name__icontains=part) | Q(booking__guest__last_name__icontains=part)
            | Q(Exists(BookingRoom.objects.filter(booking_id=OuterRef("booking_id"), room__room_number__icontains=part)))
            | Q(reference_label__icontains=part)
        )
        journal = journal.filter(
            Q(reference__icontains=part) | Q(source_reference__icontains=part)
            | Q(external_reference__icontains=part) | Q(source_key__icontains=part)
            | Q(reversal_of__source_reference__icontains=part) | Q(reversal_of__external_reference__icontains=part)
            | Q(reversal_of__reference__icontains=part) | Q(reversal_of__source_key__icontains=part)
            | Q(pos_tender_collection__reference__icontains=part) | Q(event_collection__reference__icontains=part)
            | Q(pos_return_refund__reference__icontains=part)
            | Q(pos_tender_collection__order__service_request__room__room_number__icontains=part)
            | Q(pos_tender_collection__order__minimart_sale__booking__booking_reference__icontains=part)
            | Q(pos_tender_collection__order__minimart_sale__guest__first_name__icontains=part)
            | Q(pos_tender_collection__order__minimart_sale__guest__last_name__icontains=part)
            | Q(pos_tender_collection__order__guest_name__icontains=part)
            | Q(pos_return_refund__order__guest_name__icontains=part)
            | Q(pos_tender_collection__order__minimart_sale__room_number__icontains=part)
            | Q(event_collection__event__guest__first_name__icontains=part)
            | Q(event_collection__event__guest__last_name__icontains=part)
            | Q(metadata__customer_name__icontains=part)
        )
    return tuple(_date_filters(qs, params).order_by() for qs in (gross, refunds, journal))


def _totals(gross, refunds, journal):
    totals = defaultdict(lambda: {"gross": ZERO, "refunds": ZERO, "reversals": ZERO})
    for qs, bucket in ((gross.filter(status__in=COLLECTED_PAYMENT_STATUSES, amount__gt=0), "gross"), (refunds, "refunds")):
        for row in qs.values("currency").annotate(total=Sum("amount")):
            totals[row["currency"]][bucket] += row["total"]
    for row in journal.values("currency", "type").annotate(total=Sum("signed")):
        bucket = {TX.PAYMENT_COLLECTION: "gross", TX.REFUND: "refunds", TX.VOID: "reversals"}[row["type"]]
        totals[row["currency"]][bucket] += -row["total"] if bucket == "refunds" else row["total"]
    return [{"currency": currency, **{key: money(value) for key, value in parts.items()},
             "net": money(parts["gross"] - parts["refunds"] + parts["reversals"])}
            for currency, parts in sorted(totals.items())]


ROOM_LOOKUPS = ("room_assignments__room", "stay__stay_rooms__room", "stay__stay_rooms__booking_room__room")


def _booking_room_lookups(prefix):
    return tuple(prefix + path for path in ROOM_LOOKUPS)


def _room_details(booking, stamp):
    """Prefer a matching occupancy interval; label unsnapshotted assignments."""
    if not booking:
        return {"room_numbers": [], "room_evidence": ""}
    stay = getattr(booking, "stay", None)
    numbers = set()
    for room in stay.stay_rooms.all() if stay else ():
        if not room.actual_check_in_at or max(room.actual_check_in_at, room.assigned_at) > stamp:
            continue
        departed = room.actual_check_out_at or room.released_at
        if departed and departed < stamp:
            continue
        # The reservation reassignment action can update BookingRoom in place
        # without independently timestamped occupancy. Don't assert that the
        # current room was occupied at the earlier time.
        assignment = room.booking_room
        if assignment and assignment.room_id != room.room_id:
            continue
        numbers.add(room.room.room_number)
    if numbers:
        return {"room_numbers": sorted(numbers), "room_evidence": "Stay occupancy at the recorded time"}
    assigned = sorted({a.room.room_number for a in booking.room_assignments.all()})
    return {"room_numbers": assigned, "room_evidence": "Booking assignments, not a receipt-time snapshot" if assigned else ""}


def _operator(actor):
    return (actor.full_name or actor.email) if actor and actor.is_staff_member else ""


def _source(order, *, minimart=False):
    if minimart:
        return "Mini Mart"
    if order.mode == PosOrder.Mode.ROOM_SERVICE:
        return "Room Service"
    if order.mode == PosOrder.Mode.RESTAURANT:
        return "Restaurant"
    if order.mode == PosOrder.Mode.BAR:
        return "Bar"
    areas = {line.menu_item.category.service_area for line in order.lines.all() if line.menu_item_id}
    # Takeaway is a POS mode; line categories are the only food/beverage evidence
    # for legacy orders. Don't infer a category from an item name or a price.
    if areas == {MenuCategory.ServiceArea.BAR}:
        return "Drinks"
    if areas == {MenuCategory.ServiceArea.RESTAURANT}:
        return "Food"
    return "Food & Drinks" if areas else "POS takeaway"


def _folio_charge_source(posting):
    if (posting["transaction__source_key"] or "").startswith("laundry:"):
        return "Laundry"
    if posting["transaction__pos_order_charge__minimart_sale__id"]:
        return "Mini Mart"
    mode = posting["transaction__pos_order_charge__mode"]
    return {PosOrder.Mode.ROOM_SERVICE: "Room Service", PosOrder.Mode.BAR: "Bar",
            PosOrder.Mode.RESTAURANT: "Restaurant"}.get(mode, "Other reception charges")


def _payment_sources(payments):
    """Classify at allocation time, never by the booking's current charges.

    When there is one source, or untouched identified balances are settled in
    full, amounts are provable. A partial mixed-folio credit carries no charge
    allocation in the data model. Leave it explicitly attributed to the folio.
    """
    allocations = list(PaymentAllocation.objects.filter(payment_id__in=payments).values(
        "payment_id", "transaction_id", "folio_id",
    ))
    target_tx = {a["transaction_id"]: a for a in allocations}
    portions = defaultdict(lambda: defaultdict(lambda: ZERO))
    if target_tx:
        posting_ids = FolioPosting.objects.filter(transaction_id__in=target_tx, kind=FolioPosting.Kind.PAYMENT)
        max_pk = posting_ids.order_by("-pk").values_list("pk", flat=True).first() or 0
        history = (FolioPosting.objects.filter(folio_id__in={a["folio_id"] for a in allocations}, pk__lte=max_pk,
                                              transaction__status__in=(POSTED, FinancialTransaction.Status.VOIDED))
                   .exclude(settles=FolioPosting.Settles.ACCOMMODATION)
                   .exclude(transaction__type=TX.ACCOMMODATION_CHARGE)
                   .order_by("folio_id", "pk").values(
                       "folio_id", "transaction_id", "kind", "effect", "amount", "transaction__source_key",
                       "transaction__pos_order_charge__minimart_sale__id", "transaction__pos_order_charge__mode",
                   ))
        folio_id, balance, uncertain, by_source = None, ZERO, False, {}
        for entry in history.iterator(chunk_size=1000):
            if entry["folio_id"] != folio_id:
                folio_id, balance, uncertain, by_source = entry["folio_id"], ZERO, False, {}
            value = entry["amount"]
            debit = entry["effect"] == FolioPosting.Effect.DEBIT
            if not debit and entry["kind"] == FolioPosting.Kind.PAYMENT and entry["transaction_id"] in target_tx:
                payment_id = target_tx[entry["transaction_id"]]["payment_id"]
                if value <= balance and len(by_source) == 1 and not uncertain:
                    portions[payment_id][next(iter(by_source))] += value
                elif not uncertain and len(by_source) > 1 and value == balance and sum(by_source.values()) == value:
                    for source, amount in by_source.items():
                        portions[payment_id][source] += amount
                else:
                    portions[payment_id]["Guest Folio"] += value
            if debit and entry["kind"] == FolioPosting.Kind.CHARGE:
                source = _folio_charge_source(entry)
                by_source[source] = by_source.get(source, ZERO) + value
            elif not debit and len(by_source) == 1 and value <= balance and not uncertain:
                source = next(iter(by_source))
                by_source[source] -= value
            else:
                uncertain = True
            balance += value if debit else -value
            if balance == ZERO:
                by_source, uncertain = {}, False
    keyed = dict(GuestCollectionKey.objects.filter(payment_id__in=payments).values_list("payment_id", "breakdown"))
    for payment in payments.values():
        chunks = portions[payment.pk]
        extras = sum(chunks.values(), ZERO)
        if extras > payment.amount:
            # Inconsistent legacy linkage is not a provable split.
            chunks.clear()
            chunks["Guest Folio"] = payment.amount
            extras = payment.amount
        remainder = max(ZERO, payment.amount - extras)
        metadata = payment.metadata or {}
        if metadata.get("settled_via") != "checkout_folio":
            if remainder:
                chunks["Accommodation"] += remainder
            continue
        try:
            if proof := keyed.get(payment.pk):
                expected_extras = Decimal(proof["folio_portion"])
                stay_part = Decimal(proof["stay_portion"])
                advance = Decimal(proof["unapplied_advance"])
            else:
                expected_extras = min(payment.amount, max(Decimal(metadata["folio_balance_before_payment"]), ZERO))
                stay_part = min(payment.amount - expected_extras, max(Decimal(metadata["stay_balance_before_payment"]), ZERO))
                advance = payment.amount - expected_extras - stay_part
            if not all(part.is_finite() and part >= ZERO for part in (expected_extras, stay_part, advance)):
                raise ValueError("Invalid settlement breakdown")
            if expected_extras + stay_part + advance != payment.amount:
                raise ValueError("Settlement portions do not reconcile")
        except (KeyError, InvalidOperation, TypeError, ValueError):
            if remainder:
                chunks["Guest Folio"] += remainder
            continue
        if extras != expected_extras:
            chunks.clear()
            if expected_extras:
                chunks["Guest Folio"] = expected_extras
        if stay_part:
            chunks["Accommodation"] += stay_part
        if advance:
            chunks["Guest deposit"] += advance
    return portions


def _payment_row(payment, portions, stamp, reversed_payment=False):
    collected = payment.status in COLLECTED_PAYMENT_STATUSES and payment.amount > ZERO
    breakdown = portions[payment.pk]
    return {
        "reference": payment.reference, "payment_id": payment.pk,
        "operation_reference": payment.booking.booking_reference, "booking_reference": payment.booking.booking_reference,
        "source": " + ".join(breakdown) or "Accommodation",
        "source_breakdown": [{"source": source, "amount": money(amount)} for source, amount in breakdown.items()] if collected else [],
        "guest_name": payment.booking.guest.full_name, **_room_details(payment.booking, stamp),
        "method": payment.provider, "channel": payment.channel,
        "booking_billed_amount": money(payment.booking.total_amount),
        "booking_outstanding_amount": money(payment.booking.amount_due),
        "booking_payment_status": payment.booking.payment_status,
        "external_reference": (payment.metadata or {}).get("external_payment_reference") or payment.transaction_id,
        "status": "REVERSED" if reversed_payment else payment.status,
        "entry_type": "COLLECTION" if collected else "ATTEMPT",
        "signed_amount": money(payment.amount if collected else ZERO), "amount": money(payment.amount),
        "staff_name": _operator(payment.user),
    }


def _refund_row(refund, portions, stamp, reversed_refund=False):
    sources = portions[refund.payment_id]
    return {
        "reference": refund.paystack_refund_reference or refund.paystack_refund_id or f"Refund #{refund.pk}",
        "original_reference": refund.payment.reference,
        "operation_reference": refund.booking.booking_reference, "booking_reference": refund.booking.booking_reference,
        "source": next(iter(sources)) if len(sources) == 1 else "Guest payment refund",
        "guest_name": refund.booking.guest.full_name, **_room_details(refund.booking, stamp),
        "method": refund.payment.provider, "status": "REVERSED" if reversed_refund else refund.status, "entry_type": "REFUND",
        "signed_amount": money(-refund.amount), "amount": money(refund.amount),
        "staff_name": _operator(refund.requested_by),
    }


def _journal_evidence(transactions):
    ids = set(transactions)
    evidence = {}
    order_paths = ("order__stay__booking__guest", "order__minimart_sale__booking__guest", "order__service_request__room")
    order_prefetch = ("order__lines__menu_item__category", "order__tenders") + _booking_room_lookups("order__stay__booking__")
    for t in PosTender.objects.filter(collection_transaction_id__in=ids).select_related(*order_paths, "captured_by").prefetch_related(*order_prefetch):
        evidence[t.collection_transaction_id] = t
    for r in PosOrderReturn.objects.filter(refund_transaction_id__in=ids).select_related(*order_paths, "refund_recorded_by").prefetch_related(*order_prefetch):
        evidence[r.refund_transaction_id] = r
    for e in EventPayment.objects.filter(collection_transaction_id__in=ids).select_related("event__guest", "received_by"):
        evidence[e.collection_transaction_id] = e
    for l in LaundryOrder.objects.filter(financial_transaction_id__in=ids, stay__isnull=True).order_by("pk").select_related("room", "guest"):
        evidence.setdefault(l.financial_transaction_id, l)
    return evidence


def _journal_row(tx, evidence, legacy_payment_by_reference, reversal_ids):
    basis = tx.reversal_of if tx.type == TX.VOID else tx
    record = evidence.get(basis.pk)
    meta = basis.metadata or {}
    detail = {
        "reference": tx.reference, "ledger_reference": tx.reference,
        "operation_reference": basis.source_reference, "external_reference": tx.external_reference,
        "source": "Other reception collection", "method": meta.get("method") or meta.get("provider") or "",
        "staff_name": _operator(tx.initiated_by), "guest_name": "", "room_numbers": [],
    }
    if isinstance(record, (PosTender, PosOrderReturn)):
        order = record.order
        sale = getattr(order, "minimart_sale", None)
        booking = sale.booking if sale and sale.booking_id else (order.stay.booking if order.stay_id else None)
        room = order.service_request.room if order.service_request_id else None
        if sale and sale.room_number:
            detail.update(room_numbers=sale.room_number.split(", "), room_evidence="Recorded on the Mini Mart sale")
        elif room:
            detail.update(room_numbers=[room.room_number], room_evidence="Recorded on the service request")
        elif order.stay_id:
            detail.update(_room_details(booking, basis.occurred_at))
        detail.update(source=_source(order, minimart=bool(sale)), operation_reference=order.reference,
                      booking_reference=booking.booking_reference if booking else "",
                      guest_name=order.guest_name or (booking.guest.full_name if booking else ""),
                      billed_amount=money(order.net_total), outstanding_amount=money(order_outstanding_balance(order)),
                      service_status=order.settlement_status)
        if isinstance(record, PosTender):
            detail.update(reference=record.reference, method=record.method,
                          staff_name=_operator(record.captured_by), external_reference=record.external_reference)
        else:
            detail.update(reference=record.reference, method=record.refund_method,
                          staff_name=_operator(record.refund_recorded_by), external_reference=record.refund_reference)
    elif isinstance(record, EventPayment):
        detail.update(reference=record.reference, source="Events", method=record.method,
                      operation_reference=record.event.reference, guest_name=record.event.guest.full_name,
                      external_reference=record.external_reference, staff_name=_operator(record.received_by),
                      billed_amount=money(record.event.total_charged), outstanding_amount=money(record.event.outstanding_balance))
    elif isinstance(record, LaundryOrder):
        detail.update(reference=record.reference, source="Laundry", method=record.payment_method,
                      operation_reference=record.reference, guest_name=record.customer_name,
                      room_numbers=[record.room.room_number] if record.room_id else [],
                      billed_amount=money(record.total_amount), outstanding_amount=money(ZERO))
    elif p := legacy_payment_by_reference.get(meta.get("payment_reference")):
        detail.update(reference=p.reference, source="Guest booking payment", method=p.provider,
                      booking_reference=p.booking.booking_reference, guest_name=p.booking.guest.full_name,
                      **_room_details(p.booking, basis.occurred_at))
    else:
        detail.update(guest_name=meta.get("customer_name") or "")
    if tx.type == TX.VOID:
        detail.update(original_reference=detail["reference"] if detail["reference"] != tx.reference else basis.reference,
                      reference=tx.reference, external_reference=tx.external_reference, staff_name=_operator(tx.initiated_by))
    detail.update(
        entry_type={TX.PAYMENT_COLLECTION: "COLLECTION", TX.REFUND: "REFUND", TX.VOID: "REVERSAL"}[tx.type],
        status=("REVERSED" if tx.type == TX.VOID or tx.pk in reversal_ids or tx.status != POSTED
                else "PROCESSED" if tx.type == TX.REFUND else "SUCCESS"),
        signed_amount=money(tx.signed), amount=money(abs(tx.signed)),
    )
    return detail


def list_collections(view, request):
    gross, refunds, journal = _branches(request.query_params)
    columns = ("feed_time", "feed_kind", "pk")
    feed = gross.values_list(*columns).union(refunds.values_list(*columns), journal.values_list(*columns), all=True)
    paginator = StandardPagination()
    page = list(paginator.paginate_queryset(feed.order_by("-feed_time", "feed_kind", "-pk"), request, view=view))
    ids = defaultdict(list)
    for _, kind, pk in page:
        ids[kind].append(pk)
    refund_rows = Refund.objects.filter(pk__in=ids["refund"]).select_related(
        "payment", "booking__guest", "requested_by",
    ).prefetch_related(*_booking_room_lookups("booking__")).in_bulk()
    tx_rows = journal.filter(pk__in=ids["journal"]).select_related("initiated_by", "reversal_of").in_bulk()
    native = {tx.pk: tx for tx in tx_rows.values() if not tx.reversal_of_id}
    native.update({tx.reversal_of_id: tx.reversal_of for tx in tx_rows.values() if tx.reversal_of_id})
    bridge_keys = [f"payment-collection:{reference}" for reference in
                   Payment.objects.filter(pk__in=ids["payment"]).values_list("reference", flat=True)]
    bridge_keys.extend(f"refund:{pk}" for pk in ids["refund"])
    reversed_refs = set(FinancialTransaction.objects.filter(type=TX.VOID, status=POSTED,
        reversal_of__source_key__in=bridge_keys,
    ).values_list("reversal_of__source_key", flat=True))
    reversed_ids = set(FinancialTransaction.objects.filter(type=TX.VOID, status=POSTED, reversal_of_id__in=native)
                       .values_list("reversal_of_id", flat=True))
    bridge_refs = {tx.metadata.get("payment_reference") for tx in native.values() if tx.metadata.get("payment_reference")}
    payment_rows = (Payment.objects.filter(Q(pk__in=ids["payment"] + [r.payment_id for r in refund_rows.values()])
                                           | Q(reference__in=bridge_refs))
                    .select_related("booking__guest", "user").prefetch_related(*_booking_room_lookups("booking__")).in_bulk())
    portions = _payment_sources(payment_rows)
    by_reference = {p.reference: p for p in payment_rows.values()}
    evidence = _journal_evidence(native)
    rows = []
    for stamp, kind, pk in page:
        if kind == "payment" and pk in payment_rows:
            obj = payment_rows[pk]
            row = _payment_row(obj, portions, stamp, f"payment-collection:{obj.reference}" in reversed_refs)
        elif kind == "refund" and pk in refund_rows:
            obj = refund_rows[pk]
            row = _refund_row(obj, portions, stamp, f"refund:{obj.pk}" in reversed_refs)
        elif kind == "journal" and pk in tx_rows:
            obj = tx_rows[pk]
            row = _journal_row(obj, evidence, by_reference, reversed_ids)
        else:
            continue  # source changed between the read and hydration; refresh instead of inventing a row
        rows.append({**row, "id": f"{kind}:{pk}", "currency": obj.currency, "occurred_at": stamp})
    return paginator.get_paginated_response({"results": rows, "totals": _totals(gross, refunds, journal)})
