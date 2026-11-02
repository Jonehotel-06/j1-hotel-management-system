"""Read-only legacy inventory and exception register for a supervised cutover."""
import json
from decimal import Decimal
from uuid import uuid4

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.models import Count, Exists, F, OuterRef, Q, Sum, Value
from django.db.models.functions import Concat
from django.utils import timezone

from apps.finance.models import (
    AccountingMigrationException, AccountingMigrationRun, ChartAccount, Expense,
    FinancialLine, FinancialTransaction,
)
from apps.payments.models import Payment
from apps.inventory.models import PurchaseOrderPayment

ZERO = Decimal("0.00")
SOURCE_MODELS = [
    ("bookings", "Guest"), ("bookings", "Booking"), ("bookings", "BookingRoom"),
    ("stays", "Stay"), ("stays", "StayRoom"), ("payments", "Payment"), ("payments", "Refund"),
    ("finance", "FinancialTransaction"), ("finance", "FinancialLine"), ("finance", "Folio"),
    ("finance", "FolioPosting"), ("finance", "PaymentAllocation"), ("finance", "RefundApplication"),
    ("finance", "CashSession"), ("finance", "CashMovement"), ("finance", "Expense"),
    ("finance", "ExpenseEvent"), ("finance", "FinancialEvent"), ("finance", "ApprovalRequest"),
    ("finance", "SupplierPaymentRequest"), ("finance", "SupplierSettlement"),
    ("inventory", "Supplier"), ("inventory", "PurchaseOrder"), ("inventory", "PurchaseOrderLine"),
    ("inventory", "GoodsReceipt"), ("inventory", "GoodsReceiptLine"), ("inventory", "StockMovement"),
    ("inventory", "PurchaseOrderPayment"), ("pos", "PosOrder"), ("pos", "PosTender"),
    ("pos", "PosOrderReturn"), ("staff_operations", "PayrollPeriod"), ("staff_operations", "PayrollLine"),
    ("audit", "AuditLog"),
]


def _currency_totals(queryset, amount_field="amount"):
    return [{"currency": item["currency"], "count": item["count"], "amount": str(item["amount"] or ZERO)}
            for item in queryset.values("currency").annotate(count=Count("pk"), amount=Sum(amount_field)).order_by("currency")]


