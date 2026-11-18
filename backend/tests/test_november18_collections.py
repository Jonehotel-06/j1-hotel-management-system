"""Acceptance: reconcile existing collections, not invoices or allocations twice."""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from apps.accounts.models import User
from apps.bookings.services.booking_service import check_in_booking
from apps.core.utils import hotel_today
from apps.finance.models import FinancialLine, FinancialTransaction, FolioPosting
from apps.finance.services.approval_service import review_approval
from apps.finance.services.ledger_service import create_posted_transaction
from apps.finance.services.payment_collection_service import ensure_processed_refund, settle_outstanding_balance
from apps.guest_services.models import MiniMartSale
from apps.guest_services.services.laundry_service import (
    advance_laundry_order, create_laundry_order, settle_walk_in_laundry,
)
from apps.payments.models import Payment, Refund
from apps.payments.services.payment_service import record_offline_payment
from apps.pos.models import MenuCategory, MenuItem, PosOrder, PosTender
from apps.pos.services.order_service import capture_direct_tender, create_order, submit_order, transition_order
from apps.pos.services.return_service import confirm_return_refund, record_unopened_return
from tests.base import BaseAPITestCase
from tests.factories import make_booking, make_guest, make_room, make_room_type, make_staff

from .test_events_reception import EVENTS, EventFixtures
from .test_reception_minimart_repair import MART, RetailFixtures

URL = "/api/admin/payments/"


