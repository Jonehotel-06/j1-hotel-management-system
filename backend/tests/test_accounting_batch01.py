"""Revenue split, total expenses and profit-and-loss regressions (batch 01).

Covers tasks 1, 2, 3, 4 and 5 on the backend side:

* task 1 — "Restaurants and Bar" is a real measure derived from POS outlets,
  not a relabelled total that silently contained accommodation;
* task 2 — Total Expenses is every posted EXPENSE.* account, with no
  procurement double counting and no draft or unapproved cost included;
* task 3 — the statutory tax payable aggregate is gone from the summary and
  is not named on the balance sheet, while its ledger history survives;
* task 4 — booking payments appear as a collection, never as income;
* task 5 — the balance sheet presents net assets and still proves it balances.
"""
from datetime import timedelta
from decimal import Decimal

from apps.accounts.models import User
from apps.core.utils import hotel_today
from apps.finance.models import Expense, FinancialTransaction
from apps.finance.services import accounting
from apps.finance.services.ledger_service import create_posted_transaction
from apps.finance.services.reporting_service import financial_summary
from apps.finance.services.statements_service import balance_sheet

from .base import BaseAPITestCase
from .factories import make_guest, make_room_type, make_staff


class RevenueSplitTests(BaseAPITestCase):
    """Task 1: restaurants and bar, kept apart from rooms and room service."""

    def setUp(self):
        super().setUp()
        self.today = hotel_today()
        self.actor = make_staff("split.actor@staff.dev", role=User.Role.ACCOUNTANT)
        self.room_type = make_room_type("Split Suite", price="30000.00")

    def _pos_order(self, *, mode, reference):
        from apps.pos.models import PosOrder

        extra = {}
        if mode == "ROOM_SERVICE":
            # A room-service order must be attached to a stay and a folio
            # (DB check constraint) — that is exactly what makes it room
            # service rather than counter trade.
            extra = self._stay_and_folio()
        return PosOrder.objects.create(
            reference=reference, mode=mode, status=PosOrder.Status.DELIVERED,
            subtotal=Decimal("0.00"), tax_amount=Decimal("0.00"),
            total_amount=Decimal("0.00"), currency="NGN", created_by=self.actor,
            **extra,
        )

    def _stay_and_folio(self):
        from apps.finance.models import Folio
        from apps.stays.models import Stay

        from .factories import make_booking, make_room

        guest = make_guest(f"split.guest{Stay.objects.count()}@example.com")
        room = make_room(self.room_type, 900 + Stay.objects.count())
        booking = make_booking(guest, self.room_type, rooms=[room],
                               check_in=self.today - timedelta(days=1),
                               check_out=self.today + timedelta(days=1), total="30000.00")
        stay = Stay.objects.create(
            reference=f"STY-B01-{Stay.objects.count() + 1:04d}", booking=booking, guest=guest,
            expected_arrival=booking.check_in, expected_departure=booking.check_out,
        )
        folio = Folio.objects.create(
            reference=f"FOL-B01-{Folio.objects.count() + 1:04d}",
            stay=stay, booking=booking, guest=guest,
        )
        return {"stay": stay, "folio": folio}

    def _sale(self, *, order_reference, amount, collect=False):
        create_posted_transaction(
            transaction_type=FinancialTransaction.Type.POS_CHARGE, actor=self.actor,
            source_key=f"split:charge:{order_reference}", source_reference=order_reference,
            business_date=self.today,
            lines=[
                {"account_code": accounting.ACCOUNTS_RECEIVABLE, "direction": "DEBIT", "amount": Decimal(amount)},
                {"account_code": accounting.FOOD_BEVERAGE_REVENUE, "direction": "CREDIT", "amount": Decimal(amount)},
            ],
        )
        if collect:
            create_posted_transaction(
                transaction_type=FinancialTransaction.Type.PAYMENT_COLLECTION, actor=self.actor,
                source_key=f"split:tender:{order_reference}", source_reference=order_reference,
                business_date=self.today,
                lines=[
                    {"account_code": accounting.CASH_ON_HAND, "direction": "DEBIT", "amount": Decimal(amount)},
                    {"account_code": accounting.ACCOUNTS_RECEIVABLE, "direction": "CREDIT", "amount": Decimal(amount)},
                ],
            )

    def test_restaurant_and_bar_excludes_accommodation_and_room_service(self):
        self._pos_order(mode="RESTAURANT", reference="POS-REST-1")
        self._pos_order(mode="BAR", reference="POS-BAR-1")
        self._pos_order(mode="TAKEAWAY", reference="POS-TAKE-1")
        self._pos_order(mode="ROOM_SERVICE", reference="POS-RS-1")
        self._sale(order_reference="POS-REST-1", amount="18000.00", collect=True)
        self._sale(order_reference="POS-BAR-1", amount="12000.00", collect=True)
        self._sale(order_reference="POS-TAKE-1", amount="5000.00")
        self._sale(order_reference="POS-RS-1", amount="9000.00")
        create_posted_transaction(
            transaction_type=FinancialTransaction.Type.ACCOMMODATION_CHARGE, actor=self.actor,
            source_key="split:room", business_date=self.today,
            lines=[
                {"account_code": accounting.ACCOUNTS_RECEIVABLE, "direction": "DEBIT", "amount": Decimal("60000.00")},
                {"account_code": accounting.ACCOMMODATION_REVENUE, "direction": "CREDIT", "amount": Decimal("60000.00")},
            ],
        )

        report = financial_summary(start_date=self.today, end_date=self.today)
        self.assertEqual(report["restaurant_bar_revenue"]["amount"], "35000.00")
        self.assertEqual(report["room_service_revenue"]["amount"], "9000.00")
        self.assertEqual(report["food_beverage_revenue"]["amount"], "44000.00")
        self.assertEqual(report["accommodation_revenue"]["amount"], "60000.00")
        # Only the counter sales that were actually tendered are collections.
        self.assertEqual(report["restaurant_bar_collections"]["amount"], "30000.00")
        # Nothing is lost and nothing is counted twice.
        self.assertEqual(report["recognized_revenue"]["amount"], "104000.00")
        self.assertEqual(report["unclassified_food_beverage_revenue"]["amount"], "0.00")
        self.assertEqual(
            Decimal(report["restaurant_bar_revenue"]["amount"])
            + Decimal(report["room_service_revenue"]["amount"])
            + Decimal(report["accommodation_revenue"]["amount"]),
            Decimal(report["recognized_revenue"]["amount"]),
        )

    def test_a_returned_item_reduces_the_outlet_it_was_sold_in(self):
        self._pos_order(mode="BAR", reference="POS-BAR-2")
        self._sale(order_reference="POS-BAR-2", amount="20000.00")
        create_posted_transaction(
            transaction_type=FinancialTransaction.Type.ADJUSTMENT, actor=self.actor,
            source_key="split:return", source_reference="POS-BAR-2", business_date=self.today,
            lines=[
                {"account_code": accounting.FOOD_BEVERAGE_REVENUE, "direction": "DEBIT", "amount": Decimal("4000.00")},
                {"account_code": accounting.ACCOUNTS_RECEIVABLE, "direction": "CREDIT", "amount": Decimal("4000.00")},
            ],
        )
        report = financial_summary(start_date=self.today, end_date=self.today)
        self.assertEqual(report["restaurant_bar_revenue"]["amount"], "16000.00")
        self.assertEqual(report["recognized_revenue"]["amount"], "16000.00")

    def test_food_revenue_with_no_pos_order_is_disclosed_not_absorbed(self):
        """A manual F&B journal must not be quietly claimed by the bar."""
        create_posted_transaction(
            transaction_type=FinancialTransaction.Type.ADJUSTMENT, actor=self.actor,
            source_key="split:manual", source_reference="JRN-MANUAL", business_date=self.today,
            lines=[
                {"account_code": accounting.CASH_ON_HAND, "direction": "DEBIT", "amount": Decimal("2500.00")},
                {"account_code": accounting.FOOD_BEVERAGE_REVENUE, "direction": "CREDIT", "amount": Decimal("2500.00")},
            ],
        )
        report = financial_summary(start_date=self.today, end_date=self.today)
        self.assertEqual(report["restaurant_bar_revenue"]["amount"], "0.00")
        self.assertEqual(report["unclassified_food_beverage_revenue"]["amount"], "2500.00")
        labels = [row["label"] for row in report["profit_and_loss"]["income"]]
        self.assertIn("Food and beverage not linked to a POS outlet", labels)


