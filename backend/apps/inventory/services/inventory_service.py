"""Locked, source-keyed stock ledger operations and procurement workflow."""
from __future__ import annotations

import hashlib
import logging
import secrets
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, F, Q, Sum
from django.utils import timezone

from apps.accounts.capabilities import has_capability
from apps.audit.services import log_action
from apps.core.approvals import override_event_details, require_independent_approver
from apps.core.utils import hotel_today

from ..models import (
    GoodsReceipt, GoodsReceiptLine, PurchaseOrder, PurchaseOrderLine, PurchaseOrderPayment,
    StockBalance, StockBalanceAdjustment, StockConsumptionRequest, StockConsumptionRequestEvent, StockCount, StockCountEvent,
    StockCountLine, StockDepartment, StockItem, StockLocation, StockMovement, StockRecipe,
    StockRecipeLine, Supplier,
)

logger = logging.getLogger("apps")

QTY = Decimal("0.0001")
MONEY = Decimal("0.01")

# Who/what caused a POS hand-off to be posted. Recorded as append-only evidence
# on StockConsumptionRequestEvent so an automatic depletion can always be told
# apart from a manual inventory action during an audit.
TRIGGER_POS_DELIVERY = "POS_DELIVERY"
TRIGGER_MANUAL = "MANUAL"
TRIGGER_CATCH_UP = "CATCH_UP"

#: Marks a deduction that comes from an explicit direct-sale configuration on
#: the stock item rather than from a StockRecipe. Recorded inside the frozen
#: hand-off snapshot and (through it) in the movement metadata, so a reader can
#: always tell which rule depleted the stock — and, crucially, that no recipe
#: row was invented to do it.
DIRECT_SALE_SOURCE = "DIRECT_SALE"


