"""Tender autofill, outstanding-balance and duplicate-collection coverage.

Owner-reported problem (A): after an order is delivered, the tender form opened
with an empty amount box, and nothing in the order payload said how much was
still owed. The browser had no authoritative figure to prefill and no way to
tell a partially paid order from an unpaid one.

These tests pin the corrected contract end to end:

* the order API carries server-derived ``net_total``, ``amount_paid``,
  ``outstanding_balance``, ``returned_amount`` and
  ``settlement_action_available`` — the browser prefills from those and never
  computes money itself;
* the outstanding balance is what a FULLY UNPAID order owes, what a PARTIALLY
  PAID order still owes (the remaining amount, not the original total), and what
  unopened-item RETURNS have reduced;
* a FULLY PAID order can never take another tender;
* overpayment, a stale screen, a currency mismatch and a duplicate submission
  are all refused (or converged) server-side;
* the balance is recomputed under the order row lock, so a second cashier
  cannot re-collect the same balance, and one submission produces exactly one
  ledger collection.
"""
from datetime import timedelta
from decimal import Decimal

from apps.accounts.models import User
from apps.core.utils import hotel_today
from apps.finance.models import FinancialTransaction
from apps.finance.services.cash_session_service import open_cash_session
from apps.pos.models import MenuCategory, MenuItem, PosOrder, PosOrderReturn, PosTender
from apps.pos.services.order_service import (
    capture_direct_tender,
    create_order,
    order_outstanding_balance,
    submit_order,
    tender_captured_amount,
    transition_order,
)
from apps.pos.services.return_service import record_unopened_return

from .base import BaseAPITestCase
from .factories import make_guest, make_booking, make_room_type, make_staff


class TenderTestBase(BaseAPITestCase):
    """A delivered takeaway order at ₦2,600 (2 × ₦1,300) plus a room-service stay."""

    def setUp(self):
        super().setUp()
        self.cashier = make_staff("tender.cashier@staff.dev", role=User.Role.CASHIER)
        self.housekeeper = make_staff("tender.housekeeper@staff.dev", role=User.Role.HOUSEKEEPER)
        self.today = hotel_today()
        self.category = MenuCategory.objects.create(
            name="Tender menu", slug="tender-menu", service_area=MenuCategory.ServiceArea.RESTAURANT
        )
        self.item = MenuItem.objects.create(
            category=self.category, name="Grilled chicken", sku="TENDER-CHICKEN", base_price=Decimal("1300.00")
        )
        room_type = make_room_type("Tender suite", price="15000.00")
        self.booking = make_booking(
            make_guest("tender-guest@example.com"), room_type,
            check_in=self.today, check_out=self.today + timedelta(days=2), total="30000.00",
        )

    # --- helpers -----------------------------------------------------------
    def _deliver(self, *, quantity=2, mode=PosOrder.Mode.TAKEAWAY, key=None):
        lines = [{"menu_item_id": self.item.pk, "quantity": quantity}]
        if mode == PosOrder.Mode.ROOM_SERVICE:
            from apps.stays.models import Stay

            stay = Stay.objects.create(
                reference=f"STY-{self.booking.booking_reference}", booking=self.booking, guest=self.booking.guest,
                status=Stay.Status.IN_HOUSE, expected_arrival=self.booking.check_in,
                expected_departure=self.booking.check_out,
            )
            order, _ = create_order(
                mode=mode, lines=lines, actor=self.cashier, stay=stay,
                idempotency_key=key or f"tender-rs-{quantity}",
            )
        else:
            order, _ = create_order(
                mode=mode, lines=lines, actor=self.cashier,
                idempotency_key=key or f"tender-{mode}-{quantity}",
            )
        submit_order(order=order, actor=self.cashier)
        order = transition_order(order=order, target_status=PosOrder.Status.DELIVERED, actor=self.cashier)
        order.refresh_from_db()
        return order

    def _pay(self, order, amount, *, method=PosTender.Method.CARD, **kwargs):
        return capture_direct_tender(order=order, method=method, amount=amount, actor=self.cashier, **kwargs)

    def _detail(self, reference):
        response = self.client.get(f"/api/admin/pos/orders/{reference}/")
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["data"]

    def _post_tender(self, reference, payload):
        return self.client.post(f"/api/admin/pos/orders/{reference}/tenders/", payload, format="json")


