"""POS/room-service critical workflow and finance bridge tests."""
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError

from apps.accounts.models import User
from apps.finance.models import ApprovalRequest, FinancialControlPolicy, FinancialTransaction
from apps.inventory.models import StockConsumptionRequest
from apps.finance.services.cash_session_service import close_cash_session, open_cash_session, review_cash_variance
from apps.finance.services.approval_service import review_approval
from apps.finance.services.folio_service import folio_balance_queryset
from apps.pos.models import MenuCategory, MenuItem, MenuModifier, PosOrder, PosOrderEvent, PosTender
from apps.pos.services.order_service import (
    capture_direct_tender,
    create_order,
    submit_order,
    transition_order,
)
from apps.stays.models import Stay

from .base import BaseAPITestCase
from .factories import make_booking, make_guest, make_room_type, make_staff


class PosWorkflowTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        from apps.core.utils import hotel_today

        self.actor = make_staff("pos-cashier@staff.dev", role=User.Role.CASHIER)
        self.today = hotel_today()
        room_type = make_room_type("POS suite", price="12000.00")
        booking = make_booking(
            make_guest("pos-guest@example.com"),
            room_type,
            check_in=self.today,
            check_out=self.today + timedelta(days=2),
            total="24000.00",
        )
        self.stay = Stay.objects.create(
            reference=f"STY-{booking.booking_reference}",
            booking=booking,
            guest=booking.guest,
            status=Stay.Status.IN_HOUSE,
            expected_arrival=booking.check_in,
            expected_departure=booking.check_out,
        )
        self.category = MenuCategory.objects.create(name="Kitchen", slug="kitchen")
        self.item = MenuItem.objects.create(
            category=self.category,
            name="Jollof Rice",
            sku="FOOD-JOLLOF",
            base_price=Decimal("2500.00"),
            tax_rate_percent=Decimal("7.50"),
        )
        self.modifier = MenuModifier.objects.create(
            menu_item=self.item,
            name="Extra chicken",
            price_delta=Decimal("1000.00"),
        )

    def _line(self, quantity=1):
        return {
            "menu_item_id": self.item.pk,
            "quantity": quantity,
            "modifier_ids": [self.modifier.pk],
            "notes": "No pepper",
        }

    def _deliver(self, order):
        submit_order(order=order, actor=self.actor)
        transition_order(order=order, target_status=PosOrder.Status.PREPARING, actor=self.actor)
        transition_order(order=order, target_status=PosOrder.Status.READY, actor=self.actor)
        return transition_order(order=order, target_status=PosOrder.Status.DELIVERED, actor=self.actor)

    def test_room_service_delivery_posts_one_immutable_folio_charge(self):
        order, created = create_order(
            mode=PosOrder.Mode.ROOM_SERVICE,
            stay=self.stay,
            actor=self.actor,
            lines=[self._line(quantity=2)],
            delivery_location="Room 101",
            idempotency_key="pos:room:1",
        )
        repeated, repeat_created = create_order(
            mode=PosOrder.Mode.ROOM_SERVICE,
            stay=self.stay,
            actor=self.actor,
            lines=[self._line(quantity=2)],
            idempotency_key="pos:room:1",
        )
        self.assertTrue(created)
        self.assertFalse(repeat_created)
        self.assertEqual(repeated.pk, order.pk)
        # (2,500 + 1,000) × 2 = 7,000; tax = 525; total = 7,525.
        self.assertEqual(order.subtotal, Decimal("7000.00"))
        self.assertEqual(order.tax_amount, Decimal("525.00"))
        self.assertEqual(order.total_amount, Decimal("7525.00"))

        delivered = self._deliver(order)
        delivered.refresh_from_db()
        self.assertEqual(delivered.status, PosOrder.Status.DELIVERED)
        self.assertIsNotNone(delivered.charge_transaction_id)
        self.assertEqual(delivered.charge_transaction.type, FinancialTransaction.Type.POS_CHARGE)
        self.assertEqual(
            folio_balance_queryset().get(pk=delivered.folio_id).balance,
            Decimal("7525"),
        )
        self.assertEqual(PosOrderEvent.objects.filter(order=order, type=PosOrderEvent.Type.CHARGED).count(), 1)
        consumption = StockConsumptionRequest.objects.get(pos_order=order)
        self.assertEqual(consumption.status, StockConsumptionRequest.Status.PENDING)
        self.assertEqual(consumption.payload["lines"][0]["item_sku"], self.item.sku)
        with self.assertRaisesMessage(ValidationError, "cannot transition"):
            transition_order(order=delivered, target_status=PosOrder.Status.CANCELLED, actor=self.actor)

    def test_direct_sale_is_charged_then_collected_as_separate_tender_event(self):
        order, _ = create_order(
            mode=PosOrder.Mode.TAKEAWAY,
            actor=self.actor,
            guest_name="Walk-in guest",
            lines=[self._line()],
        )
        delivered = self._deliver(order)
        tender = capture_direct_tender(
            order=delivered,
            method=PosTender.Method.CARD,
            amount=delivered.total_amount,
            actor=self.actor,
            external_reference="terminal-slip-1",
        )
        delivered.refresh_from_db()
        self.assertEqual(tender.status, PosTender.Status.CAPTURED)
        self.assertEqual(delivered.settlement_status, PosOrder.SettlementStatus.PAID)
        self.assertEqual(FinancialTransaction.objects.filter(source_reference=delivered.reference).count(), 2)
        self.assertEqual(tender.collection_transaction.type, FinancialTransaction.Type.PAYMENT_COLLECTION)

    def test_cash_sale_requires_the_actor_open_session_and_updates_drawer_projection(self):
        order, _ = create_order(
            mode=PosOrder.Mode.RESTAURANT,
            actor=self.actor,
            table_number="T-1",
            lines=[self._line()],
        )
        delivered = self._deliver(order)
        session = open_cash_session(cashier=self.actor, actor=self.actor, opening_float="100.00")
        tender = capture_direct_tender(
            order=delivered,
            method=PosTender.Method.CASH,
            amount=delivered.total_amount,
            actor=self.actor,
            cash_session=session,
        )
        session.refresh_from_db()
        self.assertEqual(tender.cash_session_id, session.pk)
        self.assertEqual(session.expected_cash, Decimal("3862.50"))

    def test_material_cash_variance_requires_manager_review_then_completes_session(self):
        manager = make_staff("pos-cash-variance-manager@staff.dev", role=User.Role.MANAGER)
        policy = FinancialControlPolicy.objects.get(name="Default financial controls")
        policy.cash_variance_manager_approval_threshold = Decimal("10.00")
        policy.save(update_fields=["cash_variance_manager_approval_threshold", "updated_at"])
        session = open_cash_session(cashier=self.actor, actor=self.actor, opening_float="100.00")
        pending = close_cash_session(cash_session=session, cashier=self.actor, counted_cash="0.00")
        self.assertEqual(pending.status, pending.Status.PENDING_REVIEW)
        approval = ApprovalRequest.objects.get(request_key=f"cash-variance:{session.reference}")
        self.assertEqual(approval.status, ApprovalRequest.Status.PENDING)

        reviewed = review_cash_variance(
            cash_session=pending, reviewer=manager, approved=True, review_note="Count witnessed."
        )
        self.assertEqual(reviewed.status, reviewed.Status.CLOSED)
        self.assertEqual(reviewed.review_approved_by_id, manager.pk)
        approval.refresh_from_db()
        self.assertEqual(approval.status, ApprovalRequest.Status.APPROVED)
        self.assertIsNotNone(approval.financial_transaction_id)
        self.assertEqual(approval.financial_transaction.type, FinancialTransaction.Type.CASH_CLOSE)
        self.assertEqual(
            review_cash_variance(
                cash_session=reviewed, reviewer=manager, approved=True, review_note="retry"
            ).pk,
            reviewed.pk,
        )

    def test_rejected_cash_variance_closes_drawer_without_posting_variance_journal(self):
        manager = make_staff("pos-cash-variance-reject-manager@staff.dev", role=User.Role.MANAGER)
        policy = FinancialControlPolicy.objects.get(name="Default financial controls")
        policy.cash_variance_manager_approval_threshold = Decimal("10.00")
        policy.save(update_fields=["cash_variance_manager_approval_threshold", "updated_at"])
        session = open_cash_session(cashier=self.actor, actor=self.actor, opening_float="100.00")
        pending = close_cash_session(cash_session=session, cashier=self.actor, counted_cash="0.00")

        closed = review_cash_variance(cash_session=pending, reviewer=manager, approved=False, review_note="Count rejected.")
        approval = ApprovalRequest.objects.get(request_key=f"cash-variance:{session.reference}")
        self.assertEqual(closed.status, closed.Status.CLOSED)
        self.assertIsNone(closed.review_approved_by_id)
        self.assertEqual(approval.status, ApprovalRequest.Status.REJECTED)
        self.assertIsNone(approval.financial_transaction_id)
        self.assertFalse(FinancialTransaction.objects.filter(source_key=f"cash-close-variance:{session.reference}").exists())

    def test_cash_variance_cannot_use_generic_review_or_self_review(self):
        manager = make_staff("pos-cash-variance-self-manager@staff.dev", role=User.Role.MANAGER)
        policy = FinancialControlPolicy.objects.get(name="Default financial controls")
        policy.cash_variance_manager_approval_threshold = Decimal("10.00")
        policy.save(update_fields=["cash_variance_manager_approval_threshold", "updated_at"])

        cashier_session = open_cash_session(cashier=self.actor, actor=self.actor, opening_float="100.00")
        pending = close_cash_session(cash_session=cashier_session, cashier=self.actor, counted_cash="0.00")
        approval = ApprovalRequest.objects.get(request_key=f"cash-variance:{cashier_session.reference}")
        with self.assertRaisesMessage(ValidationError, "controlled cash-session workflow"):
            review_approval(approval=approval, reviewer=manager, approved=True)
        pending.refresh_from_db()
        self.assertEqual(pending.status, pending.Status.PENDING_REVIEW)

        self_review_session = open_cash_session(cashier=manager, actor=manager, opening_float="100.00")
        self_review_pending = close_cash_session(cash_session=self_review_session, cashier=manager, counted_cash="0.00")
        with self.assertRaises(PermissionDenied):
            review_cash_variance(cash_session=self_review_pending, reviewer=manager, approved=True)
        self_review_pending.refresh_from_db()
        self.assertEqual(self_review_pending.status, self_review_pending.Status.PENDING_REVIEW)


    def test_room_service_rejects_non_in_house_stay(self):
        self.stay.status = Stay.Status.CHECKED_OUT
        self.stay.save(update_fields=["status", "updated_at"])
        with self.assertRaisesMessage(ValidationError, "in-house"):
            create_order(
                mode=PosOrder.Mode.ROOM_SERVICE,
                stay=self.stay,
                actor=self.actor,
                lines=[self._line()],
            )
