"""Accounting agent: the supplier payment approval workflow.

The audit found that recording a supplier payment was one step that needed only
``expense.manage`` — a capability the matrix also grants to cashiers and waiters
— with no amount, no approval and no state of its own. These tests pin the
repaired workflow and prove that the posted figures were not disturbed:

1. a payment is proposed, submitted, approved, then executed as four distinct
   states, and each transition is refused from the wrong state;
2. a non-approver cannot settle a payable directly; an approver still can, so
   the existing contract with the accounts manager is unchanged;
3. self-approval is refused by the central maker-checker rule;
4. duplicate payments, over-payments, partial amounts and executions of
   unapproved requests are refused, and nothing is posted when they are;
5. a rejection or a cancellation leaves the payable exactly as it was;
6. executing twice posts once;
7. an amount is server-authoritative: the caller cannot move more than the
   outstanding payable.
"""
from decimal import Decimal

from apps.accounts.models import User
from apps.core.utils import hotel_today
from apps.finance.models import Expense, FinancialTransaction, SupplierPaymentRequest
from apps.finance.services.expense_service import create_expense, post_expense, review_expense, submit_expense

from .base import BaseAPITestCase
from .factories import make_staff

PAYMENTS_URL = "/api/admin/finance/supplier-payments/"


class SupplierPaymentWorkflowTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.manager = make_staff("pay-manager@staff.dev", role=User.Role.MANAGER)
        self.accountant = make_staff("pay-accountant@staff.dev", role=User.Role.ACCOUNTANT)
        self.cashier = make_staff("pay-cashier@staff.dev", role=User.Role.CASHIER)

    # ------------------------------------------------------------------ helpers
    def _posted_payable(self, *, amount="50000.00", key="payable-1", requester=None, approver=None):
        requester = requester or self.cashier
        approver = approver or self.manager
        expense, _ = create_expense(
            actor=requester, category="Supplier invoice", amount=amount,
            payment_method=Expense.PaymentMethod.PAYABLE, payee="Lagos Supplies Ltd",
            idempotency_key=f"payable:{key}",
        )
        expense = submit_expense(expense=expense, actor=requester)
        expense = review_expense(expense=expense, reviewer=approver, approved=True)
        expense, _ = post_expense(expense=expense, actor=requester)
        return expense

    def _propose(self, expense, *, amount=None, key="propose-1", method="BANK_TRANSFER", reference="TRF-1"):
        body = {
            "expense_reference": expense.reference,
            "method": method,
            "payment_reference": reference,
            "idempotency_key": key,
        }
        if amount is not None:
            body["amount"] = amount
        return self.client.post(PAYMENTS_URL, body, format="json")

    def _fully_approved_request(self, expense):
        self.auth(self.accountant)
        created = self._propose(expense)
        self.assertEqual(created.status_code, 201, created.content)
        reference = created.json()["data"]["reference"]
        self.assertEqual(
            self.client.post(f"{PAYMENTS_URL}{reference}/submit/", {}, format="json").status_code, 200
        )
        self.auth(self.manager)
        reviewed = self.client.post(
            f"{PAYMENTS_URL}{reference}/review/", {"approved": True, "review_note": "Invoice matches the GRN."}, format="json",
        )
        self.assertEqual(reviewed.status_code, 200, reviewed.content)
        self.assertEqual(reviewed.json()["data"]["status"], SupplierPaymentRequest.Status.APPROVED)
        return reference

    # ---------------------------------------------------- the states are distinct
    def test_the_lifecycle_moves_through_distinct_states_and_posts_once(self):
        expense = self._posted_payable()
        reference = self._fully_approved_request(expense)
        transactions_before = FinancialTransaction.objects.count()

        self.auth(self.accountant)
        executed = self.client.post(f"{PAYMENTS_URL}{reference}/execute/", {}, format="json")
        self.assertEqual(executed.status_code, 200, executed.content)
        payload = executed.json()["data"]
        self.assertEqual(payload["status"], SupplierPaymentRequest.Status.EXECUTED)
        self.assertTrue(payload["settlement_reference"])

        expense.refresh_from_db()
        self.assertIsNotNone(expense.paid_at)
        self.assertEqual(FinancialTransaction.objects.count(), transactions_before + 1)
        request = SupplierPaymentRequest.objects.get(reference=reference)
        self.assertEqual(request.settlement_transaction_id, expense.settlement_transaction_id)
        self.assertEqual(request.executed_by, self.accountant)

        # Executing again is a no-op, not a second payment.
        repeat = self.client.post(f"{PAYMENTS_URL}{reference}/execute/", {}, format="json")
        self.assertEqual(repeat.status_code, 200, repeat.content)
        self.assertEqual(FinancialTransaction.objects.count(), transactions_before + 1)

    def test_executing_an_unapproved_request_is_refused(self):
        expense = self._posted_payable()
        self.auth(self.accountant)
        reference = self._propose(expense).json()["data"]["reference"]
        transactions_before = FinancialTransaction.objects.count()
        blocked = self.client.post(f"{PAYMENTS_URL}{reference}/execute/", {}, format="json")
        self.assertEqual(blocked.status_code, 400, blocked.content)
        self.assertIn("approved", str(blocked.content).lower())
        expense.refresh_from_db()
        self.assertIsNone(expense.paid_at)
        self.assertEqual(FinancialTransaction.objects.count(), transactions_before)

    def test_a_cashier_cannot_settle_a_payable_directly_but_an_approver_still_can(self):
        expense = self._posted_payable()
        self.auth(self.cashier)
        blocked = self.client.post(
            f"/api/admin/finance/expenses/{expense.reference}/settle-payment/",
            {"method": "BANK_TRANSFER", "payment_reference": "TRF-CASHIER"}, format="json",
        )
        self.assertEqual(blocked.status_code, 403, blocked.content)
        expense.refresh_from_db()
        self.assertIsNone(expense.paid_at)

        # The accounts manager's existing route is unchanged.
        self.auth(self.manager)
        allowed = self.client.post(
            f"/api/admin/finance/expenses/{expense.reference}/settle-payment/",
            {"method": "BANK_TRANSFER", "payment_reference": "TRF-MANAGER"}, format="json",
        )
        self.assertEqual(allowed.status_code, 200, allowed.content)
        expense.refresh_from_db()
        self.assertIsNotNone(expense.paid_at)

    def test_a_requester_cannot_approve_their_own_payment_request(self):
        expense = self._posted_payable()
        self.auth(self.manager)
        reference = self._propose(expense, key="self-approval").json()["data"]["reference"]
        self.assertEqual(self.client.post(f"{PAYMENTS_URL}{reference}/submit/", {}, format="json").status_code, 200)
        self_approved = self.client.post(
            f"{PAYMENTS_URL}{reference}/review/", {"approved": True}, format="json",
        )
        self.assertEqual(self_approved.status_code, 403, self_approved.content)
        request = SupplierPaymentRequest.objects.get(reference=reference)
        self.assertEqual(request.status, SupplierPaymentRequest.Status.SUBMITTED)

    # ---------------------------------------------------------- amount controls
    def test_over_payment_and_partial_amounts_are_refused(self):
        expense = self._posted_payable(amount="50000.00")
        self.auth(self.accountant)
        over = self._propose(expense, amount="50000.01", key="over")
        self.assertEqual(over.status_code, 400, over.content)
        self.assertIn("cannot exceed", str(over.content))
        partial = self._propose(expense, amount="20000.00", key="partial")
        self.assertEqual(partial.status_code, 400, partial.content)
        self.assertIn("one payment", str(partial.content))
        # A card/bank payment needs its reference.
        missing_reference = self._propose(expense, key="no-ref", reference="")
        self.assertEqual(missing_reference.status_code, 400, missing_reference.content)
        self.assertEqual(SupplierPaymentRequest.objects.count(), 0)

    def test_a_second_request_for_the_same_payable_is_a_duplicate(self):
        expense = self._posted_payable()
        self.auth(self.accountant)
        self.assertEqual(self._propose(expense, key="first").status_code, 201)
        duplicate = self._propose(expense, key="second")
        self.assertEqual(duplicate.status_code, 400, duplicate.content)
        self.assertIn("already has an open payment request", str(duplicate.content))
        self.assertEqual(SupplierPaymentRequest.objects.filter(expense=expense).count(), 1)

    def test_an_already_settled_payable_cannot_be_proposed_again(self):
        expense = self._posted_payable()
        self.auth(self.manager)
        self.client.post(
            f"/api/admin/finance/expenses/{expense.reference}/settle-payment/",
            {"method": "BANK_TRANSFER", "payment_reference": "TRF-SETTLED"}, format="json",
        )
        self.auth(self.accountant)
        refused = self._propose(expense, key="after-settlement")
        self.assertEqual(refused.status_code, 400, refused.content)
        self.assertIn("already been paid", str(refused.content))

    # ------------------------------------------------------- rejection / cancel
    def test_a_rejected_request_leaves_the_payable_untouched(self):
        expense = self._posted_payable()
        self.auth(self.accountant)
        reference = self._propose(expense).json()["data"]["reference"]
        self.client.post(f"{PAYMENTS_URL}{reference}/submit/", {}, format="json")
        self.auth(self.manager)
        rejected = self.client.post(
            f"{PAYMENTS_URL}{reference}/review/", {"approved": False, "review_note": "Invoice does not match the GRN."}, format="json",
        )
        self.assertEqual(rejected.status_code, 200, rejected.content)
        self.assertEqual(rejected.json()["data"]["status"], SupplierPaymentRequest.Status.REJECTED)
        self.auth(self.accountant)
        self.assertEqual(
            self.client.post(f"{PAYMENTS_URL}{reference}/execute/", {}, format="json").status_code, 400,
        )
        expense.refresh_from_db()
        self.assertIsNone(expense.paid_at)
        self.assertIsNone(expense.settlement_transaction_id)

    def test_cancelling_requires_a_reason_and_stops_the_payment(self):
        expense = self._posted_payable()
        reference = self._fully_approved_request(expense)
        self.auth(self.accountant)
        short = self.client.post(f"{PAYMENTS_URL}{reference}/cancel/", {"reason": "no"}, format="json")
        self.assertEqual(short.status_code, 400, short.content)
        cancelled = self.client.post(
            f"{PAYMENTS_URL}{reference}/cancel/", {"reason": "Supplier asked us to hold the transfer."}, format="json",
        )
        self.assertEqual(cancelled.status_code, 200, cancelled.content)
        self.assertEqual(cancelled.json()["data"]["status"], SupplierPaymentRequest.Status.CANCELLED)
        self.assertEqual(
            self.client.post(f"{PAYMENTS_URL}{reference}/execute/", {}, format="json").status_code, 400,
        )
        expense.refresh_from_db()
        self.assertIsNone(expense.paid_at)
        # The approval that was granted stays on record as evidence (it did
        # happen); the payment request itself is what is cancelled, with its
        # reason.
        request = SupplierPaymentRequest.objects.get(reference=reference)
        self.assertEqual(request.approval_request.status, "APPROVED")
        self.assertEqual(request.cancel_reason, "Supplier asked us to hold the transfer.")

    # ---------------------------------------------------------- audit and scope
    def test_history_is_preserved_and_audited(self):
        expense = self._posted_payable()
        reference = self._fully_approved_request(expense)
        transactions_before = FinancialTransaction.objects.count()
        self.auth(self.accountant)
        self.client.post(f"{PAYMENTS_URL}{reference}/execute/", {}, format="json")

        from apps.audit.models import AuditLog

        actions = set(AuditLog.objects.values_list("action", flat=True))
        self.assertIn("SUPPLIER_PAYMENT_PROPOSED", actions)
        self.assertIn("SUPPLIER_PAYMENT_SUBMITTED", actions)
        self.assertIn("SUPPLIER_PAYMENT_REVIEWED", actions)
        self.assertIn("SUPPLIER_PAYMENT_EXECUTED", actions)

        # The posted history is additive only: the payable's own posting is still
        # there, untouched, with one new settlement transaction beside it.
        self.assertEqual(FinancialTransaction.objects.count(), transactions_before + 1)
        expense.refresh_from_db()
        self.assertEqual(expense.amount, Decimal("50000.00"))
        self.assertEqual(expense.status, Expense.Status.POSTED)

    def test_another_users_payment_request_is_not_visible_without_financial_visibility(self):
        expense = self._posted_payable()
        self.auth(self.accountant)
        reference = self._propose(expense).json()["data"]["reference"]
        other_cashier = make_staff("pay-cashier-2@staff.dev", role=User.Role.CASHIER)
        self.auth(other_cashier)
        listing = self.client.get(PAYMENTS_URL)
        self.assertEqual(listing.status_code, 200, listing.content)
        rows = listing.json()["data"]
        rows = rows["results"] if isinstance(rows, dict) else rows
        self.assertEqual([row for row in rows if row["reference"] == reference], [])
        self.assertEqual(self.client.get(f"{PAYMENTS_URL}{reference}/").status_code, 404)

    def test_an_expense_that_was_paid_at_posting_is_not_a_payable(self):
        expense, _ = create_expense(
            actor=self.cashier, category="Diesel", amount="8000.00",
            payment_method=Expense.PaymentMethod.BANK_TRANSFER,
            idempotency_key="paid-at-posting", payee="Diesel Ltd",
        )
        expense = submit_expense(expense=expense, actor=self.cashier)
        expense = review_expense(expense=expense, reviewer=self.manager, approved=True)
        expense, _ = post_expense(expense=expense, actor=self.cashier)
        self.auth(self.accountant)
        refused = self._propose(expense, key="not-payable")
        self.assertEqual(refused.status_code, 400, refused.content)
        self.assertIn("settled at posting time", str(refused.content))
