# tests/test_admin_approval_override.py
"""The administrator approval exemption, proven centrally.

The hotel owner/administrator must be able to run their own business without a
second administrator on payroll — but the exemption must be ONE centrally
enforced policy, granted to the administrator role only, configuration-driven,
and fully auditable. It must never let an ordinary role self-approve, and never
produce a duplicate financial or inventory effect.

What this module proves (brief section 14E):

1. an authorised administrator can create and approve a purchase order;
2. the administrator can complete the other controlled approval workflows
   (expense, stock-count variance, payroll run, cash variance);
3. ordinary roles remain subject to maker-checker;
4. a forged request cannot borrow administrator privileges;
5. every override is recorded as append-only audit evidence naming both the
   creator and the approving administrator;
6. retrying an approval creates no duplicate posting, payment or stock movement;
7. the exemption can be switched off in configuration without a code change.
"""
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import PermissionDenied
from django.test import override_settings

from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.core.approvals import OVERRIDE_MARKER
from apps.core.utils import hotel_today
from apps.finance.models import ApprovalRequest, FinancialEvent, FinancialTransaction
from apps.finance.services.approval_service import active_control_policy
from apps.finance.services.cash_session_service import (
    close_cash_session,
    open_cash_session,
    review_cash_variance,
)
from apps.finance.services.expense_service import (
    create_expense,
    post_expense,
    review_expense,
    submit_expense,
)
from apps.inventory.models import (
    PurchaseOrder,
    StockBalance,
    StockCount,
    StockCountEvent,
    StockItem,
    StockLocation,
    StockMovement,
    Supplier,
)
from apps.inventory.services.inventory_service import (
    approve_purchase_order,
    approve_stock_count,
    create_purchase_order,
    start_stock_count,
    submit_purchase_order,
    submit_stock_count,
)
from apps.staff_operations.payroll_models import PayrollLine, PayrollEvent, PayrollPeriod, StaffCompensation
from apps.staff_operations.services.payroll_service import (
    review_payroll_period,
    submit_payroll_period,
)

from .base import BaseAPITestCase
from .factories import make_staff


class AdminOverridePolicyTestCase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.admin = make_staff("ovr.admin@staff.dev", role=User.Role.ADMIN)
        self.manager = make_staff("ovr.manager@staff.dev", role=User.Role.MANAGER)
        self.general_manager = make_staff("ovr.gm@staff.dev", role=User.Role.GENERAL_MANAGER)
        self.clerk = make_staff("ovr.clerk@staff.dev", role=User.Role.STOREKEEPER)


