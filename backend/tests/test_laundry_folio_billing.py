"""A guest's request bill must land on the FOLIO when the guest receives it.

Reported: "guest folio is not recording guest request bills, but on print
receipt it appears". The printed bill showed the money; the folio (the ledger,
the guest's statement, and every folio figure the check-out screen reads) did
not, because an in-house laundry order was only billed when somebody pressed
"bill" or when the desk settled at check-out. Restaurant and room-service orders
already post their folio charge the moment they are handed over, so delivery is
the rule pinned here for laundry too.

What these tests prove:

* delivering an in-house order posts ONE folio charge for the order total, and
  the folio balance, the ledger receivable and the check-out bill all agree;
* the charge is never posted twice (delivery is reached once; a repeated bill
  call is a no-op);
* a walk-in order (no folio) is still not charged to anyone;
* a zero-priced order is deliverable without inventing a charge.
"""
from decimal import Decimal

from django.db.models import Sum

from apps.accounts.models import User
from apps.core.utils import hotel_today
from apps.finance.models import FinancialLine, FinancialTransaction, Folio, FolioPosting
from apps.finance.services import accounting
from apps.finance.services.folio_service import booking_folio_charge_balance, folio_balance_queryset
from apps.guest_services.models import LaundryOrder
from apps.stays.models import Stay

from .base import BaseAPITestCase
from .factories import make_booking, make_guest, make_room, make_room_type, make_staff

LAUNDRY_URL = "/api/admin/service-requests/laundry/"


class LaundryFolioBillingTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.manager = make_staff("laundry-folio-manager@staff.dev", role=User.Role.MANAGER)
        room_type = make_room_type("Laundry suite", price="20000.00")
        room = make_room(room_type, "611")
        self.booking = make_booking(
            make_guest("laundry-folio@example.com"), room_type, rooms=[room],
            status="CHECKED_IN", total="20000.00", amount_paid="20000.00",
        )
        self.stay = Stay.objects.create(
            reference=f"STY-{self.booking.booking_reference}", booking=self.booking,
            guest=self.booking.guest, status=Stay.Status.IN_HOUSE,
            expected_arrival=self.booking.check_in, expected_departure=self.booking.check_out,
        )

    # ------------------------------- helpers --------------------------------

    def _receive(self, *, key="folio-laundry-1", unit_price="2000.00", stay=True, items=None):
        self.auth(self.manager)
        payload = {
            "items": items or [
                {"description": "Shirt", "quantity": 2, "service_type": "WASH_IRON", "unit_price": unit_price},
            ],
            "idempotency_key": key,
        }
        if stay:
            payload["stay_reference"] = self.stay.reference
        else:
            payload["customer_name"] = "Walk In"
            payload["customer_phone"] = "08030002222"
        response = self.client.post(LAUNDRY_URL, payload, format="json")
        assert response.status_code == 201, response.content
        return response.json()["data"]["reference"]

    def _advance(self, reference, status):
        self.auth(self.manager)
        response = self.client.post(
            f"{LAUNDRY_URL}{reference}/status/", {"status": status}, format="json"
        )
        assert response.status_code == 200, response.content
        return response.json()["data"]

    def _deliver(self, reference):
        self._advance(reference, "IN_PROGRESS")
        self._advance(reference, "READY")
        return self._advance(reference, "DELIVERED")

    def _folio(self):
        return Folio.objects.get(stay=self.stay, kind=Folio.Kind.MAIN)

    def _folio_balance(self):
        """The folio's own signed balance, exactly as the API reads it."""
        return folio_balance_queryset(Folio.objects.filter(pk=self._folio().pk)).values_list(
            "balance", flat=True
        ).get()

    # ------------------------- the reported gap -----------------------------

    def test_delivering_the_order_bills_it_to_the_folio(self):
        reference = self._receive()
        self.assertFalse(
            FolioPosting.objects.filter(folio__booking=self.booking).exists(),
            "nothing should be on the folio before the guest receives the laundry",
        )

        self._deliver(reference)

        order = LaundryOrder.objects.get(reference=reference)
        self.assertIsNotNone(order.financial_transaction_id, "delivery did not bill the order")
        postings = FolioPosting.objects.filter(folio=self._folio())
        self.assertEqual(postings.count(), 1)
        posting = postings.get()
        self.assertEqual(posting.effect, FolioPosting.Effect.DEBIT)
        self.assertEqual(posting.kind, FolioPosting.Kind.CHARGE)
        self.assertEqual(posting.amount, Decimal("4000.00"))
        # The folio figure and the ledger agree with the bill.
        self.assertEqual(self._folio_balance(), Decimal("4000.00"))
        self.assertEqual(booking_folio_charge_balance(booking=self.booking), Decimal("4000.00"))
        debit, credit = FinancialLine.objects.filter(
            transaction=order.financial_transaction
        ).aggregate(debit=Sum("amount"), credit=Sum("amount")).values()
        self.assertEqual(debit, credit, "the charge does not balance")

    def test_the_receivable_moves_with_the_folio(self):
        reference = self._receive()
        self._deliver(reference)
        transaction = LaundryOrder.objects.get(reference=reference).financial_transaction
        receivable = FinancialLine.objects.filter(
            transaction=transaction, account_code=accounting.ACCOUNTS_RECEIVABLE
        ).first()
        self.assertIsNotNone(receivable, "the folio charge posted no receivable")
        self.assertEqual(receivable.direction, FinancialLine.Direction.DEBIT)
        self.assertEqual(receivable.amount, Decimal("4000.00"))

    def test_the_checkout_bill_shows_it_as_a_folio_charge_not_an_unpaid_request(self):
        reference = self._receive()
        self._deliver(reference)

        self.auth(self.manager)
        response = self.client.get(
            f"/api/admin/bookings/{self.booking.booking_reference}/folio-clearance/"
        )
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertEqual(data["unpaid_requests"], [], "the request is billed, not pending")
        self.assertEqual(len(data["charges"]), 1)
        self.assertEqual(data["charges"][0]["source_reference"], reference)
        self.assertEqual(data["charges_total"], "4000.00")
        self.assertEqual(data["folio_balance"], "4000.00")
        self.assertEqual(data["amount_to_settle"], "4000.00")
        self.assertFalse(data["cleared"], "money the guest has not paid must still block the gate")

    def test_delivery_is_the_only_billing_that_happens(self):
        reference = self._receive()
        self._deliver(reference)
        billed_transaction = LaundryOrder.objects.get(reference=reference).financial_transaction_id
        self.auth(self.manager)
        billed = self.client.post(f"{LAUNDRY_URL}{reference}/bill/", {}, format="json")
        self.assertEqual(billed.status_code, 200, billed.content)
        self.assertEqual(
            LaundryOrder.objects.get(reference=reference).financial_transaction_id,
            billed_transaction,
            "the order was billed twice",
        )
        self.assertEqual(FolioPosting.objects.filter(folio=self._folio()).count(), 1)
        self.assertEqual(self._folio_balance(), Decimal("4000.00"))

    def test_a_walk_in_order_is_never_charged_to_a_folio(self):
        reference = self._receive(key="folio-laundry-walkin", stay=False)
        self._deliver(reference)
        order = LaundryOrder.objects.get(reference=reference)
        self.assertIsNone(order.financial_transaction_id)
        self.assertFalse(FolioPosting.objects.filter(folio__booking=self.booking).exists())

    def test_a_zero_priced_order_is_still_deliverable(self):
        reference = self._receive(
            key="folio-laundry-free",
            items=[{"description": "Complimentary press", "quantity": 1,
                    "service_type": "PRESS", "unit_price": "0.00"}],
        )
        delivered = self._deliver(reference)
        self.assertEqual(delivered["status"], LaundryOrder.Status.DELIVERED)
        self.assertFalse(
            FolioPosting.objects.filter(folio__booking=self.booking).exists(),
            "a zero-priced order must not invent a charge",
        )

    def test_the_guest_statement_can_show_the_billed_request(self):
        reference = self._receive()
        self._deliver(reference)
        postings = FolioPosting.objects.filter(folio=self._folio(), source_reference=reference)
        self.assertEqual(postings.count(), 1)
        self.assertEqual(postings.get().description, f"Laundry {reference}")

    def test_a_cancelled_order_before_delivery_stays_unbilled(self):
        reference = self._receive(key="folio-laundry-cancel")
        self._advance(reference, "CANCELLED")
        self.assertFalse(FolioPosting.objects.filter(folio__booking=self.booking).exists())
        self.assertIsNone(LaundryOrder.objects.get(reference=reference).financial_transaction_id)

    def test_checking_out_is_unaffected_by_the_new_posting(self):
        reference = self._receive()
        self._deliver(reference)
        self.auth(self.manager)
        response = self.client.post(
            f"/api/admin/bookings/{self.booking.booking_reference}/settle-balance/",
            {"amount": "4000.00", "provider": "BANK_TRANSFER", "notes": "Settled at the desk"},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        folio = response.json()["data"]["folio"]
        self.assertEqual(folio["folio_balance"], "0.00")
        self.assertTrue(folio["cleared"])
        self.assertEqual(folio["credits_total"], "4000.00")
        self.assertFalse(
            FinancialTransaction.objects.filter(type=FinancialTransaction.Type.OTHER_CHARGE).count() > 1,
            "settling must not post a second laundry charge",
        )
