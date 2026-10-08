"""Expense maker-checker, cash paid-out, and immutable ledger workflow tests."""
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError

from apps.accounts.models import User
from apps.finance.models import ApprovalRequest, CashMovement, Expense, FinancialTransaction
from apps.finance.services.approval_service import active_control_policy, review_approval
from apps.finance.services.cash_session_service import open_cash_session
from apps.finance.services.expense_service import create_expense, post_expense, review_expense, submit_expense

from .base import BaseAPITestCase
from .factories import make_staff


class ExpenseWorkflowTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.cashier = make_staff("expense-cashier@staff.dev", role=User.Role.CASHIER)
        self.manager = make_staff("expense-manager@staff.dev", role=User.Role.MANAGER)

    def test_cash_expense_is_independently_approved_then_posts_paid_out_and_balanced_ledger(self):
        session = open_cash_session(cashier=self.cashier, actor=self.cashier, opening_float="1000.00")
        expense, created = create_expense(
            actor=self.cashier, category="Cleaning supplies", amount="125.50", payment_method=Expense.PaymentMethod.CASH,
            cash_session=session, description="Emergency supplies", idempotency_key="expense-cash-one",
        )
        self.assertTrue(created)
        submitted = submit_expense(expense=expense, actor=self.cashier)
        self.assertEqual(submitted.status, Expense.Status.SUBMITTED)
        self.assertIsNotNone(submitted.approval_request_id)
        with self.assertRaisesMessage(ValidationError, "controlled expense workflow"):
            review_approval(approval=submitted.approval_request, reviewer=self.manager, approved=True)

        approved = review_expense(expense=submitted, reviewer=self.manager, approved=True, review_note="Receipt checked")
        self.assertEqual(approved.status, Expense.Status.APPROVED)
        posted, posted_now = post_expense(expense=approved, actor=self.cashier)
        self.assertTrue(posted_now)
        self.assertEqual(posted.status, Expense.Status.POSTED)
        self.assertEqual(posted.financial_transaction.type, FinancialTransaction.Type.EXPENSE)
        session.refresh_from_db()
        self.assertEqual(session.expected_cash, Decimal("874.50"))
        paid_out = CashMovement.objects.get(transaction=posted.financial_transaction)
        self.assertEqual(paid_out.type, CashMovement.Type.PAID_OUT)
        repeated, repeated_now = post_expense(expense=posted, actor=self.cashier)
        self.assertFalse(repeated_now)
        self.assertEqual(repeated.pk, posted.pk)
        posted.notes = "tampered"
        with self.assertRaisesMessage(ValidationError, "immutable"):
            posted.save()
        with self.assertRaisesMessage(ValidationError, "cannot be deleted"):
            posted.delete()

    def test_manager_requester_cannot_self_approve_a_controlled_expense(self):
        expense, _ = create_expense(
            actor=self.manager, category="Manager purchase", amount="50.00", payment_method=Expense.PaymentMethod.BANK_TRANSFER,
            idempotency_key="expense-no-self-approval",
        )
        submitted = submit_expense(expense=expense, actor=self.manager)
        with self.assertRaisesMessage(PermissionDenied, "requester cannot approve"):
            review_expense(expense=submitted, reviewer=self.manager, approved=True)
        submitted.refresh_from_db()
        self.assertEqual(submitted.status, Expense.Status.SUBMITTED)

    def test_policy_expense_threshold_can_auto_approve_low_value_expense(self):
        policy = active_control_policy()
        policy.expense_manager_approval_threshold = Decimal("100.00")
        policy.save(update_fields=["expense_manager_approval_threshold", "updated_at"])
        expense, _ = create_expense(
            actor=self.cashier, category="Petty cash", amount="50.00", payment_method=Expense.PaymentMethod.BANK_TRANSFER,
            idempotency_key="expense-low-value-one",
        )
        submitted = submit_expense(expense=expense, actor=self.cashier)
        self.assertEqual(submitted.status, Expense.Status.APPROVED)
        self.assertIsNone(submitted.approval_request_id)
        posted, created = post_expense(expense=submitted, actor=self.cashier)
        self.assertTrue(created)
        self.assertEqual(posted.status, Expense.Status.POSTED)

    def test_rejected_expense_cannot_post(self):
        expense, _ = create_expense(
            actor=self.cashier, category="Other", amount="50.00", payment_method=Expense.PaymentMethod.BANK_TRANSFER,
            idempotency_key="expense-reject-one",
        )
        submitted = submit_expense(expense=expense, actor=self.cashier)
        rejected = review_expense(expense=submitted, reviewer=self.manager, approved=False, review_note="Insufficient evidence")
        self.assertEqual(rejected.status, Expense.Status.REJECTED)
        self.assertEqual(rejected.approval_request.status, ApprovalRequest.Status.REJECTED)
        with self.assertRaisesMessage(ValidationError, "approved expense"):
            post_expense(expense=rejected, actor=self.cashier)
        self.assertFalse(FinancialTransaction.objects.filter(source_reference=rejected.reference).exists())