class PurchaseOrderOverrideTests(AdminOverridePolicyTestCase):
    def _submitted_po(self, requester):
        supplier, _ = Supplier.objects.get_or_create(
            reference="SUP-OVR-1", defaults={"name": "Override supplier"},
        )
        location, _ = StockLocation.objects.get_or_create(
            code="OVR-STORE", defaults={"name": "Override store"},
        )
        item, _ = StockItem.objects.get_or_create(
            sku="OVR-ITEM",
            defaults={"name": "Override item", "base_unit": "PIECE", "standard_cost": "400.00"},
        )
        po, _ = create_purchase_order(
            supplier=supplier, delivery_location=location, requested_by=requester,
            lines=[{"item_id": item.pk, "quantity_ordered": "3", "unit_cost": "400"}],
            idempotency_key=f"ovr-po-{requester.pk}",
        )
        submit_purchase_order(purchase_order=po, actor=requester)
        po.refresh_from_db()
        return po

    def test_administrator_can_create_and_approve_their_own_purchase_order(self):
        po = self._submitted_po(self.admin)
        approved = approve_purchase_order(purchase_order=po, actor=self.admin)
        approved.refresh_from_db()
        self.assertEqual(approved.status, PurchaseOrder.Status.APPROVED)
        self.assertEqual(approved.approved_by_id, self.admin.pk)
        # The creator and the approver are the same account, by explicit policy.
        self.assertEqual(approved.requested_by_id, approved.approved_by_id)

    def test_the_override_is_disclosed_on_the_record_and_in_the_audit_log(self):
        po = self._submitted_po(self.admin)
        approve_purchase_order(purchase_order=po, actor=self.admin)
        po.refresh_from_db()
        self.assertIn(OVERRIDE_MARKER, po.notes)

        entry = AuditLog.objects.filter(action=OVERRIDE_MARKER).order_by("-pk").first()
        self.assertIsNotNone(entry, "an override must always be auditable")
        self.assertEqual(entry.actor_id, self.admin.pk)
        self.assertEqual(entry.metadata["workflow"], "purchase order")
        self.assertEqual(entry.metadata["override_actor_id"], self.admin.pk)
        self.assertEqual(entry.metadata["requester_id"], self.admin.pk)

    def test_a_manager_and_a_general_manager_are_still_refused(self):
        for role_holder in (self.manager, self.general_manager):
            po = self._submitted_po(role_holder)
            with self.assertRaises(PermissionDenied):
                approve_purchase_order(purchase_order=po, actor=role_holder)
            po.refresh_from_db()
            self.assertEqual(po.status, PurchaseOrder.Status.SUBMITTED)
            self.assertIsNone(po.approved_by_id)

    def test_a_non_administrator_cannot_escalate_by_changing_the_request_body(self):
        """A forged role in the payload must not create an override."""
        po = self._submitted_po(self.manager)
        self.auth(self.manager)
        response = self.client.post(
            f"/api/admin/inventory/purchase-orders/{po.reference}/approve/",
            {"role": "ADMIN", "is_admin": True, "approved_by": self.admin.pk},
            format="json",
        )
        self.assertEqual(response.status_code, 403, response.content)
        po.refresh_from_db()
        self.assertEqual(po.status, PurchaseOrder.Status.SUBMITTED)

    def test_repeated_approval_creates_no_duplicate_effect(self):
        po = self._submitted_po(self.admin)
        approve_purchase_order(purchase_order=po, actor=self.admin)
        approve_purchase_order(purchase_order=po, actor=self.admin)
        po.refresh_from_db()
        self.assertEqual(po.status, PurchaseOrder.Status.APPROVED)
        self.assertEqual(
            AuditLog.objects.filter(action=OVERRIDE_MARKER, object_id=str(po.pk)).count(), 1,
            "an idempotent re-approval must not write a second override record",
        )

    def test_retry_safety_of_the_status_shortcut_precedes_the_override_check(self):
        """An already-approved PO returns early, before any policy evaluation."""
        po = self._submitted_po(self.manager)
        approve_purchase_order(purchase_order=po, actor=self.admin)
        # The manager retrying an approved order is a no-op, not an override.
        before = AuditLog.objects.filter(action=OVERRIDE_MARKER).count()
        approve_purchase_order(purchase_order=po, actor=self.manager)
        self.assertEqual(AuditLog.objects.filter(action=OVERRIDE_MARKER).count(), before)

    @override_settings(ALLOW_ADMIN_APPROVAL_OVERRIDE=False)
    def test_the_exemption_can_be_switched_off_by_configuration(self):
        po = self._submitted_po(self.admin)
        with self.assertRaises(PermissionDenied):
            approve_purchase_order(purchase_order=po, actor=self.admin)
        po.refresh_from_db()
        self.assertEqual(po.status, PurchaseOrder.Status.SUBMITTED)
        self.assertFalse(AuditLog.objects.filter(action=OVERRIDE_MARKER).exists())