def _quantity(value, *, field="quantity", positive=False):
    try:
        amount = Decimal(str(value)).quantize(QTY, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError({field: "Enter a valid quantity to four decimal places."}) from exc
    if positive and amount <= Decimal("0.0000"):
        raise ValidationError({field: "Quantity must be greater than zero."})
    if not positive and amount == Decimal("0.0000"):
        raise ValidationError({field: "Quantity delta cannot be zero."})
    return amount


def _cost(value, *, field="unit_cost"):
    try:
        amount = Decimal(str(value)).quantize(MONEY, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError({field: "Enter a valid unit cost."}) from exc
    if amount < Decimal("0.00"):
        raise ValidationError({field: "Unit cost cannot be negative."})
    return amount


def _reference(prefix):
    return f"{prefix}-{timezone.now():%Y%m%d}-{secrets.token_hex(6).upper()}"


def scoped_key(*, prefix, scope, raw_key):
    raw_key = str(raw_key or "").strip()
    if not raw_key:
        return None
    if len(raw_key) > 128:
        raise ValidationError({"idempotency_key": "Idempotency key must be at most 128 characters."})
    return f"{prefix}:{hashlib.sha256(f'{scope}:{raw_key}'.encode()).hexdigest()}"


def _locked_balance(*, item, location):
    """Serialize a projection row even when this is its first movement."""
    StockItem.objects.select_for_update().get(pk=item.pk)
    StockLocation.objects.select_for_update().get(pk=location.pk)
    balance = StockBalance.objects.select_for_update().filter(item=item, location=location).first()
    if balance:
        return balance
    try:
        with transaction.atomic():
            return StockBalance.objects.create(item=item, location=location)
    except IntegrityError:
        return StockBalance.objects.select_for_update().get(item=item, location=location)


@transaction.atomic
def record_stock_movement(
    *, item, location, movement_type, quantity_delta, source_key, actor=None,
    unit_cost=None, source_reference="", notes="", metadata=None, allow_negative=False,
    department="", recipient="",
) -> tuple[StockMovement, bool]:
    """Append an immutable movement and update the locked balance projection."""
    if movement_type not in StockMovement.Type.values:
        raise ValidationError({"type": "Invalid stock movement type."})
    source_key = str(source_key or "").strip()
    if not source_key:
        raise ValidationError("A stable stock movement source key is required.")
    existing = StockMovement.objects.select_for_update().filter(source_key=source_key).first()
    if existing:
        return existing, False
    item = StockItem.objects.select_for_update().get(pk=item.pk)
    location = StockLocation.objects.select_for_update().get(pk=location.pk)
    if not item.is_active:
        raise ValidationError("Inactive stock items cannot receive new movements.")
    if not location.is_active:
        raise ValidationError("Inactive stock locations cannot receive new movements.")
    quantity_delta = _quantity(quantity_delta, field="quantity_delta")
    balance = _locked_balance(item=item, location=location)
    new_quantity = (Decimal(balance.quantity_on_hand) + quantity_delta).quantize(QTY)
    if new_quantity < Decimal("0.0000") and not allow_negative:
        raise ValidationError({"quantity_delta": "Movement would make on-hand stock negative."})
    for _ in range(8):
        try:
            with transaction.atomic():
                movement = StockMovement.objects.create(
                    reference=_reference("STM"), source_key=source_key, item=item, location=location,
                    balance=balance, type=movement_type, quantity_delta=quantity_delta,
                    unit_cost=_cost(unit_cost) if unit_cost is not None else None,
                    currency=item.currency.upper(), source_reference=(source_reference or "")[:120],
                    notes=(notes or "")[:500], department=_department(department),
                    recipient=str(recipient or "").strip()[:120],
                    metadata=dict(metadata or {}), actor=actor,
                )
            break
        except IntegrityError:
            existing = StockMovement.objects.select_for_update().filter(source_key=source_key).first()
            if existing:
                return existing, False
    else:
        raise ValidationError("Could not allocate a unique stock movement reference; please retry.")
    balance.quantity_on_hand = new_quantity
    balance.last_movement_at = movement.occurred_at
    balance.save(update_fields=["quantity_on_hand", "last_movement_at", "updated_at"])
    return movement, True


def _department(value, *, field="department"):
    """Validate a receiving department against the shared vocabulary."""
    code = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
    if not code:
        return ""
    if code not in StockDepartment.values:
        raise ValidationError({
            field: f"'{value}' is not a known department. Use one of: "
                   f"{', '.join(row.value for row in StockDepartment)}."
        })
    return code


@transaction.atomic
def issue_stock(
    *, item, location, quantity, actor, source_key, notes="", source_reference="",
    movement_type=StockMovement.Type.ISSUE, department="", recipient="",
):
    """Issue stock out of a store to a department, with custody evidence.

    An internal issue is not a sale and not a new cost: the goods already
    entered stock through a goods receipt, so this records *who took what, from
    which store, for which department, why and when* on the immutable ledger
    (``actor`` issues, ``occurred_at`` dates, ``notes`` carries the reason) and
    nothing else. No journal entry, no POS revenue and no supplier payment is
    created here, so the department's consumption can never inflate expenses
    twice.
    """
    if movement_type not in {StockMovement.Type.ISSUE, StockMovement.Type.MAINTENANCE_ISSUE, StockMovement.Type.WRITE_OFF}:
        raise ValidationError("This issue operation only supports controlled outgoing stock movement types.")
    quantity = _quantity(quantity, positive=True)
    department = _department(department)
    recipient = str(recipient or "").strip()[:120]
    if item.is_reusable_asset and not recipient:
        # A towel or a plate leaving the store is custody, not consumption: the
        # item must come back, so the person holding it has to be named.
        raise ValidationError({
            "recipient": f"{item.name} is a reusable asset. Record who is taking custody of it so it can be returned."
        })
    if item.is_reusable_asset and movement_type == StockMovement.Type.WRITE_OFF and not (notes or "").strip():
        raise ValidationError({
            "notes": "A reusable asset can only be written off with a reason. Use the notes field to say what happened."
        })
    return record_stock_movement(
        item=item, location=location, movement_type=movement_type, quantity_delta=-quantity,
        source_key=source_key, actor=actor, source_reference=source_reference, notes=notes,
        department=department, recipient=recipient,
    )


@transaction.atomic
def transfer_stock(*, item, from_location, to_location, quantity, actor, transfer_key, notes=""):
    if from_location.pk == to_location.pk:
        raise ValidationError("Stock transfer locations must differ.")
    quantity = _quantity(quantity, positive=True)
    transfer_key = str(transfer_key or "").strip()
    if not transfer_key:
        raise ValidationError("A stable transfer key is required.")
    # Lock locations consistently before both legs; the movement service locks
    # the item/balance rows and source keys make a retry converge.
    locations = sorted([from_location, to_location], key=lambda row: row.pk)
    list(StockLocation.objects.select_for_update().filter(pk__in=[row.pk for row in locations]).order_by("pk"))
    outbound, outbound_created = record_stock_movement(
        item=item, location=from_location, movement_type=StockMovement.Type.TRANSFER_OUT, quantity_delta=-quantity,
        source_key=f"transfer:{transfer_key}:out", actor=actor, source_reference=transfer_key, notes=notes,
    )
    inbound, inbound_created = record_stock_movement(
        item=item, location=to_location, movement_type=StockMovement.Type.TRANSFER_IN, quantity_delta=quantity,
        source_key=f"transfer:{transfer_key}:in", actor=actor, source_reference=transfer_key, notes=notes,
    )
    return (outbound, inbound), outbound_created or inbound_created


def _validate_po_lines(lines):
    values = list(lines or ())
    if not 1 <= len(values) <= 200:
        raise ValidationError({"lines": "A purchase order needs between 1 and 200 lines."})
    item_ids = []
    for raw in values:
        try: item_ids.append(int(raw["item_id"]))
        except (KeyError, TypeError, ValueError) as exc: raise ValidationError({"lines": "Every line needs an item_id."}) from exc
    # Name the offending item in every refusal: "one or more items" sent staff
    # hunting through a 30-line order for the row the server meant.
    known = {item.pk: item for item in StockItem.objects.filter(pk__in=set(item_ids))}
    def label(item_id):
        item = known.get(item_id)
        return item.sku if item is not None else f"item #{item_id}"
    duplicates = sorted({item_id for item_id in item_ids if item_ids.count(item_id) > 1}, key=item_ids.index)
    if duplicates:
        raise ValidationError({"lines": (
            "An item may appear only once per purchase order: "
            + ", ".join(label(item_id) for item_id in duplicates)
            + " is on more than one line. Combine the quantities into one line."
        )})
    items = {item.pk: item for item in StockItem.objects.select_for_update().filter(pk__in=item_ids, is_active=True)}
    if len(items) != len(item_ids):
        unavailable = [label(item_id) for item_id in item_ids if item_id not in items]
        raise ValidationError({"lines": (
            "One or more stock items are inactive or missing: " + ", ".join(unavailable)
            + ". Remove the line, or ask Inventory to reactivate the item."
        )})
    normalized = []
    for raw, item_id in zip(values, item_ids):
        item = items[item_id]
        normalized.append({
            "item": item,
            "quantity_ordered": _quantity(raw.get("quantity_ordered"), field="quantity_ordered", positive=True),
            "unit_cost": _cost(raw.get("unit_cost")),
            "notes": str(raw.get("notes") or "")[:500],
        })
    return normalized


def _po_replay_fingerprint(*, supplier_id, location_id, lines, expected_on, notes, supplier_reference):
    """Comparable shape of a create request, so a key replays ONE request only."""
    rows = []
    for raw in lines or ():
        try:
            rows.append((
                int(raw["item_id"]),
                _quantity(raw.get("quantity_ordered"), field="quantity_ordered", positive=True),
                _cost(raw.get("unit_cost")),
                str(raw.get("notes") or "")[:500],
            ))
        except (KeyError, TypeError, ValueError, ValidationError):
            return None  # an unreadable retry can never equal a stored order
    return (
        int(supplier_id), int(location_id), tuple(sorted(rows)), expected_on,
        str(notes or ""), str(supplier_reference or "")[:120],
    )


def _validate_po_replay(existing, *, supplier, delivery_location, lines, expected_on, notes, supplier_reference):
    """An idempotency key may only replay the request that created the order.

    Returning the stored order for a *different* payload would tell the user
    their changed draft was saved when it was not. Mirrors the goods-receipt
    rule in ``_validate_receipt_retry``.
    """
    stored = _po_replay_fingerprint(
        supplier_id=existing.supplier_id, location_id=existing.delivery_location_id,
        lines=[
            {"item_id": line.item_id, "quantity_ordered": line.quantity_ordered,
             "unit_cost": line.unit_cost, "notes": line.notes}
            for line in existing.lines.all()
        ],
        expected_on=existing.expected_on, notes=existing.notes, supplier_reference=existing.supplier_reference,
    )
    incoming = _po_replay_fingerprint(
        supplier_id=supplier.pk, location_id=delivery_location.pk, lines=lines,
        expected_on=expected_on, notes=notes, supplier_reference=supplier_reference,
    )
    if stored != incoming:
        raise ValidationError({"idempotency_key": (
            f"This form was already saved as purchase order {existing.reference} with different details, "
            f"so nothing new was created. Close this dialog and open {existing.reference} to review or edit it."
        )})


@transaction.atomic
def create_purchase_order(*, supplier, delivery_location, lines, requested_by, expected_on=None, notes="", supplier_reference="", idempotency_key=None):
    idempotency_key = str(idempotency_key or "").strip() or None
    # A retry of a request that already succeeded (lost response, timeout,
    # double submit) returns that order before any rule that may have changed
    # since — e.g. the supplier being deactivated a minute later — can turn a
    # successful save into a confusing refusal or, worse, a second draft.
    if idempotency_key:
        existing = PurchaseOrder.objects.select_for_update().filter(idempotency_key=idempotency_key).first()
        if existing:
            _validate_po_replay(existing, supplier=supplier, delivery_location=delivery_location, lines=lines,
                                expected_on=expected_on, notes=notes, supplier_reference=supplier_reference)
            return existing, False
    if supplier.status != Supplier.Status.ACTIVE:
        raise ValidationError({"supplier_id": "Purchase orders require an active supplier. Choose an active supplier."})
    if not delivery_location.is_active:
        raise ValidationError({"delivery_location_id": "Purchase orders require an active delivery location."})
    normalized = _validate_po_lines(lines)
    for _ in range(8):
        try:
            with transaction.atomic():
                po = PurchaseOrder.objects.create(
                    reference=_reference("PO"), idempotency_key=idempotency_key, supplier=supplier,
                    delivery_location=delivery_location, currency="NGN", requested_by=requested_by,
                    expected_on=expected_on, notes=(notes or ""), supplier_reference=(supplier_reference or "")[:120],
                )
            break
        except IntegrityError:
            if idempotency_key:
                existing = PurchaseOrder.objects.select_for_update().filter(idempotency_key=idempotency_key).first()
                if existing:
                    _validate_po_replay(existing, supplier=supplier, delivery_location=delivery_location, lines=lines,
                                        expected_on=expected_on, notes=notes, supplier_reference=supplier_reference)
                    return existing, False
    else:
        raise ValidationError("Could not allocate a unique purchase-order reference; please retry.")
    PurchaseOrderLine.objects.bulk_create([
        PurchaseOrderLine(purchase_order=po, item=row["item"], item_sku=row["item"].sku, item_name=row["item"].name,
                          quantity_ordered=row["quantity_ordered"], unit_cost=row["unit_cost"], notes=row["notes"])
        for row in normalized
    ])
    return po, True


def _write_approval_receipt(purchase_order, upload, actor):
    """Validate and store an invoice/receipt on a purchase order.

    The write happens through the configured storage backend (Backblaze B2 in
    production); a storage failure is turned into a clean, honest API error
    instead of a purchase order that points at a file nobody can open.
    """
    from apps.core.storage import delete_quietly, save_upload
    from apps.core.validators import validate_document_upload

    validate_document_upload(upload)
    previous = purchase_order.approval_receipt.name if purchase_order.approval_receipt else ""

    def _write():
        # ``save=False`` only assigns the name; the single UPDATE below persists
        # the file name together with its uploader/timestamp, so the row never
        # briefly points at a half-recorded receipt.
        purchase_order.approval_receipt.save(upload.name, upload, save=False)

    save_upload(_write, context=f"purchase order {purchase_order.reference} receipt")
    purchase_order.approval_receipt_uploaded_by = actor
    purchase_order.approval_receipt_uploaded_at = timezone.now()
    purchase_order.save(update_fields=[
        "approval_receipt", "approval_receipt_uploaded_by",
        "approval_receipt_uploaded_at", "updated_at",
    ])
    if previous and previous != purchase_order.approval_receipt.name:
        delete_quietly(_PreviousFile(previous, purchase_order.approval_receipt.storage))
    return purchase_order


class _PreviousFile:
    """Minimal FileField stand-in for delete_quietly()."""

    def __init__(self, name, storage):
        self.name = name
        self.storage = storage

    def __bool__(self):
        return True


@transaction.atomic
def update_purchase_order(*, purchase_order, actor, changes=None, lines=None):
    """Edit a draft/submitted purchase order before it is approved.

    Approval freezes the commercial terms: once the maker-checker step has
    happened the record must not be silently re-priced or re-scoped, so the
    service refuses any edit from APPROVED onwards. The dashboard hides the
    Edit action at the same boundary.
    """
    po = PurchaseOrder.objects.select_for_update().get(pk=purchase_order.pk)
    if po.status == PurchaseOrder.Status.CANCELLED:
        raise ValidationError("This purchase order has been cancelled and can no longer be edited. Raise a new order instead.")
    if po.status not in {PurchaseOrder.Status.DRAFT, PurchaseOrder.Status.SUBMITTED}:
        raise ValidationError(
            "This purchase order has already been approved and can no longer be edited. "
            "Raise a new order or a controlled correction instead."
        )
    if GoodsReceipt.objects.filter(purchase_order=po).exists():
        raise ValidationError("Goods have already been received against this order, so it can no longer be edited.")

    changes = dict(changes or {})
    supplier = changes.pop("supplier", None)
    location = changes.pop("delivery_location", None)
    if supplier is not None:
        if supplier.status != Supplier.Status.ACTIVE:
            raise ValidationError({"supplier_id": "Purchase orders require an active supplier."})
        po.supplier = supplier
    if location is not None:
        if not location.is_active:
            raise ValidationError({"delivery_location_id": "Purchase orders require an active delivery location."})
        po.delivery_location = location
    if "expected_on" in changes:
        po.expected_on = changes.pop("expected_on")
    if "supplier_reference" in changes:
        po.supplier_reference = str(changes.pop("supplier_reference") or "")[:120]
    if "notes" in changes:
        po.notes = str(changes.pop("notes") or "")
    if "currency" in changes:
        raise ValidationError({"currency": "The order currency cannot be changed after creation."})
    if changes:
        raise ValidationError({key: "This field cannot be edited on a purchase order." for key in changes})

    if lines is not None:
        normalized = _validate_po_lines(lines)
        existing = list(PurchaseOrderLine.objects.select_for_update().filter(purchase_order=po))
        if any(Decimal(line.quantity_received or 0) > 0 for line in existing):
            raise ValidationError("A line already has received stock, so the order can no longer be edited.")
        PurchaseOrderLine.objects.filter(purchase_order=po).delete()
        PurchaseOrderLine.objects.bulk_create([
            PurchaseOrderLine(
                purchase_order=po, item=row["item"], item_sku=row["item"].sku,
                item_name=row["item"].name, quantity_ordered=row["quantity_ordered"],
                unit_cost=row["unit_cost"], notes=row["notes"],
            )
            for row in normalized
        ])
    po.save()
    return po


@transaction.atomic
def attach_purchase_order_receipt(*, purchase_order, actor, upload, payment_reference=None):
    """Attach/replace the invoice or payment receipt on an approved order."""
    po = PurchaseOrder.objects.select_for_update().get(pk=purchase_order.pk)
    if po.status not in {
        PurchaseOrder.Status.APPROVED, PurchaseOrder.Status.ORDERED,
        PurchaseOrder.Status.PARTIALLY_RECEIVED, PurchaseOrder.Status.RECEIVED, PurchaseOrder.Status.PAID,
    }:
        raise ValidationError("Approve the purchase order before attaching a payment receipt or invoice.")
    if upload is not None:
        _write_approval_receipt(po, upload, actor)
    if payment_reference is not None:
        po.payment_reference = str(payment_reference or "")[:160]
        po.save(update_fields=["payment_reference", "updated_at"])
    return po


@transaction.atomic
def record_purchase_order_payment(
    *, purchase_order, actor, method, amount=None, reference="",
    paid_on=None, evidence_file=None, notes="",
):
    """Record real money paid to the supplier against a purchase order.

    Approval receipts only prove what was filed when the order was
    authorised; this is the money-out itself, one append-only row per
    payment, each with its own receipt or invoice evidence. The order
    moves to ``PAID`` once the recorded payments cover its total.
    """
    from apps.core.storage import delete_quietly, save_upload
    from apps.core.validators import validate_document_upload

    if not has_capability(actor, "procurement.approve"):
        raise PermissionDenied("This user cannot record supplier payments.")
    po = PurchaseOrder.objects.select_for_update().get(pk=purchase_order.pk)
    if po.status in {PurchaseOrder.Status.DRAFT, PurchaseOrder.Status.SUBMITTED}:
        raise ValidationError("Approve the purchase order before recording a supplier payment.")
    if po.status == PurchaseOrder.Status.CANCELLED:
        raise ValidationError("A cancelled purchase order cannot be paid.")

    method = str(method or "").upper()
    if method not in dict(PurchaseOrderPayment.PaymentMethod.choices):
        raise ValidationError({
            "method": "Choose how the supplier was paid: BANK_TRANSFER, CARD, CHEQUE, or CASH.",
        })
    reference = str(reference or "").strip()[:160]
    if method != PurchaseOrderPayment.PaymentMethod.CASH and not reference:
        raise ValidationError({"reference": "Enter the bank, card or cheque reference for this payment."})
    # The order row is locked above. Prevent the same non-empty reference from
    # recording the same supplier money twice on this order. This is not a
    # substitute for an idempotency key or cross-order bank reconciliation.
    if reference and po.payments.filter(reference__iexact=reference).exists():
        raise ValidationError({"reference": "This payment reference is already recorded on this purchase order."})

    paid_on = paid_on or hotel_today()
    if paid_on > hotel_today():
        raise ValidationError({"paid_on": "A supplier payment cannot be dated in the future."})

    total = po.total_amount
    outstanding = po.outstanding_amount
    if outstanding <= 0:
        raise ValidationError("This purchase order is already fully paid.")
    if amount is None:
        amount = outstanding
    amount = Decimal(amount)
    if amount is None or not amount.is_finite() or amount <= 0:
        raise ValidationError({"amount": "Enter a payment amount greater than zero."})
    amount = amount.quantize(MONEY, rounding=ROUND_HALF_UP)
    if amount <= Decimal("0.00"):
        raise ValidationError({"amount": "Enter a payment amount of at least 0.01."})
    if amount > outstanding:
        raise ValidationError({
            "amount": f"The payment cannot exceed the outstanding balance of {outstanding} {po.currency}.",
        })

    payment = PurchaseOrderPayment(
        purchase_order=po, amount=amount, method=method, reference=reference,
        paid_on=paid_on, notes=str(notes or "")[:2000],
    )
    if evidence_file is not None:
        validate_document_upload(evidence_file)

        def _write():
            # ``save=False`` only assigns the name; the single INSERT
            # below persists it together with its uploader, so the row
            # never exists pointing at a half-recorded receipt.
            payment.evidence_file.save(evidence_file.name, evidence_file, save=False)

        save_upload(_write, context=f"purchase order {po.reference} payment evidence")
        payment.evidence_uploaded_by = actor
        payment.evidence_uploaded_at = timezone.now()
    payment.save()

    # total_paid queries all persisted rows, including the payment just saved.
    if po.total_paid >= total:
        po.status = PurchaseOrder.Status.PAID
        po.paid_at = timezone.now()
        po.paid_by = actor
        po.save(update_fields=["status", "paid_at", "paid_by", "updated_at"])
    return payment


@transaction.atomic
def submit_purchase_order(*, purchase_order, actor):
    po = PurchaseOrder.objects.select_for_update().get(pk=purchase_order.pk)
    if po.status == PurchaseOrder.Status.SUBMITTED: return po
    if po.status != PurchaseOrder.Status.DRAFT: raise ValidationError(f"Purchase order cannot submit from {po.status}.")
    if not PurchaseOrderLine.objects.filter(purchase_order=po).exists(): raise ValidationError("A purchase order needs at least one line.")
    po.status = PurchaseOrder.Status.SUBMITTED; po.save(update_fields=["status", "updated_at"])
    return po


@transaction.atomic
def approve_purchase_order(*, purchase_order, actor, receipt_file=None, payment_reference=None):
    if not has_capability(actor, "procurement.approve"):
        raise PermissionDenied("This user cannot approve purchase orders.")
    po = PurchaseOrder.objects.select_for_update().get(pk=purchase_order.pk)
    if po.status == PurchaseOrder.Status.APPROVED: return po
    if po.status != PurchaseOrder.Status.SUBMITTED: raise ValidationError(f"Purchase order cannot approve from {po.status}.")
    # Single, centrally enforced maker-checker rule (apps.core.approvals). An
    # administrator may authorise their own order under the configured policy;
    # every other role still needs a different authorising account.
    override = require_independent_approver(
        requester=po.requested_by, actor=actor, workflow="purchase order",
    )
    po.status = PurchaseOrder.Status.APPROVED; po.approved_by = actor; po.approved_at = timezone.now()
    if override:
        # Keep the override visible on the record itself, not only in the audit
        # log: PO.notes is free text and is never used for control decisions.
        po.notes = (f"{po.notes}\n[{override['marker']}] {override['reason']}").strip()
        po.save(update_fields=["status", "approved_by", "approved_at", "notes", "updated_at"])
        log_action(
            actor=actor, action=override["marker"], instance=po,
            summary=f"Purchase order {po.reference} approved by its requester under administrator override",
            metadata={"reference": po.reference, **override},
        )
    else:
        po.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
    if payment_reference:
        po.payment_reference = str(payment_reference or "")[:160]
        po.save(update_fields=["payment_reference", "updated_at"])
    if receipt_file is not None:
        # Approval is the point the advance is normally paid, so the approver
        # can file the supplier invoice/payment slip in the same action.
        _write_approval_receipt(po, receipt_file, actor)
    return po


def _decision_reason(reason, *, action):
    text = " ".join(str(reason or "").split())
    if not text:
        raise ValidationError({"reason": f"Enter the reason for {action} this purchase order."})
    if len(text) > 1000:
        raise ValidationError({"reason": "Keep the reason under 1000 characters."})
    return text


def _append_control_note(po, *, marker, actor, reason):
    """Keep a workflow decision visible on the order itself (as approval overrides do).

    ``notes`` is free text that is never used for control decisions; the audit
    log written by the view remains the authoritative record.
    """
    who = getattr(actor, "email", "") or f"user #{getattr(actor, 'pk', '?')}"
    po.notes = f"{po.notes}\n[{marker} by {who} on {hotel_today().isoformat()}] {reason}".strip()


@transaction.atomic
def reject_purchase_order(*, purchase_order, actor, reason):
    """Refuse a submitted order: it returns to DRAFT with the approver's reason.

    Rejection uses the existing status set (no REJECTED status is invented):
    the order goes back to its requester as a DRAFT, which they can correct and
    resubmit, or cancel. Nothing is deleted and no approval is recorded.
    """
    if not has_capability(actor, "procurement.approve"):
        raise PermissionDenied("This user cannot reject purchase orders.")
    po = PurchaseOrder.objects.select_for_update().get(pk=purchase_order.pk)
    if po.status != PurchaseOrder.Status.SUBMITTED:
        raise ValidationError(f"Only an order awaiting approval can be rejected; this one is {po.status}.")
    reason = _decision_reason(reason, action="rejecting")
    po.status = PurchaseOrder.Status.DRAFT
    _append_control_note(po, marker="REJECTED - returned to draft", actor=actor, reason=reason)
    po.save(update_fields=["status", "notes", "updated_at"])
    return po


@transaction.atomic
def cancel_purchase_order(*, purchase_order, actor, reason):
    """Withdraw an unapproved order without deleting it (status CANCELLED).

    Only DRAFT and SUBMITTED orders can be cancelled here: from approval
    onwards the order is an authorised commitment and needs a controlled
    correction instead. Replaying a cancellation is a no-op.
    """
    if not has_capability(actor, "procurement.manage"):
        raise PermissionDenied("This user cannot cancel purchase orders.")
    po = PurchaseOrder.objects.select_for_update().get(pk=purchase_order.pk)
    if po.status == PurchaseOrder.Status.CANCELLED:
        return po
    if po.status not in {PurchaseOrder.Status.DRAFT, PurchaseOrder.Status.SUBMITTED}:
        raise ValidationError(
            f"A {po.status} purchase order is an authorised commitment and cannot be cancelled here. "
            "Raise a controlled correction instead."
        )
    if GoodsReceipt.objects.filter(purchase_order=po).exists() or po.payments.exists():
        raise ValidationError("Goods or payments are already recorded against this order, so it cannot be cancelled.")
    reason = _decision_reason(reason, action="cancelling")
    po.status = PurchaseOrder.Status.CANCELLED
    _append_control_note(po, marker="CANCELLED", actor=actor, reason=reason)
    po.save(update_fields=["status", "notes", "updated_at"])
    return po


@transaction.atomic
def mark_purchase_order_ordered(*, purchase_order, actor):
    po = PurchaseOrder.objects.select_for_update().get(pk=purchase_order.pk)
    if po.status == PurchaseOrder.Status.ORDERED: return po
    if po.status != PurchaseOrder.Status.APPROVED: raise ValidationError(f"Purchase order cannot be ordered from {po.status}.")
    po.status = PurchaseOrder.Status.ORDERED; po.ordered_at = timezone.now(); po.save(update_fields=["status", "ordered_at", "updated_at"])
    return po


def _validate_receipt_retry(existing, *, po, raw_lines, received_on, supplier_delivery_reference, notes):
    """A source key may only replay the same receipt on the same order."""
    mismatch = ValidationError({"idempotency_key": "This receipt key was already used for different receipt details."})
    if existing.purchase_order_id != po.pk or not 1 <= len(raw_lines) <= 200:
        raise mismatch
    stored = {line.purchase_order_line_id: line for line in existing.lines.select_related("purchase_order_line")}
    if len(stored) != len(raw_lines):
        raise mismatch
    seen = set()
    for raw in raw_lines:
        try:
            line_id = int(raw["purchase_order_line_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise mismatch from exc
        line = stored.get(line_id)
        if line is None or line_id in seen:
            raise mismatch
        seen.add(line_id)
        quantity = _quantity(raw.get("quantity_received"), field="quantity_received", positive=True)
        unit_cost = _cost(raw.get("unit_cost", line.purchase_order_line.unit_cost))
        if quantity != line.quantity_received or unit_cost != line.unit_cost:
            raise mismatch
    if received_on is not None and existing.received_on != received_on:
        raise mismatch
    if existing.supplier_delivery_reference != (supplier_delivery_reference or "")[:120] or existing.notes != (notes or ""):
        raise mismatch


@transaction.atomic
def receive_goods(*, purchase_order, receipt_lines, actor, source_key, received_on=None, supplier_delivery_reference="", notes=""):
    """Receive bounded PO quantities and append stock receipts exactly once."""
    po = PurchaseOrder.objects.select_for_update().select_related("supplier", "delivery_location").get(pk=purchase_order.pk)
    source_key = str(source_key or "").strip()
    if not source_key: raise ValidationError("A stable receipt source key is required.")
    raw_lines = list(receipt_lines or ())
    existing = GoodsReceipt.objects.select_for_update().filter(source_key=source_key).first()
    if existing:
        _validate_receipt_retry(existing, po=po, raw_lines=raw_lines, received_on=received_on,
                                supplier_delivery_reference=supplier_delivery_reference, notes=notes)
        return existing, False
    if po.status not in {PurchaseOrder.Status.APPROVED, PurchaseOrder.Status.ORDERED,
                         PurchaseOrder.Status.PARTIALLY_RECEIVED, PurchaseOrder.Status.PAID}:
        raise ValidationError(f"Goods cannot be received for purchase order status {po.status}.")
    if not 1 <= len(raw_lines) <= 200: raise ValidationError({"lines": "A receipt needs between 1 and 200 lines."})
    line_ids=[]
    for raw in raw_lines:
        try: line_ids.append(int(raw["purchase_order_line_id"]))
        except (KeyError, TypeError, ValueError) as exc: raise ValidationError({"lines": "Each receipt line needs purchase_order_line_id."}) from exc
    if len(set(line_ids)) != len(line_ids): raise ValidationError({"lines": "A purchase-order line may appear once per receipt."})
    po_lines = {line.pk: line for line in PurchaseOrderLine.objects.select_for_update().select_related("item").filter(purchase_order=po, pk__in=line_ids)}
    if len(po_lines) != len(line_ids): raise ValidationError({"lines": "One or more purchase-order lines do not belong to this order."})
    normalized=[]
    for raw, line_id in zip(raw_lines, line_ids):
        po_line=po_lines[line_id]; qty=_quantity(raw.get("quantity_received"), field="quantity_received", positive=True)
        if Decimal(po_line.quantity_received) + qty > Decimal(po_line.quantity_ordered):
            raise ValidationError({"lines": f"Receipt exceeds remaining quantity for {po_line.item_sku}."})
        unit_cost = _cost(raw.get("unit_cost", po_line.unit_cost))
        normalized.append((po_line, qty, unit_cost))
    for _ in range(8):
        try:
            with transaction.atomic():
                receipt=GoodsReceipt.objects.create(
                    reference=_reference("GRN"), source_key=source_key, purchase_order=po, location=po.delivery_location,
                    supplier=po.supplier, received_on=received_on or hotel_today(), supplier_delivery_reference=(supplier_delivery_reference or "")[:120],
                    received_by=actor, notes=notes or "",
                )
            break
        except IntegrityError:
            existing=GoodsReceipt.objects.select_for_update().filter(source_key=source_key).first()
            if existing:
                _validate_receipt_retry(existing, po=po, raw_lines=raw_lines, received_on=received_on,
                                        supplier_delivery_reference=supplier_delivery_reference, notes=notes)
                return existing, False
    else: raise ValidationError("Could not allocate a unique goods-receipt reference; please retry.")
    for po_line, qty, unit_cost in normalized:
        movement, _ = record_stock_movement(
            item=po_line.item, location=po.delivery_location, movement_type=StockMovement.Type.RECEIPT,
            quantity_delta=qty, source_key=f"goods-receipt:{receipt.reference}:{po_line.pk}", actor=actor,
            unit_cost=unit_cost, source_reference=receipt.reference, notes=f"Goods receipt {receipt.reference}",
            metadata={"purchase_order": po.reference, "purchase_order_line_id": po_line.pk},
        )
        GoodsReceiptLine.objects.create(goods_receipt=receipt, purchase_order_line=po_line, item=po_line.item,
                                        quantity_received=qty, unit_cost=unit_cost, stock_movement=movement)
        po_line.quantity_received = (Decimal(po_line.quantity_received) + qty).quantize(QTY)
        po_line.save(update_fields=["quantity_received"])
    all_received = not PurchaseOrderLine.objects.filter(purchase_order=po, quantity_received__lt=F("quantity_ordered")).exists()
    # Payment and physical receipt are independent. Preserve an actually paid
    # order's status when advance-paid stock arrives, including partial arrivals.
    if po.paid_at is not None and po.total_paid >= po.total_amount:
        po.status = PurchaseOrder.Status.PAID
    elif all_received:
        po.status = PurchaseOrder.Status.RECEIVED
    else:
        po.status = PurchaseOrder.Status.PARTIALLY_RECEIVED
    po.save(update_fields=["status", "updated_at"])
    return receipt, True


#: Canonical unit names. Recipe lines, issues, receipts and stock counts are
#: typed by humans ("Kg", "grams", "litres", "pcs"), so every accepted spelling
#: is mapped onto one canonical token before a factor is looked up. Spelling is
#: never a reason to accept an unknown conversion.
UNIT_ALIASES = {
    # mass
    "mg": "mg", "milligram": "mg", "milligrams": "mg", "milligramme": "mg", "milligrammes": "mg",
    "g": "g", "gram": "g", "grams": "g", "gramme": "g", "grammes": "g",
    "kg": "kg", "kilo": "kg", "kilos": "kg", "kilogram": "kg", "kilograms": "kg", "kilogramme": "kg", "kilogrammes": "kg",
    "t": "t", "tonne": "t", "tonnes": "t", "ton": "t", "tons": "t", "mt": "t",
    # volume
    "ml": "ml", "millilitre": "ml", "millilitres": "ml", "milliliter": "ml", "milliliters": "ml",
    "cl": "cl", "centilitre": "cl", "centilitres": "cl", "centiliter": "cl", "centiliters": "cl",
    "dl": "dl", "decilitre": "dl", "decilitres": "dl", "deciliter": "dl", "deciliters": "dl",
    "l": "l", "lt": "l", "ltr": "l", "litre": "l", "litres": "l", "liter": "l", "liters": "l",
    "shot": "shot",
    # loose count
    "pc": "pc", "pcs": "pc", "pce": "pc", "piece": "pc", "pieces": "pc",
    "unit": "unit", "units": "unit", "ea": "unit", "each": "unit",
    "dozen": "dozen", "dozens": "dozen", "dz": "dozen", "pair": "pair", "pairs": "pair",
    # discrete things the hotel stocks one at a time
    "bottle": "bottle", "bottles": "bottle",
    "can": "can", "cans": "can", "tin": "tin", "tins": "tin",
    "sachet": "sachet", "sachets": "sachet", "sachet(s)": "sachet",
    "plate": "plate", "plates": "plate", "glass": "glass", "glasses": "glass",
    "towel": "towel", "towels": "towel", "sheet": "sheet", "sheets": "sheet",
    # supplier containers — convertible only with the item's own pack size
    "pack": "pack", "packs": "pack", "packet": "pack", "packets": "pack",
    "carton": "carton", "cartons": "carton", "case": "case", "cases": "case",
    "crate": "crate", "crates": "crate", "bag": "bag", "bags": "bag",
    "box": "box", "boxes": "box", "sack": "sack", "sacks": "sack",
    "roll": "roll", "rolls": "roll", "bundle": "bundle", "bundles": "bundle",
}

#: Exact ratios inside a family: ``1 <unit> == ratio[unit] * <family base>``
#: (mass is expressed in grams, volume in millilitres). Every factor is a power
#: of ten — except the hotel's conventional 50 ml shot — so a conversion is an
#: exact decimal multiplication and can never introduce a rounding error.
UNIT_FAMILIES = {
    "mass": {"mg": Decimal("0.0010"), "g": Decimal("1.0000"), "kg": Decimal("1000.0000"), "t": Decimal("1000000.0000")},
    "volume": {"ml": Decimal("1.0000"), "cl": Decimal("10.0000"), "dl": Decimal("100.0000"), "l": Decimal("1000.0000"), "shot": Decimal("50.0000")},
    "count": {"pc": Decimal("1.0000"), "unit": Decimal("1.0000"), "dozen": Decimal("12.0000"), "pair": Decimal("2.0000")},
}

#: Units that describe a supplier container holding *some* number of the item's
#: own base units. How many is stored on ``StockItem.units_per_pack``: it is
#: never guessed from the name, because a wrong crate size silently posts a
#: wrong quantity to the ledger.
PACK_UNITS = {"pack", "case", "carton", "crate", "bag", "box", "sack", "roll", "bundle"}

#: Human-readable label for every canonical/legacy unit token the hotel
#: actually stocks. The ledger stores a TOKEN (``PIECE``, ``KG``, ``CRATE``);
#: a screen must never show the raw token alone, because "12 PACK" and
#: "12 PIECE" read identically to a hurried storekeeper. Unknown tokens fall
#: back to the token itself (upper-cased) so nothing is ever blanked out.
UNIT_LABELS = {
    "pc": "Pieces", "unit": "Units", "dozen": "Dozen", "pair": "Pairs",
    "bottle": "Bottles", "can": "Cans", "tin": "Tins", "sachet": "Sachets",
    "plate": "Plates", "glass": "Glasses", "towel": "Towels", "sheet": "Sheets",
    "pack": "Packs", "packet": "Packs", "case": "Cases", "carton": "Cartons",
    "crate": "Crates", "bag": "Bags", "box": "Boxes", "sack": "Sacks",
    "roll": "Rolls", "bundle": "Bundles",
    "mg": "Milligrams (mg)", "g": "Grams (g)", "kg": "Kilograms (kg)", "t": "Tonnes (t)",
    "ml": "Millilitres (ml)", "cl": "Centilitres (cl)", "dl": "Decilitres (dl)",
    "l": "Litres (L)", "shot": "Shots",
}

#: The largest quantity the ledger column can hold (max_digits=14,
#: decimal_places=4). Anything at or above this must be refused with a field
#: error instead of surfacing as a database error.
MAX_LEDGER_QUANTITY = Decimal("10000000000")


def unit_label(unit) -> str:
    """Display label for a stock unit token ("KG" → "Kilograms (kg)")."""
    token = canonical_unit(unit)
    if not token:
        return ""
    return UNIT_LABELS.get(token, token.upper())


def is_container_unit(unit) -> bool:
    """True for a supplier container (pack/carton/crate/bag/…) token."""
    return canonical_unit(unit) in PACK_UNITS


def canonical_unit(unit) -> str:
    """Map "Kg" / "kilograms" / "KG." onto one canonical token ("" when blank)."""
    token = str(unit or "").strip().lower().rstrip(".")
    if not token:
        return ""
    return UNIT_ALIASES.get(token, token)


def _family_member(unit):
    """``(family, ratio)`` for a unit inside one family, else ``(None, None)``.

    The family matters as much as the ratio: kilograms and litres both have a
    ratio but they are not convertible, so a conversion is only allowed when
    both units belong to the *same* family.
    """
    for family, ratios in UNIT_FAMILIES.items():
        if unit in ratios:
            return family, ratios[unit]
    return None, None


def convert_stock_quantity(quantity, *, from_unit=None, to_unit=None, conversion_factor=None, item=None) -> Decimal:
    """Convert a typed quantity into a stock item's base unit, exactly.

    Every path either produces an exact :class:`~decimal.Decimal` result or
    raises: an unknown unit pair is never passed through unchanged, because a
    silent identity conversion is how a recipe for 250 g of rice ends up
    deducting 250 kg.

    * an explicit ``conversion_factor`` (the caller's own declaration) wins;
    * no unit typed, or the same unit on both sides, is an exact identity;
    * mass and volume convert inside their family at exact ratios, so
      ``kg -> g`` and ``l -> ml`` are loss-free;
    * loosed counts (pieces/units/dozen/pair) convert at exact ratios;
    * a supplier container (pack/carton/crate/...) converts only when the item
      is supplied and its ``units_per_pack`` gives the container size;
    * anything else raises ``ValidationError`` naming both units.
    """
    amount = _quantity(quantity, field="quantity", positive=True)
    if conversion_factor is not None and str(conversion_factor).strip() != "":
        factor = _quantity(conversion_factor, field="conversion_factor", positive=True)
        return (amount * factor).quantize(QTY, rounding=ROUND_HALF_UP)
    src = canonical_unit(from_unit)
    dst = canonical_unit(to_unit)
    if not src or not dst or src == dst:
        return amount.quantize(QTY, rounding=ROUND_HALF_UP)
    src_family, src_factor = _family_member(src)
    dst_family, dst_factor = _family_member(dst)
    if src_family and src_family == dst_family:
        return (amount * src_factor / dst_factor).quantize(QTY, rounding=ROUND_HALF_UP)
    if item is not None and getattr(item, "units_per_pack", None):
        base_unit = canonical_unit(getattr(item, "base_unit", ""))
        pack_size = _quantity(item.units_per_pack, field="units_per_pack", positive=True)
        if src in PACK_UNITS and dst == base_unit:
            return (amount * pack_size).quantize(QTY, rounding=ROUND_HALF_UP)
        if dst in PACK_UNITS and src == base_unit:
            return (amount / pack_size).quantize(QTY, rounding=ROUND_HALF_UP)
    raise ValidationError({
        "unit": (
            f"'{from_unit}' cannot be converted to '{to_unit}'. Record this line in the item's own "
            "unit, set the item's pieces-per-pack when one unit is a pack/carton/crate, or supply "
            "an explicit conversion factor."
        )
    })


def resolve_entered_quantity(
    quantity, *, item, unit=None, conversion_factor=None, field="quantity",
    allow_declared_pack_size=False,
) -> tuple[Decimal, dict]:
    """Turn a quantity typed in ``unit`` into the item's own base unit.

    One shared implementation for every path that records stock from a typed
    quantity (opening balance, replenishment, adjustments). It returns the
    exact base-unit quantity plus the evidence a reader needs to reconcile it
    later (what was typed, in which unit, and the factor used).

    Nothing is ever guessed:

    * the base unit is an identity and refuses a conversion factor;
    * mass and volume convert inside their own family at their exact ratios,
      and a factor that CONTRADICTS the exact ratio is refused (a "kg" line
      with a factor of 100 is neither 100 g nor 1 kg, so it is not recorded);
    * a supplier container (crate/carton/bag/…) converts with the item's
      declared ``units_per_pack`` only when the caller says that is allowed —
      otherwise the operator must state the container size, because one
      item's pack size never proves which container was delivered;
    * a stated factor that disagrees with the item's declared pack size is
      refused: the same crate cannot hold 24 bottles and 12 bottles.
    """
    unit_token = canonical_unit(unit)
    base_token = canonical_unit(getattr(item, "base_unit", ""))
    factor = None
    if conversion_factor is not None and str(conversion_factor).strip() != "":
        factor = _quantity(conversion_factor, field="conversion_factor", positive=True)
    if factor is not None and unit_token and unit_token == base_token:
        raise ValidationError({
            "conversion_factor": (
                f"The quantity is already in the item's base unit ({unit_label(item.base_unit)}); "
                "remove the conversion factor."
            )
        })
    src_family, src_ratio = _family_member(unit_token)
    dst_family, dst_ratio = _family_member(base_token)
    declared_pack = getattr(item, "units_per_pack", None)
    if factor is not None:
        if src_family and src_family == dst_family:
            exact = (src_ratio / dst_ratio).quantize(QTY, rounding=ROUND_HALF_UP)
            if factor != exact:
                raise ValidationError({
                    "conversion_factor": (
                        f"1 {unit_label(unit)} is exactly {exact.normalize()} "
                        f"{unit_label(item.base_unit)}; a factor of {factor.normalize()} would record the wrong quantity. "
                        "Remove the factor to use the exact conversion."
                    )
                })
        if declared_pack and unit_token in PACK_UNITS and base_token not in PACK_UNITS:
            declared = _quantity(declared_pack, field="units_per_pack", positive=True)
            if factor != declared:
                raise ValidationError({
                    "conversion_factor": (
                        f"{item.sku} records {declared.normalize()} {unit_label(item.base_unit)} per "
                        f"{unit_label(unit).lower()}; a factor of {factor.normalize()} contradicts the item's own pack size."
                    )
                })
    elif unit_token and unit_token in PACK_UNITS and base_token not in PACK_UNITS:
        if not (allow_declared_pack_size and declared_pack):
            raise ValidationError({
                "unit": (
                    f"Enter how many {unit_label(item.base_unit).lower()} one {unit_label(unit).lower()} holds "
                    "for this item (the base units in one unit), or record the quantity in "
                    f"{unit_label(item.base_unit).lower()}."
                )
            })
    if factor is None and "shot" in {unit_token, base_token} and unit_token != base_token:
        raise ValidationError({
            "conversion_factor": "Enter the actual conversion for a shot; its size is not assumed."
        })
    base_quantity = convert_stock_quantity(
        quantity, from_unit=unit, to_unit=getattr(item, "base_unit", ""),
        conversion_factor=factor,
        item=item if allow_declared_pack_size else None,
    )
    if base_quantity <= 0 or base_quantity >= MAX_LEDGER_QUANTITY:
        raise ValidationError({
            field: "The converted quantity must fit the stock ledger and be greater than zero."
        })
    evidence = {
        "entered_quantity": str(quantity),
        "entered_unit": unit or getattr(item, "base_unit", ""),
        "base_unit": getattr(item, "base_unit", ""),
        "conversion_factor": str(factor) if factor is not None else None,
        "converted_quantity": str(base_quantity),
    }
    return base_quantity, evidence


@transaction.atomic
def replenish_stock_item(
    *, item, location, quantity, actor, unit=None, conversion_factor=None,
    unit_cost=None, supplier_reference="", notes="", idempotency_key="",
) -> tuple[StockMovement, bool]:
    """Add stock to an EXISTING item without touching its master data.

    This is the replenishment counterpart of item creation: the SKU, name,
    category, base unit, barcode and selling references belong to the existing
    record and are never rewritten here. The ledger entry is an ordinary
    ``RECEIPT`` movement, so receiving stock is additive, auditable and
    idempotent — exactly like a goods receipt — and a reorder can never
    increase stock on its own.
    """
    item = StockItem.objects.select_for_update().select_related("preferred_supplier").get(pk=item.pk)
    location = StockLocation.objects.select_for_update().get(pk=location.pk)
    if not item.is_active:
        raise ValidationError({"item_id": "Inactive stock items cannot be replenished. Reactivate the item first."})
    if not location.is_active:
        raise ValidationError({"location_id": "Choose an active stock location."})
    base_quantity, evidence = resolve_entered_quantity(
        quantity, item=item, unit=unit, conversion_factor=conversion_factor,
        allow_declared_pack_size=True,
    )
    cost = _cost(unit_cost) if unit_cost not in (None, "") else item.standard_cost
    raw_key = str(idempotency_key or "").strip()
    key = scoped_key(prefix="stock-replenish", scope=f"staff:{getattr(actor, 'pk', 'system')}", raw_key=raw_key)
    if key is None:
        key = f"stock-replenish:{item.pk}:{location.pk}:{timezone.now():%Y%m%d%H%M%S%f}"
    movement, created = record_stock_movement(
        item=item, location=location, movement_type=StockMovement.Type.RECEIPT,
        quantity_delta=base_quantity, source_key=key, actor=actor, unit_cost=cost,
        source_reference=(supplier_reference or "STOCK_REPLENISHMENT")[:120],
        notes=notes or "Stock replenishment of an existing item", metadata=evidence,
    )
    return movement, created


@transaction.atomic
def adjust_stock_balance(
    *, balance, new_quantity, reason, actor, notes="", idempotency_key="",
) -> tuple[StockBalanceAdjustment, bool]:
    """Correct stock at hand, with the correction recorded as evidence.

    An administrator states the ACTUAL counted quantity; the service posts the
    difference as an immutable ``ADJUSTMENT`` movement, so the projection moves
    by the delta (history is never rewritten) and the previous quantity, the
    new quantity, the administrator, the UTC timestamp and the reason are kept
    in :class:`StockBalanceAdjustment`. The whole operation is one transaction:
    a ledger entry without its reason — or a reason without its ledger entry —
    cannot exist.
    """
    balance = StockBalance.objects.select_for_update().select_related("item", "location").get(pk=balance.pk)
    item, location = balance.item, balance.location
    # A retried request (lost reply, double submit) must return the correction
    # it already made, not a confusing "nothing changed" refusal. The key is
    # therefore resolved BEFORE the rules that the first, successful call has
    # already changed the world to satisfy.
    raw_key = str(idempotency_key or "").strip()
    key = scoped_key(prefix="stock-adjust", scope=f"balance:{balance.pk}", raw_key=raw_key)
    if key is not None:
        replayed = (
            StockBalanceAdjustment.objects.select_related("item", "location", "movement", "actor")
            .filter(movement__source_key=key).first()
        )
        if replayed is not None:
            return replayed, False
    if not item.is_active:
        raise ValidationError({"item_id": "Inactive stock items cannot be adjusted."})
    if not location.is_active:
        raise ValidationError({"location_id": "Inactive stock locations cannot be adjusted."})
    try:
        corrected = Decimal(str(new_quantity)).quantize(QTY, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError({"quantity": "Enter a valid corrected quantity (up to four decimal places)."}) from exc
    if corrected < Decimal("0.0000"):
        raise ValidationError({"quantity": "The corrected quantity cannot be negative."})
    if corrected >= MAX_LEDGER_QUANTITY:
        raise ValidationError({"quantity": "The corrected quantity is too large to record."})
    reason = str(reason or "").strip()
    if len(reason) < 5:
        raise ValidationError({"reason": "Give the reason for this correction (at least 5 characters)."})
    previous = Decimal(balance.quantity_on_hand).quantize(QTY)
    delta = (corrected - previous).quantize(QTY)
    if delta == Decimal("0.0000"):
        raise ValidationError({
            "quantity": "The corrected quantity matches the current balance, so no adjustment was recorded."
        })
    if key is None:
        key = f"stock-adjust:{balance.pk}:{timezone.now():%Y%m%d%H%M%S%f}"
    evidence = {
        "previous_quantity": str(previous),
        "new_quantity": str(corrected),
        "quantity_delta": str(delta),
        "base_unit": item.base_unit,
        "reason": reason,
    }
    movement, created = record_stock_movement(
        item=item, location=location, movement_type=StockMovement.Type.ADJUSTMENT,
        quantity_delta=delta, source_key=key, actor=actor,
        # An upward correction of physically found stock is valued at the
        # item's own standard cost; a downward one reduces value at the same
        # basis, so the projection never invents a cost.
        unit_cost=item.standard_cost,
        source_reference="ADMIN_BALANCE_ADJUSTMENT",
        notes=(reason or "")[:500], metadata=evidence,
    )
    if not created:
        existing = StockBalanceAdjustment.objects.filter(movement=movement).first()
        if existing is None:
            raise ValidationError({
                "idempotency_key": "This adjustment key was already used for a different correction."
            })
        return existing, False
    adjustment = StockBalanceAdjustment.objects.create(
        balance=balance, item=item, location=location, previous_quantity=previous,
        new_quantity=corrected, quantity_delta=delta, base_unit=item.base_unit,
        reason=reason, notes=notes or "", movement=movement, actor=actor,
    )
    return adjustment, True


def _active_recipe_snapshots(menu_item_ids):
    """Return the current active, serializable deduction mapping for menu IDs.

    The caller freezes this mapping into a hand-off or its resolution evidence;
    it must never rely on a mutable recipe after a stock movement is posted.

    Two — and only two — things may deduct stock for a POS menu item:

    1. a real, active :class:`StockRecipe` (COMPONENTS with BOM lines, or an
       explicit NO_STOCK decision), or
    2. an explicit direct-sale configuration on the ``StockItem`` itself
       (``direct_sale_menu_item`` + ``direct_sale_location`` +
       ``direct_sale_quantity``), which is represented here as a synthetic
       snapshot and **creates no recipe row at all**.

    A menu item whose SKU or name merely resembles a stock item is deliberately
    left unresolved: the hand-off is then parked for review with its evidence,
    and an operator either configures the direct sale or writes the recipe.
    Nothing is inferred, and no existing recipe is ever replaced or removed.
    """
    ids = sorted({int(value) for value in menu_item_ids if value is not None})
    if not ids:
        return {}
    recipes = (
        StockRecipe.objects.filter(menu_item_id__in=ids, is_active=True)
        .select_related("location", "menu_item")
        .prefetch_related("lines__item")
        .order_by("menu_item_id", "-version", "-pk")
    )
    snapshots = {}
    for recipe in recipes:
        key = str(recipe.menu_item_id)
        # Service creation guarantees one active recipe; choosing the newest
        # defensively makes an old imported duplicate deterministic.
        if key in snapshots:
            continue
        snapshots[key] = {
            "recipe_reference": recipe.reference,
            "recipe_version": recipe.version,
            "menu_item_id": recipe.menu_item_id,
            "location_id": recipe.location_id,
            "location_code": recipe.location.code,
            "tracking_mode": recipe.tracking_mode,
            "components": [
                {
                    "item_id": line.item_id,
                    "item_sku": line.item.sku,
                    "quantity_per_menu_unit": str(line.quantity_per_menu_unit),
                }
                for line in recipe.lines.all()
            ],
        }
    missing_ids = [mid for mid in ids if str(mid) not in snapshots]
    if missing_ids:
        snapshots.update(_direct_sale_snapshots(missing_ids))
    return snapshots


def _direct_sale_snapshots(menu_item_ids):
    """Explicitly configured direct-sale products, resolved without a recipe.

    A bar bottle, beer crate or pack of water that is sold exactly as it is
    stocked is deducted because somebody *configured* it: the stock item names
    the POS menu item it is sold as, the store it is drawn from, and how many
    base units one menu unit consumes. Nothing is created, guessed or linked
    here — a configured item keeps its configuration until an authorised user
    changes it, and an unconfigured item reports a reviewable parked hand-off
    instead of silently inventing a 1:1 recipe.
    """
    ids = sorted({int(value) for value in menu_item_ids if value is not None})
    if not ids:
        return {}
    candidates = (
        StockItem.objects.filter(
            is_active=True,
            direct_sale_menu_item_id__in=ids,
            direct_sale_location__isnull=False,
            direct_sale_location__is_active=True,
        )
        .select_related("direct_sale_menu_item", "direct_sale_location")
        .order_by("pk")
    )
    snapshots = {}
    for item in candidates:
        menu_item_id = item.direct_sale_menu_item_id
        if str(menu_item_id) in snapshots:
            # The unique constraint forbids a second stock item claiming the
            # same menu item; stay deterministic for an imported database that
            # predates it rather than deducting the same sale twice.
            continue
        quantity = item.direct_sale_quantity or Decimal("0.0000")
        if quantity <= Decimal("0.0000"):
            continue
        snapshots[str(menu_item_id)] = {
            # No recipe exists for this deduction; these three keys stay so a
            # reader of the evidence can tell the two sources apart.
            "source": DIRECT_SALE_SOURCE,
            "direct_sale": True,
            "recipe_reference": "",
            "recipe_version": 0,
            "menu_item_id": menu_item_id,
            "location_id": item.direct_sale_location_id,
            "location_code": item.direct_sale_location.code,
            # COMPONENTS keeps every downstream consumer (all-or-nothing
            # shortage check, per-item movements, reversal) working unchanged.
            "tracking_mode": StockRecipe.TrackingMode.COMPONENTS,
            "stock_item_id": item.pk,
            "stock_item_sku": item.sku,
            "components": [
                {
                    "item_id": item.pk,
                    "item_sku": item.sku,
                    "quantity_per_menu_unit": str(quantity.quantize(QTY, rounding=ROUND_HALF_UP)),
                }
            ],
        }
    return snapshots


def pos_stock_rules(menu_item_ids, *, strict=False):
    """One authoritative stock rule per POS menu item, for validation/display.

    This is the POS-facing resolver built on exactly the same two configuration
    sources the delivery-time deduction uses (``_active_recipe_snapshots``):
    an active :class:`StockRecipe`, or an explicit direct-sale configuration on
    the ``StockItem``. Nothing here deducts, reserves, or creates anything.

    Returns ``{menu_item_id: rule}`` where each rule carries:

    * ``tracked`` — a resolvable COMPONENTS mapping exists (stock must be
      validated before the item is accepted into an order);
    * ``no_stock`` — an explicitly non-stock-tracked recipe (never validated);
    * ``source`` — ``"recipe"`` / ``"direct_sale"`` / ``""``;
    * ``location_id`` / ``location_code`` / ``location_name`` — the store the
      sale is fulfilled from (identical to the store the delivery-time
      deduction posts its movement against);
    * ``components`` — ``[{item_id, item_sku, item_name, unit,
      quantity_per_menu_unit}]`` for active component stock items;
    * ``unconfigured`` + ``unconfigured_reason`` — the product is claimed for
      stock tracking (a direct-sale link exists, or an active recipe exists)
      but its mapping is invalid: inactive stock item, missing/inactive store,
      zero per-sale quantity, or a recipe with no usable components. Such a
      product must never be silently treated as available.

    A menu item with **no** stock configuration at all (no recipe and no
    direct-sale link) resolves to an untracked rule — the existing, deliberate
    behaviour (the sale is allowed and the delivery hand-off is parked for
    inventory review); POS stock rules are not imposed on it.
    """
    ids = sorted({int(value) for value in menu_item_ids if value is not None})
    if not ids:
        return {}
    snapshots = _active_recipe_snapshots(ids)
    ambiguous = set()
    if strict:
        # Some imported databases can contain several active recipe versions;
        # the legacy resolver chooses one deterministically, but a guest order
        # must not guess. Nor should a recipe and direct-sale claim compete.
        recipe_counts = {
            row["menu_item_id"]: row["count"]
            for row in StockRecipe.objects.filter(menu_item_id__in=ids, is_active=True)
            .values("menu_item_id").annotate(count=Count("pk"))
        }
        claim_counts = {
            row["direct_sale_menu_item_id"]: row["count"]
            for row in StockItem.objects.filter(direct_sale_menu_item_id__in=ids)
            .values("direct_sale_menu_item_id").annotate(count=Count("pk"))
        }
        ambiguous = {
            menu_id for menu_id in ids
            if recipe_counts.get(menu_id, 0) > 1 or claim_counts.get(menu_id, 0) > 1
            or (recipe_counts.get(menu_id, 0) and claim_counts.get(menu_id, 0))
        }
    # Every stock item that explicitly claims one of these menu items as a
    # direct sale, regardless of validity: a claim that did not resolve into a
    # usable snapshot above is a broken stock configuration, not an untracked
    # product, and must be reported instead of silently passing.
    claims = {
        row.direct_sale_menu_item_id: row
        for row in StockItem.objects.filter(direct_sale_menu_item_id__in=ids).select_related(
            "direct_sale_location"
        )
    }
    location_ids = {
        int(snapshot["location_id"])
        for snapshot in snapshots.values()
        if snapshot.get("location_id") is not None
    }
    component_ids = {
        int(component["item_id"])
        for snapshot in snapshots.values()
        for component in (snapshot.get("components") or ())
    }
    for claim in claims.values():
        if claim.direct_sale_location_id:
            location_ids.add(claim.direct_sale_location_id)
    locations = {row.pk: row for row in StockLocation.objects.filter(pk__in=location_ids)}
    component_items = {row.pk: row for row in StockItem.objects.filter(pk__in=component_ids)}

    rules = {}
    for menu_item_id in ids:
        snapshot = snapshots.get(str(menu_item_id))
        rule = {
            "tracked": False,
            "no_stock": False,
            "source": "",
            "recipe_reference": "",
            "location_id": None,
            "location_code": "",
            "location_name": "",
            "components": [],
            "unconfigured": False,
            "unconfigured_reason": "",
        }
        if menu_item_id in ambiguous:
            rule["unconfigured"] = True
            rule["unconfigured_reason"] = "multiple active physical stock mappings need inventory review"
            rules[menu_item_id] = rule
            continue
        if snapshot is None:
            claim = claims.get(menu_item_id)
            if claim is not None:
                rule["unconfigured"] = True
                rule["source"] = "direct_sale"
                if not claim.is_active:
                    rule["unconfigured_reason"] = (
                        f"its stock item {claim.sku} is inactive"
                    )
                elif not claim.direct_sale_location_id:
                    rule["unconfigured_reason"] = "no stock location is configured for it"
                else:
                    location = locations.get(claim.direct_sale_location_id)
                    if location is None:
                        rule["unconfigured_reason"] = "its stock location no longer exists"
                    elif not location.is_active:
                        rule["unconfigured_reason"] = (
                            f"its stock location {location.name} is inactive"
                        )
                    elif not (claim.direct_sale_quantity or Decimal("0.0000")) > Decimal("0.0000"):
                        rule["unconfigured_reason"] = "its per-sale stock quantity is not configured"
                    else:  # pragma: no cover - a valid claim always resolves above
                        rule["unconfigured"] = False
            rules[menu_item_id] = rule
            continue
        rule["source"] = "direct_sale" if snapshot.get("direct_sale") else "recipe"
        rule["recipe_reference"] = snapshot.get("recipe_reference", "")
        location = locations.get(int(snapshot.get("location_id") or 0))
        if location is not None:
            rule["location_id"] = location.pk
            rule["location_code"] = location.code
            rule["location_name"] = location.name
        if snapshot.get("tracking_mode") == StockRecipe.TrackingMode.NO_STOCK:
            rule["no_stock"] = True
            rules[menu_item_id] = rule
            continue
        components = list(snapshot.get("components") or ())
        if not components:
            rule["unconfigured"] = True
            rule["unconfigured_reason"] = (
                f"its stock recipe {rule['recipe_reference']} has no components"
            )
            rules[menu_item_id] = rule
            continue
        if location is None or not location.is_active:
            rule["unconfigured"] = True
            rule["unconfigured_reason"] = (
                f"its stock location {rule['location_code'] or rule['location_id']} is missing or inactive"
            )
            rules[menu_item_id] = rule
            continue
        resolved_components = []
        unusable = []
        for component in components:
            item = component_items.get(int(component["item_id"]))
            if item is None or not item.is_active or Decimal(str(component["quantity_per_menu_unit"])) <= Decimal("0"):
                unusable.append(str(component.get("item_sku") or component.get("item_id")))
                continue
            resolved_components.append({
                "item_id": item.pk,
                "item_sku": item.sku,
                "item_name": item.name,
                "unit": item.base_unit,
                "quantity_per_menu_unit": Decimal(str(component["quantity_per_menu_unit"])),
            })
        if unusable:
            rule["unconfigured"] = True
            rule["unconfigured_reason"] = (
                "its recipe references an inactive stock item or an invalid quantity ("
                + ", ".join(sorted(set(unusable))) + ")"
            )
            rules[menu_item_id] = rule
            continue
        rule["tracked"] = True
        rule["components"] = resolved_components
        rules[menu_item_id] = rule
    return rules


def _open_pos_order_statuses():
    """POS order statuses whose lines still hold stock for an undelivered sale.

    Imported lazily to keep ``inventory`` free of a module-level dependency on
    ``pos``. The tuple is asserted against the delivery-time authority
    (``apps.pos.services.order_service._STOCK_COMMITTED_STATUSES``) by the test
    suite so the two can never drift.
    """
    from apps.pos.models import PosOrder

    return (
        PosOrder.Status.DRAFT, PosOrder.Status.SUBMITTED, PosOrder.Status.PREPARING, PosOrder.Status.READY,
    )


def open_pos_order_demand(required_keys, *, exclude_order=None):
    """Base-unit demand already committed by OPEN POS orders, per stock key.

    The single implementation of "stock other orders have already promised" for
    both the order-entry validator (``apps.pos.services.order_service``) and the
    menu availability projection below, so a displayed quantity and the
    quantity the validator will actually accept are computed from one rule.

    ``required_keys`` iterates ``(stock_item_id, location_id)`` pairs. Demand is
    resolved through the same recipe/direct-sale mapping the deduction uses, so
    a dish whose recipe shares rice with another dish reduces rice's
    availability for both. Delivered orders are excluded: their deduction is
    already in the balance, and cancelled/voided orders never deduct.

    Callers that need a stable answer must hold the balance row locks before
    calling (the POS order validator does); readers that only display the number
    may call it without locks.
    """
    from apps.pos.models import PosOrderLine

    keys = {key for key in required_keys}
    if not keys:
        return {}
    lines = PosOrderLine.objects.filter(order__status__in=_open_pos_order_statuses())
    if exclude_order is not None:
        lines = lines.exclude(order=exclude_order)
    rows = list(lines.values("menu_item_id", "quantity", "order__metadata"))
    if not rows:
        return {}
    rules = pos_stock_rules({
        int(row["menu_item_id"]) for row in rows
        if row["menu_item_id"] is not None and (row["order__metadata"] or {}).get("portal_stock_rules") is None
    })
    demand = {}
    for row in rows:
        metadata = row["order__metadata"] or {}
        frozen = metadata.get("portal_stock_rules")
        if row["menu_item_id"] is None:
            if frozen is not None:
                # An imported/deleted menu item can null the FK. Never discard
                # its still-open physical obligation from other orders' demand.
                raise ValidationError("An open guest order has invalid stock evidence; contact inventory.")
            continue  # legacy non-portal free-text line
        menu_item_id, quantity = int(row["menu_item_id"]), int(row["quantity"])
        if frozen is not None:
            # Portal orders keep their original physical commitment even when
            # an inventory operator subsequently replaces a recipe. Failing
            # closed on corrupt evidence is safer than silently dropping it.
            rule = frozen.get(str(menu_item_id))
            if not rule or not rule.get("location_id") or not rule.get("components"):
                raise ValidationError("An open guest order has invalid stock evidence; contact inventory.")
            components = rule["components"]
        else:
            rule = rules.get(menu_item_id)
            if not rule or not rule["tracked"]:
                continue
            components = rule["components"]
        for component in components:
            try:
                key = (int(component["item_id"]), int(rule["location_id"]))
                per_unit = Decimal(str(component["quantity_per_menu_unit"]))
                if per_unit <= 0:
                    raise ValueError("non-positive stock quantity")
            except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
                raise ValidationError("An open guest order has invalid stock evidence; contact inventory.") from exc
            if key not in keys:
                continue
            amount = (per_unit * quantity).quantize(QTY)
            demand[key] = (demand.get(key, Decimal("0.0000")) + amount).quantize(QTY)
    return demand


def pos_stock_summaries(menu_item_ids, *, include_open_order_demand=True, strict=False):
    """Bulk, read-only stock summaries for the POS order-entry interfaces.

    One shared implementation for the POS menu payload and the barcode lookup:
    every figure is computed from the same recipe/direct-sale resolution the
    delivery-time deduction uses, so the location shown here is exactly the
    store the backend deducts from. Balances are read WITHOUT locks (display
    only — order validation locks its own rows).

    Returns ``{menu_item_id: summary}`` with the keys
    ``tracked / no_stock / unconfigured / unconfigured_reason / location_code /
    location_name / saleable_quantity / sufficient_for_one / stock_unit /
    available_quantity / available_sufficient_for_one / committed_quantity /
    availability_unit / components``.

    Two different quantities are reported and they must not be confused:

    * ``saleable_quantity`` — how many whole menu units the STORE's physical
      balance can still produce (on-hand only);
    * ``available_quantity`` — how many the order validator will ACCEPT right
      now, i.e. the same figure minus stock already promised to other open
      (draft/submitted/preparing/ready) POS orders, resolved through
      :func:`open_pos_order_demand`. This is the number the POS must show,
      because it is the number the next order will be measured against.

    ``stock_unit`` is the limiting component's base unit for single-component
    (direct-sale) products and stays empty for mixed-unit recipes, matching the
    existing API contract; ``availability_unit`` is the word the UI should use
    for a menu unit — ``"portion"`` for a recipe-based prepared food, the stock
    item's base unit for a direct-sale product.

    Every figure here is a snapshot: a concurrent order, a goods receipt or a
    correction in the inventory console can change it one moment later, so it
    is display guidance only. Submission-time validation in
    ``apps.pos.services.order_service`` remains authoritative.
    """
    rules = pos_stock_rules(menu_item_ids, strict=strict)
    balance_keys = {
        (component["item_id"], rule["location_id"])
        for rule in rules.values()
        if rule["tracked"]
        for component in rule["components"]
    }
    balances = {}
    if balance_keys:
        for row in StockBalance.objects.filter(
            item_id__in={key[0] for key in balance_keys},
            location_id__in={key[1] for key in balance_keys},
        ):
            balances[(row.item_id, row.location_id)] = Decimal(row.quantity_on_hand)
    demand = open_pos_order_demand(balance_keys) if include_open_order_demand else {}
    summaries = {}
    for menu_item_id, rule in rules.items():
        summary = {
            "tracked": rule["tracked"],
            "no_stock": rule["no_stock"],
            "unconfigured": rule["unconfigured"],
            "unconfigured_reason": rule["unconfigured_reason"],
            "location_code": rule["location_code"],
            "location_name": rule["location_name"],
            "saleable_quantity": None,
            "sufficient_for_one": None,
            "stock_unit": "",
            "available_quantity": None,
            "available_sufficient_for_one": None,
            "committed_quantity": "0",
            "availability_unit": "",
            "components": [],
        }
        if not rule["tracked"]:
            summaries[menu_item_id] = summary
            continue
        saleable = None
        available_units = None
        limiting_unit = ""
        component_rows = []
        for component in rule["components"]:
            per_unit = component["quantity_per_menu_unit"]
            key = (component["item_id"], rule["location_id"])
            on_hand = balances.get(key, Decimal("0.0000"))
            committed = demand.get(key, Decimal("0.0000")) if include_open_order_demand else Decimal("0.0000")
            free = on_hand - committed
            if free < Decimal("0.0000"):
                free = Decimal("0.0000")
            component_saleable = (on_hand / per_unit) if per_unit > 0 else Decimal("0")
            component_available = (free / per_unit) if per_unit > 0 else Decimal("0")
            if saleable is None or component_saleable < saleable:
                saleable = component_saleable
                limiting_unit = component["unit"]
            if available_units is None or component_available < available_units:
                available_units = component_available
            component_rows.append({
                "item_id": component["item_id"],
                "item_sku": component["item_sku"],
                "item_name": component["item_name"],
                "unit": component["unit"],
                "quantity_per_menu_unit": str(per_unit),
                "quantity_on_hand": str(on_hand),
                "quantity_committed": str(committed.quantize(QTY)),
                "quantity_available": str(free.quantize(QTY)),
            })
        whole_units = int(saleable) if saleable is not None else 0
        whole_available = int(available_units) if available_units is not None else 0
        single_component = len(rule["components"]) == 1
        summary.update({
            "saleable_quantity": str(whole_units),
            "sufficient_for_one": whole_units >= 1,
            "stock_unit": limiting_unit if single_component else "",
            "available_quantity": str(whole_available),
            "available_sufficient_for_one": whole_available >= 1,
            # Whole menu units of THIS product already promised to open orders.
            "committed_quantity": str(max(whole_units - whole_available, 0)),
            # The unit a menu unit is counted in: the stock item's own base unit
            # for a direct-sale product, "portion" for a prepared recipe (whose
            # ingredients are measured in several different units at once).
            "availability_unit": limiting_unit if single_component else "portion",
            "components": component_rows,
        })
        summaries[menu_item_id] = summary
    return summaries


def _consumption_event(*, request, event_type, actor=None, details=None):
    return StockConsumptionRequestEvent.objects.create(
        request=request, type=event_type, actor=actor, details=dict(details or {}),
    )


@transaction.atomic
def create_stock_recipe(*, menu_item, location, tracking_mode, components, actor, idempotency_key, notes=""):
    """Create a new immutable-version BOM and retire its prior active version."""
    if tracking_mode not in StockRecipe.TrackingMode.values:
        raise ValidationError({"tracking_mode": "Invalid recipe tracking mode."})
    if not location.is_active:
        raise ValidationError("An active stock location is required for a recipe.")
    idempotency_key = str(idempotency_key or "").strip()
    if not idempotency_key:
        raise ValidationError({"idempotency_key": "A recipe idempotency key is required."})
    existing = StockRecipe.objects.select_for_update().filter(idempotency_key=idempotency_key).first()
    if existing:
        return existing, False

    # Lock the menu row even when it has no current recipe to serialize version
    # allocation and active-version retirement between concurrent requests.
    from apps.pos.models import MenuItem
    menu_item = MenuItem.objects.select_for_update().get(pk=menu_item.pk)
    raw_components = list(components or ())
    if tracking_mode == StockRecipe.TrackingMode.COMPONENTS and not raw_components:
        raise ValidationError({"components": "A component-tracked recipe needs at least one BOM line."})
    if tracking_mode == StockRecipe.TrackingMode.NO_STOCK and raw_components:
        raise ValidationError({"components": "An explicitly non-stock recipe cannot have BOM lines."})
    if len(raw_components) > 100:
        raise ValidationError({"components": "A recipe may have at most 100 BOM lines."})
    item_ids = []
    for raw in raw_components:
        try:
            item_ids.append(int(raw["item_id"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError({"components": "Every BOM line needs an item_id."}) from exc
    if len(set(item_ids)) != len(item_ids):
        raise ValidationError({"components": "A stock item may appear once per recipe."})
    items = {item.pk: item for item in StockItem.objects.select_for_update().filter(pk__in=item_ids, is_active=True)}
    if len(items) != len(item_ids):
        raise ValidationError({"components": "One or more stock items are inactive or missing."})
    normalized = []
    for raw, item_id in zip(raw_components, item_ids):
        component = items[item_id]
        entered_quantity = _quantity(
            raw.get("quantity_per_menu_unit"), field="quantity_per_menu_unit", positive=True
        )
        entered_unit = str(raw.get("unit") or "").strip()[:30]
        # The stored deduction is always the component's own base unit (250 g of
        # rice becomes 0.25 KG), computed with exact decimal maths; the typed
        # value is kept beside it as evidence and is never used to deduct.
        normalized.append((
            component,
            convert_stock_quantity(
                entered_quantity,
                from_unit=entered_unit,
                to_unit=component.base_unit,
                conversion_factor=raw.get("conversion_factor"),
                item=component,
            ),
            entered_quantity,
            entered_unit,
        ))
    prior = list(StockRecipe.objects.select_for_update().filter(menu_item=menu_item).order_by("-version", "-pk"))
    version = (prior[0].version if prior else 0) + 1
    for active in (row for row in prior if row.is_active):
        active.is_active = False; active.retired_at = timezone.now(); active.retired_by = actor
        active.save(update_fields=["is_active", "retired_at", "retired_by", "updated_at"])
    for _ in range(8):
        try:
            with transaction.atomic():
                recipe = StockRecipe.objects.create(
                    reference=_reference("BOM"), idempotency_key=idempotency_key, menu_item=menu_item,
                    location=location, version=version, tracking_mode=tracking_mode, created_by=actor, notes=notes or "",
                )
            break
        except IntegrityError:
            existing = StockRecipe.objects.select_for_update().filter(idempotency_key=idempotency_key).first()
            if existing:
                return existing, False
    else:
        raise ValidationError("Could not allocate a unique recipe reference; please retry.")
    StockRecipeLine.objects.bulk_create([
        StockRecipeLine(
            recipe=recipe, item=item, quantity_per_menu_unit=quantity,
            entered_quantity=entered_quantity, entered_unit=entered_unit,
        )
        for item, quantity, entered_quantity, entered_unit in normalized
    ])
    return recipe, True


@transaction.atomic
def retire_stock_recipe(*, recipe, actor):
    # Same menu-row lock as recipe creation and portal stock validation: a
    # recipe cannot disappear between checking and freezing its components.
    from apps.pos.models import MenuItem

    menu_item_id = StockRecipe.objects.values_list("menu_item_id", flat=True).get(pk=recipe.pk)
    MenuItem.objects.select_for_update().get(pk=menu_item_id)
    recipe = StockRecipe.objects.select_for_update().get(pk=recipe.pk)
    if not recipe.is_active:
        return recipe
    recipe.is_active = False; recipe.retired_at = timezone.now(); recipe.retired_by = actor
    recipe.save(update_fields=["is_active", "retired_at", "retired_by", "updated_at"])
    return recipe


@transaction.atomic
def request_pos_stock_consumption(*, order, actor=None):
    """Persist an idempotent POS hand-off with its best available BOM snapshot."""
    source_key = f"pos-stock-consumption:{order.reference}"
    existing = StockConsumptionRequest.objects.select_for_update().filter(source_key=source_key).first()
    if existing:
        return existing, False
    lines = list(order.lines.order_by("pk").values(
        "menu_item_id", "item_name", "item_sku", "quantity", "unit_price", "modifier_total", "line_total", "modifiers_snapshot"
    ))
    recipes = _active_recipe_snapshots([line["menu_item_id"] for line in lines])
    for _ in range(8):
        try:
            with transaction.atomic():
                request = StockConsumptionRequest.objects.create(
                    reference=_reference("SCR"), source_key=source_key, pos_order=order,
                    payload={
                        "order_reference": order.reference,
                        "mode": order.mode,
                        "delivered_at": order.delivered_at.isoformat() if order.delivered_at else None,
                        "recipe_snapshot": recipes,
                        "lines": [
                            {
                                **line,
                                "unit_price": str(line["unit_price"]),
                                "modifier_total": str(line["modifier_total"]),
                                "line_total": str(line["line_total"]),
                            }
                            for line in lines
                        ],
                    },
                )
                _consumption_event(request=request, event_type=StockConsumptionRequestEvent.Type.CREATED, actor=actor,
                                   details={"recipe_snapshot_count": len(recipes), "line_count": len(lines)})
            return request, True
        except IntegrityError:
            existing = StockConsumptionRequest.objects.select_for_update().filter(source_key=source_key).first()
            if existing:
                return existing, False
    raise ValidationError("Could not allocate a POS stock-consumption request; please retry.")


@transaction.atomic
def process_pos_stock_consumption(*, request, actor, trigger=TRIGGER_MANUAL):
    """Inventory-only, idempotent posting of POS_CONSUMPTION ledger movements.

    Two different situations are deliberately kept apart:

    * a menu item with **no** configured deduction (no recipe and no
      direct-sale configuration) is recorded as ``uncontrolled`` evidence and
      deducts nothing. Nothing is guessed at, and it does not stop the lines
      that *are* configured from following their sale either. A hand-off where
      nothing at all could be deducted is parked ``FAILED`` for inventory
      review — the gap is visible, the catch-up sweep keeps offering it, and
      configuring the product later resolves it; and
    * an **invalid** recipe, an inactive item/location or a stock shortage
      fails the whole request with review evidence *before* anything is posted,
      so a partial deduction is never written to the ledger.

    Each aggregate movement has a deterministic request/location/item source
    key, so a retried processing command converges safely.

    ``trigger`` records WHERE the resolution was initiated (POS delivery,
    inventory console, or the catch-up sweep) as append-only evidence.
    """
    request = StockConsumptionRequest.objects.select_for_update().select_related("pos_order").get(pk=request.pk)
    if request.status in {StockConsumptionRequest.Status.PROCESSED, StockConsumptionRequest.Status.SKIPPED}:
        return request, False
    raw_lines = list((request.payload or {}).get("lines") or ())
    if not raw_lines:
        request.status = StockConsumptionRequest.Status.FAILED
        request.resolution_note = "The POS hand-off has no lines to resolve."
        request.save(update_fields=["status", "resolution_note", "updated_at"])
        _consumption_event(request=request, event_type=StockConsumptionRequestEvent.Type.FAILED, actor=actor,
                           details={"reason": "empty_lines", "trigger": trigger})
        return request, False
    snapshot_recipes = dict((request.payload or {}).get("recipe_snapshot") or {})
    needed_menu_ids = []
    for raw in raw_lines:
        if raw.get("menu_item_id") is not None:
            try: needed_menu_ids.append(int(raw["menu_item_id"]))
            except (TypeError, ValueError): pass
    fallback_recipes = _active_recipe_snapshots(needed_menu_ids)
    resolved = {}
    missing = []
    #: Menu items nothing is configured for. They are NOT deducted — an
    #: unconfigured product must never be guessed at — but they must not stop
    #: the lines that *are* configured from following their sale either, so
    #: they are reported as "not deducted" evidence instead of blocking the
    #: whole order's stock accuracy.
    uncontrolled = []
    aggregates = {}
    for raw in raw_lines:
        try:
            menu_item_id = int(raw["menu_item_id"])
            quantity = _quantity(raw["quantity"], field="POS line quantity", positive=True)
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            raise ValidationError("The POS hand-off includes an invalid immutable line snapshot.") from exc
        recipe = snapshot_recipes.get(str(menu_item_id)) or fallback_recipes.get(str(menu_item_id))
        if not recipe:
            uncontrolled.append(str(raw.get("item_sku") or raw.get("item_name") or menu_item_id))
            continue
        if recipe.get("tracking_mode") == StockRecipe.TrackingMode.NO_STOCK:
            resolved[str(menu_item_id)] = recipe
            continue
        components = list(recipe.get("components") or ())
        if recipe.get("tracking_mode") != StockRecipe.TrackingMode.COMPONENTS or not components:
            missing.append(f"invalid recipe for {raw.get('item_sku') or raw.get('item_name') or menu_item_id}")
            continue
        try:
            location_id = int(recipe["location_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError("The frozen recipe location is invalid.") from exc
        resolved[str(menu_item_id)] = recipe
        for component in components:
            try:
                item_id = int(component["item_id"])
                amount = _quantity(component["quantity_per_menu_unit"], field="recipe component", positive=True) * quantity
            except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
                raise ValidationError("The frozen recipe component is invalid.") from exc
            key = (item_id, location_id)
            aggregates[key] = (aggregates.get(key, Decimal("0.0000")) + amount).quantize(QTY)
    if missing:
        request.status = StockConsumptionRequest.Status.FAILED
        request.resolution_note = (
            "No configured stock deduction for: " + ", ".join(sorted(set(missing)))[:1700]
            + ". Define a stock recipe for the menu item, or configure the stock item as a direct sale, "
              "then process this hand-off again."
        )[:2000]
        request.resolution_payload = {"resolved_recipes": resolved, "missing": sorted(set(missing)), "trigger": trigger}
        request.save(update_fields=["status", "resolution_note", "resolution_payload", "updated_at"])
        _consumption_event(request=request, event_type=StockConsumptionRequestEvent.Type.FAILED, actor=actor,
                           details={"missing": sorted(set(missing)), "trigger": trigger})
        return request, False
    items = {item.pk: item for item in StockItem.objects.filter(pk__in=[key[0] for key in aggregates], is_active=True)}
    locations = {location.pk: location for location in StockLocation.objects.filter(pk__in=[key[1] for key in aggregates], is_active=True)}
    if len(items) != len({key[0] for key in aggregates}) or len(locations) != len({key[1] for key in aggregates}):
        raise ValidationError("A frozen recipe references an inactive or removed stock item/location.")
    # All-or-nothing stock validation, performed under the same row locks the
    # movements will take. A shortage must never post a partial deduction and
    # must never drive a balance negative: it is parked for inventory review and
    # completes automatically once a goods receipt restores the stock.
    shortages = []
    for (item_id, location_id), quantity in sorted(aggregates.items()):
        balance = _locked_balance(item=items[item_id], location=locations[location_id])
        available = Decimal(balance.quantity_on_hand)
        if available < quantity:
            shortages.append({
                "item": items[item_id].sku,
                "location": locations[location_id].code,
                "required": str(quantity),
                "available": str(available.quantize(QTY)),
            })
    if shortages:
        request.status = StockConsumptionRequest.Status.FAILED
        request.resolution_note = ("Insufficient stock to consume for " + ", ".join(
            f"{row['item']}@{row['location']} (need {row['required']}, have {row['available']})" for row in shortages
        ))[:2000]
        request.resolution_payload = {"resolved_recipes": resolved, "shortages": shortages, "trigger": trigger}
        request.save(update_fields=["status", "resolution_note", "resolution_payload", "updated_at"])
        _consumption_event(request=request, event_type=StockConsumptionRequestEvent.Type.FAILED, actor=actor,
                           details={"reason": "insufficient_stock", "shortages": shortages, "trigger": trigger})
        return request, False
    for (item_id, location_id), quantity in sorted(aggregates.items()):
        recipe_evidence = [recipe for recipe in resolved.values() if recipe.get("location_id") == location_id and recipe.get("tracking_mode") == StockRecipe.TrackingMode.COMPONENTS]
        record_stock_movement(
            item=items[item_id], location=locations[location_id], movement_type=StockMovement.Type.POS_CONSUMPTION,
            quantity_delta=-quantity, source_key=f"pos-consumption:{request.reference}:{location_id}:{item_id}", actor=actor,
            unit_cost=items[item_id].standard_cost, source_reference=request.pos_order.reference,
            notes=f"POS consumption for {request.pos_order.reference}",
            metadata={"stock_consumption_request": request.reference, "pos_order": request.pos_order.reference,
                      "resolved_recipes": recipe_evidence, "trigger": trigger},
        )
    has_components = bool(aggregates)
    if has_components:
        request.status = StockConsumptionRequest.Status.PROCESSED
    elif uncontrolled:
        # Nothing was deducted and nothing was wrong enough to raise on its own:
        # the sale is complete but no deduction is configured for it. That is a
        # configuration gap only a human can close, so the hand-off is parked in
        # the review queue — visible in the console, and re-offered by the
        # catch-up sweep — instead of reporting that the items are deliberately
        # not stock-tracked. No quantity is guessed and no recipe is created.
        request.status = StockConsumptionRequest.Status.FAILED
    else:
        request.status = StockConsumptionRequest.Status.SKIPPED
    request.processed_by = actor; request.processed_at = timezone.now()
    if has_components:
        note = "POS recipe consumption posted."
        if uncontrolled:
            # Stated, not hidden: the sale went through, these lines have no
            # configured deduction, and the inventory console shows this note.
            note = (
                f"{note} Not deducted — no stock recipe and no direct-sale configuration for: "
                + ", ".join(sorted(set(uncontrolled)))[:1200]
            )
    elif uncontrolled:
        note = (
            "No stock deduction is configured for: " + ", ".join(sorted(set(uncontrolled)))[:1500]
            + ". Define a stock recipe for the menu item, or configure the stock item as a direct sale "
              "(POS menu item + store + units per menu unit), then process this hand-off again. Nothing was "
              "deducted, and no recipe was created."
        )
    else:
        note = "All menu items are explicitly non-stock tracked."
    request.resolution_note = note[:2000]
    request.resolution_payload = {
        "resolved_recipes": resolved, "movement_count": len(aggregates),
        "uncontrolled": sorted(set(uncontrolled)), "trigger": trigger,
    }
    request.save(update_fields=["status", "processed_by", "processed_at", "resolution_note", "resolution_payload", "updated_at"])
    _consumption_event(request=request,
                       event_type=(
                           StockConsumptionRequestEvent.Type.PROCESSED if has_components
                           else StockConsumptionRequestEvent.Type.FAILED if uncontrolled
                           else StockConsumptionRequestEvent.Type.SKIPPED
                       ),
                       actor=actor,
                       details={"movement_count": len(aggregates), "uncontrolled": sorted(set(uncontrolled)), "trigger": trigger})
    return request, True


@transaction.atomic
def consume_stock_for_delivered_order(*, order, actor=None):
    """Deplete stock for a just-delivered POS order, exactly once.

    This is the function every stock-consuming sales channel must reach when
    its order is finalised. It performs the durable hand-off and then resolves
    it immediately, so the ``Stock on Hand`` balance follows the sale instead of
    waiting for a human to press a button in the inventory console.

    Guarantees:

    * Idempotent — the hand-off is source-keyed on the POS order reference and
      every resulting movement is source-keyed on the hand-off, so a repeated
      delivery, a retried request or a duplicate HTTP call converges on the
      same single deduction.
    * Atomic — the deduction commits with the delivery, or not at all.
    * Never blocks a completed sale — an inventory configuration problem
      (missing recipe, inactive item, insufficient stock) leaves a durable
      ``PENDING``/``FAILED`` hand-off carrying review evidence instead of
      rolling back a delivery that has already been charged to the guest.
    """
    from apps.inventory.models import StockConsumptionRequest as _Request

    request, _created = request_pos_stock_consumption(order=order, actor=actor)
    if request.status in {_Request.Status.PROCESSED, _Request.Status.SKIPPED}:
        return request, False
    try:
        return process_pos_stock_consumption(request=request, actor=actor, trigger=TRIGGER_POS_DELIVERY)
    except Exception:  # noqa: BLE001 - the hand-off is the durable record
        logger.exception(
            "POS stock consumption %s for order %s could not be posted; left for inventory review",
            request.reference, order.reference,
        )
        return request, False


@transaction.atomic
def process_pending_pos_consumption(*, limit=200, actor=None):
    """Catch-up sweep for hand-offs that were never resolved.

    Covers orders delivered before automatic depletion existed, orders whose
    recipe/stock configuration has since been corrected, and any hand-off that
    a transient failure left behind. Every attempt is idempotent, so running
    this repeatedly can never double-deduct.
    """
    try:
        limit = max(1, min(int(limit), 1000))
    except (TypeError, ValueError):
        limit = 200
    pending = list(
        StockConsumptionRequest.objects.select_related("pos_order")
        .filter(status__in=[StockConsumptionRequest.Status.PENDING, StockConsumptionRequest.Status.FAILED])
        .order_by("requested_at", "pk")[:limit]
    )
    posted = failed = 0
    for request in pending:
        try:
            resolved, created = process_pos_stock_consumption(
                request=request, actor=actor, trigger=TRIGGER_CATCH_UP,
            )
        except Exception:  # noqa: BLE001 - one bad hand-off must not stop the sweep
            logger.exception("Catch-up stock consumption failed for %s", request.reference)
            failed += 1
            continue
        if created and resolved.status == StockConsumptionRequest.Status.PROCESSED:
            posted += 1
        else:
            failed += 1
    return {"examined": len(pending), "posted": posted, "still_open": failed}


@transaction.atomic
def reverse_pos_stock_consumption(*, request_or_order=None, pos_order=None, order=None, actor=None, reason=""):
    """Idempotently reverse posted POS_CONSUMPTION movements for a voided/returned order."""
    target = request_or_order or pos_order or order
    if isinstance(target, StockConsumptionRequest):
        req = StockConsumptionRequest.objects.select_for_update().select_related("pos_order").get(pk=target.pk)
    else:
        req = (
            StockConsumptionRequest.objects.select_for_update()
            .select_related("pos_order")
            .filter(pos_order=target)
            .first()
        )
        if req is None:
            return None, False
    payload = dict(req.resolution_payload or {})
    if payload.get("reversed"):
        return req, False
    if req.status != StockConsumptionRequest.Status.PROCESSED:
        return req, False
    movements = list(
        StockMovement.objects.select_related("item", "location")
        .filter(
            type=StockMovement.Type.POS_CONSUMPTION,
            source_key__startswith=f"pos-consumption:{req.reference}:",
        )
        .order_by("pk")
    )
    reversed_movements = []
    for mov in movements:
        restore_qty = abs(Decimal(mov.quantity_delta)).quantize(QTY)
        rev_mov, _ = record_stock_movement(
            item=mov.item,
            location=mov.location,
            movement_type=StockMovement.Type.ADJUSTMENT,
            quantity_delta=restore_qty,
            source_key=f"pos-consumption-reversal:{req.reference}:{mov.location_id}:{mov.item_id}",
            actor=actor,
            unit_cost=mov.unit_cost if mov.unit_cost is not None else mov.item.standard_cost,
            source_reference=req.pos_order.reference,
            notes=(reason or f"POS order reversal/return for {req.pos_order.reference}")[:500],
            metadata={
                "stock_consumption_request": req.reference,
                "pos_order": req.pos_order.reference,
                "reversed_movement": mov.reference,
                "reason": reason or "order_reversal",
            },
        )
        reversed_movements.append(rev_mov.reference)
    payload["reversed"] = True
    payload["reversed_at"] = timezone.now().isoformat()
    payload["reversed_movements"] = reversed_movements
    if reason:
        payload["reversal_reason"] = str(reason)[:500]
    req.resolution_payload = payload
    req.resolution_note = f"POS consumption reversed ({len(reversed_movements)} movement(s) restored)."
    req.save(update_fields=["resolution_payload", "resolution_note", "updated_at"])
    return req, True


@transaction.atomic
def restock_returned_pos_items(
    *, order, line_quantities, actor, return_reference, reason="", location=None,
):
    """Post append-only ``POS_RETURN`` movements for returned, unopened items.

    Used by the POS unopened-item return workflow. Nothing is inferred: each
    returned order line is resolved through the SAME two configuration sources
    the sale was deducted from — an active :class:`StockRecipe` (ingredients) or
    an explicit direct-sale configuration on the ``StockItem`` — and the same
    base-unit quantity the sale consumed comes back.

    Returns ``(movements, aggregates, per_line_base_quantities, skipped_reason)``:

    * ``movements`` — the ``StockMovement`` rows created (or already existing)
    * ``aggregates`` — ``{(item_id, location_id): Decimal}`` restocked base units
    * ``per_line_base_quantities`` — ``{order_line_id: Decimal}`` base units that
      came back for each returned order line (zero when that line is explicitly
      non-stock tracked), so the return evidence is line-accurate
    * ``skipped_reason`` — a non-empty explanation when nothing was restocked
      because the order's stock was never deducted (an unprocessed catch-up
      hand-off), so a return can never invent stock the hotel never issued.

    A line whose menu item has no configured deduction raises ``ValidationError``
    naming the product: silently restocking an unconfigured product, or silently
    skipping it, would both misstate the ledger.
    """
    from apps.inventory.models import StockConsumptionRequest

    line_quantities = {
        int(line_id): _quantity(quantity, field="quantity", positive=True)
        for line_id, quantity in (line_quantities or {}).items()
    }
    if not line_quantities:
        raise ValidationError({"lines": "At least one returned line quantity is required."})
    order = order.__class__.objects.select_for_update().get(pk=order.pk)
    lines = {line.pk: line for line in order.lines.select_related("menu_item").filter(pk__in=line_quantities)}
    missing = sorted(set(line_quantities) - set(lines))
    if missing:
        raise ValidationError({"lines": f"Unknown order line identifier(s): {missing}."})

    hand_off = StockConsumptionRequest.objects.filter(pos_order=order).first()
    if hand_off is None or hand_off.status != StockConsumptionRequest.Status.PROCESSED:
        return [], {}, {}, (
            "No posted stock consumption exists for this order, so nothing was deducted and "
            "nothing is restocked. Resolve the order's inventory hand-off first."
        )

    snapshots = _active_recipe_snapshots([line.menu_item_id for line in lines.values()])
    aggregates = {}
    per_line = {line_id: Decimal("0.0000") for line_id in line_quantities}
    unresolved = []
    for line_id, quantity in sorted(line_quantities.items()):
        line = lines[line_id]
        snapshot = snapshots.get(str(line.menu_item_id)) if line.menu_item_id else None
        if not snapshot:
            unresolved.append(line.item_sku or line.item_name)
            continue
        if snapshot.get("tracking_mode") == StockRecipe.TrackingMode.NO_STOCK:
            continue
        components = list(snapshot.get("components") or ())
        if not components:
            unresolved.append(line.item_sku or line.item_name)
            continue
        target_location_id = location.pk if location is not None else snapshot.get("location_id")
        for component in components:
            try:
                item_id = int(component["item_id"])
                per_unit = Decimal(str(component["quantity_per_menu_unit"]))
            except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
                raise ValidationError("The configured stock deduction for this order line is invalid.") from exc
            amount = (per_unit * quantity).quantize(QTY, rounding=ROUND_HALF_UP)
            key = (item_id, int(target_location_id))
            aggregates[key] = (aggregates.get(key, Decimal("0.0000")) + amount).quantize(QTY)
            per_line[line_id] = (per_line[line_id] + amount).quantize(QTY)
    if unresolved:
        raise ValidationError({
            "lines": (
                "These products have no configured stock deduction, so the returned quantity cannot be "
                "put back into the ledger: " + ", ".join(sorted(set(unresolved)))
                + ". Configure the stock item as a direct sale or define a stock recipe first."
            )
        })
    if not aggregates:
        return [], {}, per_line, "Every returned line is explicitly non-stock tracked; no movement was posted."

    items = {item.pk: item for item in StockItem.objects.filter(pk__in=[key[0] for key in aggregates])}
    locations = {loc.pk: loc for loc in StockLocation.objects.filter(pk__in=[key[1] for key in aggregates])}
    if len(items) != len({key[0] for key in aggregates}) or len(locations) != len({key[1] for key in aggregates}):
        raise ValidationError("A configured stock deduction references a removed stock item or location.")
    movements = []
    for (item_id, location_id), quantity in sorted(aggregates.items()):
        item = items[item_id]
        target = locations[location_id]
        movement, _created = record_stock_movement(
            item=item,
            location=target,
            movement_type=StockMovement.Type.POS_RETURN,
            quantity_delta=quantity,
            source_key=key_for_pos_return(return_reference=return_reference, location_id=location_id, item_id=item_id),
            actor=actor,
            unit_cost=item.standard_cost,
            source_reference=order.reference,
            notes=(reason or f"Unopened items returned for {order.reference}")[:500],
            metadata={
                "pos_order": order.reference,
                "pos_order_return": return_reference,
                "stock_consumption_request": hand_off.reference,
            },
        )
        movements.append(movement)
    return movements, aggregates, per_line, ""


def key_for_pos_return(*, return_reference, location_id, item_id):
    """Deterministic source key for one returned-item movement."""
    return f"pos-return:{return_reference}:{int(location_id)}:{int(item_id)}"


@transaction.atomic
def record_opening_stock(*, item, location, quantity, actor, source_key="", idempotency_key="", unit_cost=None, notes="", metadata=None):
    """Record an idempotent opening-balance stock movement."""
    key = source_key or idempotency_key
    quantity = _quantity(quantity, positive=True)
    cost = _cost(unit_cost) if unit_cost not in (None, "") else item.standard_cost
    return record_stock_movement(
        item=item,
        location=location,
        movement_type=StockMovement.Type.OPENING,
        quantity_delta=quantity,
        source_key=key,
        actor=actor,
        unit_cost=cost,
        source_reference="OPENING_BALANCE",
        notes=notes or "Opening stock balance",
        metadata=metadata,
    )


def _issue_returnable_balance(*, item, source_reference):
    """How much of a cited issue may still come back into the store.

    Returns ``None`` when the cited reference is not an issue of this item (an
    order, an invoice, a supplier return), which preserves the existing
    behaviour for every other kind of return. When it *is* an issue, the
    outstanding issued quantity is the ceiling: returning more than was issued
    would create stock the hotel never bought, and this ledger does not invent
    stock.
    """
    reference = str(source_reference or "").strip()
    if not reference:
        return None
    issue = (
        StockMovement.objects.select_for_update()
        .filter(
            Q(reference=reference) | Q(source_reference=reference),
            item=item,
            type__in=[StockMovement.Type.ISSUE, StockMovement.Type.MAINTENANCE_ISSUE],
        )
        .order_by("pk")
        .first()
    )
    if issue is None:
        return None
    candidates = {value for value in (reference, issue.reference, issue.source_reference) if value}
    issued = (-Decimal(issue.quantity_delta)).quantize(QTY, rounding=ROUND_HALF_UP)
    returned = (
        StockMovement.objects.filter(
            item=item,
            type=StockMovement.Type.MAINTENANCE_RETURN,
            source_reference__in=sorted(candidates),
        ).aggregate(total=Sum("quantity_delta"))["total"]
    ) or Decimal("0.0000")
    returned = Decimal(returned).quantize(QTY, rounding=ROUND_HALF_UP)
    return issue, issued, returned, max(issued - returned, Decimal("0.0000"))


@transaction.atomic
def record_stock_return(
    *, item, location, quantity, actor, source_key="", idempotency_key="",
    direction="IN", unit_cost=None, source_reference="", notes="", department="", recipient=""
):
    """Record an idempotent return-in or return-out stock movement.

    A return into the store may cite the issue it comes back from
    (``source_reference``); the ceiling and the return history are then written
    into the movement's evidence so the issue/return/adjustment trail of a
    reusable asset is auditable without any side table.
    """
    key = source_key or idempotency_key
    quantity = _quantity(quantity, positive=True)
    cost = _cost(unit_cost) if unit_cost not in (None, "") else item.standard_cost
    is_outward = str(direction or "IN").upper() in ("OUT", "OUTWARD", "SUPPLIER")
    evidence = None
    if not is_outward:
        limit = _issue_returnable_balance(item=item, source_reference=source_reference)
        if limit is not None:
            issue, issued, already_returned, remaining = limit
            if quantity > remaining:
                raise ValidationError({
                    "quantity": (
                        f"{item.sku} was issued {issued} against {issue.reference} and {already_returned} has "
                        f"already been returned, so at most {remaining} can be returned to the store."
                    )
                })
            evidence = {
                "returned_against_issue": issue.reference,
                "issue_source_reference": issue.source_reference,
                "issued_quantity": str(issued),
                "returned_previously": str(already_returned),
            }
    return record_stock_movement(
        item=item,
        location=location,
        movement_type=StockMovement.Type.ISSUE if is_outward else StockMovement.Type.MAINTENANCE_RETURN,
        quantity_delta=-quantity if is_outward else quantity,
        source_key=key,
        actor=actor,
        unit_cost=cost,
        source_reference=source_reference or ("SUPPLIER_RETURN" if is_outward else "STOCK_RETURN"),
        notes=notes or ("Returned stock to supplier" if is_outward else "Returned stock to store"),
        metadata=evidence,
        department=department,
        recipient=recipient,
    )


def _count_event(*, stock_count, event_type, actor=None, details=None):
    return StockCountEvent.objects.create(stock_count=stock_count, type=event_type, actor=actor, details=dict(details or {}))


@transaction.atomic
def start_stock_count(*, location, item_ids, actor, notes=""):
    """Snapshot selected item/location balances for a controlled physical count."""
    if not location.is_active:
        raise ValidationError("Stock count requires an active location.")
    try:
        item_ids = [int(value) for value in item_ids or ()]
    except (TypeError, ValueError) as exc:
        raise ValidationError({"item_ids": "Item identifiers must be integers."}) from exc
    if not 1 <= len(item_ids) <= 500 or len(set(item_ids)) != len(item_ids):
        raise ValidationError({"item_ids": "Provide 1–500 distinct active stock item identifiers."})
    items = {item.pk: item for item in StockItem.objects.select_for_update().filter(pk__in=item_ids, is_active=True)}
    if len(items) != len(item_ids):
        raise ValidationError({"item_ids": "One or more items are inactive or missing."})
    balances = {
        balance.item_id: balance
        for balance in StockBalance.objects.select_for_update().filter(location=location, item_id__in=item_ids)
    }
    stock_count = StockCount.objects.create(reference=_reference("CNT"), location=location, initiated_by=actor, notes=notes or "")
    StockCountLine.objects.bulk_create([
        StockCountLine(stock_count=stock_count, item=items[item_id], expected_quantity=balances.get(item_id).quantity_on_hand if item_id in balances else Decimal("0.0000"))
        for item_id in item_ids
    ])
    _count_event(stock_count=stock_count, event_type=StockCountEvent.Type.CREATED, actor=actor, details={"line_count": len(item_ids)})
    return stock_count


@transaction.atomic
def submit_stock_count(*, stock_count, counts, actor):
    """Freeze physical counts; nonzero variances require a separate approver."""
    stock_count = StockCount.objects.select_for_update().get(pk=stock_count.pk)
    if stock_count.status != StockCount.Status.DRAFT:
        if stock_count.status in {StockCount.Status.PENDING_APPROVAL, StockCount.Status.POSTED}:
            return stock_count
        raise ValidationError(f"Stock count cannot submit from {stock_count.status}.")
    raw_counts = list(counts or ())
    lines = {line.pk: line for line in StockCountLine.objects.select_for_update().filter(stock_count=stock_count)}
    if len(raw_counts) != len(lines):
        raise ValidationError({"lines": "A counted quantity is required for every stock-count line."})
    seen = set()
    for raw in raw_counts:
        try: line_id = int(raw["line_id"])
        except (KeyError, TypeError, ValueError) as exc: raise ValidationError({"lines": "Each count needs line_id."}) from exc
        if line_id in seen or line_id not in lines: raise ValidationError({"lines": "Count lines must each appear exactly once."})
        seen.add(line_id)
        try:
            counted = Decimal(str(raw.get("counted_quantity"))).quantize(QTY)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValidationError({"counted_quantity": "A valid counted quantity is required."}) from exc
        if counted < 0:
            raise ValidationError({"counted_quantity": "Counted quantity cannot be negative."})
        line = lines[line_id]
        line.counted_quantity = counted
        line.variance = (counted - Decimal(line.expected_quantity)).quantize(QTY)
        line.save(update_fields=["counted_quantity", "variance"])
    has_variance = StockCountLine.objects.filter(stock_count=stock_count).exclude(variance=Decimal("0.0000")).exists()
    stock_count.status = StockCount.Status.PENDING_APPROVAL if has_variance else StockCount.Status.POSTED
    stock_count.submitted_by = actor; stock_count.submitted_at = timezone.now()
    stock_count.save(update_fields=["status", "submitted_by", "submitted_at", "updated_at"])
    _count_event(stock_count=stock_count, event_type=StockCountEvent.Type.SUBMITTED, actor=actor, details={"has_variance": has_variance})
    if not has_variance:
        _count_event(stock_count=stock_count, event_type=StockCountEvent.Type.POSTED, actor=actor, details={"movement_count": 0})
    return stock_count


@transaction.atomic
def approve_stock_count(*, stock_count, actor):
    if not has_capability(actor, "inventory.adjust.approve"):
        raise PermissionDenied("This user cannot approve stock-count adjustments.")
    stock_count = StockCount.objects.select_for_update().select_related("location", "initiated_by").get(pk=stock_count.pk)
    if stock_count.status == StockCount.Status.POSTED: return stock_count
    if stock_count.status != StockCount.Status.PENDING_APPROVAL:
        raise ValidationError(f"Stock count cannot approve from {stock_count.status}.")
    override = require_independent_approver(
        requester=stock_count.initiated_by, actor=actor, workflow="stock-count variance adjustment",
    )
    lines = list(StockCountLine.objects.select_for_update().select_related("item").filter(stock_count=stock_count))
    for line in lines:
        if line.counted_quantity is None or line.variance is None:
            raise ValidationError("All stock-count lines must be submitted before approval.")
        if line.variance == Decimal("0.0000"):
            continue
        balance = _locked_balance(item=line.item, location=stock_count.location)
        if Decimal(balance.quantity_on_hand) != Decimal(line.expected_quantity):
            raise ValidationError("Stock changed since this count began; start a new count instead of applying a stale variance.")
        movement, _ = record_stock_movement(
            item=line.item, location=stock_count.location, movement_type=StockMovement.Type.ADJUSTMENT,
            quantity_delta=line.variance, source_key=f"stock-count:{stock_count.reference}:{line.pk}", actor=actor,
            source_reference=stock_count.reference, notes="Approved stock-count variance",
            metadata={"stock_count_id": stock_count.pk, "stock_count_line_id": line.pk, "expected": str(line.expected_quantity), "counted": str(line.counted_quantity)},
        )
        line.adjustment_movement = movement
        line.save(update_fields=["adjustment_movement"])
    stock_count.status = StockCount.Status.POSTED; stock_count.approved_by = actor; stock_count.approved_at = timezone.now()
    stock_count.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
    _count_event(stock_count=stock_count, event_type=StockCountEvent.Type.APPROVED, actor=actor,
                 details=override_event_details({"variance_lines": sum(1 for line in lines if line.variance)}, override))
    _count_event(stock_count=stock_count, event_type=StockCountEvent.Type.POSTED, actor=actor,
                 details=override_event_details({"movement_count": sum(1 for line in lines if line.variance)}, override))
    return stock_count
