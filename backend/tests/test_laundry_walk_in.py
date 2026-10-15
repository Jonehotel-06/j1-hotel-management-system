"""Walk-in laundry: laundry is not limited to guests who booked a room.

A non-resident leaves laundry at reception, is identified by their own name,
and pays at the desk. These tests pin the four things that make that safe:
the order is created without a stay, the customer is always identifiable, the
money posts exactly once through the ledger, and cash lands in the acting
cashier's own drawer so the count reconciles.
"""
from decimal import Decimal

from django.db.models import Sum

from apps.accounts.models import User
from apps.core.utils import hotel_today
from apps.finance.models import CashMovement, CashSession, FinancialLine, FinancialTransaction
from apps.finance.services import accounting
from apps.finance.services.cash_session_service import open_cash_session
from apps.guest_services.models import LaundryOrder

from .base import BaseAPITestCase
from .factories import make_staff

LAUNDRY_URL = "/api/admin/service-requests/laundry/"


class WalkInLaundryTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.cashier = make_staff("laundry-cashier@staff.dev", role=User.Role.RECEPTIONIST)
        self.manager = make_staff("laundry-walkin-manager@staff.dev", role=User.Role.MANAGER)

    def _receive(self, *, key="walkin-laundry-1", name="Chinedu Okeke", phone="08030001111",
                 items=None, actor=None):
        self.auth(actor or self.manager)
        payload = {
            "items": items or [
                {"description": "Shirt", "quantity": 2, "service_type": "WASH", "unit_price": "500.00"},
                {"description": "Trouser", "quantity": 1, "service_type": "PRESS", "unit_price": "1000.00"},
            ],
            "idempotency_key": key,
        }
        if name:
            payload["customer_name"] = name
        if phone:
            payload["customer_phone"] = phone
        return self.client.post(LAUNDRY_URL, payload, format="json")

    # --- Receiving --------------------------------------------------------

    def test_a_walk_in_customer_can_leave_laundry_without_any_booking(self):
        response = self._receive()
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()["data"]
        self.assertTrue(data["is_walk_in"])
        self.assertEqual(data["guest_name"], "Chinedu Okeke")
        self.assertEqual(data["guest_phone"], "08030001111")
        self.assertEqual(data["stay_reference"], None)
        self.assertEqual(data["total_amount"], "2000.00")
        self.assertEqual(data["status"], "RECEIVED")
        # Nothing is owed to a folio and nothing has been paid yet.
        self.assertIsNone(data["folio_reference"])
        self.assertIsNone(data["financial_reference"])
        self.assertFalse(data["settled"])

        order = LaundryOrder.objects.get(reference=data["reference"])
        self.assertIsNone(order.stay_id)
        self.assertIsNone(order.guest_id)

    def test_receiving_without_a_stay_or_a_name_is_refused_with_a_usable_message(self):
        self.auth(self.manager)
        response = self.client.post(LAUNDRY_URL, {
            "items": [{"description": "Shirt", "quantity": 1, "unit_price": "500.00"}],
            "idempotency_key": "walkin-nobody",
        }, format="json")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("stay_reference", response.json()["errors"])

    def test_a_walk_in_receive_is_idempotent(self):
        first = self._receive(key="walkin-idem").json()["data"]
        again = self._receive(key="walkin-idem").json()["data"]
        self.assertEqual(first["reference"], again["reference"])
        self.assertEqual(LaundryOrder.objects.count(), 1)

    def test_laundry_processing_works_the_same_for_a_walk_in(self):
        reference = self._receive(key="walkin-flow").json()["data"]["reference"]
        self.auth(self.manager)
        for target in ("IN_PROGRESS", "READY"):
            moved = self.client.post(f"{LAUNDRY_URL}{reference}/status/", {"status": target}, format="json")
            self.assertEqual(moved.status_code, 200, moved.content)
        delivered = self.client.post(
            f"{LAUNDRY_URL}{reference}/status/",
            {"status": "DELIVERED", "delivered_to": "Chinedu Okeke"}, format="json",
        )
        self.assertEqual(delivered.status_code, 200, delivered.content)
        self.assertEqual(delivered.json()["data"]["delivered_to"], "Chinedu Okeke")

    # --- Money ------------------------------------------------------------

    def test_walk_in_laundry_cannot_be_charged_to_a_guest_folio(self):
        reference = self._receive(key="walkin-no-folio").json()["data"]["reference"]
        self.auth(self.manager)
        billed = self.client.post(f"{LAUNDRY_URL}{reference}/bill/", {}, format="json")
        self.assertEqual(billed.status_code, 400, billed.content)
        self.assertIn("no guest folio", str(billed.json()["errors"]))
        order = LaundryOrder.objects.get(reference=reference)
        self.assertIsNone(order.financial_transaction_id)

    def test_a_bank_transfer_payment_posts_revenue_once(self):
        reference = self._receive(key="walkin-bank").json()["data"]["reference"]
        self.auth(self.manager)
        paid = self.client.post(f"{LAUNDRY_URL}{reference}/settle/", {
            "method": "BANK_TRANSFER", "external_reference": "TRF-9911",
        }, format="json")
        self.assertEqual(paid.status_code, 200, paid.content)
        data = paid.json()["data"]
        self.assertTrue(data["settled"])
        self.assertEqual(data["payment_method"], "BANK_TRANSFER")
        self.assertTrue(data["financial_reference"])

        # Retrying does not collect a second time.
        again = self.client.post(f"{LAUNDRY_URL}{reference}/settle/", {
            "method": "BANK_TRANSFER", "external_reference": "TRF-9911",
        }, format="json")
        self.assertEqual(again.status_code, 200, again.content)
        self.assertEqual(
            FinancialTransaction.objects.filter(source_reference=reference).count(), 1,
        )

    def test_the_collection_entry_moves_the_tender_and_recognizes_laundry_revenue(self):
        reference = self._receive(key="walkin-ledger").json()["data"]["reference"]
        self.auth(self.manager)
        self.client.post(f"{LAUNDRY_URL}{reference}/settle/", {"method": "CARD"}, format="json")

        transaction = FinancialTransaction.objects.get(source_reference=reference)
        lines = {
            (line.account_code, line.direction): line.amount
            for line in FinancialLine.objects.filter(transaction=transaction)
        }
        self.assertEqual(lines[(accounting.CARD_CLEARING, "DEBIT")], Decimal("2000.00"))
        self.assertEqual(lines[(accounting.OTHER_OPERATING_REVENUE, "CREDIT")], Decimal("2000.00"))
        # A walk-in never creates a receivable: the customer owes nothing.
        self.assertNotIn((accounting.ACCOUNTS_RECEIVABLE, "DEBIT"), lines)
        self.assertEqual(transaction.type, FinancialTransaction.Type.PAYMENT_COLLECTION)
        self.assertEqual(transaction.status, FinancialTransaction.Status.POSTED)

    def test_cash_must_land_in_the_acting_cashier_own_open_drawer(self):
        reference = self._receive(key="walkin-cash", actor=self.cashier).json()["data"]["reference"]
        session = open_cash_session(cashier=self.cashier, opening_float="1000.00")
        before = Decimal(session.expected_cash)

        self.auth(self.cashier)
        paid = self.client.post(f"{LAUNDRY_URL}{reference}/settle/", {
            "method": "CASH", "cash_session_reference": session.reference,
        }, format="json")
        self.assertEqual(paid.status_code, 200, paid.content)

        session.refresh_from_db()
        self.assertEqual(Decimal(session.expected_cash), before + Decimal("2000.00"))
        movement = CashMovement.objects.get(source_reference=reference)
        self.assertEqual(movement.type, CashMovement.Type.COLLECTION)
        self.assertEqual(movement.amount, Decimal("2000.00"))
        self.assertEqual(movement.cash_session_id, session.pk)

        # Another cashier cannot bank this money into someone else's drawer.
        other = make_staff("laundry-other@staff.dev", role=User.Role.RECEPTIONIST)
        second = self._receive(key="walkin-cash-2", actor=self.cashier).json()["data"]["reference"]
        open_cash_session(cashier=other, opening_float="0.00")
        self.auth(other)
        stolen = self.client.post(f"{LAUNDRY_URL}{second}/settle/", {
            "method": "CASH", "cash_session_reference": session.reference,
        }, format="json")
        self.assertEqual(stolen.status_code, 403, stolen.content)
        # And nothing was recorded against the order or the first drawer.
        self.assertIsNone(LaundryOrder.objects.get(reference=second).financial_transaction_id)
        session.refresh_from_db()
        self.assertEqual(Decimal(session.expected_cash), before + Decimal("2000.00"))

    def test_a_cash_payment_without_a_drawer_is_refused(self):
        reference = self._receive(key="walkin-cash-missing").json()["data"]["reference"]
        self.auth(self.manager)
        response = self.client.post(f"{LAUNDRY_URL}{reference}/settle/", {"method": "CASH"}, format="json")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("cash_session_reference", response.json()["errors"])

    def test_a_closed_drawer_cannot_take_a_cash_payment(self):
        reference = self._receive(key="walkin-cash-closed", actor=self.cashier).json()["data"]["reference"]
        session = open_cash_session(cashier=self.cashier, opening_float="0.00")
        CashSession.objects.filter(pk=session.pk).update(status=CashSession.Status.CLOSED)
        self.auth(self.cashier)
        response = self.client.post(f"{LAUNDRY_URL}{reference}/settle/", {
            "method": "CASH", "cash_session_reference": session.reference,
        }, format="json")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("not open", str(response.json()["errors"]))

    def test_an_in_house_order_still_settles_to_the_folio_not_at_the_desk(self):
        from datetime import timedelta

        from apps.stays.models import Stay

        from .factories import make_booking, make_guest, make_room, make_room_type

        today = hotel_today()
        room_type = make_room_type("Walk-in guard suite", price="20000.00")
        room = make_room(room_type, "801")
        booking = make_booking(
            make_guest("walkin-guard@example.com"), room_type, rooms=[room],
            check_in=today, check_out=today + timedelta(days=2), total="40000.00",
        )
        stay = Stay.objects.create(
            reference=f"STY-{booking.booking_reference}", booking=booking, guest=booking.guest,
            status=Stay.Status.IN_HOUSE, expected_arrival=booking.check_in,
            expected_departure=booking.check_out,
        )
        self.auth(self.manager)
        created = self.client.post(LAUNDRY_URL, {
            "stay_reference": stay.reference,
            "items": [{"description": "Shirt", "quantity": 1, "unit_price": "500.00"}],
            "idempotency_key": "inhouse-guard",
        }, format="json")
        self.assertEqual(created.status_code, 201, created.content)
        data = created.json()["data"]
        self.assertFalse(data["is_walk_in"])
        self.assertEqual(data["stay_reference"], stay.reference)
        self.assertEqual(data["guest_name"], booking.guest.full_name)

        # The desk-payment route is for walk-ins only.
        refused = self.client.post(f"{LAUNDRY_URL}{data['reference']}/settle/", {
            "method": "BANK_TRANSFER",
        }, format="json")
        self.assertEqual(refused.status_code, 400, refused.content)
        self.assertIn("folio", str(refused.json()["errors"]))

    def test_settling_needs_the_laundry_capability(self):
        reference = self._receive(key="walkin-perm").json()["data"]["reference"]
        # A cashier holds money capabilities but not guest_request.manage, so
        # this proves the gate is the guest-services capability, not a role guess.
        outsider = make_staff("walkin-outsider@staff.dev", role=User.Role.CASHIER)
        self.auth(outsider)
        response = self.client.post(f"{LAUNDRY_URL}{reference}/settle/", {"method": "CARD"}, format="json")
        self.assertIn(response.status_code, (401, 403))
        self.assertIsNone(LaundryOrder.objects.get(reference=reference).financial_transaction_id)

    def test_a_cancelled_walk_in_order_cannot_be_paid_for(self):
        reference = self._receive(key="walkin-cancelled").json()["data"]["reference"]
        self.auth(self.manager)
        cancelled = self.client.post(
            f"{LAUNDRY_URL}{reference}/status/", {"status": "CANCELLED"}, format="json",
        )
        self.assertEqual(cancelled.status_code, 200, cancelled.content)
        response = self.client.post(f"{LAUNDRY_URL}{reference}/settle/", {"method": "CARD"}, format="json")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("Cancelled", str(response.json()["errors"]))

    def test_walk_in_laundry_revenue_reaches_the_financial_summary(self):
        from apps.finance.services.reporting_service import financial_summary

        reference = self._receive(key="walkin-report").json()["data"]["reference"]
        self.auth(self.manager)
        self.client.post(f"{LAUNDRY_URL}{reference}/settle/", {"method": "CARD"}, format="json")

        summary = financial_summary(start_date=hotel_today(), end_date=hotel_today(), currency="NGN")
        collected = FinancialLine.objects.filter(
            account_code=accounting.CARD_CLEARING, direction="DEBIT", transaction__status="POSTED",
        ).aggregate(total=Sum("amount"))["total"]
        self.assertEqual(collected, Decimal("2000.00"))
        # Collections is a measure block (amount + transaction count), exactly
        # like every other measure the Accounting screen reads.
        self.assertEqual(summary["collections"]["amount"], "2000.00")
        self.assertEqual(summary["collections"]["transactions"], 1)
        # The revenue itself is classified as other operating income.
        other = [
            row for row in summary["by_department"]
            if row["account_code"] == accounting.OTHER_OPERATING_REVENUE
        ]
        self.assertEqual(len(other), 1)
        self.assertEqual(other[0]["amount"], "2000.00")