class StockCountVarianceOverrideTests(AdminOverridePolicyTestCase):
    def setUp(self):
        super().setUp()
        self.location = StockLocation.objects.create(code="OVR-COUNT", name="Override count store")
        self.item = StockItem.objects.create(
            sku="OVR-COUNT-ITEM", name="Counted item", base_unit="PIECE", standard_cost="250.00",
        )

    def _pending_variance_count(self, initiator):
        from apps.inventory.services.inventory_service import record_stock_movement

        record_stock_movement(
            item=self.item, location=self.location, movement_type=StockMovement.Type.OPENING,
            quantity_delta="10", source_key=f"ovr-count-opening-{initiator.pk}", actor=self.clerk,
        )
        stock_count = start_stock_count(
            location=self.location, item_ids=[self.item.pk], actor=initiator,
        )
        line = stock_count.lines.get()
        return submit_stock_count(
            stock_count=stock_count, actor=initiator,
            counts=[{"line_id": line.pk, "counted_quantity": "7"}],
        )

    def test_administrator_may_approve_their_own_stock_count_variance(self):
        stock_count = self._pending_variance_count(self.admin)
        self.assertEqual(stock_count.status, StockCount.Status.PENDING_APPROVAL)
        posted = approve_stock_count(stock_count=stock_count, actor=self.admin)
        self.assertEqual(posted.status, StockCount.Status.POSTED)
        self.assertEqual(
            StockBalance.objects.get(item=self.item, location=self.location).quantity_on_hand,
            Decimal("7.0000"),
        )
        event = StockCountEvent.objects.filter(
            stock_count=posted, type=StockCountEvent.Type.APPROVED
        ).first()
        self.assertEqual(event.details["admin_override"]["marker"], OVERRIDE_MARKER)

    def test_administrator_cannot_override_the_stale_balance_validation(self):
        """The exemption relaxes separation of duties — not data integrity."""
        stock_count = self._pending_variance_count(self.admin)
        from apps.inventory.services.inventory_service import record_stock_movement

        record_stock_movement(
            item=self.item, location=self.location, movement_type=StockMovement.Type.RECEIPT,
            quantity_delta="5", source_key="ovr-count-stale-receipt", actor=self.clerk,
        )
        from django.core.exceptions import ValidationError

        with self.assertRaisesMessage(ValidationError, "Stock changed since this count began"):
            approve_stock_count(stock_count=stock_count, actor=self.admin)

    def test_a_manager_is_still_refused_when_they_initiated_the_count(self):
        stock_count = self._pending_variance_count(self.manager)
        with self.assertRaises(PermissionDenied):
            approve_stock_count(stock_count=stock_count, actor=self.manager)


class ExpenseOverrideTests(AdminOverridePolicyTestCase):
    def _submitted_expense(self, requester):
        expense, _ = create_expense(
            actor=requester, category="Other operating expense", amount="40000.00",
            currency="NGN", description="Override-control expense",
            incurred_on=hotel_today(), idempotency_key=f"ovr-expense-{requester.pk}",
        )
        submit_expense(expense=expense, actor=requester)
        expense.refresh_from_db()
        return expense

    def test_administrator_may_approve_their_own_expense_with_auditable_evidence(self):
        expense = self._submitted_expense(self.admin)
        reviewed = review_expense(expense=expense, reviewer=self.admin, approved=True)
        reviewed.refresh_from_db()
        self.assertEqual(reviewed.status, reviewed.Status.APPROVED)
        self.assertEqual(reviewed.approved_by_id, self.admin.pk)

        approval = ApprovalRequest.objects.get(pk=reviewed.approval_request_id)
        event = FinancialEvent.objects.filter(approval_request=approval, type=FinancialEvent.Type.REVIEWED).first()
        self.assertEqual(event.details["admin_override"]["marker"], OVERRIDE_MARKER)
        self.assertIn("Administrator override", approval.review_note)

    def test_expense_posting_remains_single_and_reconciled_under_override(self):
        expense = self._submitted_expense(self.admin)
        review_expense(expense=expense, reviewer=self.admin, approved=True)
        posted, created = post_expense(expense=expense, actor=self.admin)
        posted.refresh_from_db()
        self.assertTrue(created)
        self.assertEqual(posted.status, posted.Status.POSTED)
        _, created_again = post_expense(expense=expense, actor=self.admin)
        self.assertFalse(created_again)
        self.assertEqual(
            FinancialTransaction.objects.filter(source_key=f"expense:{expense.reference}").count(), 1,
        )

    def test_a_manager_is_still_refused_on_their_own_expense(self):
        expense = self._submitted_expense(self.manager)
        with self.assertRaises(PermissionDenied):
            review_expense(expense=expense, reviewer=self.manager, approved=True)


