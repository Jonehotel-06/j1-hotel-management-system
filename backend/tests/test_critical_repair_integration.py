"""Cross-app integration evidence for the controlled production repair."""

import importlib.util
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from django.apps import apps as global_apps
from django.core.exceptions import PermissionDenied

from apps.accounts.models import User
from apps.core.utils import hotel_today
from apps.finance.models import Expense, FinancialControlPolicy, FinancialTransaction
from apps.finance.services.expense_service import (
    create_expense_handover,
    post_expense,
    prepare_expense_for_approval,
    review_expense,
    submit_expense,
)
from apps.finance.services.financial_reconciliation_service import build_reconciliation
from apps.finance.services.payment_visibility import unposted_procurement_payment_visibility
from apps.finance.services.reporting_service import financial_summary
from apps.finance.services import guest_receivables_service  # noqa: F401 - imported to exercise report wiring
from apps.inventory.models import (
    GoodsReceiptLine,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderPayment,
    StockItem,
    StockLocation,
    StockMovement,
    Supplier,
)
from apps.inventory.services.inventory_service import (
    create_manual_ingredient_issue,
    receive_goods,
    record_purchase_order_payment,
    record_stock_movement,
)
from apps.pos.models import MenuCategory, MenuItem
from apps.reports.services.dashboard import build_dashboard

from .base import BaseAPITestCase
from .factories import make_staff


class CriticalRepairIntegrationTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.admin = make_staff("critical-repair.admin@staff.dev", role=User.Role.ADMIN)
        self.storekeeper = make_staff("critical-repair.storekeeper@staff.dev", role=User.Role.STOREKEEPER)
        self.accountant = make_staff("critical-repair.accountant@staff.dev", role=User.Role.ACCOUNTANT)
        self.manager = make_staff("critical-repair.gm@staff.dev", role=User.Role.GENERAL_MANAGER)
        FinancialControlPolicy.objects.create(
            name="Critical repair control policy",
            effective_from=hotel_today(),
            is_active=True,
        )
        self.location = StockLocation.objects.create(code="REPAIR-STORE", name="Repair store")
        self.supplier = Supplier.objects.create(reference="SUP-REPAIR", name="Repair supplier")
        self.item = StockItem.objects.create(
            sku="REPAIR-RICE", name="Rice", item_type=StockItem.ItemType.RECIPE_INGREDIENT,
            stock_consumption_mode=StockItem.ConsumptionMode.MANUAL,
            base_unit="kg", standard_cost="2.00",
        )
        self.menu_category = MenuCategory.objects.create(
            name="Repair dishes", slug="repair-dishes",
            service_area=MenuCategory.ServiceArea.RESTAURANT,
        )
        self.menu_item = MenuItem.objects.create(
            category=self.menu_category, name="Jollof rice", sku="REPAIR-JOLLOF",
            base_price="3000.00",
        )

    def _purchase_order(self, suffix, *, status=PurchaseOrder.Status.APPROVED, quantity=10):
        po = PurchaseOrder.objects.create(
            reference=f"PO-REPAIR-{suffix}",
            supplier=self.supplier,
            delivery_location=self.location,
            status=status,
            requested_by=self.admin,
            approved_by=self.admin,
            currency="NGN",
        )
        PurchaseOrderLine.objects.create(
            purchase_order=po,
            item=self.item,
            item_sku=self.item.sku,
            item_name=self.item.name,
            quantity_ordered=Decimal(str(quantity)),
            unit_cost=Decimal("5.00"),
        )
        return po

    def test_posted_ledger_reconciliation_and_owner_dashboard_share_one_basis(self):
        today = hotel_today()
        summary = financial_summary(start_date=today, end_date=today, currency="NGN")
        reconciliation = summary["reconciliation"]
        self.assertEqual(reconciliation["status"], "RECONCILED")
        self.assertTrue(reconciliation["all_arithmetic_checks_reconciled"])
        self.assertEqual(len(reconciliation["checks"]), 11)
        self.assertTrue(all(check["reconciles"] for check in reconciliation["checks"]))
        self.assertEqual(reconciliation["unposted_procurement_payments"]["count"], 0)
        self.assertFalse(reconciliation["unposted_procurement_payments"]["added_to_posted_totals"])

        dashboard = build_dashboard(self.admin)
        self.assertEqual(dashboard["reconciliation"]["status"], "RECONCILED")
        self.assertEqual(dashboard["financial_basis"]["source"], "posted_immutable_financial_lines")
        self.assertFalse(dashboard["financial_basis"]["legacy_payment_booking_fallback_used"])
        self.assertNotIn("revenue", dashboard)
        self.assertIn("recent_payments", dashboard)

        self.auth(self.admin)
        response = self.client.get(
            "/api/admin/finance/reports/reconciliation/",
            {"start": today.isoformat(), "end": today.isoformat(), "currency": "NGN"},
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["data"]["status"], "RECONCILED")

        # The same two authoritative reports also drive the standalone builder.
        from apps.finance.services.guest_receivables_service import guest_receivables

        receivables = guest_receivables(as_of=today, currency="NGN", limit=1, include_settled=False)
        standalone = build_reconciliation(summary=summary, receivables=receivables)
        self.assertEqual(standalone["status"], "RECONCILED")
        self.assertTrue(standalone["posted_only"])

    def test_storekeeper_handover_is_draft_only_and_posts_once_after_independent_approval(self):
        expense, created = create_expense_handover(
            actor=self.storekeeper,
            category="Kitchen supplies",
            amount="1200.00",
            payee="Repair supplier",
            description="Store handover",
            workflow_origin=Expense.WorkflowOrigin.STOREKEEPER,
            idempotency_key="critical-repair-handover-1",
        )
        self.assertTrue(created)
        self.assertEqual(expense.status, Expense.Status.DRAFT)
        self.assertIsNone(expense.financial_transaction_id)
        self.assertEqual(FinancialTransaction.objects.filter(type=FinancialTransaction.Type.EXPENSE).count(), 0)

        with self.assertRaises(PermissionDenied):
            submit_expense(expense=expense, actor=self.storekeeper)

        prepare_expense_for_approval(expense=expense, actor=self.accountant, payee="Repair supplier Ltd")
        submitted = submit_expense(expense=expense, actor=self.accountant)
        self.assertEqual(submitted.status, Expense.Status.SUBMITTED)
        self.assertIsNotNone(submitted.approval_request_id)
        self.assertIsNotNone(submitted.prepared_by_id)
        self.assertIsNotNone(submitted.prepared_at)

        approved = review_expense(expense=submitted, reviewer=self.manager, approved=True)
        self.assertEqual(approved.status, Expense.Status.APPROVED)
        posted, posted_created = post_expense(expense=approved, actor=self.accountant)
        self.assertTrue(posted_created)
        self.assertEqual(posted.status, Expense.Status.POSTED)
        self.assertIsNotNone(posted.financial_transaction_id)
        self.assertEqual(FinancialTransaction.objects.filter(type=FinancialTransaction.Type.EXPENSE).count(), 1)

        replay, replay_created = post_expense(expense=posted, actor=self.accountant)
        self.assertFalse(replay_created)
        self.assertEqual(FinancialTransaction.objects.filter(type=FinancialTransaction.Type.EXPENSE).count(), 1)

    def test_procurement_payment_creates_one_handover_and_stays_out_of_posted_totals(self):
        po = self._purchase_order("PAYMENT")
        payment = record_purchase_order_payment(
            purchase_order=po,
            actor=self.admin,
            method="BANK_TRANSFER",
            amount="25.00",
            reference="BANK-REPAIR-001",
        )
        payment.refresh_from_db()
        self.assertEqual(payment.accounting_posting_status, PurchaseOrderPayment.AccountingPostingStatus.HANDED_TO_ACCOUNTING)
        self.assertTrue(payment.finance_expense_reference)
        self.assertEqual(payment.finance_transaction_reference, "")
        self.assertEqual(Expense.objects.filter(purchase_order=po).count(), 1)
        self.assertEqual(Expense.objects.get(purchase_order=po).status, Expense.Status.DRAFT)
        self.assertEqual(FinancialTransaction.objects.filter(type=FinancialTransaction.Type.EXPENSE).count(), 0)

        po.refresh_from_db()
        self.assertEqual(po.payment_status, PurchaseOrder.PaymentStatus.PARTIALLY_PAID)
        self.assertEqual(po.receipt_status, PurchaseOrder.ReceiptStatus.NOT_RECEIVED)

        summary = financial_summary(start_date=hotel_today(), end_date=hotel_today(), currency="NGN")
        unposted = summary["supplier_payments"]["unposted_procurement_payments"]
        self.assertEqual(unposted["count"], 1)
        self.assertEqual(unposted["amount"], "25.00")
        self.assertFalse(unposted["added_to_posted_totals"])
        self.assertEqual(summary["reconciliation"]["status"], "RECONCILED_WITH_EXCEPTIONS")
        self.assertEqual(summary["reconciliation"]["unposted_procurement_payments"]["count"], 1)
        self.assertEqual(unposted_procurement_payment_visibility(currency="NGN")["count"], 1)

    def test_manual_ingredient_issue_is_idempotent_and_records_dish_batch_evidence(self):
        record_stock_movement(
            item=self.item, location=self.location,
            movement_type=StockMovement.Type.RECEIPT,
            quantity_delta=Decimal("20"), source_key="critical-repair-opening-rice",
            unit_cost=Decimal("2.00"), actor=self.admin,
        )
        lines = [{"item_id": self.item.pk, "quantity": "3"}]
        batch, created = create_manual_ingredient_issue(
            menu_item_id=self.menu_item.pk,
            location_id=self.location.pk,
            department="KITCHEN",
            recipient="Chef Ada",
            preparation_on=hotel_today(),
            batch_reference="REPAIR-BATCH-01",
            lines=lines,
            actor=self.admin,
            idempotency_key="critical-repair-manual-issue-1",
        )
        self.assertTrue(created)
        self.assertEqual(batch.consumption_method, "MANUAL")
        movements = list(
            StockMovement.objects.filter(preparation_batch=batch, issue_method=StockMovement.IssueMethod.MANUAL)
        )
        self.assertEqual(len(movements), 1)
        self.assertEqual(movements[0].menu_item_id, self.menu_item.pk)
        self.assertEqual(movements[0].department, "KITCHEN")
        self.assertEqual(movements[0].recipient, "Chef Ada")
        self.assertTrue(movements[0].metadata["automatic_recipe_suppressed"])
        self.assertEqual(str(movements[0].quantity_delta), "-3.0000")

        replay, replay_created = create_manual_ingredient_issue(
            menu_item_id=self.menu_item.pk,
            location_id=self.location.pk,
            department="KITCHEN",
            recipient="Chef Ada",
            preparation_on=hotel_today(),
            batch_reference="REPAIR-BATCH-01",
            lines=lines,
            actor=self.admin,
            idempotency_key="critical-repair-manual-issue-1",
        )
        self.assertFalse(replay_created)
        self.assertEqual(replay.pk, batch.pk)
        self.assertEqual(
            StockMovement.objects.filter(preparation_batch=batch, issue_method=StockMovement.IssueMethod.MANUAL).count(),
            1,
        )

    def test_damaged_and_rejected_receipt_quantities_never_enter_stock(self):
        po = self._purchase_order("DAMAGED")
        po_line = po.lines.get()
        receipt, created = receive_goods(
            purchase_order=po,
            actor=self.admin,
            source_key="critical-repair-receipt-damaged",
            receipt_lines=[{
                "purchase_order_line_id": po_line.pk,
                "quantity_received": 6,
                "quantity_damaged": 2,
                "quantity_rejected": 2,
            }],
        )
        self.assertTrue(created)
        line = receipt.lines.get()
        self.assertEqual(line.quantity_received, Decimal("6.0000"))
        self.assertEqual(line.quantity_damaged, Decimal("2.0000"))
        self.assertEqual(line.quantity_rejected, Decimal("2.0000"))
        self.assertIsNotNone(line.stock_movement_id)
        self.assertEqual(line.stock_movement.quantity_delta, Decimal("6.0000"))
        po.refresh_from_db()
        self.assertEqual(po.receipt_status, PurchaseOrder.ReceiptStatus.RECEIVED)
        self.assertEqual(po.payment_status, PurchaseOrder.PaymentStatus.UNPAID)

        all_rejected_po = self._purchase_order("REJECTED")
        all_rejected_line = all_rejected_po.lines.get()
        rejected_receipt, _ = receive_goods(
            purchase_order=all_rejected_po,
            actor=self.admin,
            source_key="critical-repair-receipt-rejected",
            receipt_lines=[{
                "purchase_order_line_id": all_rejected_line.pk,
                "quantity_received": 0,
                "quantity_damaged": 10,
                "quantity_rejected": 0,
            }],
        )
        rejected_line = rejected_receipt.lines.get()
        self.assertEqual(rejected_line.quantity_received, Decimal("0.0000"))
        self.assertEqual(rejected_line.quantity_damaged, Decimal("10.0000"))
        self.assertIsNone(rejected_line.stock_movement_id)
        all_rejected_po.refresh_from_db()
        self.assertEqual(all_rejected_po.receipt_status, PurchaseOrder.ReceiptStatus.RECEIVED)
        all_rejected_line.refresh_from_db()
        self.assertEqual(all_rejected_line.quantity_received, Decimal("0.0000"))
        self.assertEqual(all_rejected_line.quantity_damaged, Decimal("10.0000"))
        self.assertFalse(
            StockMovement.objects.filter(source_key=f"goods-receipt:{rejected_receipt.reference}:{all_rejected_line.pk}").exists()
        )

    def test_migration_backfill_mirrors_historical_settlements_and_derives_statuses(self):
        po = self._purchase_order("HISTORICAL", status=PurchaseOrder.Status.PAID)
        po.payment_status = PurchaseOrder.PaymentStatus.UNPAID
        po.receipt_status = PurchaseOrder.ReceiptStatus.NOT_RECEIVED
        po.save(update_fields=["payment_status", "receipt_status"])
        po_line = po.lines.get()
        po_line.quantity_received = Decimal("3.0000")
        po_line.quantity_damaged = Decimal("1.0000")
        po_line.save(update_fields=["quantity_received", "quantity_damaged"])
        payment = PurchaseOrderPayment.objects.create(
            purchase_order=po,
            amount=Decimal("50.00"),
            method=PurchaseOrderPayment.PaymentMethod.BANK_TRANSFER,
            reference="HIST-SETTLEMENT",
            accounting_settlement_reference="SPR-HISTORICAL",
            accounting_posting_status=PurchaseOrderPayment.AccountingPostingStatus.NOT_POSTED,
        )

        migration_path = (
            Path(__file__).resolve().parents[1]
            / "apps" / "inventory" / "migrations" / "0016_manual_issue_and_procurement_status.py"
        )
        spec = importlib.util.spec_from_file_location("inventory_migration_0016", migration_path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        editor = SimpleNamespace(connection=SimpleNamespace(alias="default"))
        module.backfill_payment_and_receipt_status(global_apps, editor)

        po.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(po.payment_status, PurchaseOrder.PaymentStatus.PAID)
        self.assertEqual(po.receipt_status, PurchaseOrder.ReceiptStatus.PARTIALLY_RECEIVED)
        self.assertEqual(payment.accounting_posting_status, PurchaseOrderPayment.AccountingPostingStatus.MIRRORED)
        self.assertEqual(unposted_procurement_payment_visibility(currency="NGN")["count"], 0)
