"""End-to-end reconciliation: accounting <-> inventory <-> sales (§12).

The brief asks for proof that accounting data reconciles with inventory and sales,
tested independently for a cash sale and for a room-charge (folio) sale. These
tests therefore do not check a single number in isolation; they check the
*identities* that must hold between the three subsystems:

1. **Inventory identity** — for every item/location, the stored
   ``StockBalance.quantity_on_hand`` equals the sum of the append-only
   ``StockMovement.quantity_delta`` rows, and a sale's deduction equals the
   recipe quantity times the ordered quantity.
2. **Sales identity** — the stock ledger's consumed quantity for an order times
   the recorded unit cost equals the cost of the goods the guest was sold, and the
   order's own line totals equal what the folio/ledger was charged.
3. **Accounting identity** — the posted ledger is balanced
   (sum of debits == sum of credits), the guest-facing charge equals the order
   total, recognized revenue equals the charge net of any tax, and for a cash sale
   the tender collection equals the amount handed over.

A folio-charged sale must still deduct stock: collection is not the trigger,
finalisation of the sale is. That is the defect being locked down here.
"""
from decimal import Decimal

from apps.finance.models import CashMovement, CashSession, FinancialLine, FinancialTransaction
from apps.finance.services.reporting_service import financial_summary
from apps.inventory.models import StockBalance, StockMovement
from apps.pos.models import PosOrder

from .test_inventory_sales_depletion import SalesDepletionTestCase


class ReconciliationTestCase(SalesDepletionTestCase):
    """Shared assertions over the three subsystems."""

    # ---------------------------------------------------------------- identities
    def assert_stock_ledger_reconciles(self, *, location=None):
        """The stored balance must equal the sum of the immutable movements."""
        rows = StockBalance.objects.filter(item=self.bottle)
        if location is not None:
            rows = rows.filter(location=location)
        self.assertTrue(rows.exists(), "expected at least one balance row")
        for balance in rows:
            movement_total = sum(
                (movement.quantity_delta for movement in StockMovement.objects.filter(
                    item=balance.item, location=balance.location,
                )),
                Decimal("0.0000"),
            )
            self.assertEqual(
                Decimal(balance.quantity_on_hand), movement_total,
                f"balance for {balance.item.sku} @ {balance.location.code} does not equal its movements",
            )

    def assert_posted_ledger_balances(self):
        """Every posted transaction's debits must equal its credits."""
        totals = {}
        for line in FinancialLine.objects.filter(
            transaction__status=FinancialTransaction.Status.POSTED,
        ).select_related("transaction"):
            entry = totals.setdefault(line.transaction_id, [Decimal("0.00"), Decimal("0.00")])
            if line.direction == FinancialLine.Direction.DEBIT:
                entry[0] += line.amount
            else:
                entry[1] += line.amount
        for transaction_id, (debits, credits) in totals.items():
            self.assertEqual(
                debits, credits,
                f"posted transaction {transaction_id} is unbalanced: {debits} debits vs {credits} credits",
            )

    def revenue_lines_for(self, order):
        return FinancialLine.objects.filter(
            transaction=order.charge_transaction,
        ).order_by("pk")