class OutstandingBalanceTests(TenderTestBase):
    def test_order_payload_carries_the_server_derived_balance_for_a_new_delivery(self):
        order = self._deliver(quantity=2)
        self.auth(self.cashier)
        data = self._detail(order.reference)

        self.assertEqual(data["total_amount"], "2600.00")
        self.assertEqual(data["net_total"], "2600.00")
        self.assertEqual(data["returned_amount"], "0.00")
        self.assertEqual(data["amount_paid"], "0.00")
        # The tender form's amount box is prefilled from this field.
        self.assertEqual(data["outstanding_balance"], "2600.00")
        self.assertTrue(data["settlement_action_available"])

    def test_partially_paid_order_reports_the_remaining_amount_not_the_original_total(self):
        order = self._deliver(quantity=2)
        self._pay(order, Decimal("1000.00"))
        order.refresh_from_db()

        self.assertEqual(order.settlement_status, PosOrder.SettlementStatus.PARTIALLY_PAID)
        self.assertEqual(tender_captured_amount(order), Decimal("1000.00"))
        self.assertEqual(order_outstanding_balance(order), Decimal("1600.00"))

        self.auth(self.cashier)
        data = self._detail(order.reference)
        self.assertEqual(data["total_amount"], "2600.00")
        self.assertEqual(data["amount_paid"], "1000.00")
        self.assertEqual(data["outstanding_balance"], "1600.00", "a stale screen must never re-owe the full total")
        self.assertTrue(data["settlement_action_available"])

    def test_partial_payments_are_still_supported_and_the_last_one_settles_the_order(self):
        order = self._deliver(quantity=2)
        self._pay(order, Decimal("600.00"))
        self._pay(order, Decimal("1500.00"))
        self._pay(order, Decimal("500.00"))
        order.refresh_from_db()

        self.assertEqual(tender_captured_amount(order), Decimal("2600.00"))
        self.assertEqual(order_outstanding_balance(order), Decimal("0.00"))
        self.assertEqual(order.settlement_status, PosOrder.SettlementStatus.PAID)
        self.assertEqual(PosTender.objects.filter(order=order, status=PosTender.Status.CAPTURED).count(), 3)

    def test_fully_paid_order_reports_no_outstanding_balance_and_offers_no_action(self):
        order = self._deliver(quantity=2)
        self._pay(order, Decimal("2600.00"))
        self.auth(self.cashier)
        data = self._detail(order.reference)

        self.assertEqual(data["settlement_status"], PosOrder.SettlementStatus.PAID)
        self.assertEqual(data["amount_paid"], "2600.00")
        self.assertEqual(data["outstanding_balance"], "0.00")
        self.assertFalse(data["settlement_action_available"], "a settled order must not offer another collection")

    def test_unopened_return_reduces_the_outstanding_balance_instead_of_the_frozen_total(self):
        # Returns are recorded for bar/restaurant service, so this order is a
        # dine-in order rather than a takeaway.
        order = self._deliver(quantity=2, mode=PosOrder.Mode.RESTAURANT)
        self._pay(order, Decimal("2600.00"))
        returned, created = record_unopened_return(
            order=order,
            lines=[{"order_line_id": order.lines.first().pk, "quantity_returned": 1}],
            actor=self.cashier,
            reason="Guest left one unopened portion",
            idempotency_key="tender-return-1",
            refund_method=PosOrderReturn.RefundMethod.NONE,
        )
        self.assertTrue(created)
        order.refresh_from_db()

        # The charge keeps its original figure as evidence; the amount owed falls.
        self.assertEqual(order.total_amount, Decimal("2600.00"))
        self.assertEqual(order.net_total, Decimal("1300.00"))
        self.assertEqual(order.returned_amount, Decimal("1300.00"))
        self.assertGreaterEqual(tender_captured_amount(order), order.net_total)
        self.assertEqual(order_outstanding_balance(order), Decimal("0.00"))

        self.auth(self.cashier)
        data = self._detail(order.reference)
        self.assertEqual(data["net_total"], "1300.00")
        self.assertEqual(data["returned_amount"], "1300.00")
        self.assertEqual(data["outstanding_balance"], "0.00")

    def test_room_service_orders_never_offer_a_direct_tender(self):
        order = self._deliver(quantity=1, mode=PosOrder.Mode.ROOM_SERVICE)
        self.auth(self.cashier)
        data = self._detail(order.reference)
        self.assertFalse(data["settlement_action_available"])
        with self.assertRaisesMessage(Exception, "folio"):
            self._pay(order, Decimal("1300.00"))