class PayrollOverrideTests(AdminOverridePolicyTestCase):
    def setUp(self):
        super().setUp()
        self.employee = make_staff("ovr.payroll.employee@staff.dev", role=User.Role.RECEPTIONIST)
        compensation = StaffCompensation.objects.create(
            staff=self.employee, effective_from=hotel_today() - timedelta(days=90),
            currency="NGN", basic_salary=Decimal("200000.00"), created_by=self.admin,
        )
        self.period = PayrollPeriod.objects.create(
            reference="PAY-OVR-0001", starts_on=hotel_today() - timedelta(days=30),
            ends_on=hotel_today() - timedelta(days=1), currency="NGN",
            status=PayrollPeriod.Status.DRAFT, created_by=self.admin,
            total_gross=Decimal("200000.00"), total_deductions=Decimal("0.00"),
            total_net=Decimal("200000.00"), total_paye=Decimal("0.00"),
        )
        PayrollLine.objects.create(
            period=self.period, staff=self.employee, compensation=compensation,
            employee_name="Override Employee", basic_pay=Decimal("200000.00"),
            gross_pay=Decimal("200000.00"), net_pay=Decimal("200000.00"),
            deductions_total=Decimal("0.00"),
        )

    def test_administrator_may_approve_the_run_they_prepared(self):
        submit_payroll_period(period=self.period, actor=self.admin)
        approved = review_payroll_period(period=self.period, reviewer=self.admin, approved=True)
        approved.refresh_from_db()
        self.assertEqual(approved.status, PayrollPeriod.Status.APPROVED)
        self.assertEqual(approved.reviewed_by_id, approved.created_by_id)
        event = PayrollEvent.objects.filter(period=approved, type=PayrollEvent.Type.APPROVED).first()
        self.assertEqual(event.details["admin_override"]["marker"], OVERRIDE_MARKER)
        self.assertEqual(
            FinancialTransaction.objects.filter(source_reference=approved.reference).count(), 1,
        )

    def test_the_preparer_that_is_not_an_administrator_is_still_refused(self):
        period = PayrollPeriod.objects.create(
            reference="PAY-OVR-0002", starts_on=hotel_today() - timedelta(days=30),
            ends_on=hotel_today() - timedelta(days=1), currency="NGN",
            status=PayrollPeriod.Status.DRAFT, created_by=self.manager,
            total_gross=Decimal("1000.00"), total_deductions=Decimal("0.00"),
            total_net=Decimal("1000.00"), total_paye=Decimal("0.00"),
        )
        PayrollLine.objects.create(
            period=period, staff=self.employee,
            compensation=StaffCompensation.objects.filter(staff=self.employee).first(),
            employee_name="Override Employee", basic_pay=Decimal("1000.00"),
            gross_pay=Decimal("1000.00"), net_pay=Decimal("1000.00"),
            deductions_total=Decimal("0.00"),
        )
        submit_payroll_period(period=period, actor=self.manager)
        with self.assertRaises(PermissionDenied):
            review_payroll_period(period=period, reviewer=self.manager, approved=True)


class CashVarianceOverrideTests(AdminOverridePolicyTestCase):
    def test_administrator_may_review_a_variance_on_their_own_drawer(self):
        policy = active_control_policy()
        policy.cash_variance_manager_approval_threshold = Decimal("10.00")
        policy.save(update_fields=["cash_variance_manager_approval_threshold", "updated_at"])

        session = open_cash_session(cashier=self.admin, actor=self.admin, opening_float="100.00")
        pending = close_cash_session(cash_session=session, cashier=self.admin, counted_cash="0.00")
        self.assertEqual(pending.status, pending.Status.PENDING_REVIEW)

        reviewed = review_cash_variance(
            cash_session=pending, reviewer=self.admin, approved=True, review_note="Owner counted the drawer.",
        )
        self.assertEqual(reviewed.status, reviewed.Status.CLOSED)
        self.assertEqual(reviewed.review_approved_by_id, self.admin.pk)

    def test_a_non_administrator_still_may_not_review_their_own_drawer(self):
        policy = active_control_policy()
        policy.cash_variance_manager_approval_threshold = Decimal("10.00")
        policy.save(update_fields=["cash_variance_manager_approval_threshold", "updated_at"])

        session = open_cash_session(cashier=self.manager, actor=self.manager, opening_float="100.00")
        pending = close_cash_session(cash_session=session, cashier=self.manager, counted_cash="0.00")
        with self.assertRaises(PermissionDenied):
            review_cash_variance(
                cash_session=pending, reviewer=self.manager, approved=True, review_note="self",
            )
