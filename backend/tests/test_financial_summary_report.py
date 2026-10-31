"""Ledger-authoritative financial summary report coverage."""
from datetime import timedelta

from apps.accounts.models import User
from apps.core.utils import hotel_today
from apps.finance.models import FinancialLine, FinancialTransaction
from apps.finance.services import accounting
from apps.finance.services.ledger_service import create_posted_transaction
from apps.finance.services.reporting_service import financial_summary

from .base import BaseAPITestCase
from .factories import make_staff


class FinancialSummaryReportTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.today = hotel_today()
        self.manager = make_staff("financial-summary-manager@staff.dev", role=User.Role.MANAGER)
        self.receptionist = make_staff("financial-summary-frontdesk@staff.dev", role=User.Role.RECEPTIONIST)
        self._post_sample_ledger()

    def _post(self, *, suffix, transaction_type, lines, business_date=None, currency="NGN"):
        return create_posted_transaction(
            transaction_type=transaction_type,
            source_key=f"financial-summary:{suffix}",
            idempotency_key=f"financial-summary:{suffix}",
            actor=self.manager,
            source_reference=f"SUM-{suffix}",
            narrative=f"Financial summary fixture {suffix}",
            currency=currency,
            business_date=business_date or self.today,
            lines=lines,
        )

    @staticmethod
    def _line(account_code, direction, amount):
        return {"account_code": account_code, "direction": direction, "amount": amount}

    def _post_sample_ledger(self):
        debit = FinancialLine.Direction.DEBIT
        credit = FinancialLine.Direction.CREDIT
        self._post(
            suffix="charge",
            transaction_type=FinancialTransaction.Type.ACCOMMODATION_CHARGE,
            lines=[
                self._line(accounting.ACCOUNTS_RECEIVABLE, debit, "110.00"),
                self._line(accounting.ACCOMMODATION_REVENUE, credit, "100.00"),
                self._line(accounting.TAX_PAYABLE, credit, "10.00"),
            ],
        )
        self._post(
            suffix="collection",
            transaction_type=FinancialTransaction.Type.PAYMENT_COLLECTION,
            lines=[
                self._line(accounting.CASH_ON_HAND, debit, "110.00"),
                self._line(accounting.GUEST_DEPOSITS, credit, "110.00"),
            ],
        )
        self._post(
            suffix="refund",
            transaction_type=FinancialTransaction.Type.REFUND,
            lines=[
                self._line(accounting.GUEST_DEPOSITS, debit, "20.00"),
                self._line(accounting.CASH_ON_HAND, credit, "20.00"),
            ],
        )
        self._post(
            suffix="operating-expense",
            transaction_type=FinancialTransaction.Type.EXPENSE,
            lines=[
                self._line(accounting.OPERATING_EXPENSE, debit, "15.00"),
                self._line(accounting.CASH_ON_HAND, credit, "15.00"),
            ],
        )
        self._post(
            suffix="inventory-expense",
            transaction_type=FinancialTransaction.Type.EXPENSE,
            lines=[
                self._line(accounting.INVENTORY_ASSET, debit, "30.00"),
                self._line(accounting.CASH_ON_HAND, credit, "30.00"),
            ],
        )
        self._post(
            suffix="outside-range",
            transaction_type=FinancialTransaction.Type.PAYMENT_COLLECTION,
            business_date=self.today + timedelta(days=1),
            lines=[
                self._line(accounting.BANK_CLEARING, debit, "999.00"),
                self._line(accounting.GUEST_DEPOSITS, credit, "999.00"),
            ],
        )
        self._post(
            suffix="other-currency",
            transaction_type=FinancialTransaction.Type.PAYMENT_COLLECTION,
            currency="USD",
            lines=[
                self._line(accounting.BANK_CLEARING, debit, "77.00"),
                self._line(accounting.GUEST_DEPOSITS, credit, "77.00"),
            ],
        )

    def test_summary_uses_posted_ledger_lines_without_conflating_measures(self):
        # The bounded plan is seven statements, and each one is named so the
        # budget cannot drift silently:
        #   1. one transaction count (`ledger_transactions`);
        #   2. one conditional aggregate across ledger lines (every measure);
        #   3. one conditional aggregate grouped by business date;
        #   4. one grouped revenue aggregate (by department);
        #   5. one goods-receipt aggregate (supplier stock received, which is an
        #      inventory fact and deliberately NOT a posted-money measure);
        #   6. the opening payable balance for the supplier-payment block;
        #   7. the expense-approval counts for the same block.
        # Every supplier-payment measure itself rides inside the existing
        # conditional aggregate (see settlement_measure_filters), so the block
        # added exactly two statements and did not multiply the per-measure
        # query count. This number is a tight budget, not an observation:
        # raising it needs a new documented aggregate here.
        with self.assertNumQueries(9):  # +2: expense-by-account roll-up and the posted-expense detail (batch 01 task 2)
            report = financial_summary(start_date=self.today, end_date=self.today)

        self.assertEqual(report["basis"], "posted_immutable_ledger_business_date")
        self.assertEqual(report["ledger_transactions"], 5)
        self.assertEqual(report["guest_charges"], {"amount": "110.00", "transactions": 1})
        self.assertEqual(report["recognized_revenue"], {"amount": "100.00", "transactions": 1})
        # Batch 01 task 3: the statutory tax payable aggregate is no longer
        # presented by the financial summary. The historic ledger posting is
        # untouched and still reachable through the chart of accounts.
        self.assertNotIn("tax_accrued", report)
        self.assertEqual(report["collections"], {"amount": "110.00", "transactions": 1})
        self.assertEqual(report["refunds"], {"amount": "20.00", "transactions": 1})
        self.assertEqual(report["operating_expenses"], {"amount": "15.00", "transactions": 1})
        self.assertEqual(report["inventory_acquisitions"], {"amount": "30.00", "transactions": 1})
        self.assertEqual(report["cash_paid_out"], {"amount": "45.00", "transactions": 2})
        self.assertEqual(report["net_collections"], "90.00")
        self.assertEqual(report["gross_operating_result"], "85.00")
        self.assertEqual(report["actual_payments_made"], "45.00")
        self.assertEqual(report["by_department"][0]["account_code"], accounting.ACCOMMODATION_REVENUE)
        self.assertEqual(report["by_department"][0]["amount"], "100.00")
        self.assertEqual(len(report["by_day"]), 1)
        self.assertEqual(report["by_day"][0]["date"], self.today.isoformat())
        self.assertEqual(report["by_day"][0]["cash_paid_out"], {"amount": "45.00"})

        usd_report = financial_summary(start_date=self.today, end_date=self.today, currency="usd")
        self.assertEqual(usd_report["currency"], "USD")
        self.assertEqual(usd_report["ledger_transactions"], 1)
        self.assertEqual(usd_report["collections"], {"amount": "77.00", "transactions": 1})

    def test_salary_payment_report_nets_immutable_reversals(self):
        debit = FinancialLine.Direction.DEBIT
        credit = FinancialLine.Direction.CREDIT
        payment, _ = self._post(
            suffix="salary-payment",
            transaction_type=FinancialTransaction.Type.PAYROLL_PAYMENT,
            lines=[
                self._line(accounting.PAYROLL_PAYABLE, debit, "50000.00"),
                self._line(accounting.BANK_CLEARING, credit, "50000.00"),
            ],
        )
        create_posted_transaction(
            transaction_type=FinancialTransaction.Type.PAYROLL_PAYMENT,
            source_key="financial-summary:salary-payment-reversal",
            idempotency_key="financial-summary:salary-payment-reversal",
            actor=self.manager,
            source_reference="SAL-TEST-REVERSAL",
            narrative="Authorized salary-payment reversal fixture",
            currency="NGN",
            business_date=self.today,
            reversal_of=payment,
            lines=[
                self._line(accounting.BANK_CLEARING, debit, "15000.00"),
                self._line(accounting.PAYROLL_PAYABLE, credit, "15000.00"),
            ],
        )

        report = financial_summary(start_date=self.today, end_date=self.today)
        self.assertEqual(report["payroll_payments"], {"amount": "50000.00", "transactions": 1})
        self.assertEqual(report["payroll_payment_reversals"], {"amount": "15000.00", "transactions": 1})
        self.assertEqual(report["net_payroll_payments"], "35000.00")
        self.assertEqual(report["by_day"][0]["net_payroll_payments"], "35000.00")

    def test_summary_range_index_is_declared_for_posted_currency_business_date_filter(self):
        indexes = [tuple(index.fields) for index in FinancialTransaction._meta.indexes]
        self.assertIn(("status", "currency", "business_date"), indexes)

    def test_summary_endpoint_requires_financial_report_capability(self):
        params = {"start": self.today.isoformat(), "end": self.today.isoformat()}
        self.auth(self.receptionist)
        self.assertEqual(self.client.get("/api/admin/finance/reports/summary/", params).status_code, 403)

        self.auth(self.manager)
        response = self.client.get("/api/admin/finance/reports/summary/", params)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["data"]["recognized_revenue"]["amount"], "100.00")

    def test_summary_endpoint_validates_bounded_single_currency_range(self):
        self.auth(self.manager)
        self.assertEqual(
            self.client.get("/api/admin/finance/reports/summary/", {"start": self.today.isoformat()}).status_code,
            400,
        )
        self.assertEqual(
            self.client.get(
                "/api/admin/finance/reports/summary/",
                {"start": self.today.isoformat(), "end": self.today.isoformat(), "currency": "N"},
            ).status_code,
            400,
        )

    def test_summary_excel_and_pdf_exports_use_the_same_ledger_totals(self):
        self.auth(self.manager)
        params = {"start": self.today.isoformat(), "end": self.today.isoformat(), "export": "xlsx"}
        xlsx = self.client.get("/api/admin/finance/reports/summary/", params)
        self.assertEqual(xlsx.status_code, 200, xlsx.content)
        self.assertIn("spreadsheetml", xlsx["Content-Type"])
        self.assertGreater(len(xlsx.content), 100)
        params["export"] = "pdf"
        pdf = self.client.get("/api/admin/finance/reports/summary/", params)
        self.assertEqual(pdf.status_code, 200, pdf.content)
        self.assertEqual(pdf["Content-Type"], "application/pdf")
        self.assertTrue(pdf.content.startswith(b"%PDF"))