class TenderRejectionTests(TenderTestBase):
    def test_overpayment_is_refused_and_changes_nothing(self):
        order = self._deliver(quantity=2)
        with self.assertRaisesMessage(Exception, "Tender exceeds"):
            self._pay(order, Decimal("2600.01"))
        self.assertEqual(PosTender.objects.filter(order=order).count(), 0)
        order.refresh_from_db()
        self.assertEqual(order.settlement_status, PosOrder.SettlementStatus.UNPAID)
        self.assertEqual(order_outstanding_balance(order), Decimal("2600.00"))

    def test_overpayment_after_a_partial_payment_is_measured_against_the_remainder(self):
        order = self._deliver(quantity=2)
        self._pay(order, Decimal("1000.00"))
        with self.assertRaisesMessage(Exception, "1600.00"):
            self._pay(order, Decimal("1600.01"))
        self.assertEqual(PosTender.objects.filter(order=order, status=PosTender.Status.CAPTURED).count(), 1)

    def test_fully_paid_order_refuses_another_collection_with_a_clear_reason(self):
        order = self._deliver(quantity=2)
        self._pay(order, Decimal("2600.00"))
        with self.assertRaisesMessage(Exception, "already fully settled"):
            self._pay(order, Decimal("0.01"))

    def test_zero_and_negative_amounts_are_refused(self):
        order = self._deliver(quantity=2)
        for amount in (Decimal("0.00"), Decimal("-5.00")):
            with self.assertRaises(Exception):
                self._pay(order, amount)
        self.assertEqual(PosTender.objects.filter(order=order).count(), 0)

    def test_unknown_method_and_wrong_currency_are_refused(self):
        order = self._deliver(quantity=2)
        with self.assertRaisesMessage(Exception, "Invalid POS tender method"):
            self._pay(order, Decimal("100.00"), method="BITCOIN")
        with self.assertRaisesMessage(Exception, "denominated in NGN"):
            self._pay(order, Decimal("100.00"), currency="USD")
        self.assertEqual(PosTender.objects.filter(order=order).count(), 0)

    def test_a_stale_screen_balance_is_refused_instead_of_collected_twice(self):
        order = self._deliver(quantity=2)
        # Cashier B collects ₦1,000 while cashier A's form still shows ₦2,600.
        self._pay(order, Decimal("1000.00"))
        with self.assertRaisesMessage(Exception, "outstanding balance changed"):
            self._pay(order, Decimal("2600.00"), expected_outstanding_balance=Decimal("2600.00"))
        self.assertEqual(
            tender_captured_amount(order), Decimal("1000.00"),
            "the stale submission must not be collected on top of the first one",
        )
        # Reloading the order and collecting the fresh figure succeeds.
        self._pay(order, Decimal("1600.00"), expected_outstanding_balance=Decimal("1600.00"))
        self.assertEqual(order_outstanding_balance(order), Decimal("0.00"))

    def test_duplicate_submission_with_one_idempotency_key_collects_once(self):
        order = self._deliver(quantity=2)
        first = self._pay(order, Decimal("1000.00"), idempotency_key="tender-key-1")
        second = self._pay(order, Decimal("1000.00"), idempotency_key="tender-key-1")

        self.assertEqual(first.pk, second.pk, "the replay returns the tender that already exists")
        self.assertTrue(getattr(second, "idempotent_replay", False))
        self.assertEqual(PosTender.objects.filter(order=order).count(), 1)
        self.assertEqual(tender_captured_amount(order), Decimal("1000.00"))
        self.assertEqual(
            FinancialTransaction.objects.filter(
                type=FinancialTransaction.Type.PAYMENT_COLLECTION, source_reference=order.reference,
            ).count(),
            1,
            "a retried submission must not post a second collection to the ledger",
        )
        # A genuinely different collection still needs a different key.
        self._pay(order, Decimal("1000.00"), idempotency_key="tender-key-2")
        self.assertEqual(tender_captured_amount(order), Decimal("2000.00"))

    def test_cash_tender_requires_the_cashier_s_own_open_session(self):
        order = self._deliver(quantity=2)
        with self.assertRaisesMessage(Exception, "open cashier session"):
            self._pay(order, Decimal("500.00"), method=PosTender.Method.CASH)
        other = make_staff("tender.other-cashier@staff.dev", role=User.Role.CASHIER)
        other_session = open_cash_session(cashier=other, actor=other, opening_float=Decimal("10000.00"))
        with self.assertRaisesMessage(Exception, "acting cashier's open session"):
            self._pay(order, Decimal("500.00"), method=PosTender.Method.CASH, cash_session=other_session)

        own_session = open_cash_session(cashier=self.cashier, actor=self.cashier, opening_float=Decimal("10000.00"))
        tender = self._pay(order, Decimal("500.00"), method=PosTender.Method.CASH, cash_session=own_session)
        self.assertEqual(tender.cash_session_id, own_session.pk)
        own_session.refresh_from_db()
        self.assertEqual(own_session.expected_cash, Decimal("10500.00"))


