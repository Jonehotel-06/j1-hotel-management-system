"""The approvals inbox (GET /api/admin/reports/approvals/).

One page must answer "what is waiting for my decision?" across expenses,
finance approval requests, payroll, leave, stock counts, purchase orders and
the two Admin-only queues — without ever showing a queue the caller may not
decide.
"""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from apps.accounts.models import User
from apps.core.utils import hotel_today
from apps.finance.models import ApprovalRequest, Expense
from apps.finance.services.expense_service import create_expense, submit_expense
from apps.inventory.models import PurchaseOrder, StockCount, StockLocation, Supplier
from apps.staff_operations.models import AttendanceRecord, Department, LeaveRequest
from apps.staff_operations.payroll_models import (
    PayrollPeriod,
    PayrollStockShortageCase,
    StaffLatenessDeduction,
)

from .base import BaseAPITestCase
from .factories import make_staff

URL = "/api/admin/reports/approvals/"


class ApprovalsInboxTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.admin = make_staff("inbox-admin@staff.dev", role=User.Role.ADMIN)
        self.manager = make_staff("inbox-manager@staff.dev", role=User.Role.MANAGER)
        self.cashier = make_staff("inbox-cashier@staff.dev", role=User.Role.CASHIER)
        self.housekeeper = make_staff("inbox-hk@staff.dev", role=User.Role.HOUSEKEEPER)
        self.today = hotel_today()
        self._seed_queues()

    def _seed_queues(self):
        expense, _ = create_expense(
            actor=self.cashier, category="Supplier invoice", amount="25000.00",
            payment_method=Expense.PaymentMethod.PAYABLE,
            accounting_class=Expense.AccountingClass.INVENTORY,
            payee="Drinks Supplier Ltd", idempotency_key="inbox-expense-1",
        )
        submit_expense(expense=expense, actor=self.cashier)
        self.expense = expense

        ApprovalRequest.objects.create(
            reference="APR-INBOX-REFUND", type=ApprovalRequest.Type.REFUND,
            status=ApprovalRequest.Status.PENDING, amount=Decimal("5000.00"),
            requester=self.cashier, source_reference="PAY-INBOX-1", reason="Guest refund",
        )
        ApprovalRequest.objects.create(
            reference="APR-INBOX-EXP", type=ApprovalRequest.Type.EXPENSE,
            status=ApprovalRequest.Status.PENDING, amount=Decimal("25000.00"),
            requester=self.cashier, source_reference=expense.reference,
        )
        LeaveRequest.objects.create(
            reference="LV-INBOX-1", staff=self.housekeeper, type=LeaveRequest.Type.ANNUAL,
            status=LeaveRequest.Status.PENDING,
            start_date=self.today + timedelta(days=3), end_date=self.today + timedelta(days=7),
            reason="Family event",
        )
        PayrollPeriod.objects.create(
            reference="PAY-INBOX-RUN", starts_on=self.today.replace(day=1),
            ends_on=self.today, status=PayrollPeriod.Status.SUBMITTED,
            created_by=self.manager, submitted_at=timezone.now(),
            total_gross=Decimal("900000.00"),
        )
        location = StockLocation.objects.create(code="BAR-INBOX", name="Bar store")
        StockCount.objects.create(
            reference="SC-INBOX-1", location=location, status=StockCount.Status.PENDING_APPROVAL,
            initiated_by=self.manager, submitted_by=self.manager, submitted_at=timezone.now(),
        )
        supplier = Supplier.objects.create(name="Inbox Supplier Ltd")
        PurchaseOrder.objects.create(
            reference="PO-INBOX-1", supplier=supplier, delivery_location=location,
            status=PurchaseOrder.Status.SUBMITTED, requested_by=self.cashier,
            expected_on=self.today + timedelta(days=2),
        )
        department = Department.objects.create(code="BAR", name="Bar", slug="bar")
        PayrollStockShortageCase.objects.create(
            reference="SHC-INBOX-1", department=department, department_code="BAR",
            department_name="Bar", item_summary="2 × Gin 70cl missing",
            incident_date=self.today, effective_date=self.today,
            total_amount=Decimal("30000.00"), evidence_reference="CNT-INBOX-1",
            status=PayrollStockShortageCase.Status.PENDING_ADMIN_APPROVAL,
            created_by=self.manager,
        )
        attendance = AttendanceRecord.objects.create(
            reference="ATT-INBOX-1", staff=self.housekeeper, business_date=self.today,
            status=AttendanceRecord.Status.COMPLETED,
            clock_in_at=timezone.now(), clock_out_at=timezone.now(),
            last_event_at=timezone.now(),
        )
        StaffLatenessDeduction.objects.create(
            reference="LAT-INBOX-1", staff=self.housekeeper, attendance_record=attendance,
            business_date=self.today, scheduled_start_at=timezone.now(),
            clock_in_at=timezone.now(), late_minutes=45, grace_minutes=10,
            deduction_percent=Decimal("50.00"),
            amount=Decimal("2500.00"),
            status=StaffLatenessDeduction.Status.REVOCATION_REQUESTED,
            revocation_reason="Bus broke down; conductor note attached",
            revocation_requested_by=self.manager, revocation_requested_at=timezone.now(),
        )

    def _categories(self, user):
        self.auth(user)
        response = self.client.get(URL)
        assert response.status_code == 200, response.content
        return response.json()["data"]

    def test_admin_sees_every_queue_with_correct_totals(self):
        data = self._categories(self.admin)
        by_key = {c["key"]: c for c in data["categories"]}
        self.assertEqual(by_key["expenses"]["count"], 1)
        self.assertEqual(by_key["expenses"]["items"][0]["reference"], self.expense.reference)
        # The REFUND approval is listed; the EXPENSE-type approval is not
        # duplicated alongside the expense queue.
        self.assertEqual(by_key["finance_approvals"]["count"], 1)
        self.assertEqual(by_key["finance_approvals"]["items"][0]["reference"], "APR-INBOX-REFUND")
        self.assertEqual(by_key["payroll_runs"]["count"], 1)
        self.assertEqual(by_key["leave"]["count"], 1)
        self.assertEqual(by_key["stock_counts"]["count"], 1)
        self.assertEqual(by_key["purchase_orders"]["count"], 1)
        self.assertEqual(by_key["lateness_revocations"]["count"], 1)
        self.assertEqual(by_key["shortage_cases"]["count"], 1)
        self.assertEqual(data["total_pending"], 8)

    def test_manager_never_sees_admin_only_queues(self):
        data = self._categories(self.manager)
        keys = {c["key"] for c in data["categories"]}
        self.assertIn("expenses", keys)
        self.assertNotIn("lateness_revocations", keys)
        self.assertNotIn("shortage_cases", keys)

    def test_staff_without_approval_capabilities_are_turned_away(self):
        self.auth(self.housekeeper)
        response = self.client.get(URL)
        self.assertEqual(response.status_code, 403)

    def test_unauthenticated_callers_are_rejected(self):
        response = self.client.get(URL)
        self.assertIn(response.status_code, (401, 403))
