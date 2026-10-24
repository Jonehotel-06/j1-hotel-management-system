"""Regression coverage for the bar unopened-item return workflow.

The owner's scenario: a guest orders twelve bottles at ₦2,000, drinks seven and
hands five unopened bottles back. The hotel must charge ₦14,000, put five bottles
back in the bar store, and (because ₦24,000 was already collected) refund
₦10,000 — exactly once, never more than was paid, and never labelled successful
before it actually happened.

These tests exercise the real service and the real HTTP endpoints, and they
assert the ledger, not just the response body: the stock movement type, the
balance, the compensating financial entry, the refund state, and the fact that
the original order/charge/tender rows were not rewritten.
"""
from datetime import timedelta
from decimal import Decimal

from apps.accounts.models import User
from apps.core.utils import hotel_today
from apps.finance.models import ApprovalRequest, FinancialControlPolicy, FinancialTransaction
from apps.finance.services.cash_session_service import open_cash_session
from apps.inventory.models import StockBalance, StockConsumptionRequest, StockItem, StockLocation, StockMovement
from apps.pos.models import MenuCategory, MenuItem, PosOrder, PosOrderEvent, PosOrderReturn, PosTender
from apps.pos.services.order_service import (
    capture_direct_tender,
    create_order,
    submit_order,
    transition_order,
)
from apps.pos.services.return_service import (
    confirm_return_refund,
    record_unopened_return,
    return_preview,
)

from .base import BaseAPITestCase
from .factories import make_staff


class UnopenedReturnTestBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.cashier = make_staff("returns.cashier@staff.dev", role=User.Role.CASHIER)
        self.bartender = make_staff("returns.bartender@staff.dev", role=User.Role.BARTENDER)
        self.manager = make_staff("returns.manager@staff.dev", role=User.Role.MANAGER)
        self.accountant = make_staff("returns.accountant@staff.dev", role=User.Role.ACCOUNTANT)
        self.location = StockLocation.objects.create(code="BAR-S", name="Bar store (returns)")
        self.beer = StockItem.objects.create(
            sku="BEER-33CL", name="Beer bottle 33cl", category="Beverages",
            item_type=StockItem.ItemType.SELLABLE_PRODUCT, base_unit="BOTTLE",
            standard_cost=Decimal("800.00"),
        )
        self.category = MenuCategory.objects.create(
            name="Bar drinks", slug="bar-drinks-returns", service_area=MenuCategory.ServiceArea.BAR
        )
        self.beer_menu = MenuItem.objects.create(
            category=self.category, name="Beer bottle", sku="MENU-BEER-33", base_price=Decimal("2000.00")
        )
        # Explicit direct-sale configuration: one menu unit = one bottle of this store.
        StockItem.objects.filter(pk=self.beer.pk).update(
            direct_sale_menu_item=self.beer_menu,
            direct_sale_location=self.location,
            direct_sale_quantity=Decimal("1.0000"),
        )
        self.beer.refresh_from_db()
        # Opening stock: 200 bottles on the bar shelf.
        from apps.inventory.services.inventory_service import record_opening_stock

        record_opening_stock(
            item=self.beer, location=self.location, quantity=Decimal("200.0000"),
            actor=self.manager, idempotency_key="opening-beer-200",
        )
        policy = FinancialControlPolicy.objects.get(name="Default financial controls")
        policy.refund_manager_approval_threshold = Decimal("100000.00")
        policy.save(update_fields=["refund_manager_approval_threshold", "updated_at"])

    def _deliver_bar_order(self, quantity=12, *, mode=PosOrder.Mode.BAR):
        order, _ = create_order(
            mode=mode, actor=self.cashier, guest_name="Table 4 guest", table_number="T4",
            lines=[{"menu_item_id": self.beer_menu.pk, "quantity": quantity}],
            idempotency_key=f"returns-order-{quantity}-{mode}",
        )
        submit_order(order=order, actor=self.cashier)
        order = transition_order(order=order, target_status=PosOrder.Status.DELIVERED, actor=self.cashier)
        order.refresh_from_db()
        return order

    def _pay(self, order, amount, method=PosTender.Method.CARD):
        from apps.finance.services.cash_session_service import open_cash_session

        session = None
        if method == PosTender.Method.CASH:
            session = open_cash_session(cashier=self.cashier, actor=self.cashier, opening_float=Decimal("50000.00"))
        return capture_direct_tender(
            order=order, method=method, amount=amount, actor=self.cashier, cash_session=session,
        )

    def balance(self):
        return StockBalance.objects.get(item=self.beer, location=self.location).quantity_on_hand


