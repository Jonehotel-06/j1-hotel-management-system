"""Locked, source-keyed stock ledger operations and procurement workflow."""
from __future__ import annotations

import hashlib
import logging
import secrets
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F, Q, Sum
from django.utils import timezone

from apps.accounts.capabilities import has_capability
from apps.audit.services import log_action
from apps.core.approvals import override_event_details, require_independent_approver
from apps.core.utils import hotel_today

from ..models import (
    GoodsReceipt, GoodsReceiptLine, PurchaseOrder, PurchaseOrderLine, PurchaseOrderPayment,
    StockBalance, StockConsumptionRequest, StockConsumptionRequestEvent, StockCount, StockCountEvent,
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
    if len(set(item_ids)) != len(item_ids):
        raise ValidationError({"lines": "An item may appear only once per purchase order."})
    items = {item.pk: item for item in StockItem.objects.select_for_update().filter(pk__in=item_ids, is_active=True)}
    if len(items) != len(item_ids):
        raise ValidationError({"lines": "One or more stock items are inactive or missing."})
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


@transaction.atomic
def create_purchase_order(*, supplier, delivery_location, lines, requested_by, expected_on=None, notes="", supplier_reference="", idempotency_key=None):
    if supplier.status != Supplier.Status.ACTIVE:
        raise ValidationError("Purchase orders require an active supplier.")
    if not delivery_location.is_active:
        raise ValidationError("Purchase orders require an active delivery location.")
    idempotency_key = str(idempotency_key or "").strip() or None
    if idempotency_key:
        existing = PurchaseOrder.objects.select_for_update().filter(idempotency_key=idempotency_key).first()
        if existing: return existing, False
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
                if existing: return existing, False
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
        PurchaseOrder.Status.PARTIALLY_RECEIVED, PurchaseOrder.Status.RECEIVED,
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

    if (po.total_paid + amount) >= total:
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


@transaction.atomic
def mark_purchase_order_ordered(*, purchase_order, actor):
    po = PurchaseOrder.objects.select_for_update().get(pk=purchase_order.pk)
    if po.status == PurchaseOrder.Status.ORDERED: return po
    if po.status != PurchaseOrder.Status.APPROVED: raise ValidationError(f"Purchase order cannot be ordered from {po.status}.")
    po.status = PurchaseOrder.Status.ORDERED; po.ordered_at = timezone.now(); po.save(update_fields=["status", "ordered_at", "updated_at"])
    return po


@transaction.atomic
def receive_goods(*, purchase_order, receipt_lines, actor, source_key, received_on=None, supplier_delivery_reference="", notes=""):
    """Receive bounded PO quantities and append stock receipts exactly once."""
    po = PurchaseOrder.objects.select_for_update().select_related("supplier", "delivery_location").get(pk=purchase_order.pk)
    if po.status not in {PurchaseOrder.Status.APPROVED, PurchaseOrder.Status.ORDERED, PurchaseOrder.Status.PARTIALLY_RECEIVED}:
        raise ValidationError(f"Goods cannot be received for purchase order status {po.status}.")
    source_key = str(source_key or "").strip()
    if not source_key: raise ValidationError("A stable receipt source key is required.")
    existing = GoodsReceipt.objects.select_for_update().filter(source_key=source_key).first()
    if existing: return existing, False
    raw_lines = list(receipt_lines or ())
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
            if existing: return existing, False
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
    # Import locally to keep the hot stock path's imports simple.
    if all_received:
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
def record_opening_stock(*, item, location, quantity, actor, source_key="", idempotency_key="", unit_cost=None, notes=""):
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