class Batch02LedgerRegressionTests(FinancialSummaryReportTests):
    """Batch 02: signed revenue, paid-only disbursements and read-only details."""

    def test_revenue_reversal_and_cash_disbursements_reconcile(self):
        debit, credit = FinancialLine.Direction.DEBIT, FinancialLine.Direction.CREDIT
        self._post(suffix="bar", transaction_type=FinancialTransaction.Type.POS_CHARGE, lines=[
            self._line(accounting.ACCOUNTS_RECEIVABLE, debit, "60.00"),
            self._line(accounting.FOOD_BEVERAGE_REVENUE, credit, "60.00"),
        ])
        original, _ = self._post(suffix="restaurant", transaction_type=FinancialTransaction.Type.POS_CHARGE, lines=[
            self._line(accounting.ACCOUNTS_RECEIVABLE, debit, "10.00"),
            self._line(accounting.FOOD_BEVERAGE_REVENUE, credit, "10.00"),
        ])
        reversal, _ = create_posted_transaction(
            transaction_type=FinancialTransaction.Type.VOID, actor=self.manager,
            source_key="financial-summary:restaurant-reversed", reversal_of=original,
            business_date=self.today, lines=[
                self._line(accounting.ACCOUNTS_RECEIVABLE, credit, "10.00"),
                self._line(accounting.FOOD_BEVERAGE_REVENUE, debit, "10.00"),
            ],
        )
        self._post(suffix="payable", transaction_type=FinancialTransaction.Type.EXPENSE, lines=[
            self._line(accounting.OPERATING_EXPENSE, debit, "25.00"),
            self._line(accounting.ACCOUNTS_PAYABLE, credit, "25.00"),
        ])
        self._post(suffix="paid-payable", transaction_type=FinancialTransaction.Type.EXPENSE, lines=[
            self._line(accounting.ACCOUNTS_PAYABLE, debit, "25.00"),
            self._line(accounting.BANK_CLEARING, credit, "25.00"),
        ])
        expense = FinancialTransaction.objects.get(source_key="financial-summary:operating-expense")
        create_posted_transaction(
            transaction_type=FinancialTransaction.Type.VOID, actor=self.manager,
            source_key="financial-summary:expense-reversed", reversal_of=expense,
            business_date=self.today, lines=[
                self._line(accounting.CASH_ON_HAND, debit, "15.00"),
                self._line(accounting.OPERATING_EXPENSE, credit, "15.00"),
            ],
        )
        summary = financial_summary(start_date=self.today, end_date=self.today)
        self.assertEqual(summary["recognized_revenue"]["amount"], "160.00")
        self.assertEqual(summary["by_day"][0]["recognized_revenue"]["amount"], "160.00")
        departments = {row["account_code"]: row["amount"] for row in summary["by_department"]}
        self.assertEqual(departments[accounting.FOOD_BEVERAGE_REVENUE], "60.00")
        self.assertEqual(summary["supplier_payments"]["total_cash_disbursements"], "55.00")
        self.assertEqual(summary["operating_expenses"]["amount"], "25.00")
        self.assertEqual(summary["gross_operating_result"], "135.00")
        self.auth(self.manager)
        url = f"/api/admin/finance/transactions/{original.reference}/"
        detail = self.client.get(url)
        self.assertEqual(detail.status_code, 200, detail.content)
        self.assertEqual(detail.json()["data"]["reversals"][0]["reference"], reversal.reference)
        self.assertEqual(self.client.patch(url, {"narrative": "overwrite"}, format="json").status_code, 405)
        self.auth(self.receptionist)
        self.assertEqual(self.client.get(url).status_code, 403)