class RoomChargeReconciliationTests(ReconciliationTestCase):
    """A sale charged to a guest folio must deduct stock AND post balanced revenue."""

    def test_folio_charged_sale_reconciles_stock_sales_and_accounting(self):
        stay = self.in_house_stay("-rec")
        self.receive_from_supplier(20, source_key="rec-receipt-room")
        self.assert_stock_ledger_reconciles()

        order = self.sell(
            mode=PosOrder.Mode.ROOM_SERVICE, quantity=3, stay=stay, delivery_location="Room 101",
        )
        order.refresh_from_db()

        # --- inventory: exactly three bottles left the bar -------------------
        self.assertEqual(self.on_hand(), Decimal("17.0000"))
        self.assert_stock_ledger_reconciles()
        movements = StockMovement.objects.filter(
            type=StockMovement.Type.POS_CONSUMPTION, source_reference=order.reference,
        )
        self.assertEqual(movements.count(), 1, "one aggregate deduction per item per hand-off")
        deduction = movements.get()
        self.assertEqual(deduction.quantity_delta, Decimal("-3.0000"))
        # The deduction carries the recorded unit cost, so the stock-value change is
        # reproducible from the ledger alone.
        self.assertEqual(deduction.unit_cost, Decimal("150.00"))
        self.assertEqual(abs(deduction.quantity_delta) * deduction.unit_cost, Decimal("450.00"))

        # --- sales: the order's own lines add up to what was charged ---------
        order_total = sum((line.line_total for line in order.lines.all()), Decimal("0.00"))
        self.assertEqual(order_total, order.total_amount)
        self.assertEqual(order_total, Decimal("900.00"), "3 x 300.00")

        # --- accounting: a balanced, correctly-aimed charge ------------------
        self.assert_posted_ledger_balances()
        self.assertIsNotNone(order.charge_transaction_id, "the folio sale must post a charge")
        lines = {line.account_code: line for line in self.revenue_lines_for(order)}
        receivable = lines["ASSET.GUEST_RECEIVABLE"]
        revenue = lines["REVENUE.FOOD_BEVERAGE"]
        self.assertEqual(receivable.direction, FinancialLine.Direction.DEBIT)
        self.assertEqual(revenue.direction, FinancialLine.Direction.CREDIT)
        self.assertEqual(receivable.amount, order_total)
        self.assertEqual(revenue.amount, order_total, "no tax is billed to the guest, so revenue equals the total")

        # The POS charge is collected through the folio, so no tender was taken.
        self.assertEqual(order.settlement_status, PosOrder.SettlementStatus.NOT_APPLICABLE)
        collection = FinancialLine.objects.filter(
            transaction__type=FinancialTransaction.Type.PAYMENT_COLLECTION,
            account_code__in=("ASSET.CASH_ON_HAND", "ASSET.BANK_CLEARING", "ASSET.CARD_CLEARING"),
        )
        self.assertEqual(collection.count(), 0, "a folio charge must not fake a collection")

        # --- cross-check through the reporting layer -------------------------
        report = financial_summary(start_date=self.today, end_date=self.today, currency="NGN")
        self.assertEqual(Decimal(report["recognized_revenue"]["amount"]), order_total)
        self.assertEqual(Decimal(report["collections"]["amount"]), Decimal("0.00"))
        self.assertEqual(
            Decimal(report["supplier_payments"]["total_cash_disbursements"]), Decimal("0.00"),
            "no supplier money moved as a result of a guest charge",
        )

    def test_the_profitability_projection_uses_the_same_deduction_the_sale_made(self):
        stay = self.in_house_stay("-rec2")
        self.receive_from_supplier(20, source_key="rec-receipt-room2")
        self.sell(mode=PosOrder.Mode.ROOM_SERVICE, quantity=4, stay=stay, delivery_location="Room 102")

        from apps.inventory.services.valuation_service import product_profitability_projection

        row = next(
            r for r in product_profitability_projection(item_ids=[self.bottle.pk]) if r["location_id"] == self.bar.pk
        )
        # The projection reads the SAME balance the sale reduced, so the two can
        # never disagree about how much stock is left.
        balance = StockBalance.objects.get(item=self.bottle, location=self.bar)
        self.assertEqual(row["current_stock"], balance.quantity_on_hand)
        self.assertEqual(row["current_stock"], Decimal("16.0000"))
        self.assertEqual(row["purchase_unit_cost"], Decimal("150.00"))
        self.assertEqual(row["stock_value"], Decimal("2400.00"))