class TotalExpenseTests(BaseAPITestCase):
    """Task 2: one honest total, reconciled to its own detail."""

    def setUp(self):
        super().setUp()
        self.today = hotel_today()
        self.actor = make_staff("expense.actor@staff.dev", role=User.Role.ACCOUNTANT)

    def _expense(self, *, amount, department, category, accounting_class=Expense.AccountingClass.OPERATING,
                 status=Expense.Status.POSTED, reference=None):
        reference = reference or f"EXP-B01-{Expense.objects.count() + 1:04d}"
        financial_transaction = None
        if status == Expense.Status.POSTED:
            debit = (accounting.INVENTORY_ASSET
                     if accounting_class == Expense.AccountingClass.INVENTORY
                     else accounting.OPERATING_EXPENSE)
            financial_transaction, _ = create_posted_transaction(
                transaction_type=FinancialTransaction.Type.EXPENSE, actor=self.actor,
                source_key=f"expense:{reference}", business_date=self.today,
                lines=[
                    {"account_code": debit, "direction": "DEBIT", "amount": Decimal(amount)},
                    {"account_code": accounting.ACCOUNTS_PAYABLE, "direction": "CREDIT",
                     "amount": Decimal(amount)},
                ],
            )
        # Created in its final state in one write: a posted expense is
        # immutable, so it can never be saved twice.
        return Expense.objects.create(
            reference=reference, status=status, category=category, department=department,
            accounting_class=accounting_class, amount=Decimal(amount), currency="NGN",
            incurred_on=self.today, requested_by=self.actor,
            financial_transaction=financial_transaction,
        )

    def test_total_expenses_covers_every_posted_expense_account(self):
        self._expense(amount="40000.00", department="Kitchen", category="Food supplies")
        self._expense(amount="15000.00", department="Housekeeping", category="Cleaning")
        self._expense(amount="25000.00", department="Procurement", category="Hardware")
        create_posted_transaction(
            transaction_type=FinancialTransaction.Type.PAYROLL_ACCRUAL, actor=self.actor,
            source_key="expense:payroll", business_date=self.today,
            lines=[
                {"account_code": accounting.PAYROLL_EXPENSE, "direction": "DEBIT", "amount": Decimal("300000.00")},
                {"account_code": accounting.PAYROLL_PAYABLE, "direction": "CREDIT", "amount": Decimal("300000.00")},
            ],
        )
        create_posted_transaction(
            transaction_type=FinancialTransaction.Type.CASH_CLOSE, actor=self.actor,
            source_key="expense:shortage", business_date=self.today,
            lines=[
                {"account_code": accounting.CASH_OVER_SHORT, "direction": "DEBIT", "amount": Decimal("1200.00")},
                {"account_code": accounting.CASH_ON_HAND, "direction": "CREDIT", "amount": Decimal("1200.00")},
            ],
        )

        report = financial_summary(start_date=self.today, end_date=self.today)
        self.assertEqual(report["total_expenses"]["amount"], "381200.00")
        breakdown = report["expense_breakdown"]
        self.assertTrue(breakdown["reconciliation"]["reconciles"], breakdown["reconciliation"])
        self.assertEqual(breakdown["reconciliation"]["difference"], "0.00")
        by_department = {row["label"]: row["amount"] for row in breakdown["by_department"]}
        self.assertEqual(by_department["Kitchen"], "40000.00")
        self.assertEqual(by_department["Housekeeping"], "15000.00")
        self.assertEqual(by_department["Procurement"], "25000.00")
        accounts = {row["account_code"]: row["amount"] for row in breakdown["by_account"]}
        self.assertEqual(accounts[accounting.OPERATING_EXPENSE], "80000.00")
        self.assertEqual(accounts[accounting.PAYROLL_EXPENSE], "300000.00")
        self.assertEqual(accounts[accounting.CASH_OVER_SHORT], "1200.00")

    def test_unposted_expenses_are_excluded(self):
        self._expense(amount="10000.00", department="Kitchen", category="Food supplies")
        for status in (Expense.Status.DRAFT, Expense.Status.SUBMITTED,
                       Expense.Status.APPROVED, Expense.Status.REJECTED):
            self._expense(amount="999999.00", department="Kitchen", category="Never posted",
                          status=status)

        report = financial_summary(start_date=self.today, end_date=self.today)
        self.assertEqual(report["total_expenses"]["amount"], "10000.00")
        categories = {row["label"] for row in report["expense_breakdown"]["by_category"]}
        self.assertNotIn("Never posted", categories)

    def test_stock_purchases_are_an_asset_not_an_expense(self):
        self._expense(amount="50000.00", department="Stores", category="Drinks stock",
                      accounting_class=Expense.AccountingClass.INVENTORY)
        self._expense(amount="7000.00", department="Kitchen", category="Gas")

        report = financial_summary(start_date=self.today, end_date=self.today)
        self.assertEqual(report["total_expenses"]["amount"], "7000.00")
        self.assertEqual(report["inventory_acquisitions"]["amount"], "50000.00")
        self.assertEqual(
            report["expense_breakdown"]["inventory_class_expenses"]["amount"], "50000.00")

    def test_paying_a_supplier_later_does_not_count_the_cost_twice(self):
        self._expense(amount="90000.00", department="Procurement", category="Linen",
                      reference="EXP-PAYABLE-1")
        # Settlement debits ACCOUNTS PAYABLE, never the expense account again.
        create_posted_transaction(
            transaction_type=FinancialTransaction.Type.EXPENSE, actor=self.actor,
            source_key="expense:settle", business_date=self.today,
            metadata={"expense_settlement": True, "accounting_class": "OPERATING"},
            lines=[
                {"account_code": accounting.ACCOUNTS_PAYABLE, "direction": "DEBIT", "amount": Decimal("90000.00")},
                {"account_code": accounting.BANK_CLEARING, "direction": "CREDIT", "amount": Decimal("90000.00")},
            ],
        )
        report = financial_summary(start_date=self.today, end_date=self.today)
        self.assertEqual(report["total_expenses"]["amount"], "90000.00")

    def test_a_reversal_reduces_the_total(self):
        self._expense(amount="20000.00", department="Maintenance", category="Repairs")
        create_posted_transaction(
            transaction_type=FinancialTransaction.Type.ADJUSTMENT, actor=self.actor,
            source_key="expense:reverse", business_date=self.today,
            lines=[
                {"account_code": accounting.ACCOUNTS_PAYABLE, "direction": "DEBIT", "amount": Decimal("5000.00")},
                {"account_code": accounting.OPERATING_EXPENSE, "direction": "CREDIT", "amount": Decimal("5000.00")},
            ],
        )
        report = financial_summary(start_date=self.today, end_date=self.today)
        self.assertEqual(report["total_expenses"]["amount"], "15000.00")
        self.assertTrue(report["expense_breakdown"]["reconciliation"]["reconciles"])

    def test_the_range_is_respected(self):
        self._expense(amount="12000.00", department="Kitchen", category="Food supplies")
        report = financial_summary(
            start_date=self.today - timedelta(days=10), end_date=self.today - timedelta(days=5))
        self.assertEqual(report["total_expenses"]["amount"], "0.00")
        self.assertEqual(report["expense_breakdown"]["by_department"], [])


