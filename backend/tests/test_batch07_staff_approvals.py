"""Batch 07 — staff navigation access, Manager approvals, approval audit trail.

* The Manager (User.Role.MANAGER) can decide the five defined approval
  categories through the real API endpoints: leave, stock-count variances,
  purchase orders, payroll runs and expenses.
* The General Manager (and the manager tier) may READ the staff account
  collection; every account write stays administrator-only.
* Unauthorized department staff are refused on every approval and staff-admin
  endpoint with an authorization error.
* Every approval decision writes the uniform Batch 07 audit evidence
  (reference, category, initiator, approver, previous/new status, datetime,
  note) while preserving each workflow's own history and validation.
"""
from datetime import timedelta
from decimal import Decimal

from apps.accounts.capabilities import has_capability
from apps.accounts.models import RoleCapability, User
from apps.audit.models import AuditLog
from apps.core.utils import hotel_today
from apps.finance.models import Expense
from apps.finance.services.expense_service import create_expense, submit_expense
from apps.inventory.models import (
    PurchaseOrder,
    StockBalance,
    StockCount,
    StockItem,
    StockLocation,
    StockMovement,
    Supplier,
)
from apps.inventory.services.inventory_service import (
    create_purchase_order,
    record_stock_movement,
    start_stock_count,
    submit_purchase_order,
    submit_stock_count,
)
from apps.staff_operations.models import LeaveRequest
from apps.staff_operations.payroll_models import PayrollPeriod
from apps.staff_operations.services.staff_operations_service import ensure_staff_profile, request_leave

from .base import BaseAPITestCase
from .factories import make_staff

USERS_URL = "/api/admin/users/"
INBOX_URL = "/api/admin/reports/approvals/"

MANAGER_APPROVAL_CAPABILITIES = (
    "leave.approve",
    "inventory.adjust.approve",
    "procurement.approve",
    "payroll.approve",
    "expense.approve",
)


