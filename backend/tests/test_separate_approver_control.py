# tests/test_separate_approver_control.py
"""The separate-approver control, proven with synthetic accounts (audit D2).

The audit could not verify maker-checker because no independent production
approver existed for it to use, and the repair brief forbids approving the
hotel's real pending records just to demonstrate the workflow. What CAN be
proven — on synthetic records in the test database, with clearly synthetic
accounts — is that the control is enabled, that it is enforced server-side on
every controlled money path, and that two distinct authorising accounts are
genuinely required. That is what this module does.

Production verification of the live pending approvals remains BLOCKED pending an
independent approver; see REMAINING_BLOCKERS.md.
"""
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import PermissionDenied

from apps.accounts.models import User
from apps.core.utils import hotel_today
from apps.finance.models import ApprovalRequest, Expense
from apps.finance.services.approval_service import active_control_policy
from apps.finance.services.expense_service import create_expense, review_expense, submit_expense
from apps.inventory.models import PurchaseOrder
from apps.inventory.services.inventory_service import (
    approve_purchase_order,
    create_purchase_order,
    submit_purchase_order,
)
from apps.staff_operations.payroll_models import PayrollLine, PayrollPeriod, StaffCompensation
from apps.staff_operations.services.payroll_service import review_payroll_period, submit_payroll_period

from .base import BaseAPITestCase
from .factories import make_staff


class SeparateApproverPolicyTests(BaseAPITestCase):
    """The policy that drives every check below must be ON by default."""

    def test_separate_approver_is_required_by_the_active_policy(self):
        self.assertTrue(
            active_control_policy().require_separate_approver,
            "the two-person rule must remain enabled; it was never a toggle for tests",
        )