class ProfitAndLossTests(BaseAPITestCase):
    """Task 4: income, expenses and collections, never confused."""

    def setUp(self):
        super().setUp()
        self.today = hotel_today()
        self.actor = make_staff("pnl.actor@staff.dev", role=User.Role.ACCOUNTANT)
        make_guest("pnl.guest@example.com")

    def test_a_booking_payment_is_a_collection_and_never_income(self):
        create_posted_transaction(
            transaction_type=FinancialTransaction.Type.PAYMENT_COLLECTION, actor=self.actor,
            source_key="pnl:deposit", business_date=self.today,
            lines=[
                {"account_code": accounting.BANK_CLEARING, "direction": "DEBIT", "amount": Decimal("250000.00")},
                {"account_code": accounting.GUEST_DEPOSITS, "direction": "CREDIT", "amount": Decimal("250000.00")},
            ],
        )
        create_posted_transaction(
            transaction_type=FinancialTransaction.Type.ACCOMMODATION_CHARGE, actor=self.actor,
            source_key="pnl:night", business_date=self.today,
            lines=[
                {"account_code": accounting.ACCOUNTS_RECEIVABLE, "direction": "DEBIT", "amount": Decimal("80000.00")},
                {"account_code": accounting.ACCOMMODATION_REVENUE, "direction": "CREDIT", "amount": Decimal("80000.00")},
            ],
        )

        report = financial_summary(start_date=self.today, end_date=self.today)
        self.assertEqual(report["booking_payments_received"]["amount"], "250000.00")
        # Income is the night earned, NOT the ₦250,000 received.
        self.assertEqual(report["recognized_revenue"]["amount"], "80000.00")
        profit_and_loss = report["profit_and_loss"]
        self.assertEqual(profit_and_loss["total_income"], "80000.00")
        self.assertEqual(profit_and_loss["net_result"], "80000.00")
        memorandum = {row["key"]: row["amount"] for row in profit_and_loss["collections_memorandum"]}
        self.assertEqual(memorandum["booking_payments_received"], "250000.00")
        income_keys = {row["key"] for row in profit_and_loss["income"]}
        self.assertNotIn("booking_payments_received", income_keys)
        self.assertNotIn("collections", income_keys)

    def test_net_result_is_income_less_discounts_and_total_expenses(self):
        create_posted_transaction(
            transaction_type=FinancialTransaction.Type.ACCOMMODATION_CHARGE, actor=self.actor,
            source_key="pnl:revenue", business_date=self.today,
            lines=[
                {"account_code": accounting.ACCOUNTS_RECEIVABLE, "direction": "DEBIT", "amount": Decimal("500000.00")},
                {"account_code": accounting.ACCOMMODATION_REVENUE, "direction": "CREDIT", "amount": Decimal("500000.00")},
            ],
        )
        create_posted_transaction(
            transaction_type=FinancialTransaction.Type.DISCOUNT, actor=self.actor,
            source_key="pnl:discount", business_date=self.today,
            lines=[
                {"account_code": accounting.DISCOUNTS_ALLOWED, "direction": "DEBIT", "amount": Decimal("20000.00")},
                {"account_code": accounting.ACCOUNTS_RECEIVABLE, "direction": "CREDIT", "amount": Decimal("20000.00")},
            ],
        )
        create_posted_transaction(
            transaction_type=FinancialTransaction.Type.EXPENSE, actor=self.actor,
            source_key="pnl:cost", business_date=self.today,
            lines=[
                {"account_code": accounting.OPERATING_EXPENSE, "direction": "DEBIT", "amount": Decimal("130000.00")},
                {"account_code": accounting.CASH_ON_HAND, "direction": "CREDIT", "amount": Decimal("130000.00")},
            ],
        )

        report = financial_summary(start_date=self.today, end_date=self.today)
        self.assertEqual(report["net_result"], "350000.00")
        self.assertEqual(report["profit_and_loss"]["net_result"], "350000.00")
        self.assertEqual(
            Decimal(report["recognized_revenue"]["amount"])
            - Decimal(report["discounts"]["amount"])
            - Decimal(report["total_expenses"]["amount"]),
            Decimal(report["net_result"]),
        )