class Batch07ManagerApprovalTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.admin = make_staff("b07-admin@staff.dev", role=User.Role.ADMIN)
        self.manager = make_staff("b07-manager@staff.dev", role=User.Role.MANAGER)
        self.gm = make_staff("b07-gm@staff.dev", role=User.Role.GENERAL_MANAGER)
        self.accountant = make_staff("b07-accountant@staff.dev", role=User.Role.ACCOUNTANT)
        self.clerk = make_staff("b07-clerk@staff.dev", role=User.Role.INVENTORY_CLERK)
        self.waiter = make_staff("b07-waiter@staff.dev", role=User.Role.WAITER)
        self.receptionist = make_staff("b07-reception@staff.dev", role=User.Role.RECEPTIONIST)
        self.housekeeper = make_staff("b07-housekeeping@staff.dev", role=User.Role.HOUSEKEEPING)
        self.payroll_creator = make_staff("b07-payroll@staff.dev", role=User.Role.HR_MANAGER)
        profile, _ = ensure_staff_profile(staff=self.housekeeper)
        profile.department = "Housekeeping"
        profile.save(update_fields=["department", "updated_at"])
        self.today = hotel_today()
        self.supplier = Supplier.objects.create(reference="SUP-B07", name="Batch07 Supplies Ltd")
        self.location = StockLocation.objects.create(code="STORE-B07", name="Batch07 Store")
        self.item = StockItem.objects.create(
            sku="B07-TOWEL", name="Batch07 towel", base_unit="PIECE", standard_cost="500.00"
        )

    # -- helpers ---------------------------------------------------------
    def _leave(self, staff=None, key="b07-leave-1"):
        staff = staff or self.housekeeper
        leave_request, _ = request_leave(
            staff=staff, leave_type=LeaveRequest.Type.ANNUAL,
            start_date=self.today + timedelta(days=10), end_date=self.today + timedelta(days=12),
            reason="Family event", idempotency_key=key, actor=staff,
        )
        return leave_request

    def _pending_count(self):
        record_stock_movement(
            item=self.item, location=self.location, movement_type=StockMovement.Type.RECEIPT,
            quantity_delta="5", source_key="b07-opening-stock", actor=self.clerk,
        )
        stock_count = start_stock_count(location=self.location, item_ids=[self.item.pk], actor=self.clerk)
        line = stock_count.lines.get()
        return submit_stock_count(
            stock_count=stock_count, actor=self.clerk,
            counts=[{"line_id": line.pk, "counted_quantity": "3"}],
        )

    def _submitted_po(self, key="b07-po-1"):
        po, _ = create_purchase_order(
            supplier=self.supplier, delivery_location=self.location, requested_by=self.clerk,
            lines=[{"item_id": self.item.pk, "quantity_ordered": "2", "unit_cost": "500"}],
            idempotency_key=key,
        )
        return submit_purchase_order(purchase_order=po, actor=self.clerk)

    def _submitted_payroll(self, key="b07-payroll-1"):
        self.auth(self.payroll_creator)
        response = self.client.post("/api/admin/staff-operations/payroll/compensation/", {
            "staff_id": self.housekeeper.pk, "effective_from": self.today.isoformat(),
            "currency": "NGN", "basic_salary": "60000.00", "overtime_rate": "1000.00",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        created = self.client.post("/api/admin/staff-operations/payroll/periods/", {
            "starts_on": self.today.isoformat(), "ends_on": self.today.isoformat(),
            "department": "Housekeeping", "idempotency_key": key,
            "adjustments": [{
                "staff_id": self.housekeeper.pk, "overtime_hours": "2.50",
                "allowances": [{"label": "Transport", "amount": "5000.00"}],
                "bonus": "0.00", "deductions": [{"label": "Uniform", "amount": "1000.00"}],
            }],
        }, format="json")
        self.assertIn(created.status_code, (200, 201), created.content)
        reference = created.json()["data"]["reference"]
        submitted = self.client.post(
            f"/api/admin/staff-operations/payroll/periods/{reference}/submit/", {}, format="json"
        )
        self.assertEqual(submitted.status_code, 200, submitted.content)
        return reference

    def _submitted_expense(self, key="b07-expense-1"):
        expense, _ = create_expense(
            actor=self.accountant, category="Supplier invoice", amount="25000.00",
            payment_method=Expense.PaymentMethod.PAYABLE,
            accounting_class=Expense.AccountingClass.INVENTORY,
            payee="Drinks Supplier Ltd", idempotency_key=key,
        )
        return submit_expense(expense=expense, actor=self.accountant)

    def _decision_log(self, action, reference):
        return AuditLog.objects.filter(action=action, metadata__reference=reference).order_by("-pk").first()

    def _assert_uniform_evidence(self, log, *, category, reference, initiator, decider,
                                 previous_status, new_status):
        self.assertIsNotNone(log, "the decision must write an audit-log entry")
        metadata = log.metadata or {}
        self.assertEqual(metadata.get("approval_category"), category)
        self.assertEqual(metadata.get("reference"), reference)
        self.assertEqual((metadata.get("initiated_by") or {}).get("id"), initiator.pk)
        self.assertEqual((metadata.get("initiated_by") or {}).get("email"), initiator.email)
        self.assertEqual((metadata.get("decided_by") or {}).get("id"), decider.pk)
        self.assertEqual((metadata.get("decided_by") or {}).get("email"), decider.email)
        self.assertEqual(metadata.get("previous_status"), previous_status)
        self.assertEqual(metadata.get("new_status"), new_status)
        self.assertTrue(metadata.get("decided_at"), "the decision datetime must be recorded")
        self.assertIn("note", metadata)

    # -- Task 3: the Manager holds the five defined approval permissions --
    def test_manager_holds_exactly_the_five_defined_approval_capabilities(self):
        persisted = set(
            RoleCapability.objects.filter(
                role=User.Role.MANAGER, capability__code__in=MANAGER_APPROVAL_CAPABILITIES
            ).values_list("capability__code", flat=True)
        )
        self.assertEqual(persisted, set(MANAGER_APPROVAL_CAPABILITIES))
        for code in MANAGER_APPROVAL_CAPABILITIES:
            self.assertTrue(has_capability(self.manager, code), f"Manager must hold {code}")
            self.assertTrue(has_capability(self.gm, code), f"General Manager keeps {code}")
            self.assertTrue(has_capability(self.admin, code), f"Administrator keeps {code}")
        for code in MANAGER_APPROVAL_CAPABILITIES:
            self.assertFalse(has_capability(self.waiter, code), f"Waiter must not hold {code}")

    def test_manager_approves_leave_request(self):
        leave_request = self._leave()
        self.auth(self.manager)
        response = self.client.post(
            f"/api/admin/staff-operations/leave-requests/{leave_request.reference}/review/",
            {"approved": True, "review_note": "Cover arranged."}, format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        leave_request.refresh_from_db()
        self.assertEqual(leave_request.status, LeaveRequest.Status.APPROVED)
        self.assertEqual(leave_request.reviewer_id, self.manager.pk)
        # Leave balances and validation are preserved: the request itself is untouched.
        self.assertEqual(leave_request.type, LeaveRequest.Type.ANNUAL)
        self.assertEqual(leave_request.start_date, self.today + timedelta(days=10))
        self.assertEqual(leave_request.end_date, self.today + timedelta(days=12))
        self._assert_uniform_evidence(
            self._decision_log("LEAVE_REVIEWED", leave_request.reference),
            category="leave", reference=leave_request.reference,
            initiator=self.housekeeper, decider=self.manager,
            previous_status=LeaveRequest.Status.PENDING, new_status=LeaveRequest.Status.APPROVED,
        )

    def test_manager_approves_stock_count_variance_without_silent_adjustment(self):
        stock_count = self._pending_count()
        balance = StockBalance.objects.get(item=self.item, location=self.location)
        self.assertEqual(balance.quantity_on_hand, Decimal("5.0000"),
                         "submission must not silently adjust stock")
        self.auth(self.manager)
        response = self.client.post(
            f"/api/admin/inventory/stock-counts/{stock_count.reference}/approve/", {}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.content)
        stock_count.refresh_from_db()
        self.assertEqual(stock_count.status, StockCount.Status.POSTED)
        balance.refresh_from_db()
        self.assertEqual(balance.quantity_on_hand, Decimal("3.0000"))
        self._assert_uniform_evidence(
            self._decision_log("STOCK_COUNT_APPROVED_AND_POSTED", stock_count.reference),
            category="stock_count", reference=stock_count.reference,
            initiator=self.clerk, decider=self.manager,
            previous_status=StockCount.Status.PENDING_APPROVAL, new_status=StockCount.Status.POSTED,
        )

    def test_manager_approves_purchase_order(self):
        po = self._submitted_po()
        self.auth(self.manager)
        response = self.client.post(
            f"/api/admin/inventory/purchase-orders/{po.reference}/approve/", {}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.content)
        po.refresh_from_db()
        self.assertEqual(po.status, PurchaseOrder.Status.APPROVED)
        self.assertEqual(po.approved_by_id, self.manager.pk)
        self.assertEqual(po.supplier_id, self.supplier.pk)
        self._assert_uniform_evidence(
            self._decision_log("PURCHASE_ORDER_APPROVED", po.reference),
            category="purchase_order", reference=po.reference,
            initiator=self.clerk, decider=self.manager,
            previous_status=PurchaseOrder.Status.SUBMITTED, new_status=PurchaseOrder.Status.APPROVED,
        )

    def test_manager_approves_payroll_run_without_changing_amounts(self):
        reference = self._submitted_payroll()
        before = PayrollPeriod.objects.get(reference=reference)
        totals = (before.total_gross, before.total_deductions, before.total_net)
        self.auth(self.manager)
        response = self.client.post(
            f"/api/admin/staff-operations/payroll/periods/{reference}/review/",
            {"approved": True, "review_note": "Verified against attendance."}, format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        after = PayrollPeriod.objects.get(reference=reference)
        self.assertEqual(after.status, PayrollPeriod.Status.APPROVED)
        self.assertEqual((after.total_gross, after.total_deductions, after.total_net), totals,
                         "approval must not silently change payroll amounts")
        self._assert_uniform_evidence(
            self._decision_log("PAYROLL_RUN_APPROVED", reference),
            category="payroll", reference=reference,
            initiator=self.payroll_creator, decider=self.manager,
            previous_status=PayrollPeriod.Status.SUBMITTED, new_status=PayrollPeriod.Status.APPROVED,
        )

    def test_manager_approves_expense_without_paying_it(self):
        expense = self._submitted_expense()
        self.auth(self.manager)
        response = self.client.post(
            f"/api/admin/finance/expenses/{expense.reference}/review/",
            {"approved": True, "review_note": "Invoice verified."}, format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        expense.refresh_from_db()
        self.assertEqual(expense.status, Expense.Status.APPROVED)
        self.assertIsNone(expense.financial_transaction_id,
                           "approval does not imply payment: posting stays a separate step")
        self._assert_uniform_evidence(
            self._decision_log("EXPENSE_REVIEWED", expense.reference),
            category="expense", reference=expense.reference,
            initiator=self.accountant, decider=self.manager,
            previous_status=Expense.Status.SUBMITTED, new_status=Expense.Status.APPROVED,
        )

    def test_manager_sees_all_five_queues_in_the_approvals_inbox(self):
        leave_request = self._leave()
        stock_count = self._pending_count()
        po = self._submitted_po()
        payroll_reference = self._submitted_payroll()
        expense = self._submitted_expense()
        self.auth(self.manager)
        response = self.client.get(INBOX_URL)
        self.assertEqual(response.status_code, 200, response.content)
        by_key = {c["key"]: c for c in response.json()["data"]["categories"]}
        self.assertIn(leave_request.reference, [i["reference"] for i in by_key["leave"]["items"]])
        self.assertIn(stock_count.reference, [i["reference"] for i in by_key["stock_counts"]["items"]])
        self.assertIn(po.reference, [i["reference"] for i in by_key["purchase_orders"]["items"]])
        self.assertIn(payroll_reference, [i["reference"] for i in by_key["payroll_runs"]["items"]])
        self.assertIn(expense.reference, [i["reference"] for i in by_key["expenses"]["items"]])
        # The Admin-only queues are not the Manager's to decide.
        self.assertNotIn("lateness_revocations", by_key)
        self.assertNotIn("shortage_cases", by_key)

    # -- Task 4: separation of duties, duplicates, rejections -------------
    def test_manager_cannot_approve_own_request(self):
        own = self._leave(staff=self.manager, key="b07-leave-own")
        self.auth(self.manager)
        response = self.client.post(
            f"/api/admin/staff-operations/leave-requests/{own.reference}/review/",
            {"approved": True, "review_note": "Self approval attempt."}, format="json",
        )
        self.assertEqual(response.status_code, 403, response.content)
        own.refresh_from_db()
        self.assertEqual(own.status, LeaveRequest.Status.PENDING)

    def test_duplicate_approvals_do_not_reapply_effects(self):
        stock_count = self._pending_count()
        self.auth(self.manager)
        url = f"/api/admin/inventory/stock-counts/{stock_count.reference}/approve/"
        first = self.client.post(url, {}, format="json")
        self.assertEqual(first.status_code, 200, first.content)
        movements = StockMovement.objects.filter(
            source_reference=stock_count.reference, type=StockMovement.Type.ADJUSTMENT
        ).count()
        second = self.client.post(url, {}, format="json")
        self.assertEqual(second.status_code, 200, second.content)
        stock_count.refresh_from_db()
        self.assertEqual(stock_count.status, StockCount.Status.POSTED)
        self.assertEqual(
            StockMovement.objects.filter(
                source_reference=stock_count.reference, type=StockMovement.Type.ADJUSTMENT
            ).count(), movements, "re-approval must not post a second adjustment",
        )

    def test_rejection_and_return_for_correction_workflows_preserved(self):
        leave_request = self._leave(key="b07-leave-reject")
        self.auth(self.manager)
        rejected = self.client.post(
            f"/api/admin/staff-operations/leave-requests/{leave_request.reference}/review/",
            {"approved": False, "review_note": "Peak period, pick new dates."}, format="json",
        )
        self.assertEqual(rejected.status_code, 200, rejected.content)
        leave_request.refresh_from_db()
        self.assertEqual(leave_request.status, LeaveRequest.Status.REJECTED)

        po = self._submitted_po(key="b07-po-reject")
        refused = self.client.post(
            f"/api/admin/inventory/purchase-orders/{po.reference}/reject/",
            {"reason": "Split across two suppliers first."}, format="json",
        )
        self.assertEqual(refused.status_code, 200, refused.content)
        po.refresh_from_db()
        self.assertEqual(po.status, PurchaseOrder.Status.DRAFT)
        self._assert_uniform_evidence(
            self._decision_log("PURCHASE_ORDER_REJECTED", po.reference),
            category="purchase_order", reference=po.reference,
            initiator=self.clerk, decider=self.manager,
            previous_status=PurchaseOrder.Status.SUBMITTED, new_status=PurchaseOrder.Status.DRAFT,
        )

    def test_unauthorized_staff_are_refused_on_all_five_approval_endpoints(self):
        leave_request = self._leave(key="b07-leave-403")
        stock_count = self._pending_count()
        po = self._submitted_po(key="b07-po-403")
        payroll_reference = self._submitted_payroll(key="b07-payroll-403")
        expense = self._submitted_expense(key="b07-expense-403")
        targets = [
            ("post", f"/api/admin/staff-operations/leave-requests/{leave_request.reference}/review/",
             {"approved": True}),
            ("post", f"/api/admin/inventory/stock-counts/{stock_count.reference}/approve/", {}),
            ("post", f"/api/admin/inventory/purchase-orders/{po.reference}/approve/", {}),
            ("post", f"/api/admin/staff-operations/payroll/periods/{payroll_reference}/review/",
             {"approved": True, "review_note": "x"}),
            ("post", f"/api/admin/finance/expenses/{expense.reference}/review/", {"approved": True}),
        ]
        for intruder in (self.waiter, self.receptionist):
            self.auth(intruder)
            for method, url, payload in targets:
                response = getattr(self.client, method)(url, payload, format="json")
                self.assertEqual(response.status_code, 403, f"{intruder.role} reached {url}")
        leave_request.refresh_from_db()
        self.assertEqual(leave_request.status, LeaveRequest.Status.PENDING)

    # -- Task 2: General Manager staff-directory access --------------------
    def test_general_manager_reads_but_cannot_write_staff_accounts(self):
        self.auth(self.gm)
        listed = self.client.get(USERS_URL)
        self.assertEqual(listed.status_code, 200, listed.content)
        detail = self.client.get(f"{USERS_URL}{self.waiter.pk}/")
        self.assertEqual(detail.status_code, 200, detail.content)
        created = self.client.post(USERS_URL, {
            "email": "b07-new@staff.dev", "first_name": "New", "last_name": "Staff",
            "role": "RECEPTIONIST",
        }, format="json")
        self.assertEqual(created.status_code, 403, created.content)
        updated = self.client.patch(f"{USERS_URL}{self.waiter.pk}/", {"phone": "08030000000"}, format="json")
        self.assertEqual(updated.status_code, 403, updated.content)

    def test_manager_tier_reads_but_cannot_write_staff_accounts(self):
        self.auth(self.manager)
        self.assertEqual(self.client.get(USERS_URL).status_code, 200)
        self.assertEqual(
            self.client.patch(f"{USERS_URL}{self.waiter.pk}/", {"phone": "08030000000"}, format="json").status_code,
            403,
        )

    def test_admin_staff_management_privileges_unchanged(self):
        self.auth(self.admin)
        created = self.client.post(USERS_URL, {
            "email": "b07-new@staff.dev", "first_name": "New", "last_name": "Staff",
            "role": "RECEPTIONIST",
        }, format="json")
        self.assertEqual(created.status_code, 201, created.content)
        user_id = created.json()["data"]["id"]
        updated = self.client.patch(f"{USERS_URL}{user_id}/", {"phone": "08030000000"}, format="json")
        self.assertEqual(updated.status_code, 200, updated.content)
        self.assertEqual(self.client.get(f"{USERS_URL}{user_id}/").status_code, 200)

    def test_department_staff_cannot_open_the_staff_account_collection(self):
        for intruder in (self.waiter, self.receptionist, self.housekeeper, self.accountant, self.clerk):
            self.auth(intruder)
            self.assertEqual(self.client.get(USERS_URL).status_code, 403, intruder.role)
            self.assertEqual(
                self.client.get(f"{USERS_URL}{self.waiter.pk}/").status_code, 403, intruder.role
            )
