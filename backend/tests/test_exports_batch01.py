"""Excel and PDF export regression suite (batch 01, task 8).

The reported failure was that every Excel/PDF button in Accounting and Reports
returned "The server did not return a downloadable file". The cause was in the
browser, not the server (``api.js downloadReport`` dropped ``start``, ``end``
and ``export`` from the query string — covered by the JS suite), but the
backend had no test proving the file it produces is actually openable, carries
the screen's figures, formats money as naira, or survives an empty period.
These tests close that gap.
"""
from datetime import timedelta
from decimal import Decimal
from io import BytesIO

from apps.accounts.models import User
from apps.core.utils import hotel_today
from apps.finance.models import FinancialTransaction
from apps.finance.services import accounting
from apps.finance.services.ledger_service import create_posted_transaction
from apps.finance.services.reporting_service import (
    financial_summary,
    summary_pdf_bytes,
    summary_xlsx_bytes,
)

from .base import BaseAPITestCase
from .factories import make_staff

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class SummaryExportTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.today = hotel_today()
        self.start = self.today - timedelta(days=3)
        self.manager = make_staff("export.manager@staff.dev", role=User.Role.MANAGER)
        self.actor = make_staff("export.actor@staff.dev", role=User.Role.ACCOUNTANT)

    def _post_activity(self):
        create_posted_transaction(
            transaction_type=FinancialTransaction.Type.ACCOMMODATION_CHARGE,
            actor=self.actor, source_key="export:room", business_date=self.today,
            lines=[
                {"account_code": accounting.ACCOUNTS_RECEIVABLE, "direction": "DEBIT",
                 "amount": Decimal("125000.00")},
                {"account_code": accounting.ACCOMMODATION_REVENUE, "direction": "CREDIT",
                 "amount": Decimal("125000.00")},
            ],
        )
        create_posted_transaction(
            transaction_type=FinancialTransaction.Type.EXPENSE,
            actor=self.actor, source_key="export:expense", business_date=self.today,
            lines=[
                {"account_code": accounting.OPERATING_EXPENSE, "direction": "DEBIT",
                 "amount": Decimal("38000.00")},
                {"account_code": accounting.CASH_ON_HAND, "direction": "CREDIT",
                 "amount": Decimal("38000.00")},
            ],
        )

    def _report(self):
        return financial_summary(start_date=self.start, end_date=self.today)

    # -- workbook ---------------------------------------------------------
    def test_workbook_is_valid_and_carries_the_screen_figures(self):
        from openpyxl import load_workbook

        self._post_activity()
        report = self._report()
        book = load_workbook(BytesIO(summary_xlsx_bytes(report)))
        self.assertEqual(book.sheetnames, ["Financial summary", "Profit and loss", "Expenses"])

        summary = book["Financial summary"]
        header = [cell.value for cell in summary[7]][:3]
        self.assertEqual(header, ["Measure", "Amount", "Ledger transactions"])
        measures = {
            row[0]: row[1] for row in summary.iter_rows(min_row=8, max_col=3, values_only=True)
            if row and row[0]
        }
        # Real numbers, not pre-formatted strings, so Excel can sum the column.
        self.assertNotIsInstance(measures["Total revenue earned"], str)
        self.assertEqual(Decimal(measures["Total revenue earned"]), Decimal("125000.00"))
        self.assertEqual(Decimal(measures["Accommodation earned (rooms)"]), Decimal("125000.00"))
        self.assertEqual(Decimal(measures["Total expenses"]), Decimal("38000.00"))
        self.assertEqual(Decimal(measures["Total revenue earned"]),
                         Decimal(report["recognized_revenue"]["amount"]))
        self.assertEqual(Decimal(measures["Total expenses"]),
                         Decimal(report["total_expenses"]["amount"]))
        # Currency is declared in the cell format, so the figure stays numeric.
        self.assertIn("NGN", summary.cell(row=8, column=2).number_format)

    def test_workbook_profit_and_loss_sheet_separates_income_from_collections(self):
        from openpyxl import load_workbook

        self._post_activity()
        book = load_workbook(BytesIO(summary_xlsx_bytes(self._report())))
        labels = [row[0] for row in book["Profit and loss"].iter_rows(max_col=1, values_only=True)]
        self.assertIn("Accommodation earned (rooms)", labels)
        self.assertIn("Restaurants and bar", labels)
        self.assertIn("Total expenses", labels)
        self.assertIn("Net result", labels)
        self.assertIn("Cash collected in the period (NOT income)", labels)
        self.assertIn("Booking payments received (cash collected, not revenue)", labels)

    def test_workbook_expense_sheet_reconciles_to_the_ledger_total(self):
        from openpyxl import load_workbook

        self._post_activity()
        report = self._report()
        book = load_workbook(BytesIO(summary_xlsx_bytes(report)))
        rows = list(book["Expenses"].iter_rows(values_only=True))
        reconciliation = next(row for row in rows if row[0] == "EXPENSE.* debits less reversing credits")
        self.assertEqual(reconciliation[1], report["total_expenses"]["amount"])
        self.assertEqual(reconciliation[3], "0.00")
        self.assertEqual(reconciliation[4], "YES")

    def test_workbook_is_still_valid_for_an_empty_period(self):
        from openpyxl import load_workbook

        quiet = financial_summary(
            start_date=self.today - timedelta(days=200),
            end_date=self.today - timedelta(days=190),
        )
        book = load_workbook(BytesIO(summary_xlsx_bytes(quiet)))
        self.assertEqual(book.sheetnames, ["Financial summary", "Profit and loss", "Expenses"])
        measures = {
            row[0]: row[1] for row in book["Financial summary"].iter_rows(
                min_row=8, max_col=2, values_only=True) if row and row[0]
        }
        self.assertEqual(Decimal(measures["Total expenses"]), Decimal("0.00"))

    # -- PDF ---------------------------------------------------------------
    def test_pdf_is_a_real_document_and_states_amounts_in_naira(self):
        self._post_activity()
        payload = summary_pdf_bytes(self._report())
        self.assertTrue(payload.startswith(b"%PDF"))
        self.assertTrue(payload.rstrip().endswith(b"%%EOF"))
        self.assertGreater(len(payload), 2000)

    def test_pdf_survives_an_empty_period(self):
        quiet = financial_summary(
            start_date=self.today - timedelta(days=200),
            end_date=self.today - timedelta(days=190),
        )
        payload = summary_pdf_bytes(quiet)
        self.assertTrue(payload.startswith(b"%PDF"))

    def test_display_amount_formats_naira_without_an_unprintable_glyph(self):
        from apps.finance.services.reporting_service import _display_amount

        self.assertEqual(_display_amount("1234567.5", "NGN"), "NGN 1,234,567.50")
        self.assertEqual(_display_amount("-80.1", "NGN"), "NGN -80.10")
        self.assertEqual(_display_amount("unavailable", "NGN"), "unavailable")

    # -- endpoint ----------------------------------------------------------
    def test_endpoint_returns_each_format_with_the_right_headers(self):
        self._post_activity()
        self.auth(self.manager)
        params = {"start": self.start.isoformat(), "end": self.today.isoformat()}
        for export, mime, extension in (("xlsx", XLSX_MIME, "xlsx"), ("pdf", "application/pdf", "pdf")):
            with self.subTest(export=export):
                response = self.client.get(
                    "/api/admin/finance/reports/summary/", {**params, "export": export})
                self.assertEqual(response.status_code, 200, response.content)
                self.assertEqual(response["Content-Type"], mime)
                self.assertEqual(
                    response["Content-Disposition"],
                    f'attachment; filename="jone-ledger-{self.start}_{self.today}.{extension}"',
                )
                self.assertGreater(len(response.content), 1000)

    def test_export_honours_the_requested_date_range(self):
        from openpyxl import load_workbook

        self._post_activity()
        self.auth(self.manager)
        # A window that ends before the activity must export a nil report.
        response = self.client.get("/api/admin/finance/reports/summary/", {
            "start": (self.today - timedelta(days=30)).isoformat(),
            "end": (self.today - timedelta(days=20)).isoformat(),
            "export": "xlsx",
        })
        self.assertEqual(response.status_code, 200, response.content)
        book = load_workbook(BytesIO(response.content))
        measures = {
            row[0]: row[1] for row in book["Financial summary"].iter_rows(
                min_row=8, max_col=2, values_only=True) if row and row[0]
        }
        self.assertEqual(Decimal(measures["Total revenue earned"]), Decimal("0.00"))
        self.assertEqual(
            [cell.value for cell in book["Financial summary"][3]][1:3],
            [(self.today - timedelta(days=30)).isoformat(),
             (self.today - timedelta(days=20)).isoformat()],
        )

    def test_export_rejects_a_missing_range_rather_than_sending_a_broken_file(self):
        self.auth(self.manager)
        response = self.client.get("/api/admin/finance/reports/summary/", {"export": "xlsx"})
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("application/json", response["Content-Type"])

    def test_export_requires_the_financial_reporting_capability(self):
        self.auth(make_staff("export.cashier@staff.dev", role=User.Role.CASHIER))
        response = self.client.get("/api/admin/finance/reports/summary/", {
            "start": self.start.isoformat(), "end": self.today.isoformat(), "export": "xlsx",
        })
        self.assertEqual(response.status_code, 403, response.content)


class OccupancyExportTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.today = hotel_today()
        self.manager = make_staff("export.occ@staff.dev", role=User.Role.MANAGER)

    def test_occupancy_exports_are_valid_files_with_the_right_headers(self):
        from openpyxl import load_workbook

        self.auth(self.manager)
        params = {
            "start_date": (self.today - timedelta(days=5)).isoformat(),
            "end_date": self.today.isoformat(),
        }
        workbook_response = self.client.get(
            "/api/admin/reports/occupancy/", {**params, "export": "xlsx"})
        self.assertEqual(workbook_response.status_code, 200, workbook_response.content)
        self.assertEqual(workbook_response["Content-Type"], XLSX_MIME)
        book = load_workbook(BytesIO(workbook_response.content))
        self.assertEqual(book.sheetnames, ["Occupancy"])
        self.assertEqual(book["Occupancy"]["A1"].value,
                         "J-ONE HOTEL & LODGE — Occupancy report")

        pdf_response = self.client.get("/api/admin/reports/occupancy/", {**params, "export": "pdf"})
        self.assertEqual(pdf_response.status_code, 200, pdf_response.content)
        self.assertEqual(pdf_response["Content-Type"], "application/pdf")
        self.assertTrue(pdf_response.content.startswith(b"%PDF"))