class StatutoryTaxAndEquityPresentationTests(BaseAPITestCase):
    """Tasks 3 and 5: withdrawn from the statement, intact in the ledger."""

    def setUp(self):
        super().setUp()
        self.today = hotel_today()
        self.actor = make_staff("pres.actor@staff.dev", role=User.Role.ACCOUNTANT)

    def test_statutory_tax_is_not_named_but_still_counted_and_still_posted(self):
        create_posted_transaction(
            transaction_type=FinancialTransaction.Type.ACCOMMODATION_CHARGE, actor=self.actor,
            source_key="pres:historic-tax", business_date=self.today - timedelta(days=2),
            lines=[
                {"account_code": accounting.ACCOUNTS_RECEIVABLE, "direction": "DEBIT", "amount": Decimal("107500.00")},
                {"account_code": accounting.ACCOMMODATION_REVENUE, "direction": "CREDIT", "amount": Decimal("100000.00")},
                {"account_code": accounting.TAX_PAYABLE, "direction": "CREDIT", "amount": Decimal("7500.00")},
            ],
        )
        sheet = balance_sheet(as_of=self.today)
        labels = [row["label"] for row in sheet["liabilities"]]
        codes = [row["account_code"] for row in sheet["liabilities"]]
        self.assertNotIn(accounting.TAX_PAYABLE, codes)
        self.assertNotIn("Statutory tax payable (historical guest tax)", labels)
        # The balance is still in total liabilities, inside a neutral line, so
        # the section foots and the statement still balances.
        self.assertIn("Other liabilities", labels)
        self.assertEqual(
            sum(Decimal(row["amount"]) for row in sheet["liabilities"]),
            Decimal(sheet["total_liabilities"]),
        )
        self.assertTrue(sheet["balanced"], sheet)
        # And the posting itself is untouched.
        from apps.finance.models import FinancialLine

        self.assertEqual(
            FinancialLine.objects.filter(account_code=accounting.TAX_PAYABLE).count(), 1)

        summary = financial_summary(
            start_date=self.today - timedelta(days=3), end_date=self.today)
        self.assertNotIn("tax_accrued", summary)

    def test_equity_is_presented_as_net_assets_and_the_sheet_still_balances(self):
        create_posted_transaction(
            transaction_type=FinancialTransaction.Type.ADJUSTMENT, actor=self.actor,
            source_key="pres:opening", business_date=self.today - timedelta(days=5),
            lines=[
                {"account_code": accounting.CASH_ON_HAND, "direction": "DEBIT", "amount": Decimal("400000.00")},
                {"account_code": accounting.OPENING_BALANCE_EQUITY, "direction": "CREDIT", "amount": Decimal("400000.00")},
            ],
        )
        sheet = balance_sheet(as_of=self.today)
        self.assertNotIn("equity", sheet)
        self.assertNotIn("total_equity", sheet)
        self.assertNotIn("total_liabilities_and_equity", sheet)
        self.assertEqual(sheet["net_assets"], "400000.00")
        self.assertEqual(
            Decimal(sheet["net_assets"]),
            Decimal(sheet["total_assets"]) - Decimal(sheet["total_liabilities"]),
        )
        self.assertTrue(sheet["balanced"], sheet)
        self.assertEqual(sheet["difference"], "0.00")
        self.assertEqual(sheet["presentation"]["equity_section"], "withdrawn")
        # The equity journal is still in the ledger.
        from apps.finance.models import FinancialLine

        self.assertEqual(
            FinancialLine.objects.filter(
                account_code=accounting.OPENING_BALANCE_EQUITY).count(), 1)