class ExpenseApprovalTests(BaseAPITestCase):
    """Expense approval is the canonical maker-checker money path."""

    def setUp(self):
        super().setUp()
        self.maker = make_staff("d2.expense.maker@staff.dev", role=User.Role.MANAGER)
        self.checker = make_staff("d2.expense.checker@staff.dev", role=User.Role.MANAGER)

    def _submitted_expense(self):
        expense, _ = create_expense(
            actor=self.maker, category="Other operating expense", amount="12500.00",
            currency="NGN", description="D2 synthetic approval-control expense",
            incurred_on=hotel_today(), idempotency_key="d2-expense-1",
        )
        submit_expense(expense=expense, actor=self.maker)
        expense.refresh_from_db()
        self.assertEqual(expense.status, Expense.Status.SUBMITTED)
        return expense

    def test_the_maker_cannot_approve_their_own_expense(self):
        expense = self._submitted_expense()
        with self.assertRaises(PermissionDenied):
            review_expense(expense=expense, reviewer=self.maker, approved=True)

        expense.refresh_from_db()
        self.assertEqual(expense.status, Expense.Status.SUBMITTED, "nothing may be approved")
        self.assertIsNone(expense.approved_by_id)

    def test_a_second_distinct_account_can_approve(self):
        expense = self._submitted_expense()
        review_expense(expense=expense, reviewer=self.checker, approved=True)

        expense.refresh_from_db()
        self.assertEqual(expense.status, Expense.Status.APPROVED)
        self.assertEqual(expense.approved_by_id, self.checker.pk)
        self.assertNotEqual(expense.approved_by_id, expense.requested_by_id)

        approval = ApprovalRequest.objects.get(pk=expense.approval_request_id)
        self.assertEqual(approval.status, ApprovalRequest.Status.APPROVED)
        self.assertEqual(approval.requester_id, self.maker.pk)
        self.assertEqual(approval.reviewer_id, self.checker.pk)
        self.assertIsNotNone(approval.reviewed_at)

    def test_the_api_enforces_the_rule_too_not_only_the_service(self):
        expense = self._submitted_expense()
        url = f"/api/admin/finance/expenses/{expense.reference}/review/"

        self.auth(self.maker)
        response = self.client.post(url, {"approved": True, "review_note": "self"}, format="json")
        # The maker HOLDS expense.approve — the refusal must come from the
        # two-person rule, not from a missing capability.
        self.assertEqual(response.status_code, 403, response.content)
        self.assertIn("own sensitive monetary action", response.json()["message"])
        expense.refresh_from_db()
        self.assertEqual(expense.status, Expense.Status.SUBMITTED, "the API must not self-approve")

        self.auth(self.checker)
        response = self.client.post(url, {"approved": True, "review_note": "independent"}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        expense.refresh_from_db()
        self.assertEqual(expense.status, Expense.Status.APPROVED)


class PurchaseOrderApprovalTests(BaseAPITestCase):
    """Procurement carries the same two-person rule."""

    def setUp(self):
        super().setUp()
        self.requester = make_staff("d2.po.requester@staff.dev", role=User.Role.INVENTORY_CLERK)
        self.checker = make_staff("d2.po.checker@staff.dev", role=User.Role.MANAGER)

    def _submitted_po(self):
        from apps.inventory.models import StockItem, StockLocation, Supplier

        supplier = Supplier.objects.create(reference="SUP-D2-1", name="Synthetic Supplier")
        location = StockLocation.objects.create(code="D2STORE", name="D2 store")
        item = StockItem.objects.create(
            sku="D2-ITEM", name="Synthetic item", base_unit="PIECE", standard_cost="1000.00",
        )
        po, _ = create_purchase_order(
            supplier=supplier, delivery_location=location, requested_by=self.requester,
            lines=[{"item_id": item.pk, "quantity_ordered": "2", "unit_cost": "1000"}],
            idempotency_key="d2-po-1",
        )
        submit_purchase_order(purchase_order=po, actor=self.requester)
        po.refresh_from_db()
        self.assertEqual(po.status, PurchaseOrder.Status.SUBMITTED)
        return po

    def test_a_requester_cannot_approve_their_own_purchase_order(self):
        po = self._submitted_po()
        with self.assertRaises(PermissionDenied):
            approve_purchase_order(purchase_order=po, actor=self.requester)
        po.refresh_from_db()
        self.assertEqual(po.status, PurchaseOrder.Status.SUBMITTED)

    def test_an_independent_approver_can_approve(self):
        po = self._submitted_po()
        approve_purchase_order(purchase_order=po, actor=self.checker)
        po.refresh_from_db()
        self.assertEqual(po.status, PurchaseOrder.Status.APPROVED)
        self.assertEqual(po.approved_by_id, self.checker.pk)


class PayrollApprovalTests(BaseAPITestCase):
    """Payroll runs require a preparer and a different approver."""

    def setUp(self):
        super().setUp()
        self.preparer = make_staff("d2.payroll.preparer@staff.dev", role=User.Role.MANAGER)
        self.approver = make_staff("d2.payroll.approver@staff.dev", role=User.Role.MANAGER)
        self.employee = make_staff("d2.payroll.employee@staff.dev", role=User.Role.RECEPTIONIST)

        compensation = StaffCompensation.objects.create(
            staff=self.employee, effective_from=hotel_today() - timedelta(days=90),
            currency="NGN", basic_salary=Decimal("200000.00"), created_by=self.preparer,
        )
        self.period = PayrollPeriod.objects.create(
            reference="PAY-D2-0001", starts_on=hotel_today() - timedelta(days=30),
            ends_on=hotel_today() - timedelta(days=1), currency="NGN",
            status=PayrollPeriod.Status.DRAFT, created_by=self.preparer,
            total_gross=Decimal("200000.00"), total_deductions=Decimal("0.00"),
            total_net=Decimal("200000.00"), total_paye=Decimal("0.00"),
        )
        PayrollLine.objects.create(
            period=self.period, staff=self.employee, compensation=compensation,
            employee_name="Synthetic Employee", basic_pay=Decimal("200000.00"),
            gross_pay=Decimal("200000.00"), net_pay=Decimal("200000.00"),
            deductions_total=Decimal("0.00"),
        )

    def test_the_preparer_cannot_approve_their_own_payroll_run(self):
        submit_payroll_period(period=self.period, actor=self.preparer)
        self.period.refresh_from_db()
        self.assertEqual(self.period.status, PayrollPeriod.Status.SUBMITTED)

        with self.assertRaises(PermissionDenied):
            review_payroll_period(period=self.period, reviewer=self.preparer, approved=True)

        self.period.refresh_from_db()
        self.assertEqual(self.period.status, PayrollPeriod.Status.SUBMITTED, "the run must stay submitted")
        self.assertIsNone(self.period.reviewed_by_id)

    def test_a_second_account_can_approve_the_run(self):
        submit_payroll_period(period=self.period, actor=self.preparer)
        review_payroll_period(period=self.period, reviewer=self.approver, approved=True)

        self.period.refresh_from_db()
        self.assertEqual(self.period.status, PayrollPeriod.Status.APPROVED)
        self.assertEqual(self.period.reviewed_by_id, self.approver.pk)
        assert self.period.reviewed_by_id != self.period.created_by_id