class CentralCollectionsTests(EventFixtures, RetailFixtures, BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.event_setup()
        self.retail_setup()
        self.actor = make_staff("reconcile.admin@example.test", role=User.Role.ADMIN)
        self.auth(self.actor)
        self.today = hotel_today()
        self.rt = make_room_type()
        self.room = make_room(self.rt, "730")
        self.booking = make_booking(make_guest(), self.rt, rooms=[self.room],
                                    check_in=self.today, check_out=self.today + timedelta(days=2))
        self.food = self.menu("Food", MenuCategory.ServiceArea.RESTAURANT)
        self.drink = self.menu("Beverage", MenuCategory.ServiceArea.BAR)

    def menu(self, name, area):
        category = MenuCategory.objects.create(name=name, slug=name.lower(), service_area=area)
        return MenuItem.objects.create(name=name, category=category, sku=f"NOV-{name}", base_price=Decimal("1300.00"))

    def deliver(self, mode, *, item=None, quantity=1, stay=None):
        order, _ = create_order(mode=mode, actor=self.actor, stay=stay,
                                lines=[{"menu_item_id": (item or self.food).pk, "quantity": quantity}])
        submit_order(order=order, actor=self.actor)
        return transition_order(order=order, target_status=PosOrder.Status.DELIVERED, actor=self.actor)

    def take(self, order, amount, ref):
        return capture_direct_tender(order=order, method=PosTender.Method.CARD, amount=Decimal(amount),
                                     actor=self.actor, external_reference=ref)

    def laundry(self, *, stay=None):
        order, _ = create_laundry_order(actor=self.actor, stay=stay, customer_name="Test Walk-In",
                                       items=[{"description": "Shirt", "quantity": 2, "unit_price": "500"}])
        if stay:
            for state in ("IN_PROGRESS", "READY", "DELIVERED"):
                order = advance_laundry_order(order=order, actor=self.actor, new_status=state)
        return order

    def feed(self, **params):
        response = self.client.get(URL, {"view": "collections", **params})
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def rows(self, **params):
        return self.feed(**params)["data"]["results"]

    def mart_sale(self, *, room_charge=False):
        self.auth(self.staff)
        data = dict(self.payload, payment_method="CASH")
        if room_charge:
            data.update(payment_method="ROOM_CHARGE", guest_id=self.booking.guest_id, booking_id=self.booking.pk)
            data.pop("cash_session_reference")
        response = self.client.post(MART + "checkout/", data, format="json")
        self.auth(self.actor)
        self.assertEqual(response.status_code, 200, response.content)
        return MiniMartSale.objects.select_related("order").get()

    def test_named_collected_sources_split_tenders_and_no_read_side_effects(self):
        check_in_booking(self.booking, staff_user=self.actor)
        payment = record_offline_payment(booking=self.booking, staff_user=self.actor, amount=Decimal("1000"), provider="CASH")
        restaurant = self.deliver(PosOrder.Mode.RESTAURANT, quantity=2)
        first = self.take(restaurant, "400", "NOV-RESTAURANT-A")
        self.take(restaurant, "600", "NOV-RESTAURANT-B")
        self.take(self.deliver(PosOrder.Mode.BAR, item=self.drink), "300", "NOV-BAR")
        self.take(self.deliver(PosOrder.Mode.TAKEAWAY), "400", "NOV-FOOD")
        self.take(self.deliver(PosOrder.Mode.TAKEAWAY, item=self.drink), "300", "NOV-DRINKS")
        self.mart_sale()
        e = self.client.post(EVENTS, EventFixtures.payload(self, amount_paid="800.00", payment_method="CASH",
                                                payment_idempotency_key="event-nov18"), format="json")
        self.assertEqual(e.status_code, 201, e.content)
        settle_walk_in_laundry(order=self.laundry(), actor=self.actor, method="BANK_TRANSFER", external_reference="NOV-LDY")
        # A pending gateway attempt and a delivered but uncollected order do not add cash.
        Payment.objects.create(booking=self.booking, reference="NOV-ATTEMPT", provider="PAYSTACK",
                               status=Payment.Status.PENDING, amount=Decimal("1000"))
        self.deliver(PosOrder.Mode.TAKEAWAY)
        count_models = (Payment, Refund, PosTender, FinancialTransaction, FinancialLine, FolioPosting)
        before = [model.objects.count() for model in count_models]
        feed = self.feed()
        rows = feed["data"]["results"]
        self.assertEqual(len(rows), 10)
        self.assertEqual({r["source"] for r in rows if r["entry_type"] == "COLLECTION"},
                         {"Accommodation", "Restaurant", "Bar", "Food", "Drinks", "Mini Mart", "Events", "Laundry"})
        self.assertEqual(feed["data"]["totals"], [{"currency": "NGN", "gross": "5100.00", "refunds": "0.00",
                                                 "reversals": "0.00", "net": "5100.00"}])
        native = next(row for row in rows if row["reference"] == payment.reference)
        self.assertEqual(native["room_numbers"], ["730"])
        self.assertEqual(native["guest_name"], self.booking.guest.full_name)
        split = next(row for row in rows if row["reference"] == first.reference)
        self.assertEqual(split["billed_amount"], "2600.00")
        self.assertEqual(split["outstanding_amount"], "1600.00")
        self.assertEqual(split["staff_name"], self.actor.full_name)
        self.assertEqual(self.rows(search=first.reference)[0]["id"], split["id"])
        self.assertEqual(self.rows(search="NOV-RESTAURANT-A")[0]["id"], split["id"])
        self.assertEqual({r["method"] for r in self.rows(provider="POS")}, {"CARD"})
        self.assertEqual(len(self.rows(date=self.today.isoformat())), 10)
        self.assertEqual(self.rows(date=(self.today - timedelta(days=1)).isoformat()), [])
        for params in ({"date_from": "broken"}, {"date_to": "9999-12-31"},
                       {"date_from": "2026-11-19", "date_to": "2026-11-18"}):
            self.assertEqual(self.client.get(URL, {"view": "collections", **params}).status_code, 400)
        pages = [self.feed(page=i, page_size=3) for i in range(1, 5)]
        self.assertEqual(len({row["id"] for page in pages for row in page["data"]["results"]}), 10)
        self.assertTrue(all(page["data"]["totals"] == feed["data"]["totals"] for page in pages))
        self.assertEqual([model.objects.count() for model in count_models], before)

    def test_permissions_native_compatibility_and_verified_legacy_payment(self):
        Payment.objects.create(booking=self.booking, reference="LEGACY-VERIFIED", provider="PAYSTACK",
                               currency="USD", status=Payment.Status.SUCCESS, amount=Decimal("14.31"), paid_at=timezone.now())
        self.assertEqual(self.feed()["data"]["totals"][0]["net"], "14.31")
        self.assertEqual(self.rows()[0]["reference"], "LEGACY-VERIFIED")
        original = self.client.get(URL)
        self.assertEqual(original.status_code, 200, original.content)
        self.assertIsInstance(original.json()["data"], list)
        self.auth(self.housekeeping)
        self.assertEqual(self.client.get(URL, {"view": "collections"}).status_code, 403)

    def test_paid_refund_void_and_bill_only_return_are_not_conflated(self):
        payment = record_offline_payment(booking=self.booking, staff_user=self.actor, amount=Decimal("1000"), provider="POS")
        original = FinancialTransaction.objects.get(source_key=f"payment-collection:{payment.reference}")
        void, _ = create_posted_transaction(
            transaction_type=FinancialTransaction.Type.VOID, source_key="test:reversal", actor=self.actor,
            source_reference=payment.reference, reversal_of=original, currency="NGN",
            lines=[{"account_code": line.account_code, "amount": line.amount,
                    "direction": "CREDIT" if line.direction == "DEBIT" else "DEBIT"} for line in original.lines.all()],
        )
        refund_payment = record_offline_payment(booking=self.booking, staff_user=self.actor, amount=Decimal("1000"), provider="CASH")
        refund = Refund.objects.create(payment=refund_payment, booking=self.booking, amount=Decimal("400"), status="PROCESSED",
                                      requested_by=self.actor, processed_at=timezone.now(), paystack_refund_reference="TEST-REFUND-400")
        ensure_processed_refund(refund=refund, actor=self.actor)
        paid_order = self.deliver(PosOrder.Mode.RESTAURANT, quantity=2)
        self.take(paid_order, "2600", "NOV-POS-REFUND")
        returned, _ = record_unopened_return(order=paid_order, actor=self.actor, refund_method="CARD",
                               lines=[{"order_line_id": paid_order.lines.first().pk, "quantity": 1}],
                               reason="Unopened item returned", idempotency_key="nov18-pos-refund")
        self.assertEqual(self.feed()["data"]["totals"][0]["refunds"], "400.00", "awaiting confirmation is not a refund")
        if returned.approval_id:
            review_approval(approval=returned.approval, reviewer=self.manager, approved=True, review_note="Approved test refund")
        confirm_return_refund(record=returned, actor=self.actor, refund_method="CARD", provider_reference="BANK-PAID-REFUND")
        unpaid = self.deliver(PosOrder.Mode.RESTAURANT, quantity=2)
        record_unopened_return(order=unpaid, actor=self.actor, refund_method="NONE",
                               lines=[{"order_line_id": unpaid.lines.first().pk, "quantity": 1}],
                               reason="Unopened item returned", idempotency_key="nov18-pos-credit")
        report = self.feed()
        totals = report["data"]["totals"][0]
        self.assertEqual((totals["gross"], totals["refunds"], totals["reversals"], totals["net"]),
                         ("4600.00", "1700.00", "-1000.00", "1900.00"))
        self.assertEqual({row["reference"] for row in self.rows(status="REVERSED")}, {payment.reference, void.reference})
        self.assertEqual(len(self.rows(status="PROCESSED")), 2)
        self.assertFalse(any(r.get("operation_reference") == unpaid.reference for r in report["data"]["results"]))

    def folio_extras(self):
        check_in_booking(self.booking, staff_user=self.actor)
        stay = self.booking.stay
        order = self.deliver(PosOrder.Mode.ROOM_SERVICE, stay=stay)
        laundry = self.laundry(stay=stay)
        sale = self.mart_sale(room_charge=True)
        self.assertTrue(laundry.financial_transaction_id or type(laundry).objects.get(pk=laundry.pk).financial_transaction_id)
        self.assertFalse(self.rows(), "charges without a tender are not payments")
        return order.net_total, laundry.total_amount, sale.order.total_amount

    def settle(self, amount, key):
        return settle_outstanding_balance(booking=self.booking, actor=self.actor, amount=amount, provider="BANK_TRANSFER",
                                          external_reference=f"bank-{key}", idempotency_key=key)

    def test_mixed_partial_is_collected_once_without_guessing_the_split(self):
        self.folio_extras()
        payment, _ = self.settle(Decimal("500"), "mix-partial")
        [row] = self.rows()
        self.assertEqual(row["reference"], payment.reference)
        self.assertEqual(row["source_breakdown"], [{"source": "Guest Folio", "amount": "500.00"}])

    def test_whole_folio_attributes_proven_extras_and_rooms_on_one_collection(self):
        rs, laundry, mart = self.folio_extras()
        expected = {"Room Service": rs, "Laundry": laundry, "Mini Mart": mart, "Accommodation": self.booking.total_amount}
        total = sum(expected.values())
        payment, _ = self.settle(total, "whole-bill")
        retry, _ = self.settle(total, "whole-bill")
        self.assertEqual(payment.pk, retry.pk)
        [row] = self.rows()
        self.assertEqual(row["signed_amount"], f"{total:.2f}")
        self.assertEqual({p["source"]: Decimal(p["amount"]) for p in row["source_breakdown"]}, expected)
