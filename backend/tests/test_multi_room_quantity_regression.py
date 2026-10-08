# tests/test_multi_room_quantity_regression.py
"""Regression tests for the multi-room QUANTITY bug (₦60,000 shown / 1 room
recorded for a 2-room stay) and the payment leg that carries the quantity.

``test_multi_room_booking.py`` already pins the inventory guarantees on a
5-room type. This file covers the specific reported scenario end to end on a
2-room-of-3 hotel and — critically — the API RESPONSE CONTRACT the frontend
reads, which is where the visible bug actually lived:

* ``POST /api/bookings/quote/`` returns its totals FLAT on ``data``
  (``data["total"]``, ``data["rooms"]``), with NO nested ``pricing`` object.
  The booking page read ``data.pricing.total``, got ``undefined``, and left a
  stale 1-room price on screen while the backend charged the correct
  2-room amount. These tests fail loudly if that shape ever changes again.
* the quantity survives quote → create → detail → receipt → payment, and the
  payment amount is derived from the multi-room total, never from the client.
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import override_settings

from apps.bookings.models import Booking, BookingRoom
from apps.payments.models import Payment
from tests.base import BaseAPITestCase
from tests.factories import make_booking, make_guest, make_room, make_room_type


CI = "2026-11-10"
CO = "2026-11-12"          # 2 hotel nights
PRICE = "60000.00"         # matches the reported scenario
GUEST = {
    "first_name": "Qty",
    "last_name": "Guest",
    "email": "qty.guest@example.com",
    "phone": "+2348012345678",
}


class MultiRoomQuantityContractTests(BaseAPITestCase):
    """3 physical rooms @ ₦60,000; the guest asks for 2 across 2 nights."""

    def setUp(self):
        super().setUp()
        self.rt = make_room_type(name="Qty Superior", price=PRICE, slug="qty-superior",
                                 max_guests=2)
        self.rooms = [make_room(self.rt, "Q10%d" % (i + 1)) for i in range(3)]

    # -- helpers -------------------------------------------------------------
    def quote(self, rooms, adults=2):
        return self.client.post("/api/bookings/quote/", {
            "room_type": self.rt.slug, "check_in": CI, "check_out": CO,
            "rooms": rooms, "adults": adults, "children": 0,
        }, format="json")

    def book(self, *, rooms, key, adults=2):
        return self.client.post("/api/bookings/", {
            "room_type": self.rt.slug, "check_in": CI, "check_out": CO,
            "rooms": rooms, "adults": adults, "children": 0, "guest": dict(GUEST),
        }, format="json", HTTP_IDEMPOTENCY_KEY=key)

    # -- Test 1: quote response shape the frontend depends on ----------------
    def test_quote_returns_flat_total_and_echoes_rooms(self):
        """The bug: the page read ``data.pricing.total`` — which never exists.

        Quote totals are FLAT on ``data``. Pin both the flat keys and the
        absence of the nested object so the contract can't silently drift.
        """
        res = self.quote(2)
        self.assertEqual(res.status_code, 200, res.json())
        data = res.json()["data"]

        self.assertNotIn("pricing", data)              # no nested wrapper — ever
        self.assertIn("total", data)                   # flat total …
        self.assertIn("rooms", data)                   # … and an echoed quantity

        self.assertEqual(data["rooms"], 2)             # echo lets the UI drop stale replies
        self.assertEqual(data["nights"], 2)
        self.assertEqual(data["price_per_night"], "60000.00")
        self.assertEqual(data["subtotal"], "240000.00")   # 60000 × 2 nights × 2 rooms
        self.assertEqual(data["total"], "240000.00")

    def test_quote_total_scales_with_quantity_only(self):
        """1 room vs 2 rooms on identical dates → exactly double, nothing else."""
        one = self.quote(1).json()["data"]
        two = self.quote(2).json()["data"]
        self.assertEqual(one["rooms"], 1)
        self.assertEqual(one["total"], "120000.00")    # 60000 × 2 nights × 1
        self.assertEqual(two["rooms"], 2)
        self.assertEqual(two["total"], "240000.00")    # 60000 × 2 nights × 2
        self.assertEqual(Decimal(two["total"]), Decimal(one["total"]) * 2)
        self.assertEqual(two["price_per_night"], one["price_per_night"])

    # -- Test 2: two rooms are actually recorded -----------------------------
    def test_two_rooms_recorded_priced_and_exposed(self):
        """rooms=2 → number_of_rooms 2, 2 distinct assignments, 240000 total.

        Also asserts the DETAIL payload the confirmation page reads, since the
        page keyed off ``rooms`` while the API field is ``number_of_rooms``.
        """
        res = self.book(rooms=2, key="qty-2of3-0001")
        self.assertEqual(res.status_code, 201, res.json())
        body = res.json()["data"]
        ref = body["booking_reference"]

        b = Booking.objects.get(booking_reference=ref)
        self.assertEqual(b.number_of_rooms, 2)                       # not silently 1
        self.assertEqual(b.room_assignments.count(), 2)
        self.assertEqual(len({a.room_id for a in b.room_assignments.all()}), 2)  # distinct
        self.assertEqual(str(b.total_amount), "240000.00")           # 60000 × 2 × 2

        # Creation response already carries the quantity for the review step.
        self.assertEqual(body["number_of_rooms"], 2)
        self.assertEqual(body["total_amount"], "240000.00")

        # Detail payload (confirmation page + resume guard).
        detail = self.client.get(
            f"/api/bookings/{ref}/",
            HTTP_X_GUEST_ACCESS_TOKEN=body["guest_access_token"],
        )
        self.assertEqual(detail.status_code, 200, detail.json())
        d = detail.json()["data"]
        self.assertEqual(d["number_of_rooms"], 2)
        self.assertEqual(len(d["room_assignments"]), 2)
        self.assertEqual(d["total_amount"], "240000.00")

    # -- Test 3: partial inventory (3 → 1 left) ------------------------------
    def test_partial_inventory_two_of_three_leaves_one_bookable(self):
        """Booking 2 of 3 leaves exactly 1 — availability must say so."""
        res = self.book(rooms=2, key="qty-partial-0002")
        self.assertEqual(res.status_code, 201, res.json())

        avail = self.client.get("/api/rooms/availability/", {
            "check_in": CI, "check_out": CO, "guests": 2, "rooms": 1,
        })
        self.assertEqual(avail.status_code, 200, avail.json())
        entry = next(r for r in avail.json()["data"]["results"]
                     if r["room_type"]["slug"] == self.rt.slug)
        self.assertEqual(entry["available_rooms"], 1)                # decremented by 2
        self.assertTrue(entry["bookable"])

        # The remaining single room is still genuinely bookable …
        ok = self.book(rooms=1, key="qty-partial-0003")
        self.assertEqual(ok.status_code, 201, ok.json())

        # … and all 3 physical rooms are now held for the stay, one per row.
        self.assertEqual(
            BookingRoom.objects.filter(room__room_type=self.rt,
                                       check_in__lt=CO, check_out__gt=CI).count(),
            3,
        )
        # A further request must fail rather than overbook.
        self.assertEqual(self.book(rooms=1, key="qty-partial-0004b").status_code, 409)

    # -- Test 4: full consumption (2 → 0) ------------------------------------
    def test_full_consumption_drops_availability_to_zero(self):
        """Take every room of the type → availability 0 and not bookable."""
        make_booking(make_guest(email="blk-qty@example.com"), self.rt,
                     rooms=self.rooms[:1], check_in=CI, check_out=CO, number_of_rooms=1)

        res = self.book(rooms=2, key="qty-full-0004")                # the last 2
        self.assertEqual(res.status_code, 201, res.json())

        avail = self.client.get("/api/rooms/availability/", {
            "check_in": CI, "check_out": CO, "guests": 2, "rooms": 1,
        })
        entry = next(r for r in avail.json()["data"]["results"]
                     if r["room_type"]["slug"] == self.rt.slug)
        self.assertEqual(entry["available_rooms"], 0)
        self.assertFalse(entry["bookable"])

    # -- Test 5: insufficient inventory is rejected, never downgraded --------
    def test_insufficient_inventory_rejects_with_contract_error(self):
        """Ask for 3 when 2 remain → 409 ROOM_UNAVAILABLE, nothing created."""
        make_booking(make_guest(email="blk-qty2@example.com"), self.rt,
                     rooms=self.rooms[:1], check_in=CI, check_out=CO, number_of_rooms=1)
        before = Booking.objects.count()

        res = self.book(rooms=3, key="qty-over-0005")
        self.assertEqual(res.status_code, 409, res.json())
        body = res.json()
        self.assertFalse(body.get("success"))
        self.assertEqual(body.get("code"), "ROOM_UNAVAILABLE")
        self.assertIn("Only 2 room", body.get("message", ""))        # honest remaining count

        # No partial booking and NO silent downgrade to 1 or 2 rooms.
        self.assertEqual(Booking.objects.count(), before)
        self.assertFalse(Booking.objects.filter(guest__email=GUEST["email"]).exists())

    # -- Test 6: payment amount follows the multi-room total -----------------
    @override_settings(PAYSTACK_SECRET_KEY="sk_test_mock")
    @patch("apps.payments.services.paystack.initialize_transaction")
    def test_payment_amount_is_server_derived_from_multi_room_total(self, mock_init):
        """Paystack is charged the 2-room amount even if the client lies."""
        mock_init.side_effect = lambda **kw: {
            "authorization_url": "https://checkout.paystack.com/xyz",
            "access_code": "xyz", "reference": kw["reference"],
        }
        created = self.book(rooms=2, key="qty-pay-0006").json()["data"]
        ref = created["booking_reference"]

        res = self.client.post("/api/payments/initialize/", {
            "booking_reference": ref,
            "amount": "60000.00",          # hostile client input — must be ignored
        }, format="json", HTTP_X_GUEST_ACCESS_TOKEN=created["guest_access_token"])
        self.assertEqual(res.status_code, 201, res.json())

        self.assertEqual(res.json()["data"]["amount"], "240000.00")
        payment = Payment.objects.get(booking__booking_reference=ref)
        self.assertEqual(payment.amount, Decimal("240000.00"))
        _, kwargs = mock_init.call_args
        self.assertEqual(kwargs["amount_kobo"], 24_000_000)          # 240000 × 100

    # -- Test 7: verification confirms the booking across ALL rooms ----------
    @override_settings(PAYSTACK_SECRET_KEY="sk_test_mock")
    @patch("apps.payments.services.paystack.verify_transaction")
    def test_verification_confirms_every_room_and_receipt_lists_them(self, mock_verify):
        """A verified 2-room payment confirms the booking and both rooms, and
        the receipt reports rooms=2 with both room numbers."""
        created = self.book(rooms=2, key="qty-verify-0007").json()["data"]
        ref, token = created["booking_reference"], created["guest_access_token"]
        booking = Booking.objects.get(booking_reference=ref)

        payment = Payment.objects.create(
            booking=booking, reference="J1P-QTY-0001",
            provider=Payment.Provider.PAYSTACK, amount=Decimal("240000.00"),
            currency="NGN", status=Payment.Status.PENDING,
        )
        mock_verify.return_value = {
            "status": True, "message": "Verification successful",
            "data": {"status": "success", "reference": payment.reference,
                     "amount": 24_000_000, "currency": "NGN", "channel": "card",
                     "gateway_response": "Successful", "id": 424242,
                     "paid_at": "2026-09-18T10:00:00Z", "ip_address": "127.0.0.1"},
        }

        res = self.client.get(f"/api/payments/verify/{payment.reference}/",
                              HTTP_X_GUEST_ACCESS_TOKEN=token)
        self.assertEqual(res.status_code, 200, res.json())
        data = res.json()["data"]
        self.assertEqual(data["payment_status"], "PAID")
        self.assertEqual(data["booking_status"], "CONFIRMED")

        booking.refresh_from_db()
        self.assertEqual(booking.amount_paid, Decimal("240000.00"))
        self.assertEqual(booking.number_of_rooms, 2)                 # quantity survived
        self.assertEqual(booking.room_assignments.count(), 2)        # both rooms still held

        receipt = self.client.get(f"/api/bookings/{ref}/receipt/",
                                  HTTP_X_GUEST_ACCESS_TOKEN=token)
        self.assertEqual(receipt.status_code, 200, receipt.json())
        r = receipt.json()["data"]
        self.assertEqual(r["rooms"], 2)                              # receipt.js reads this
        self.assertEqual(len(r["room_numbers"]), 2)
        self.assertEqual(r["total"], "240000.00")
        self.assertEqual(r["amount_paid"], "240000.00")

    # -- Single-room flow must be completely unaffected ----------------------
    def test_single_room_flow_unchanged(self):
        """The default 1-room journey keeps its old numbers exactly."""
        q = self.quote(1).json()["data"]
        self.assertEqual(q["rooms"], 1)
        self.assertEqual(q["total"], "120000.00")                    # 60000 × 2 nights

        res = self.book(rooms=1, key="qty-single-0008")
        self.assertEqual(res.status_code, 201, res.json())
        b = Booking.objects.get(booking_reference=res.json()["data"]["booking_reference"])
        self.assertEqual(b.number_of_rooms, 1)
        self.assertEqual(b.room_assignments.count(), 1)
        self.assertEqual(str(b.total_amount), "120000.00")

    def test_rooms_omitted_defaults_to_one(self):
        """A payload with no ``rooms`` key still books exactly 1 room."""
        res = self.client.post("/api/bookings/", {
            "room_type": self.rt.slug, "check_in": CI, "check_out": CO,
            "adults": 2, "children": 0, "guest": dict(GUEST),
        }, format="json", HTTP_IDEMPOTENCY_KEY="qty-default-0009")
        self.assertEqual(res.status_code, 201, res.json())
        b = Booking.objects.get(booking_reference=res.json()["data"]["booking_reference"])
        self.assertEqual(b.number_of_rooms, 1)
        self.assertEqual(b.room_assignments.count(), 1)