class UnopenedReturnServiceTests(UnopenedReturnTestBase):
    def test_partial_return_restores_stock_and_refunds_exactly_the_returned_value(self):
        order = self._deliver_bar_order(12)
        self.assertEqual(order.total_amount, Decimal("24000.00"))
        # The sale already deducted twelve bottles.
        self.assertEqual(self.balance(), Decimal("188.0000"))
        self._pay(order, Decimal("24000.00"))

        record, created = record_unopened_return(
            order=order,
            lines=[{"order_line_id": order.lines.first().pk, "quantity_returned": 5}],
            actor=self.bartender,
            reason="Guest left five unopened",
            idempotency_key="ret-1",
            refund_method=PosOrderReturn.RefundMethod.CARD,
        )
        self.assertTrue(created)

        # 5 × ₦2,000 = ₦10,000 returned; the guest pays ₦14,000.
        self.assertEqual(record.returned_amount, Decimal("10000.00"))
        self.assertEqual(record.final_total, Decimal("14000.00"))
        self.assertEqual(record.amount_paid, Decimal("24000.00"))
        self.assertEqual(record.refund_due, Decimal("10000.00"))
        # Card refund is NOT claimed successful before the provider confirms.
        self.assertEqual(record.refund_status, PosOrderReturn.RefundStatus.PROCESSING)
        self.assertIsNone(record.refund_transaction_id)

        # Five bottles are physically back on the shelf, as a POS_RETURN movement.
        self.assertEqual(self.balance(), Decimal("193.0000"))
        movement = StockMovement.objects.get(type=StockMovement.Type.POS_RETURN)
        self.assertEqual(movement.quantity_delta, Decimal("5.0000"))
        self.assertEqual(movement.source_reference, order.reference)
        self.assertIn(record.reference, movement.metadata["pos_order_return"])

        # The original charge and tender rows were not rewritten.
        order.refresh_from_db()
        self.assertEqual(order.total_amount, Decimal("24000.00"))
        self.assertEqual(order.returned_amount, Decimal("10000.00"))
        self.assertEqual(order.net_total, Decimal("14000.00"))
        self.assertEqual(order.tenders.get().amount, Decimal("24000.00"))
        # Money in still covers the final charge, so the order is PAID; the
        # ₦10,000 the hotel owes back rides on the return's refund state until
        # the provider actually pays it.
        self.assertEqual(order.settlement_status, PosOrder.SettlementStatus.PAID)

        # One balanced compensating entry takes the returned value off the bill.
        credit = record.credit_transaction
        self.assertIsNotNone(credit)
        self.assertEqual(credit.type, FinancialTransaction.Type.ADJUSTMENT)
        self.assertEqual(
            sum(line.amount for line in credit.lines.all()), Decimal("20000.00")  # debit + credit
        )
        self.assertTrue(PosOrderEvent.objects.filter(order=order, type=PosOrderEvent.Type.RETURN_RECORDED).exists())

        # No money-back entry exists yet for a card refund awaiting confirmation.
        self.assertFalse(
            FinancialTransaction.objects.filter(
                type=FinancialTransaction.Type.REFUND, source_reference=order.reference
            ).exists()
        )

    def test_provider_confirmation_completes_the_refund_once(self):
        order = self._deliver_bar_order(12)
        self._pay(order, Decimal("24000.00"))
        record, _ = record_unopened_return(
            order=order,
            lines=[{"order_line_id": order.lines.first().pk, "quantity_returned": 5}],
            actor=self.bartender, reason="Guest left five unopened",
            refund_method=PosOrderReturn.RefundMethod.CARD,
        )
        confirmed = confirm_return_refund(
            record=record, actor=self.manager, refund_method=PosOrderReturn.RefundMethod.CARD,
            provider_reference="PSK-RFND-0001",
        )
        self.assertEqual(confirmed.refund_status, PosOrderReturn.RefundStatus.COMPLETED)
        self.assertEqual(confirmed.refund_reference, "PSK-RFND-0001")
        self.assertEqual(confirmed.refund_transaction.type, FinancialTransaction.Type.REFUND)
        # A repeated confirmation changes nothing and posts nothing new.
        again = confirm_return_refund(
            record=record, actor=self.manager, refund_method=PosOrderReturn.RefundMethod.CARD,
            provider_reference="PSK-RFND-0001",
        )
        self.assertEqual(again.refund_transaction_id, confirmed.refund_transaction_id)
        self.assertEqual(
            FinancialTransaction.objects.filter(source_key=f"pos-return-refund:{record.reference}").count(), 1
        )
        order.refresh_from_db()
        self.assertEqual(order.settlement_status, PosOrder.SettlementStatus.PARTIALLY_REFUNDED)

    def test_card_refund_cannot_be_marked_successful_without_a_provider_reference(self):
        order = self._deliver_bar_order(6)
        self._pay(order, Decimal("12000.00"))
        record, _ = record_unopened_return(
            order=order,
            lines=[{"order_line_id": order.lines.first().pk, "quantity_returned": 2}],
            actor=self.bartender, refund_method=PosOrderReturn.RefundMethod.CARD,
        )
        from django.core.exceptions import ValidationError

        with self.assertRaisesMessage(ValidationError, "provider/bank confirmation reference"):
            confirm_return_refund(
                record=record, actor=self.manager, refund_method=PosOrderReturn.RefundMethod.CARD,
                provider_reference="",
            )
        record.refresh_from_db()
        self.assertEqual(record.refund_status, PosOrderReturn.RefundStatus.PROCESSING)

    def test_unpaid_order_reduces_the_payable_without_inventing_a_refund(self):
        order = self._deliver_bar_order(10)
        record, _ = record_unopened_return(
            order=order,
            lines=[{"order_line_id": order.lines.first().pk, "quantity_returned": 4}],
            actor=self.bartender, reason="Four unopened bottles",
        )
        self.assertEqual(record.final_total, Decimal("12000.00"))
        self.assertEqual(record.refund_due, Decimal("0.00"))
        self.assertEqual(record.refund_status, PosOrderReturn.RefundStatus.NOT_DUE)
        order.refresh_from_db()
        self.assertEqual(order.net_total, Decimal("12000.00"))
        self.assertEqual(order.settlement_status, PosOrder.SettlementStatus.UNPAID)
        # Collecting the payable is capped at the FINAL charge, not the original.
        from django.core.exceptions import ValidationError

        with self.assertRaisesMessage(ValidationError, "Tender exceeds"):
            self._pay(order, Decimal("16000.00"))
        tender = self._pay(order, Decimal("12000.00"))
        self.assertEqual(tender.amount, Decimal("12000.00"))
        order.refresh_from_db()
        self.assertEqual(order.settlement_status, PosOrder.SettlementStatus.PAID)

    def test_partial_payment_never_refunds_more_than_was_collected(self):
        order = self._deliver_bar_order(12)
        self._pay(order, Decimal("5000.00"))
        record, _ = record_unopened_return(
            order=order,
            lines=[{"order_line_id": order.lines.first().pk, "quantity_returned": 5}],
            actor=self.bartender, reason="Five unopened",
            refund_method=PosOrderReturn.RefundMethod.CASH,
        )
        # Returned value is ₦10,000 but only ₦5,000 was paid and the guest still
        # owes ₦9,000 — so there is nothing to hand back.
        self.assertEqual(record.returned_amount, Decimal("10000.00"))
        self.assertEqual(record.final_total, Decimal("14000.00"))
        self.assertEqual(record.refund_due, Decimal("0.00"))
        self.assertEqual(record.refund_status, PosOrderReturn.RefundStatus.NOT_DUE)
        order.refresh_from_db()
        self.assertEqual(order.settlement_status, PosOrder.SettlementStatus.PARTIALLY_PAID)
        self.assertFalse(FinancialTransaction.objects.filter(type=FinancialTransaction.Type.REFUND).exists())

    def test_discounted_line_refunds_the_net_price_not_the_menu_price(self):
        order = self._deliver_bar_order(6)
        # Simulate the frozen discounted line the guest was actually billed:
        # ₦2,000 list price discounted to a ₦9,000 line total for six bottles.
        line = order.lines.first()
        PosOrder.objects.filter(pk=order.pk).update(subtotal=Decimal("9000.00"), total_amount=Decimal("9000.00"))
        from apps.pos.models import PosOrderLine

        PosOrderLine.objects.filter(pk=line.pk).update(unit_price=Decimal("1500.00"), line_total=Decimal("9000.00"))
        order.refresh_from_db()
        self._pay(order, Decimal("9000.00"))
        record, _ = record_unopened_return(
            order=order,
            lines=[{"order_line_id": line.pk, "quantity_returned": 2}],
            actor=self.bartender, reason="Two unopened",
            refund_method=PosOrderReturn.RefundMethod.CARD,
        )
        # 2/6 of ₦9,000 = ₦3,000, not 2 × ₦2,000 = ₦4,000.
        self.assertEqual(record.returned_amount, Decimal("3000.00"))
        self.assertEqual(record.refund_due, Decimal("3000.00"))
        self.assertEqual(record.final_total, Decimal("6000.00"))

    def test_full_return_restores_every_bottle_and_zeroes_the_charge(self):
        order = self._deliver_bar_order(4)
        self._pay(order, Decimal("8000.00"))
        record, _ = record_unopened_return(
            order=order,
            lines=[{"order_line_id": order.lines.first().pk, "quantity_returned": 4}],
            actor=self.bartender, reason="Guest cancelled the round",
            refund_method=PosOrderReturn.RefundMethod.CARD,
        )
        self.assertEqual(record.final_total, Decimal("0.00"))
        self.assertEqual(record.refund_due, Decimal("8000.00"))
        self.assertEqual(self.balance(), Decimal("200.0000"))
        order.refresh_from_db()
        self.assertEqual(order.settlement_status, PosOrder.SettlementStatus.REFUNDED)

    def test_multiple_lines_are_returned_independently(self):
        second_item = MenuItem.objects.create(
            category=self.category, name="Malt bottle", sku="MENU-MALT-33", base_price=Decimal("1500.00")
        )
        malt = StockItem.objects.create(
            sku="MALT-33CL", name="Malt bottle 33cl", category="Beverages",
            item_type=StockItem.ItemType.SELLABLE_PRODUCT, base_unit="BOTTLE", standard_cost=Decimal("700.00"),
            direct_sale_menu_item=second_item, direct_sale_location=self.location,
            direct_sale_quantity=Decimal("1.0000"),
        )
        from apps.inventory.services.inventory_service import record_opening_stock

        record_opening_stock(
            item=malt, location=self.location, quantity=Decimal("50.0000"),
            actor=self.manager, idempotency_key="opening-malt-50",
        )
        order, _ = create_order(
            mode=PosOrder.Mode.BAR, actor=self.cashier, guest_name="Mixed round",
            lines=[
                {"menu_item_id": self.beer_menu.pk, "quantity": 6},
                {"menu_item_id": second_item.pk, "quantity": 4},
            ],
            idempotency_key="returns-mixed-round",
        )
        submit_order(order=order, actor=self.cashier)
        order = transition_order(order=order, target_status=PosOrder.Status.DELIVERED, actor=self.cashier)
        order.refresh_from_db()
        lines = list(order.lines.order_by("pk"))
        record, _ = record_unopened_return(
            order=order,
            lines=[
                {"order_line_id": lines[0].pk, "quantity_returned": 2},
                {"order_line_id": lines[1].pk, "quantity_returned": 3},
            ],
            actor=self.bartender, reason="Mixed unopened return",
        )
        # 2 × ₦2,000 + 3 × ₦1,500 = ₦8,500
        self.assertEqual(record.returned_amount, Decimal("8500.00"))
        self.assertEqual(self.balance(), Decimal("196.0000"))
        self.assertEqual(
            StockBalance.objects.get(item=malt, location=self.location).quantity_on_hand, Decimal("49.0000")
        )
        self.assertEqual(record.lines.count(), 2)

    def test_repeated_request_is_idempotent_and_duplicate_returns_are_refused(self):
        order = self._deliver_bar_order(12)
        line = order.lines.first()
        first, created = record_unopened_return(
            order=order, lines=[{"order_line_id": line.pk, "quantity_returned": 5}],
            actor=self.bartender, idempotency_key="ret-idem-1",
        )
        self.assertTrue(created)
        repeat, created_again = record_unopened_return(
            order=order, lines=[{"order_line_id": line.pk, "quantity_returned": 5}],
            actor=self.bartender, idempotency_key="ret-idem-1",
        )
        self.assertFalse(created_again)
        self.assertEqual(repeat.pk, first.pk)
        self.assertEqual(self.balance(), Decimal("193.0000"))
        self.assertEqual(PosOrderReturn.objects.filter(order=order).count(), 1)

        from django.core.exceptions import ValidationError

        # Nor can a second request return more than the remaining seven — the
        # same bottle can never be returned twice.
        with self.assertRaisesMessage(ValidationError, "at most 7"):
            record_unopened_return(
                order=order, lines=[{"order_line_id": line.pk, "quantity_returned": 8}],
                actor=self.bartender, idempotency_key="ret-idem-2",
            )
        # And once every bottle is back, nothing more can be returned.
        record_unopened_return(
            order=order, lines=[{"order_line_id": line.pk, "quantity_returned": 7}],
            actor=self.bartender, idempotency_key="ret-idem-3",
        )
        with self.assertRaisesMessage(ValidationError, "at most 0"):
            record_unopened_return(
                order=order, lines=[{"order_line_id": line.pk, "quantity_returned": 1}],
                actor=self.bartender, idempotency_key="ret-idem-4",
            )
        self.assertEqual(self.balance(), Decimal("200.0000"))

    def test_second_return_after_a_first_one_uses_only_the_remaining_quantity(self):
        order = self._deliver_bar_order(12)
        line = order.lines.first()
        record_unopened_return(
            order=order, lines=[{"order_line_id": line.pk, "quantity_returned": 5}],
            actor=self.bartender, idempotency_key="ret-seq-1",
        )
        second, _ = record_unopened_return(
            order=order, lines=[{"order_line_id": line.pk, "quantity_returned": 7}],
            actor=self.bartender, idempotency_key="ret-seq-2",
        )
        self.assertEqual(second.returned_amount, Decimal("14000.00"))
        self.assertEqual(second.final_total, Decimal("0.00"))
        self.assertEqual(self.balance(), Decimal("200.0000"))
        order.refresh_from_db()
        self.assertEqual(order.returned_amount, Decimal("24000.00"))

    def test_undelivered_or_non_bar_orders_cannot_return_items(self):
        order, _ = create_order(
            mode=PosOrder.Mode.BAR, actor=self.cashier,
            lines=[{"menu_item_id": self.beer_menu.pk, "quantity": 2}],
            idempotency_key="returns-undelivered",
        )
        from django.core.exceptions import ValidationError

        with self.assertRaisesMessage(ValidationError, "delivered"):
            record_unopened_return(
                order=order, lines=[{"order_line_id": order.lines.first().pk, "quantity_returned": 1}],
                actor=self.bartender,
            )
        restaurant_category = MenuCategory.objects.create(
            name="Restaurant kitchen", slug="restaurant-kitchen-returns",
            service_area=MenuCategory.ServiceArea.RESTAURANT,
        )
        takeaway_item = MenuItem.objects.create(
            category=restaurant_category, name="Takeaway rice", sku="MENU-TAKEAWAY-RICE",
            base_price=Decimal("2000.00"),
        )
        takeaway, _ = create_order(
            mode=PosOrder.Mode.TAKEAWAY, actor=self.cashier,
            lines=[{"menu_item_id": takeaway_item.pk, "quantity": 2}],
            idempotency_key="returns-takeaway-order",
        )
        submit_order(order=takeaway, actor=self.cashier)
        takeaway = transition_order(order=takeaway, target_status=PosOrder.Status.DELIVERED, actor=self.cashier)
        takeaway.refresh_from_db()
        with self.assertRaisesMessage(ValidationError, "bar and restaurant"):
            record_unopened_return(
                order=takeaway, lines=[{"order_line_id": takeaway.lines.first().pk, "quantity_returned": 1}],
                actor=self.bartender,
            )

    def test_return_without_a_posted_stock_deduction_does_not_invent_stock(self):
        # A product with no direct-sale configuration and no recipe: the sale
        # deducted nothing, so a return must not create stock that was never issued.
        menu_only = MenuItem.objects.create(
            category=self.category, name="Unconfigured spirit", sku="MENU-SPIRIT", base_price=Decimal("3000.00")
        )
        order, _ = create_order(
            mode=PosOrder.Mode.BAR, actor=self.cashier,
            lines=[{"menu_item_id": menu_only.pk, "quantity": 3}],
            idempotency_key="returns-unconfigured",
        )
        submit_order(order=order, actor=self.cashier)
        order = transition_order(order=order, target_status=PosOrder.Status.DELIVERED, actor=self.cashier)
        order.refresh_from_db()
        self.assertEqual(
            StockConsumptionRequest.objects.get(pos_order=order).status, StockConsumptionRequest.Status.FAILED
        )
        record, _ = record_unopened_return(
            order=order,
            lines=[{"order_line_id": order.lines.first().pk, "quantity_returned": 1}],
            actor=self.bartender,
            reason="Unconfigured product returned",
        )
        self.assertFalse(StockMovement.objects.filter(type=StockMovement.Type.POS_RETURN).exists())
        self.assertIn("nothing is restocked", record.metadata["restock_skipped_reason"])
        # The bill is still corrected — the guest is not charged for what came back.
        self.assertEqual(record.returned_amount, Decimal("3000.00"))
        self.assertEqual(record.final_total, Decimal("6000.00"))

    def test_refund_above_policy_threshold_needs_an_independent_approver(self):
        policy = FinancialControlPolicy.objects.get(name="Default financial controls")
        policy.refund_manager_approval_threshold = Decimal("1000.00")
        policy.save(update_fields=["refund_manager_approval_threshold", "updated_at"])
        order = self._deliver_bar_order(12)
        self._pay(order, Decimal("24000.00"))
        record, _ = record_unopened_return(
            order=order,
            lines=[{"order_line_id": order.lines.first().pk, "quantity_returned": 5}],
            actor=self.bartender, reason="Five unopened",
            refund_method=PosOrderReturn.RefundMethod.CARD,
        )
        self.assertEqual(record.refund_status, PosOrderReturn.RefundStatus.AWAITING_APPROVAL)
        approval = ApprovalRequest.objects.get(request_key=f"pos-return-refund:{record.reference}")
        self.assertEqual(approval.status, ApprovalRequest.Status.PENDING)
        self.assertEqual(approval.requester_id, self.bartender.pk)
        # No money may move until a second person approves.
        from django.core.exceptions import ValidationError

        with self.assertRaisesMessage(ValidationError, "needs a manager's approval"):
            confirm_return_refund(
                record=record, actor=self.manager, refund_method=PosOrderReturn.RefundMethod.CARD,
                provider_reference="PSK-RFND-0002",
            )
        self.assertFalse(
            FinancialTransaction.objects.filter(source_key=f"pos-return-refund:{record.reference}").exists()
        )

    def test_cash_refund_pays_out_of_the_drawer_and_updates_expected_cash(self):
        order = self._deliver_bar_order(12)
        session = open_cash_session(cashier=self.cashier, actor=self.cashier, opening_float=Decimal("50000.00"))
        capture_direct_tender(
            order=order, method=PosTender.Method.CASH, amount=Decimal("24000.00"),
            actor=self.cashier, cash_session=session,
        )
        session.refresh_from_db()
        before = session.expected_cash
        record, _ = record_unopened_return(
            order=order,
            lines=[{"order_line_id": order.lines.first().pk, "quantity_returned": 5}],
            actor=self.cashier, reason="Five unopened",
            refund_method=PosOrderReturn.RefundMethod.CASH,
            cash_session=session,
        )
        self.assertEqual(record.refund_status, PosOrderReturn.RefundStatus.COMPLETED)
        session.refresh_from_db()
        self.assertEqual(session.expected_cash, before - Decimal("10000.00"))
        self.assertEqual(record.refund_cash_session_id, session.pk)
        self.assertEqual(record.refund_transaction.type, FinancialTransaction.Type.REFUND)

    def test_preview_reports_every_figure_the_modal_shows(self):
        order = self._deliver_bar_order(12)
        self._pay(order, Decimal("24000.00"))
        record_unopened_return(
            order=order, lines=[{"order_line_id": order.lines.first().pk, "quantity_returned": 5}],
            actor=self.bartender, idempotency_key="ret-preview-1",
        )
        preview = return_preview(order)
        self.assertEqual(preview["original_total"], "24000.00")
        self.assertEqual(preview["returned_amount"], "10000.00")
        self.assertEqual(preview["final_total"], "14000.00")
        self.assertEqual(preview["amount_paid"], "24000.00")
        self.assertEqual(preview["refund_due"], "10000.00")
        self.assertEqual(preview["lines"][0]["quantity_returned"], 5)
        self.assertEqual(preview["lines"][0]["quantity_returnable"], 7)
        self.assertEqual(len(preview["returns"]), 1)