def inspect(*, max_exceptions=10000, source_revision=""):
    """Inventory source tables and flag evidence gaps; never creates a journal."""
    counts = {f"{app}.{name}": apps.get_model(app, name).objects.count() for app, name in SOURCE_MODELS}
    exceptions, observed = [], 0

    def flag(model, identifier, reference, reason, **snapshot):
        nonlocal observed
        observed += 1
        if max_exceptions == 0 or len(exceptions) < max_exceptions:
            exceptions.append({"source_model": model, "source_id": str(identifier),
                               "source_reference": str(reference or "")[:160], "reason": reason,
                               "snapshot": {key: str(value) for key, value in snapshot.items()}})

    # A successful source payment must link to the existing, uniquely keyed
    # collection journal. Failure to find it is an exception, not permission to
    # invent a cash/deposit entry.
    collection = FinancialTransaction.objects.filter(
        source_key=Concat(Value("payment-collection:"), OuterRef("reference")), status="POSTED")
    missing_collections = (Payment.objects.filter(status__in=["SUCCESS", "PARTIALLY_REFUNDED", "REFUNDED"])
                           .annotate(has_collection=Exists(collection)).filter(has_collection=False))
    for payment in missing_collections.iterator(chunk_size=1000):
        flag("payments.Payment", payment.pk, payment.reference, "MISSING_COLLECTION_JOURNAL",
             amount=payment.amount, currency=payment.currency, status=payment.status, booking_id=payment.booking_id)

    balanced = FinancialTransaction.objects.filter(status="POSTED").annotate(
        debit=Sum("lines__amount", filter=Q(lines__direction="DEBIT"), default=ZERO),
        credit=Sum("lines__amount", filter=Q(lines__direction="CREDIT"), default=ZERO),
        line_count=Count("lines"),
    )
    for entry in balanced.filter(Q(line_count__lt=2) | ~Q(debit=F("credit"))).iterator():
        flag("finance.FinancialTransaction", entry.pk, entry.reference, "UNBALANCED_POSTED_JOURNAL",
             debit=entry.debit, credit=entry.credit, lines=entry.line_count)

    known_codes = set(ChartAccount.objects.values_list("code", flat=True))
    for item in (FinancialLine.objects.exclude(account_code__in=known_codes).values("account_code")
                 .annotate(line_count=Count("pk")).order_by("account_code")):
        code = item["account_code"]
        flag("finance.FinancialLine", code, code, "UNMAPPED_ACCOUNT_CODE", line_count=item["line_count"])

    for expense in Expense.objects.filter(status="POSTED", financial_transaction__isnull=True).iterator():
        flag("finance.Expense", expense.pk, expense.reference, "POSTED_EXPENSE_WITHOUT_JOURNAL",
             amount=expense.amount, currency=expense.currency)

    for payment in PurchaseOrderPayment.objects.exclude(accounting_posting_status__in=["POSTED", "MIRRORED"]).iterator():
        flag("inventory.PurchaseOrderPayment", payment.pk, payment.reference, "PROCUREMENT_PAYMENT_REQUIRES_RECONCILIATION",
             amount=payment.amount, purchase_order_id=payment.purchase_order_id,
             posting_status=payment.accounting_posting_status)

    # The old nightly journal used a whole-booking allocation. Do not assume
    # its historic occupancy was correct or alter the journal automatically.
    for entry in FinancialTransaction.objects.filter(type="ACCOMMODATION_CHARGE", status="POSTED").iterator():
        metadata = entry.metadata or {}
        if not metadata.get("occupied_rooms"):
            flag("finance.FinancialTransaction", entry.pk, entry.reference, "ROOM_OCCUPANCY_UNVERIFIED",
                 source_reference=entry.source_reference, business_date=entry.business_date)

    # A historic migration copied the booking-level timestamp to each room.
    # An identical timestamp may also be a legitimate whole-booking check-in,
    # so this is a review flag, not an assertion that a room was absent.
    Booking = apps.get_model("bookings", "Booking")
    potentially_inherited = (Booking.objects.filter(number_of_rooms__gt=1, checked_in_at__isnull=False,
                             room_assignments__checked_in_at=F("checked_in_at"))
                             .distinct())
    for booking in potentially_inherited.iterator(chunk_size=1000):
        flag("bookings.Booking", booking.pk, booking.booking_reference,
             "MULTI_ROOM_TIMESTAMP_PROVENANCE_UNVERIFIED", checked_in_at=booking.checked_in_at,
             number_of_rooms=booking.number_of_rooms)

    duplicate_refs = (PurchaseOrderPayment.objects.exclude(reference="").values("reference")
                      .annotate(count=Count("pk")).filter(count__gt=1))
    for item in duplicate_refs:
        flag("inventory.PurchaseOrderPayment", item["reference"], item["reference"],
             "DUPLICATE_PROCUREMENT_PAYMENT_REFERENCE", count=item["count"])

    ledger = [{"currency": row["transaction__currency"], "account_code": row["account_code"],
               "direction": row["direction"], "count": row["count"], "amount": str(row["amount"] or ZERO)}
              for row in FinancialLine.objects.filter(transaction__status="POSTED")
              .values("transaction__currency", "account_code", "direction")
              .annotate(count=Count("pk"), amount=Sum("amount")).order_by("transaction__currency", "account_code", "direction")]
    po_payments = [{"currency": row["purchase_order__currency"], "count": row["count"], "amount": str(row["amount"] or ZERO)}
                   for row in PurchaseOrderPayment.objects.values("purchase_order__currency")
                   .annotate(count=Count("pk"), amount=Sum("amount")).order_by("purchase_order__currency")]
    report = {"schema": "jone-accounting-audit-v1", "created_at": timezone.now().isoformat(),
              "source_revision": source_revision, "database_vendor": connection.vendor,
              "production_cutover_approved": False, "certified": False,
              "note": "Source records and posted journals remain in their existing tables. This inventory is not a complete reconciliation or approval.",
              "source_counts": counts,
              "successful_payments": _currency_totals(Payment.objects.filter(status__in=["SUCCESS", "PARTIALLY_REFUNDED", "REFUNDED"])),
              "posted_expenses": _currency_totals(Expense.objects.filter(status="POSTED")),
              "procurement_payments": po_payments, "posted_ledger_by_account": ledger,
              "exception_count": observed, "exceptions_included": len(exceptions),
              "exceptions_truncated": observed > len(exceptions), "exceptions": exceptions}
    return report


class Command(BaseCommand):
    help = "Inventory historical accounting evidence; read-only by default. Never posts or deletes financial data."

    def add_arguments(self, parser):
        parser.add_argument("--output", help="Write JSON to this file (otherwise stdout). Treat as confidential.")
        parser.add_argument("--source-revision", default="", help="Deployed repository commit for traceability.")
        parser.add_argument("--max-exceptions", type=int, default=10000,
                            help="Maximum exceptions in JSON; 0 means unlimited. Truncation is explicitly marked.")
        parser.add_argument("--record", action="store_true", help="Record a staging-only run and exceptions in new tables.")
        parser.add_argument("--ack-staging-and-backup", action="store_true", help="Required for --record after a tested backup.")

    def handle(self, *args, **options):
        if options["max_exceptions"] < 0:
            raise CommandError("--max-exceptions cannot be negative.")
        if options["record"] and not options["ack_staging_and_backup"]:
            raise CommandError("--record requires --ack-staging-and-backup; do not use on production during development.")
        report = inspect(max_exceptions=options["max_exceptions"], source_revision=options["source_revision"])
        if options["record"]:
            if report["exceptions_truncated"]:
                raise CommandError("Refusing to persist an incomplete exception register. Re-run with --max-exceptions 0.")
            with transaction.atomic():
                run = AccountingMigrationRun.objects.create(
                    reference=f"AMR-{uuid4().hex.upper()}", source_revision=options["source_revision"],
                    database_vendor=connection.vendor, summary={k: v for k, v in report.items() if k != "exceptions"},
                    completed_at=timezone.now(),
                )
                AccountingMigrationException.objects.bulk_create(
                    [AccountingMigrationException(run=run, **entry) for entry in report["exceptions"]], batch_size=500)
                report["run_reference"] = run.reference
        payload = json.dumps(report, indent=2, ensure_ascii=False)
        if options["output"]:
            with open(options["output"], "w", encoding="utf-8") as output:
                output.write(payload + "\n")
            self.stdout.write(f"Audit written to {options['output']}. Cutover is NOT approved.")
        else:
            self.stdout.write(payload)