class TenderApiTests(TenderTestBase):
    def test_tender_endpoint_prefills_from_and_settles_the_outstanding_balance(self):
        order = self._deliver(quantity=2)
        self.auth(self.cashier)

        partial = self._post_tender(order.reference, {
            "method": "CARD", "amount": "1000.00", "currency": "NGN",
            "expected_outstanding_balance": "2600.00", "idempotency_key": "api-tender-1",
        })
        self.assertEqual(partial.status_code, 201, partial.content)
        self.assertEqual(partial.json()["data"]["outstanding_balance"], "1600.00")
        self.assertEqual(partial.json()["data"]["settlement_status"], PosOrder.SettlementStatus.PARTIALLY_PAID)

        remainder = self._post_tender(order.reference, {
            "method": "CARD", "amount": "1600.00", "currency": "NGN",
            "expected_outstanding_balance": "1600.00", "idempotency_key": "api-tender-2",
        })
        self.assertEqual(remainder.status_code, 201, remainder.content)
        self.assertEqual(remainder.json()["data"]["outstanding_balance"], "0.00")
        self.assertEqual(remainder.json()["data"]["settlement_status"], PosOrder.SettlementStatus.PAID)

    def test_repeated_http_submission_answers_200_and_does_not_collect_twice(self):
        order = self._deliver(quantity=2)
        self.auth(self.cashier)
        payload = {"method": "CARD", "amount": "1000.00", "idempotency_key": "api-retry-key"}

        first = self._post_tender(order.reference, payload)
        second = self._post_tender(order.reference, payload)
        self.assertEqual(first.status_code, 201, first.content)
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(PosTender.objects.filter(order=order).count(), 1)
        self.assertEqual(self._detail(order.reference)["amount_paid"], "1000.00")

    def test_paid_order_returns_a_clear_400_not_an_internal_error(self):
        order = self._deliver(quantity=2)
        self.auth(self.cashier)
        self.assertEqual(self._post_tender(order.reference, {"method": "CARD", "amount": "2600.00"}).status_code, 201)
        again = self._post_tender(order.reference, {"method": "CARD", "amount": "1.00"})
        self.assertEqual(again.status_code, 400, again.content)
        body = again.json()
        self.assertFalse(body["success"])
        self.assertIn("fully settled", str(body))

    def test_overpayment_and_wrong_currency_are_rejected_by_the_api(self):
        order = self._deliver(quantity=2)
        self.auth(self.cashier)
        over = self._post_tender(order.reference, {"method": "CARD", "amount": "3000.00"})
        self.assertEqual(over.status_code, 400, over.content)
        currency = self._post_tender(order.reference, {"method": "CARD", "amount": "100.00", "currency": "USD"})
        self.assertEqual(currency.status_code, 400, currency.content)
        self.assertEqual(PosTender.objects.filter(order=order).count(), 0)

    def test_cash_tender_without_a_session_reference_is_a_validation_error(self):
        order = self._deliver(quantity=2)
        self.auth(self.cashier)
        response = self._post_tender(order.reference, {"method": "CASH", "amount": "100.00"})
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("cash_session_reference", str(response.json()))

    def test_staff_without_the_payment_capability_cannot_collect(self):
        order = self._deliver(quantity=2)
        self.auth(self.housekeeper)
        response = self._post_tender(order.reference, {"method": "CARD", "amount": "100.00"})
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(PosTender.objects.filter(order=order).count(), 0)

    def test_undelivered_order_cannot_be_collected(self):
        order, _ = create_order(
            mode=PosOrder.Mode.TAKEAWAY, lines=[{"menu_item_id": self.item.pk, "quantity": 1}],
            actor=self.cashier, idempotency_key="tender-undelivered",
        )
        self.auth(self.cashier)
        response = self._post_tender(order.reference, {"method": "CARD", "amount": "100.00"})
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("delivered", str(response.json()).lower())
