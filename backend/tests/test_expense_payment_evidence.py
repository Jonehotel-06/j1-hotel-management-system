# tests/test_expense_payment_evidence.py
"""Transfer-receipt evidence for a supplier payment that already left the money.

A CASH / BANK_TRANSFER / CARD expense credits its tender account when it is
posted, so the ledger already treats the money as out; there is no second
settlement to post. Until this endpoint existed, the transfer slip for such an
expense had nowhere to live. These tests pin the four properties that make the
new evidence route safe:

1. it stores the receipt (file, link, reference, date paid) and a PAID event,
2. it posts **no** ledger entry and moves **no** financial figure — the payables
   and disbursement measures are byte-identical before and after,
3. an on-account expense that is still unpaid is refused here (the payable has
   to settle through /settle-payment/ so the liability is released once), and
4. evidence already on the record is never overwritten.
"""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from apps.accounts.models import User
from apps.finance.models import (
    Expense,
    ExpenseEvent,
    FinancialLine,
    FinancialTransaction,
    PaymentAllocation,
)
from apps.finance.services.expense_service import (
    attach_expense_payment_evidence,
    post_expense,
    review_expense,
    settle_expense_payment,
    submit_expense,
)
from apps.finance.services.payment_collection_service import (
    apply_payment_to_folio,
    ensure_payment_collection,
)

from .base import BaseAPITestCase
from .factories import make_staff


def png_bytes():
    """A 1x1 PNG — the smallest real image the storage layer will keep."""
    import base64

    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
    )


class ExpensePaymentEvidenceTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.manager = make_staff("evidence-manager@staff.dev", role=User.Role.MANAGER)
        self.accountant = make_staff("evidence-accountant@staff.dev", role=User.Role.ACCOUNTANT)
        self.receptionist = make_staff("evidence-desk@staff.dev", role=User.Role.RECEPTIONIST)

    # ------------------------------------------------------------------ helpers
    def _posted_expense(self, *, payment_method, amount="50000.00", category="Generator diesel",
                        reference_key="evidence"):
        expense, _ = Expense.objects.get_or_create(
            reference=f"EXP-TEST-{reference_key}",
            defaults={
                "category": category,
                "payment_method": payment_method,
                "amount": Decimal(amount),
                "currency": "NGN",
                "payee": "Maiduguri Diesel Ltd",
                "requested_by": self.manager,
                "status": Expense.Status.APPROVED,
                "approved_by": self.manager,
            },
        )
        post_expense(expense=expense, actor=self.manager)
        expense.refresh_from_db()
        return expense

    def _ledger_snapshot(self):
        return {
            "transactions": FinancialTransaction.objects.count(),
            "lines": FinancialLine.objects.count(),
            "debit": sum(FinancialLine.objects.values_list("amount", flat=True), Decimal("0.00")),
            "payable_credits": sum(
                FinancialLine.objects.filter(
                    account_code="LIABILITY.ACCOUNTS_PAYABLE",
                    direction=FinancialLine.Direction.CREDIT,
                ).values_list("amount", flat=True),
                Decimal("0.00"),
            ),
        }

    def _summary(self):
        """The owner-facing figures, straight from the reporting service."""
        from apps.finance.services.reporting_service import financial_summary
        from apps.core.utils import hotel_today

        today = hotel_today()
        report = financial_summary(start_date=today, end_date=today)
        supplier = report["supplier_payments"]
        return {
            "outstanding_supplier_payables": supplier["outstanding_supplier_payables"],
            "total_cash_disbursements": supplier["total_cash_disbursements"],
            "supplier_payments_made": supplier["supplier_payments_made"],
            "operating_expenses": report["operating_expenses"]["amount"],
        }

    # -------------------------------------------------------------------- tests
    def test_a_bank_transfer_expense_keeps_its_receipt_without_a_new_posting(self):
        expense = self._posted_expense(payment_method=Expense.PaymentMethod.BANK_TRANSFER)
        before_ledger = self._ledger_snapshot()
        before_figures = self._summary()

        receipt = SimpleUploadedFile("transfer-slip.png", png_bytes(), content_type="image/png")
        self.auth(self.manager)
        response = self.client.post(
            f"/api/admin/finance/expenses/{expense.reference}/payment-evidence/",
            {
                "receipt": receipt,
                "receipt_url": "https://bank.example.com/receipt/ABC",
                "payment_reference": "TRF-99887766",
                "paid_on": "2026-10-14",
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()["data"]
        self.assertEqual(payload["payment_reference"], "TRF-99887766")
        self.assertEqual(payload["payment_receipt_url"], "https://bank.example.com/receipt/ABC")
        self.assertTrue(payload["payment_receipt_file_url"].endswith(".png"), payload["payment_receipt_file_url"])
        self.assertTrue(payload["paid_at"], "the expense must now read as paid")

        # The evidence is on the append-only expense history, and it says plainly
        # that nothing was posted to the ledger.
        event = ExpenseEvent.objects.filter(
            expense=expense, type=ExpenseEvent.Type.PAID
        ).order_by("-pk").first()
        self.assertIsNotNone(event)
        self.assertTrue(event.details["evidence_only"])
        self.assertEqual(event.details["ledger_effect"], "none")
        self.assertEqual(event.details["paid_on"], "2026-10-14")

        # No ledger effect at all, and no financial figure moves.
        self.assertEqual(self._ledger_snapshot(), before_ledger)
        self.assertEqual(self._summary(), before_figures)

    def test_an_on_account_expense_still_unpaid_is_refused_here(self):
        expense = self._posted_expense(
            payment_method=Expense.PaymentMethod.PAYABLE, category="Linen supplier",
            reference_key="payable",
        )
        self.auth(self.manager)
        response = self.client.post(
            f"/api/admin/finance/expenses/{expense.reference}/payment-evidence/",
            {"payment_reference": "TRF-1"},
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("on account", str(response.content))
        expense.refresh_from_db()
        self.assertIsNone(expense.paid_at)

        # The supported route settles the liability once and keeps its own evidence.
        settle_expense_payment(
            expense=expense, actor=self.manager, method=Expense.PaymentMethod.BANK_TRANSFER,
            payment_reference="TRF-2",
        )
        expense.refresh_from_db()
        self.assertIsNotNone(expense.settlement_transaction_id)

    def test_the_settlement_can_still_gain_the_receipt_it_missed(self):
        expense = self._posted_expense(
            payment_method=Expense.PaymentMethod.PAYABLE, category="Water supplier",
            reference_key="payable-settled",
        )
        settle_expense_payment(
            expense=expense, actor=self.manager, method=Expense.PaymentMethod.BANK_TRANSFER,
            payment_reference="TRF-500",
        )
        before = self._ledger_snapshot()
        self.auth(self.manager)
        response = self.client.post(
            f"/api/admin/finance/expenses/{expense.reference}/payment-evidence/",
            {"receipt": SimpleUploadedFile("slip.png", png_bytes(), content_type="image/png")},
            format="multipart",
        )
        self.assertEqual(response.status_code, 200, response.content)
        expense.refresh_from_db()
        self.assertTrue(expense.payment_receipt_file)
        # The settlement date recorded by the payment itself is not rewritten.
        self.assertEqual(expense.payment_reference, "TRF-500")
        self.assertEqual(self._ledger_snapshot(), before)

    def test_existing_evidence_is_never_overwritten(self):
        expense = self._posted_expense(
            payment_method=Expense.PaymentMethod.CARD, category="Card fee",
            reference_key="card",
        )
        self.auth(self.manager)
        first = self.client.post(
            f"/api/admin/finance/expenses/{expense.reference}/payment-evidence/",
            {"receipt_url": "https://bank.example.com/receipt/FIRST"},
            format="json",
        )
        self.assertEqual(first.status_code, 200, first.content)
        second = self.client.post(
            f"/api/admin/finance/expenses/{expense.reference}/payment-evidence/",
            {"receipt_url": "https://bank.example.com/receipt/SECOND"},
            format="json",
        )
        self.assertEqual(second.status_code, 400, second.content)
        expense.refresh_from_db()
        self.assertEqual(expense.payment_receipt_url, "https://bank.example.com/receipt/FIRST")

    def test_evidence_needs_something_on_the_record(self):
        expense = self._posted_expense(
            payment_method=Expense.PaymentMethod.BANK_TRANSFER, category="Fuel",
            reference_key="empty",
        )
        with self.assertRaises(ValidationError):
            attach_expense_payment_evidence(expense=expense, actor=self.manager)

    def test_a_desk_account_without_expense_manage_cannot_attach_evidence(self):
        expense = self._posted_expense(
            payment_method=Expense.PaymentMethod.BANK_TRANSFER, category="Cleaning", reference_key="rbac",
        )
        self.auth(self.receptionist)
        response = self.client.post(
            f"/api/admin/finance/expenses/{expense.reference}/payment-evidence/",
            {"payment_reference": "TRF-3"},
            format="json",
        )
        self.assertEqual(response.status_code, 403, response.content)
        expense.refresh_from_db()
        self.assertIsNone(expense.paid_at)

    def test_a_future_payment_date_is_refused(self):
        expense = self._posted_expense(
            payment_method=Expense.PaymentMethod.BANK_TRANSFER, category="Transport", reference_key="future",
        )
        self.auth(self.manager)
        response = self.client.post(
            f"/api/admin/finance/expenses/{expense.reference}/payment-evidence/",
            {"payment_reference": "TRF-4", "paid_on": "2099-01-01"},
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.content)
