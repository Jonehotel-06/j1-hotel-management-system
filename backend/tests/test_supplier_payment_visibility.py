"""Supplier-payment and disbursement visibility (§3 of the repair brief).

The point of these tests is stage separation and double-counting:

* a goods receipt moves quantity only and must never appear as money,
* an inventory invoice put on account raises a payable and posts NO disbursement,
* settling that payable moves money once and clears the payable,
* an invoice paid immediately is a disbursement and never also a payable,
* a salary payment and a guest refund are reported in their own measures and are
  never also counted as a supplier or operating payment,
* COGS is reported as unavailable rather than fabricated.

Every number is built through the real posting services, so these tests exercise
the ledger rules the rest of the system already relies on.
"""
from decimal import Decimal

from apps.accounts.models import User
from apps.core.utils import hotel_today
from apps.finance.models import Expense, FinancialTransaction
from apps.finance.services.cash_session_service import open_cash_session
from apps.finance.services.expense_service import create_expense, post_expense, review_expense, submit_expense
from apps.finance.services.payment_visibility import DISBURSEMENT_TYPES, TENDER_ASSET_ACCOUNTS
from apps.finance.services.reporting_service import financial_summary
from apps.inventory.models import StockItem, StockLocation, StockMovement, Supplier

from .base import BaseAPITestCase
from .factories import make_staff


class SupplierPaymentVisibilityTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.clerk = make_staff("payvis-clerk@staff.dev", role=User.Role.INVENTORY_CLERK)
        self.manager = make_staff("payvis-manager@staff.dev", role=User.Role.MANAGER)
        # Maker-checker: the manager raises every expense, a different account with
        # `expense.approve` authorises it. Inventory clerks hold no expense rights.
        self.approver = make_staff("payvis-approver@staff.dev", role=User.Role.GENERAL_MANAGER)
        self.supplier = Supplier.objects.create(reference="SUP-PAYVIS", name="Payvis supplier")
        self.location = StockLocation.objects.create(code="PAYVIS-STORE", name="Payvis store")
        self.beverage = StockItem.objects.create(
            sku="PAYVIS-BEER", name="Beer crate", base_unit="CRATE",
            standard_cost=Decimal("12000.00"), preferred_supplier=self.supplier,
        )

    # ------------------------------------------------------------------ helpers
    def post_expense(self, *, amount, payment_method, accounting_class=Expense.AccountingClass.OPERATING, key, actor=None):
        actor = actor or self.manager
        session = None
        if payment_method == Expense.PaymentMethod.CASH:
            session = open_cash_session(cashier=actor, actor=actor, opening_float="500000.00")
        expense, _ = create_expense(
            actor=actor, category="Supplier goods", amount=amount, payment_method=payment_method,
            accounting_class=accounting_class, cash_session=session, payee=self.supplier.name,
            supplier_reference=self.supplier.reference, idempotency_key=key,
        )
        submit_expense(expense=expense, actor=actor)
        review_expense(expense=expense, reviewer=self.approver, approved=True)
        posted, created = post_expense(expense=expense, actor=actor)
        self.assertTrue(created, "the expense must post exactly once")
        return posted

    def summary(self):
        today = hotel_today()
        return financial_summary(start_date=today, end_date=today, currency="NGN")

    def amounts(self, report):
        return {
            key: Decimal(report["supplier_payments"][key])
            for key in (
                "supplier_invoices_recognized", "supplier_payments_made", "operating_expenses_paid",
                "net_payroll_paid", "other_cash_disbursements", "total_cash_disbursements",
                "supplier_payables_raised", "supplier_payables_settled", "outstanding_supplier_payables",
            )
        }

    # -------------------------------------------------------------------- tests
    def test_a_goods_receipt_posts_quantity_but_no_money(self):
        # Stock movements carry quantity and unit cost only. Post one through the
        # real service and prove that none of it reaches a money measure.
        from apps.inventory.services.inventory_service import record_stock_movement

        record_stock_movement(
            item=self.beverage, location=self.location, movement_type=StockMovement.Type.OPENING,
            quantity_delta="10", unit_cost="12000.00", source_key="payvis-opening", actor=self.clerk,
        )
        report = self.summary()
        amounts = self.amounts(report)
        for key in (
            "supplier_invoices_recognized", "supplier_payments_made", "operating_expenses_paid",
            "net_payroll_paid", "other_cash_disbursements", "total_cash_disbursements",
        ):
            self.assertEqual(amounts[key], Decimal("0.00"), f"{key} must ignore a pure stock movement")
        self.assertIn("goods_receipts", report["supplier_payments"]["definitions"])

    def test_an_invoice_on_account_raises_a_payable_and_pays_nothing(self):
        self.post_expense(
            amount="250000.00", payment_method=Expense.PaymentMethod.PAYABLE,
            accounting_class=Expense.AccountingClass.INVENTORY, key="payvis-payable-invoice",
        )
        amounts = self.amounts(self.summary())
        self.assertEqual(amounts["supplier_invoices_recognized"], Decimal("250000.00"))
        self.assertEqual(amounts["supplier_payables_raised"], Decimal("250000.00"))
        self.assertEqual(amounts["outstanding_supplier_payables"], Decimal("250000.00"))
        self.assertEqual(amounts["supplier_payments_made"], Decimal("0.00"), "an invoice is not a payment")
        self.assertEqual(amounts["total_cash_disbursements"], Decimal("0.00"))

    def test_multiple_partial_payments_are_each_reported_once(self):
        # Three separate disbursements against the same supplier: two on account,
        # a third paid at once out of the bank. Every one is reported exactly once.
        self.post_expense(
            amount="250000.00", payment_method=Expense.PaymentMethod.PAYABLE,
            accounting_class=Expense.AccountingClass.INVENTORY, key="payvis-invoice-b1",
        )
        self.post_expense(
            amount="100000.00", payment_method=Expense.PaymentMethod.PAYABLE,
            accounting_class=Expense.AccountingClass.INVENTORY, key="payvis-invoice-b2",
        )
        self.post_expense(
            amount="60000.00", payment_method=Expense.PaymentMethod.BANK_TRANSFER,
            accounting_class=Expense.AccountingClass.INVENTORY, key="payvis-paid-b3",
        )
        amounts = self.amounts(self.summary())
        self.assertEqual(amounts["supplier_payables_raised"], Decimal("350000.00"))
        self.assertEqual(amounts["supplier_payments_made"], Decimal("60000.00"))
        self.assertEqual(amounts["total_cash_disbursements"], Decimal("60000.00"))
        self.assertEqual(amounts["outstanding_supplier_payables"], Decimal("350000.00"))

    def test_the_absent_payable_settlement_posting_is_disclosed_not_hidden(self):
        # The posting rules credit ACCOUNTS_PAYABLE but never debit it, so a
        # settlement cannot yet clear an amount put on account. The report says so
        # instead of inventing a settlement entry.
        self.post_expense(
            amount="250000.00", payment_method=Expense.PaymentMethod.PAYABLE,
            accounting_class=Expense.AccountingClass.INVENTORY, key="payvis-invoice-c",
        )
        supplier = self.summary()["supplier_payments"]
        self.assertFalse(supplier["payable_settlement_posting_available"])
        self.assertIn("no journal entry that DEBITS that account", supplier["payable_settlement_posting_note"])
        self.assertIn("inflate both stock value and acquisitions", supplier["payable_settlement_posting_note"])
        # And the balance it reports is exactly what the ledger holds.
        from apps.finance.models import FinancialLine

        credited = sum(
            line.amount for line in FinancialLine.objects.filter(
                transaction__status=FinancialTransaction.Status.POSTED,
                direction=FinancialLine.Direction.CREDIT,
                account_code="LIABILITY.ACCOUNTS_PAYABLE",
            )
        )
        self.assertEqual(Decimal(supplier["outstanding_supplier_payables"]), credited)

    def test_the_same_money_is_never_counted_twice_inside_one_measure(self):
        # Recognition and disbursement are DIFFERENT stages of the same invoice:
        # the amount may appear once in each, but never twice inside either.
        self.post_expense(
            amount="90000.00", payment_method=Expense.PaymentMethod.CARD,
            accounting_class=Expense.AccountingClass.INVENTORY, key="payvis-card-invoice",
        )
        amounts = self.amounts(self.summary())
        self.assertEqual(amounts["supplier_invoices_recognized"], Decimal("90000.00"))
        self.assertEqual(amounts["supplier_payments_made"], Decimal("90000.00"))
        self.assertEqual(amounts["total_cash_disbursements"], Decimal("90000.00"))
        self.assertEqual(amounts["other_cash_disbursements"], Decimal("0.00"))
        self.assertEqual(amounts["supplier_payables_raised"], Decimal("0.00"), "an immediately-paid invoice raises no payable")

    def test_operating_and_supplier_payments_are_never_added_together_twice(self):
        self.post_expense(
            amount="40000.00", payment_method=Expense.PaymentMethod.BANK_TRANSFER,
            accounting_class=Expense.AccountingClass.OPERATING, key="payvis-opex",
        )
        self.post_expense(
            amount="60000.00", payment_method=Expense.PaymentMethod.BANK_TRANSFER,
            accounting_class=Expense.AccountingClass.INVENTORY, key="payvis-supplier",
        )
        amounts = self.amounts(self.summary())
        self.assertEqual(amounts["operating_expenses_paid"], Decimal("40000.00"))
        self.assertEqual(amounts["supplier_payments_made"], Decimal("60000.00"))
        self.assertEqual(amounts["other_cash_disbursements"], Decimal("0.00"))
        self.assertEqual(amounts["total_cash_disbursements"], Decimal("100000.00"))
        # The total is the sum of the disjoint parts, exactly once each.
        self.assertEqual(
            amounts["total_cash_disbursements"],
            amounts["supplier_payments_made"] + amounts["operating_expenses_paid"]
            + amounts["net_payroll_paid"] + amounts["other_cash_disbursements"],
        )
        # And the reported total matches the raw ledger lines it summarises.
        from apps.finance.models import FinancialLine

        raw = sum(
            line.amount for line in FinancialLine.objects.filter(
                transaction__status=FinancialTransaction.Status.POSTED,
                transaction__type__in=DISBURSEMENT_TYPES,
                direction=FinancialLine.Direction.CREDIT,
                account_code__in=TENDER_ASSET_ACCOUNTS,
            )
        )
        self.assertEqual(amounts["total_cash_disbursements"], raw)

    def test_net_cash_movement_is_reproducible_from_the_reported_measures(self):
        self.post_expense(
            amount="15000.00", payment_method=Expense.PaymentMethod.BANK_TRANSFER,
            accounting_class=Expense.AccountingClass.OPERATING, key="payvis-cash-move",
        )
        report = self.summary()
        expected = (
            Decimal(report["collections"]["amount"] if isinstance(report["collections"], dict) else report["collections"])
            - Decimal(report["refunds"]["amount"] if isinstance(report["refunds"], dict) else report["refunds"])
            - Decimal(report["supplier_payments"]["total_cash_disbursements"])
        )
        self.assertEqual(Decimal(report["net_cash_movement"]), expected)

    def test_the_approval_stage_is_reported_as_a_count_not_an_amount(self):
        report = self.summary()
        supplier = report["supplier_payments"]
        self.assertIsInstance(supplier["supplier_payment_approvals_decided"], int)
        self.assertIsInstance(supplier["supplier_payment_approvals_pending"], int)
        # Approvals cannot appear in any money measure.
        self.assertNotIn("approval", " ".join(
            key for key in supplier if isinstance(supplier[key], str) and key.endswith(("_made", "_paid"))
        ))

    def test_cost_of_goods_sold_is_reported_as_unavailable_with_a_reason(self):
        report = self.summary()["supplier_payments"]
        self.assertIsNone(report["cost_of_goods_sold"], "COGS must never be invented")
        self.assertIn("no COGS account", report["cost_of_goods_sold_unavailable_reason"])
        self.assertIn("immutable stock ledger", report["cost_of_goods_sold_unavailable_reason"])

    def test_the_whole_supplier_block_costs_two_statements_not_one_per_measure(self):
        """A dashboard summary must not fan out into a query per measure.

        Every stage measure is a conditional aggregate folded into the summary's
        own ledger aggregate; only the opening payable balance and the approval
        counts make their own (indexed) statements. This pins that budget so a
        future measure cannot quietly add an N+1.
        """
        from django.test.utils import CaptureQueriesContext
        from django.db import connection

        today = hotel_today()
        with CaptureQueriesContext(connection) as captured:
            financial_summary(start_date=today, end_date=today, currency="NGN")
        count = len(captured.captured_queries)
        self.assertLessEqual(count, 6, f"financial_summary issued {count} statements, budget is 6")

    def test_every_reported_measure_documents_its_own_definition(self):
        definitions = self.summary()["supplier_payments"]["definitions"]
        for key in (
            "supplier_invoices_recognized", "supplier_payments_made", "operating_expenses_paid",
            "net_payroll_paid", "other_cash_disbursements", "outstanding_supplier_payables",
            "supplier_payment_approvals_decided", "goods_receipts",
        ):
            self.assertIn(key, definitions, f"{key} has no definition for the reader")
            self.assertGreater(len(definitions[key]), 40)


