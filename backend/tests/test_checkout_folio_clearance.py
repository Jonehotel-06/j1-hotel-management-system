"""Checkout folio / gate-clearance summary (GET admin/bookings/<ref>/folio-clearance/).

The checkout desk must see everything the guest consumed (food, drinks,
laundry and other folio charges), what is still owed, and what has not been
billed yet — before printing a gate pass. Itemisation must come from the
immutable folio postings and totals must agree with the audit-D4 helpers.
"""
from datetime import timedelta

from apps.accounts.models import User
from apps.bookings.models import Booking
from apps.core.utils import hotel_today
from apps.finance.models import CashSession, FinancialTransaction, Folio, PaymentAllocation
from apps.finance.services import accounting
from apps.finance.services.cash_session_service import open_cash_session
from apps.finance.services.ledger_service import create_posted_transaction
from apps.guest_services.models import LaundryOrder, ServiceRequest
from apps.payments.models import Payment
from apps.stays.models import Stay

from .base import BaseAPITestCase
from .factories import make_booking, make_guest, make_room, make_room_type, make_staff


class FolioClearanceTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.receptionist = make_staff("clear-desk@staff.dev", role=User.Role.RECEPTIONIST)
        self.manager = make_staff("clear-manager@staff.dev", role=User.Role.MANAGER)
        self.housekeeper = make_staff("clear-hk@staff.dev", role=User.Role.HOUSEKEEPER)
        self.today = hotel_today()
        room_type = make_room_type("Clearance suite", price="20000.00")
        room = make_room(room_type, "801")
        # Room fully paid at booking level: only folio charges can block clearance.
        self.booking = make_booking(
            make_guest("clearance-guest@example.com"), room_type, rooms=[room],
            check_in=self.today, check_out=self.today + timedelta(days=2),
            total="40000.00", amount_paid="40000.00",
            status="CHECKED_IN",
        )
        self.stay = Stay.objects.create(
            reference=f"STY-{self.booking.booking_reference}", booking=self.booking,
            guest=self.booking.guest, status=Stay.Status.IN_HOUSE,
            expected_arrival=self.booking.check_in, expected_departure=self.booking.check_out,
        )
        self.url = f"/api/admin/bookings/{self.booking.booking_reference}/folio-clearance/"

    def _laundry(self, key="clear-laundry-1"):
        self.auth(self.manager)
        created = self.client.post("/api/admin/service-requests/laundry/", {
            "stay_reference": self.stay.reference,
            "items": [{"description": "Shirt", "quantity": 2, "service_type": "WASH_IRON", "unit_price": "2000.00"}],
            "idempotency_key": key,
        }, format="json")
        assert created.status_code == 201, created.content
        return created.json()["data"]["reference"]

    def _get(self):
        self.auth(self.receptionist)
        response = self.client.get(self.url)
        assert response.status_code == 200, response.content
        return response.json()["data"]

    def test_unbilled_then_billed_laundry_blocks_clearance_until_paid(self):
        # Nothing consumed yet: cleared, nothing unbilled.
        data = self._get()
        self.assertTrue(data["cleared"])
        self.assertEqual(data["charges"], [])
        self.assertEqual(data["unbilled_laundry"], [])

        # Laundry received but NOT billed: visible as a warning, not yet a debt.
        reference = self._laundry()
        data = self._get()
        self.assertEqual(len(data["unbilled_laundry"]), 1)
        self.assertEqual(data["unbilled_laundry"][0]["total_amount"], "4000.00")
        self.assertTrue(data["cleared"])

        # Billed to the folio: now it is a real charge and blocks clearance.
        self.auth(self.manager)
        billed = self.client.post(f"/api/admin/service-requests/laundry/{reference}/bill/", {}, format="json")
        self.assertEqual(billed.status_code, 200, billed.content)
        data = self._get()
        self.assertEqual(data["unbilled_laundry"], [])
        self.assertEqual(len(data["charges"]), 1)
        self.assertEqual(data["charges_total"], "4000.00")
        self.assertEqual(data["folio_balance"], "4000.00")
        self.assertEqual(data["outstanding_balance"], "4000.00")
        self.assertFalse(data["cleared"])
        # The room itself was already settled at booking level.
        self.assertEqual(data["accommodation"]["amount_due"], "0.00")

        # Guest pays the folio at the desk: a PAYMENT credit posting settles it.
        folio = Folio.objects.get(stay=self.stay, kind=Folio.Kind.MAIN)
        create_posted_transaction(
            transaction_type=FinancialTransaction.Type.PAYMENT_COLLECTION,
            source_key="clearance-test:folio-payment", actor=self.manager,
            business_date=self.today, narrative="Guest settled folio at checkout desk",
            lines=[
                {"account_code": accounting.CASH_ON_HAND, "direction": "DEBIT", "amount": "4000.00"},
                {"account_code": accounting.ACCOUNTS_RECEIVABLE, "direction": "CREDIT", "amount": "4000.00", "folio": folio},
            ],
            postings=[{
                "line_index": 1, "folio": folio, "kind": "PAYMENT", "effect": "CREDIT",
                "amount": "4000.00", "description": "Cash payment at desk",
            }],
        )
        data = self._get()
        self.assertEqual(data["folio_balance"], "0.00")
        self.assertEqual(data["outstanding_balance"], "0.00")
        self.assertTrue(data["cleared"])
        self.assertEqual(data["credits_total"], "4000.00")

    def test_open_guest_requests_are_listed_until_resolved(self):
        ServiceRequest.objects.create(
            reference="SR-CLEAR-1", stay=self.stay, guest=self.booking.guest,
            category=ServiceRequest.Category.FOOD_BEVERAGE, channel=ServiceRequest.Channel.FRONT_DESK,
            status=ServiceRequest.Status.OPEN, summary="Club sandwich to room 801",
        )
        data = self._get()
        self.assertEqual(len(data["open_requests"]), 1)
        self.assertEqual(data["open_requests"][0]["summary"], "Club sandwich to room 801")

    # ------------------------------------------------------------------
    # Statement of the bill + staff-confirmed payment (issue: money owed for
    # a guest's own requests could not be collected at the desk at all)
    # ------------------------------------------------------------------
    def _settle(self, payload, actor=None):
        self.auth(actor or self.receptionist)
        return self.client.post(
            f"/api/admin/bookings/{self.booking.booking_reference}/settle-balance/",
            payload, format="json",
        )

    def test_the_bill_states_unpaid_requests_and_blocks_the_gate_pass(self):
        reference = self._laundry(key="clear-bill-1")   # received, NOT billed

        data = self._get()
        # The unbilled request is money, not a footnote: it is stated...
        self.assertEqual(data["unpaid_requests"], [{
            "reference": reference, "kind": "LAUNDRY",
            "description": f"Laundry {reference}", "status": LaundryOrder.Status.RECEIVED,
            "amount": "4000.00", "billed": False, "action": "BILL_AND_SETTLE",
        }])
        self.assertEqual(data["unpaid_requests_total"], "4000.00")
        self.assertEqual(data["amount_to_settle"], "4000.00")
        # ...and it blocks the gate pass, even though nothing is on the folio.
        self.assertEqual(data["folio_balance"], "0.00")
        self.assertFalse(data["gate_pass_allowed"])
        codes = [blocker["code"] for blocker in data["gate_pass_blockers"]]
        self.assertEqual(codes, ["UNPAID_REQUESTS"])
        self.assertEqual(data["gate_pass_blockers"][0]["amount"], "4000.00")
        # The desk can take that money.
        self.assertTrue(data["can_settle"])
        self.assertEqual(data["settle_blocked_reason"], "")

    def test_settling_takes_the_money_and_clears_the_folio_in_one_step(self):
        reference = self._laundry(key="clear-bill-2")

        response = self._settle({"amount": "4000.00", "provider": "CASH",
                                 "notes": "Settled at check-out"})
        self.assertEqual(response.status_code, 201, response.content)
        payload = response.json()["data"]

        # The money is attributed to the staff member who received and recorded it.
        payment = Payment.objects.get(reference=payload["receipt_reference"])
        self.assertEqual(payment.booking_id, self.booking.pk)
        self.assertEqual(str(payment.amount), "4000.00")
        self.assertEqual(payment.provider, Payment.Provider.CASH)
        self.assertEqual(payment.user, self.receptionist)
        self.assertEqual(payment.metadata["recorded_by"], self.receptionist.email)
        self.assertEqual(payload["recorded_by"], self.receptionist.email)

        # Unbilled request billed once, in the same transaction as the money.
        self.assertEqual(payload["settlement"]["billed_requests"], [reference])
        self.assertEqual(payload["settlement"]["folio_portion"], "4000.00")
        self.assertEqual(payload["settlement"]["stay_portion"], "0.00")
        self.assertEqual(payment.metadata["bill_before_payment"], "4000.00")
        self.assertEqual(payment.metadata["folio_balance_before_payment"], "4000.00")
        self.assertEqual(payment.metadata["stay_balance_before_payment"], "0.00")

        order = LaundryOrder.objects.get(reference=reference)
        self.assertIsNotNone(order.financial_transaction_id)

        # The response carries the refreshed folio read: the money shows there
        # immediately, and the gate pass is allowed from the same transaction.
        folio = payload["folio"]
        self.assertEqual(folio["outstanding_balance"], "0.00")
        self.assertEqual(folio["amount_to_settle"], "0.00")
        self.assertTrue(folio["cleared"])
        self.assertTrue(folio["gate_pass_allowed"])
        self.assertEqual(folio["gate_pass_blockers"], [])
        self.assertEqual(folio["unpaid_requests"], [])
        self.assertEqual(folio["credits_total"], "4000.00")
        self.assertFalse(folio["can_settle"])

        # A real ledger application exists, not just a projection change.
        self.assertTrue(PaymentAllocation.objects.filter(payment=payment).exists())
        self.assertEqual(
            PaymentAllocation.objects.filter(payment=payment).first().folio.booking_id,
            self.booking.pk,
        )
        # And the folio really is empty on a fresh read (no stale snapshot).
        self.assertEqual(self._get()["outstanding_balance"], "0.00")

    def test_the_stay_part_of_the_bill_is_still_credited_to_the_booking(self):
        room_type = make_room_type("Clearance suite twin", price="20000.00")
        booking = make_booking(
            make_guest("clearance-mixed@example.com"), room_type,
            rooms=[make_room(room_type, "802")],
            check_in=self.today, check_out=self.today + timedelta(days=2),
            total="40000.00", amount_paid="20000.00", status="CHECKED_IN",
        )
        Stay.objects.create(
            reference=f"STY-{booking.booking_reference}", booking=booking,
            guest=booking.guest, status=Stay.Status.IN_HOUSE,
            expected_arrival=booking.check_in, expected_departure=booking.check_out,
        )
        self.auth(self.manager)
        created = self.client.post("/api/admin/service-requests/laundry/", {
            "stay_reference": f"STY-{booking.booking_reference}",
            "items": [{"description": "Suit", "quantity": 2, "service_type": "WASH_IRON",
                       "unit_price": "1000.00"}],
            "idempotency_key": "clear-bill-mixed-1",
        }, format="json")
        self.assertEqual(created.status_code, 201, created.content)

        self.auth(self.receptionist)
        response = self.client.post(
            f"/api/admin/bookings/{booking.booking_reference}/settle-balance/",
            {"amount": "22000.00", "provider": "BANK_TRANSFER"}, format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()["data"]
        self.assertEqual(data["settlement"]["folio_portion"], "2000.00")
        self.assertEqual(data["settlement"]["stay_portion"], "20000.00")
        booking.refresh_from_db()
        self.assertEqual(str(booking.amount_paid), "40000.00")
        self.assertEqual(booking.payment_status, Booking.PaymentStatus.PAID)
        self.assertEqual(data["folio"]["outstanding_balance"], "0.00")
        self.assertTrue(data["folio"]["gate_pass_allowed"])

    def test_a_partial_payment_leaves_the_rest_stated_and_the_gate_pass_blocked(self):
        self._laundry(key="clear-bill-partial")
        response = self._settle({"amount": "1500.00", "provider": "POS"})
        self.assertEqual(response.status_code, 201, response.content)
        folio = response.json()["data"]["folio"]
        self.assertEqual(folio["outstanding_balance"], "2500.00")
        self.assertEqual(folio["amount_to_settle"], "2500.00")
        self.assertFalse(folio["cleared"])
        self.assertFalse(folio["gate_pass_allowed"])
        self.assertTrue(folio["can_settle"])   # the desk can still take the rest

    def test_the_desk_cannot_collect_more_than_the_bill(self):
        self._laundry(key="clear-bill-over")
        response = self._settle({"amount": "9000.00", "provider": "CASH"})
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("exceeds the bill", str(response.json()["errors"]))
        self.assertEqual(Payment.objects.filter(booking=self.booking).count(), 0)

    def test_nothing_outstanding_cannot_be_settled_twice(self):
        self._laundry(key="clear-bill-twice")
        first = self._settle({"amount": "4000.00", "provider": "CASH"})
        self.assertEqual(first.status_code, 201, first.content)
        again = self._settle({"amount": "4000.00", "provider": "CASH"})
        self.assertEqual(again.status_code, 409, again.content)
        self.assertEqual(Payment.objects.filter(booking=self.booking).count(), 1)

    def test_settling_requires_booking_management_and_payment_capture(self):
        self._laundry(key="clear-bill-rbac")
        response = self._settle({"amount": "4000.00", "provider": "CASH"}, actor=self.housekeeper)
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(Payment.objects.filter(booking=self.booking).count(), 0)
        # The same money cannot be collected without the money permission.
        self.unauth()
        self.assertIn(
            self.client.post(
                f"/api/admin/bookings/{self.booking.booking_reference}/settle-balance/",
                {"amount": "4000.00", "provider": "CASH"}, format="json",
            ).status_code,
            (401, 403),
        )

    def test_a_cash_settlement_needs_the_acting_cashiers_own_open_drawer(self):
        self._laundry(key="clear-bill-cash")
        # Someone else's drawer is never used to hold this cashier's cash.
        other = make_staff("clear-other-cashier@staff.dev", role=User.Role.RECEPTIONIST)
        foreign = open_cash_session(cashier=other, opening_float="0.00")
        response = self._settle({
            "amount": "4000.00", "provider": "CASH",
            "cash_session_reference": foreign.reference,
        })
        self.assertEqual(response.status_code, 403, response.content)
        # A closed drawer is not a drawer.
        session = open_cash_session(cashier=self.receptionist, opening_float="0.00")
        CashSession.objects.filter(pk=session.pk).update(status=CashSession.Status.CLOSED)
        response = self._settle({
            "amount": "4000.00", "provider": "CASH",
            "cash_session_reference": session.reference,
        })
        self.assertEqual(response.status_code, 400, response.content)
        # The acting cashier's own open drawer is accepted and recorded.
        own = open_cash_session(cashier=self.receptionist, opening_float="0.00")
        response = self._settle({
            "amount": "4000.00", "provider": "CASH",
            "cash_session_reference": own.reference,
        })
        self.assertEqual(response.status_code, 201, response.content)
        payment = Payment.objects.get(reference=response.json()["data"]["receipt_reference"])
        self.assertEqual(payment.metadata["cash_session_reference"], own.reference)

    def test_an_open_guest_request_blocks_the_gate_pass_until_it_is_closed(self):
        request = ServiceRequest.objects.create(
            reference="SR-CLEAR-BILL-1", stay=self.stay, guest=self.booking.guest,
            category=ServiceRequest.Category.ROOM_SERVICE,
            channel=ServiceRequest.Channel.PORTAL,
            status=ServiceRequest.Status.OPEN, summary="Towels and a bucket of ice",
        )
        data = self._get()
        self.assertFalse(data["gate_pass_allowed"])
        self.assertEqual([b["code"] for b in data["gate_pass_blockers"]], ["OPEN_REQUESTS"])
        self.assertEqual(data["gate_pass_blockers"][0]["count"], 1)
        # Nothing is owed, so there is no money action to offer — only the close.
        self.assertEqual(data["amount_to_settle"], "0.00")
        self.assertFalse(data["can_settle"])
        self.assertIn("Nothing is outstanding", data["settle_blocked_reason"])

        request.status = ServiceRequest.Status.CLOSED
        request.save(update_fields=["status"])
        data = self._get()
        self.assertTrue(data["gate_pass_allowed"])
        self.assertEqual(data["gate_pass_blockers"], [])

    def test_a_checked_out_booking_cannot_take_a_desk_payment(self):
        self._laundry(key="clear-bill-after-checkout")
        self.booking.status = Booking.Status.CHECKED_OUT
        self.booking.save(update_fields=["status"])
        data = self._get()
        self.assertFalse(data["can_settle"])
        self.assertIn("cannot take a desk payment", data["settle_blocked_reason"])

    def test_clearance_requires_the_folio_view_capability(self):
        self.auth(self.housekeeper)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 403)
        # And unauthenticated callers are rejected outright.
        self.unauth()
        response = self.client.get(self.url)
        self.assertIn(response.status_code, (401, 403))
