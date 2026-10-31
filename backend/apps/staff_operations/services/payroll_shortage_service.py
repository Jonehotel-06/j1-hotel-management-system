"""Read-only payroll evidence helpers; never mutate inventory or attendance."""
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.utils import timezone
from apps.inventory.models import StockMovement
from ..models import AttendanceRecord


def validate_discovery(value):
    if value is None or timezone.is_naive(value):
        raise ValidationError({"discovered_at": "Record a valid hotel-local discovery date and time."})
    if value > timezone.now():
        raise ValidationError({"discovered_at": "Discovery time cannot be in the future."})
    return value


def eligible_staff(at, department):
    """Require both actual attendance and an uncancelled shift covering the instant.

    Shift department, not today's employee profile, establishes historical duty.
    Open attendance is bounded by the planned shift end. Exceptions fail closed.
    """
    records = AttendanceRecord.objects.select_related("staff", "shift").filter(
        clock_in_at__lte=at, shift__planned_start_at__lte=at,
        shift__planned_end_at__gt=at, staff__is_superuser=False,
    ).exclude(status="EXCEPTION").exclude(shift__status="CANCELLED").exclude(staff__role="ADMIN")
    names = {department.name.casefold(), department.code.casefold()}
    result = []
    for record in records:
        if record.clock_out_at and record.clock_out_at <= at:
            continue
        if record.shift.department.casefold() not in names:
            continue
        result.append({
            "staff_id": record.staff_id, "name": record.staff.full_name,
            "department": record.shift.department, "department_id": department.pk,
            "attendance_id": record.pk, "attendance_reference": record.reference,
            "shift_id": record.shift_id, "shift_reference": record.shift.reference,
            "clock_in_at": record.clock_in_at.isoformat(),
            "clock_out_at": record.clock_out_at.isoformat() if record.clock_out_at else None,
            "scheduled_start_at": record.shift.planned_start_at.isoformat(),
            "scheduled_end_at": record.shift.planned_end_at.isoformat(),
        })
    return result


def purchase_basis(line, at):
    """Inventory's receipt-weighted average, restricted to discovery-time evidence.

    StockCount quantities and StockMovement quantities/costs use base units already.
    No standard-cost fallback is safe for wage recovery. Snapshot every source.
    """
    receipts = list(StockMovement.objects.filter(
        type=StockMovement.Type.RECEIPT, item_id=line.item_id,
        location_id=line.stock_count.location_id, occurred_at__lte=at,
        quantity_delta__gt=0,
    ).order_by("pk"))
    if not receipts or any(row.unit_cost is None or row.unit_cost <= 0 or row.currency != line.item.currency for row in receipts):
        raise ValidationError({"stock_count_reference": f"{line.item.name}: purchase receipt cost is missing or invalid; investigate inventory before recovery."})
    quantity = sum((row.quantity_delta for row in receipts), Decimal("0"))
    value = sum((row.quantity_delta * row.unit_cost for row in receipts), Decimal("0"))
    return value / quantity, {
        "method": "receipt_weighted_average_as_of_discovery", "as_of": at.isoformat(),
        "receipt_quantity_base_units": str(quantity), "receipt_value": str(value),
        "movements": [{"id": row.pk, "quantity": str(row.quantity_delta), "unit_cost": str(row.unit_cost)} for row in receipts],
    }
