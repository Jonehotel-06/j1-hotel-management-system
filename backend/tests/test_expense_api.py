"""Expense API capability and lifecycle coverage."""
from apps.accounts.models import User
from apps.finance.models import Expense
from apps.finance.services.cash_session_service import open_cash_session

from .base import BaseAPITestCase
from .factories import make_staff


class ExpenseApiTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.cashier = make_staff("expense-api-cashier@staff.dev", role=User.Role.CASHIER)
        self.manager = make_staff("expense-api-manager@staff.dev", role=User.Role.MANAGER)

    def test_cashier_creates_submits_and_posts_after_manager_review(self):
        session = open_cash_session(cashier=self.cashier, actor=self.cashier, opening_float="500.00")
        self.auth(self.cashier)
        created = self.client.post("/api/admin/finance/expenses/", {
            "category": "Guest supplies", "amount": "50.00", "payment_method": Expense.PaymentMethod.CASH,
            "cash_session_reference": session.reference, "idempotency_key": "expense-api-one",
        }, format="json")
        self.assertEqual(created.status_code, 201, created.content)
        reference = created.json()["data"]["reference"]
        submitted = self.client.post(f"/api/admin/finance/expenses/{reference}/submit/", {}, format="json")
        self.assertEqual(submitted.status_code, 200, submitted.content)
        self.assertEqual(submitted.json()["data"]["status"], Expense.Status.SUBMITTED)
        self.assertEqual(self.client.post(f"/api/admin/finance/expenses/{reference}/review/", {"approved": True}, format="json").status_code, 403)
        self.auth(self.manager)
        generic_review = self.client.post(
            f"/api/admin/finance/approvals/{submitted.json()['data']['approval_reference']}/review/", {"approved": True}, format="json"
        )
        self.assertEqual(generic_review.status_code, 400, generic_review.content)
        reviewed = self.client.post(f"/api/admin/finance/expenses/{reference}/review/", {"approved": True}, format="json")
        self.assertEqual(reviewed.status_code, 200, reviewed.content)
        self.assertEqual(reviewed.json()["data"]["status"], Expense.Status.APPROVED)
        self.auth(self.cashier)
        posted = self.client.post(f"/api/admin/finance/expenses/{reference}/post/", {}, format="json")
        self.assertEqual(posted.status_code, 200, posted.content)
        self.assertEqual(posted.json()["data"]["status"], Expense.Status.POSTED)