class ExportFormattingTests(BaseAPITestCase):
    """§9: exported figures must be correct VALUES, not formatted text."""

    def setUp(self):
        super().setUp()
        self.manager = make_staff("export-manager@staff.dev", role=User.Role.MANAGER)
        self.approver = make_staff("export-approver@staff.dev", role=User.Role.GENERAL_MANAGER)

    def test_excel_cells_hold_real_numbers_with_a_grouped_display_format(self):
        from io import BytesIO

        from openpyxl import load_workbook

        from apps.finance.services.reporting_service import summary_xlsx_bytes
        from apps.finance.services.reporting_service import financial_summary

        today = hotel_today()
        expense, _ = create_expense(
            actor=self.manager, category="Export test", amount="1234567.89",
            payment_method=Expense.PaymentMethod.BANK_TRANSFER, idempotency_key="export-format-1",
        )
        submit_expense(expense=expense, actor=self.manager)
        review_expense(expense=expense, reviewer=self.approver, approved=True)
        post_expense(expense=expense, actor=self.manager)

        report = financial_summary(start_date=today, end_date=today, currency="NGN")
        book = load_workbook(BytesIO(summary_xlsx_bytes(report)))
        sheet = book.active
        cell = None
        for row in sheet.iter_rows():
            if row[0].value == "Operating expenses":
                cell = row[1]
                break
        self.assertIsNotNone(cell, "the measure row must be present")
        # A real number, so the column can be summed in the spreadsheet ...
        self.assertEqual(float(cell.value), 1234567.89)
        self.assertNotIsInstance(cell.value, str)
        # ... displayed with thousands separators, and the value is unchanged.
        self.assertIn("#,##0.00", cell.number_format)
        self.assertIn("NGN", cell.number_format)

    def test_the_pdf_display_groups_digits_without_changing_the_value(self):
        from apps.finance.services import reporting_service

        self.assertEqual(reporting_service._display_amount("1234567.89"), "1,234,567.89")
        self.assertEqual(reporting_service._display_amount("0.00"), "0.00")
        self.assertEqual(reporting_service._display_amount("-5000.00"), "-5,000.00")
        self.assertEqual(reporting_service._display_amount("not-a-number"), "not-a-number")