class UnopenedReturnApiTests(UnopenedReturnTestBase):
    def _post_return(self, order, quantity, **extra):
        payload = {
            "lines": [{"order_line_id": order.lines.first().pk, "quantity_returned": quantity}],
            "reason": "Unopened bottles",
            "idempotency_key": f"api-ret-{quantity}",
        }
        payload.update(extra)
        return self.client.post(
            f"/api/admin/pos/orders/{order.reference}/returns/", payload, format="json"
        )

    def test_bartender_records_return_and_endpoint_returns_the_figures(self):
        order = self._deliver_bar_order(12)
        self._pay(order, Decimal("24000.00"))
        self.auth(self.bartender)
        eligible = self.client.get("/api/admin/pos/orders/returns/eligible/")
        self.assertEqual(eligible.status_code, 200, eligible.content)
        self.assertIn(order.reference, [row["reference"] for row in eligible.json()["data"]])
        preview = self.client.get(f"/api/admin/pos/orders/{order.reference}/return-preview/")
        self.assertEqual(preview.status_code, 200, preview.content)
        self.assertEqual(preview.json()["data"]["lines"][0]["quantity_returnable"], 12)

        response = self._post_return(order, 5, refund_method="CARD")
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()["data"]
        self.assertEqual(body["returned_amount"], "10000.00")
        self.assertEqual(body["final_total"], "14000.00")
        self.assertEqual(body["refund_due"], "10000.00")
        self.assertEqual(body["refund_status"], "PROCESSING")
        self.assertEqual(body["lines"][0]["quantity_returned"], 5)

        repeat = self._post_return(order, 5, refund_method="CARD")
        self.assertEqual(repeat.status_code, 200, repeat.content)
        self.assertEqual(repeat.json()["data"]["reference"], body["reference"])

    def test_bartender_cannot_pay_the_refund_out_but_a_cashier_can(self):
        order = self._deliver_bar_order(12)
        self._pay(order, Decimal("24000.00"))
        self.auth(self.bartender)
        created = self._post_return(order, 5, refund_method="CARD").json()["data"]
        reference = created["reference"]
        denied = self.client.post(
            f"/api/admin/pos/returns/{reference}/refund/",
            {"refund_method": "CARD", "provider_reference": "PSK-1"}, format="json",
        )
        self.assertEqual(denied.status_code, 403, denied.content)
        self.auth(self.cashier)
        allowed = self.client.post(
            f"/api/admin/pos/returns/{reference}/refund/",
            {"refund_method": "CARD", "provider_reference": "PSK-1"}, format="json",
        )
        self.assertEqual(allowed.status_code, 200, allowed.content)
        self.assertEqual(allowed.json()["data"]["refund_status"], "COMPLETED")
        self.assertEqual(allowed.json()["data"]["refund_reference"], "PSK-1")

    def test_accountant_and_receptionist_are_denied_the_bar_return_workflow(self):
        order = self._deliver_bar_order(6)
        for user in (self.accountant,):
            self.auth(user)
            self.assertEqual(
                self.client.get("/api/admin/pos/orders/returns/eligible/").status_code, 403
            )
            self.assertEqual(self._post_return(order, 1).status_code, 403)
        receptionist = make_staff("returns.reception@staff.dev", role=User.Role.RECEPTIONIST)
        self.auth(receptionist)
        self.assertEqual(self.client.get("/api/admin/pos/orders/returns/eligible/").status_code, 403)
        self.assertEqual(self._post_return(order, 1).status_code, 403)

    def test_returns_list_and_order_detail_expose_the_audit_trail(self):
        order = self._deliver_bar_order(12)
        self._pay(order, Decimal("24000.00"))
        self.auth(self.bartender)
        created = self._post_return(order, 5, refund_method="CARD").json()["data"]
        listing = self.client.get("/api/admin/pos/orders/returns/", {"search": order.reference})
        self.assertEqual(listing.status_code, 200, listing.content)
        self.assertEqual(listing.json()["pagination"]["count"], 1)
        self.assertEqual(listing.json()["data"][0]["reference"], created["reference"])
        filtered = self.client.get("/api/admin/pos/orders/returns/", {"refund_status": "PROCESSING"})
        self.assertEqual(filtered.json()["pagination"]["count"], 1)
        order_detail = self.client.get(f"/api/admin/pos/orders/{order.reference}/")
        self.assertEqual(order_detail.status_code, 200, order_detail.content)
        events = [event["type"] for event in order_detail.json()["data"]["events"]]
        self.assertIn("RETURN_RECORDED", events)
