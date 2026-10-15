"""Checkout folio / gate-clearance summary (GET admin/bookings/<ref>/folio-clearance/).

The checkout desk must see everything the guest consumed (food, drinks,
laundry and other folio charges), what is still owed, and what has not been
billed yet — before printing a gate pass. Itemisation must come from the
immutable folio postings and totals must agree with the audit-D4 helpers.
"""
from datetime import timedelta

from apps.accounts.models import User
from apps.core.utils import hotel_today
from apps.finance.models import FinancialTransaction, Folio
from apps.finance.services import accounting
from apps.finance.services.ledger_service import create_posted_transaction
from apps.guest_services.models import ServiceRequest
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

    def test_clearance_requires_the_folio_view_capability(self):
        self.auth(self.housekeeper)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 403)
        # And unauthenticated callers are rejected outright.
        self.unauth()
        response = self.client.get(self.url)
        self.assertIn(response.status_code, (401, 403))