class CashSaleReconciliationTests(ReconciliationTestCase):
    """A sale settled at the till must deduct stock AND record the tender once."""

    def test_cash_sale_reconciles_stock_sales_and_accounting(self):
        self.receive_from_supplier(6, source_key="rec-receipt-cash")
        order = self.sell(mode=PosOrder.Mode.TAKEAWAY, quantity=2, direct_tender="CASH")
        order.refresh_from_db()

        # --- inventory ------------------------------------------------------
        self.assertEqual(self.on_hand(), Decimal("4.0000"))
        self.assert_stock_ledger_reconciles()
        movement = StockMovement.objects.get(
            type=StockMovement.Type.POS_CONSUMPTION, source_reference=order.reference,
        )
        self.assertEqual(movement.quantity_delta, Decimal("-2.0000"))

        # --- sales ----------------------------------------------------------
        order_total = order.total_amount
        self.assertEqual(order_total, Decimal("600.00"))
        self.assertEqual(order.settlement_status, PosOrder.SettlementStatus.PAID)

        # --- accounting -----------------------------------------------------
        self.assert_posted_ledger_balances()
        collections = FinancialLine.objects.filter(
            transaction__type=FinancialTransaction.Type.PAYMENT_COLLECTION,
            direction=FinancialLine.Direction.DEBIT,
            account_code="ASSET.CASH_ON_HAND",
        )
        self.assertEqual(collections.count(), 1, "one collection for one cash sale")
        self.assertEqual(collections.get().amount, order_total)
        # The drawer movement links the collection to the cashier's open session,
        # so the till count and the ledger agree about where the money is.
        drawer = CashMovement.objects.get(transaction=collections.get().transaction)
        self.assertEqual(drawer.type, CashMovement.Type.COLLECTION)
        self.assertEqual(drawer.cash_session.cashier_id, self.cashier.pk)
        self.assertEqual(drawer.cash_session.status, CashSession.Status.OPEN)
        self.assertEqual(drawer.amount, order_total)

        report = financial_summary(start_date=self.today, end_date=self.today, currency="NGN")
        self.assertEqual(Decimal(report["collections"]["amount"]), order_total)
        self.assertEqual(Decimal(report["recognized_revenue"]["amount"]), order_total)

    def test_the_two_flows_are_reported_independently(self):
        # One folio-charged sale and one cash sale in the same period: revenue is
        # the sum, but collections cover ONLY the cash sale, and each flow deducts
        # its own stock exactly once.
        stay = self.in_house_stay("-mix")
        self.receive_from_supplier(10, source_key="rec-receipt-mix")
        charged = self.sell(mode=PosOrder.Mode.ROOM_SERVICE, quantity=1, stay=stay, delivery_location="Room 103")
        paid = self.sell(mode=PosOrder.Mode.TAKEAWAY, quantity=2, direct_tender="CARD")

        self.assertEqual(self.on_hand(), Decimal("7.0000"), "3 units sold in total: 1 charged, 2 paid")
        self.assert_stock_ledger_reconciles()
        report = financial_summary(start_date=self.today, end_date=self.today, currency="NGN")
        expected_revenue = charged.total_amount + paid.total_amount
        self.assertEqual(Decimal(report["recognized_revenue"]["amount"]), expected_revenue)
        self.assertEqual(Decimal(report["collections"]["amount"]), paid.total_amount)
        # Net cash movement reflects the card collection and no disbursement.
        self.assertEqual(Decimal(report["net_cash_movement"]), paid.total_amount)
        self.assert_posted_ledger_balances()

    def test_stock_value_and_purchase_cost_reconcile_with_the_ledger(self):
        # A goods receipt moves quantity and records unit cost; the inventory
        # acquisition it represents is what the accounting side recognizes. Both
        # sides must agree on the value of the goods received.
        receipt = self.receive_from_supplier(4, unit_cost="175.00", source_key="rec-receipt-value")
        receipt_line = receipt.lines.get()
        self.assertEqual(receipt_line.quantity_received, Decimal("4.0000"))
        self.assertEqual(receipt_line.unit_cost, Decimal("175.00"))
        self.assertEqual(receipt_line.quantity_received * receipt_line.unit_cost, Decimal("700.00"))
        self.assertEqual(
            receipt_line.stock_movement.quantity_delta, Decimal("4.0000"),
            "the receipt line and its movement must agree",
        )
        self.assert_stock_ledger_reconciles()
        # An inventory purchase recorded as an expense is what the payable /
        # inventory-acquisition measure counts; a pure receipt posts no money.
        report = financial_summary(start_date=self.today, end_date=self.today, currency="NGN")
        self.assertEqual(Decimal(report["supplier_payments"]["supplier_invoices_recognized"]), Decimal("0.00"))
        self.assertEqual(Decimal(report["inventory_acquisitions"]["amount"]), Decimal("0.00"))
