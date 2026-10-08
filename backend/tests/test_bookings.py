# tests/test_bookings.py
"""BOOKING ENGINE tests: creation, double-booking protection, validation,
cancellation, expiration, guest record isolation (IDOR)."""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from apps.bookings.models import Booking, Guest
from apps.core.utils import hotel_today

from .base import BaseAPITestCase
from .factories import make_room, make_room_type, make_user, make_staff


class BookingFlowTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.room_type = make_room_type("Standard", price="15000.00", max_guests=2)
        self.room_a = make_room(self.room_type, "101")
        self.room_b = make_room(self.room_type, "102")
        self.user = make_user("guest@book.dev", password="Str0ng!Pass", first_name="Ada", last_name="Obi")
        self.auth(self.user)
        self.today = hotel_today()

    def _create(self, **overrides):
        payload = {
            "room_type": self.room_type.slug,
            "check_in": (self.today + timedelta(days=5)).isoformat(),
            "check_out": (self.today + timedelta(days=7)).isoformat(),
            "rooms": 1, "adults": 2, "children": 0,
            "guest": {"phone": "08031234567"},
            "special_requests": "High floor please",
        }
        payload.update(overrides)
        return self.client.post("/api/bookings/", payload, format="json")

    def test_create_booking_happy_path(self):
        response = self._create()
        self.assertEqual(response.status_code, 201, response.json())
        data = response.json()["data"]
        self.assertEqual(data["status"], "PENDING")
        self.assertEqual(data["payment_status"], "UNPAID")
        self.assertEqual(data["total_amount"], "30000.00")   # 15000 × 2 nights
        self.assertEqual(data["required_payment"], "30000.00")
        self.assertEqual(len(data["room_assignments"]), 1)   # physical room blocked
        self.assertTrue(data["can_pay"])
        booking = Booking.objects.get(booking_reference=data["booking_reference"])
        self.assertIsNotNone(booking.expires_at)
        self.assertEqual(booking.guest.user, self.user)

    def test_anonymous_booking_requires_complete_guest_details(self):
        self.unauth()
        response = self._create()
        self.assertEqual(response.status_code, 400)
        response = self._create(guest={
            "first_name": "Ada", "last_name": "Obi",
            "email": "ada@example.test", "phone": "08031234567",
        })
        self.assertEqual(response.status_code, 201, response.json())

    def test_create_rejects_past_dates(self):
        response = self._create(
            check_in=(self.today - timedelta(days=1)).isoformat(),
            check_out=(self.today + timedelta(days=1)).isoformat(),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "INVALID_DATES")

    def test_create_rejects_checkout_before_checkin(self):
        response = self._create(
            check_in=(self.today + timedelta(days=7)).isoformat(),
            check_out=(self.today + timedelta(days=5)).isoformat(),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "INVALID_DATES")

    def test_create_rejects_capacity_overflow(self):
        response = self._create(adults=5)  # 2 guests max per room × 1 room
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "CAPACITY_EXCEEDED")

    def test_double_booking_is_prevented(self):
        """All inventory reserved → the next requester gets ROOM_UNAVAILABLE."""
        first = self._create(rooms=2)
        self.assertEqual(first.status_code, 201)
        second = self._create(rooms=1)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json()["code"], "ROOM_UNAVAILABLE")
        self.assertEqual(Booking.objects.count(), 1)

    def test_double_booking_allowed_after_staff_cancellation(self):
        first = self._create(rooms=2)
        reference = first.json()["data"]["booking_reference"]
        staff = make_staff("cancel-desk@staff.dev")
        self.auth(staff)
        cancel = self.client.post(f"/api/admin/bookings/{reference}/cancel/", {"reason": "Changed plans"})
        self.assertEqual(cancel.status_code, 200, cancel.json())
        self.auth(self.user)
        second = self._create(rooms=2)
        self.assertEqual(second.status_code, 201, second.json())

    def test_my_bookings_lists_only_mine(self):
        self._create()
        other = make_user("other@book.dev", password="Str0ng!Pass")
        self.auth(other)
        response = self.client.get("/api/bookings/")
        data = response.json()
        self.assertEqual(data["pagination"]["count"], 0)
        self.assertEqual(data["data"], [])

    def test_guest_cannot_read_another_guests_booking(self):
        reference = self._create().json()["data"]["booking_reference"]
        other = make_user("idor@book.dev", password="Str0ng!Pass")
        self.auth(other)
        response = self.client.get(f"/api/bookings/{reference}/")
        # Object-level permission denies access without leaking the object.
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "RESOURCE_NOT_FOUND")
        # Also by guessing the numeric id.
        booking = Booking.objects.get(booking_reference=reference)
        by_id = self.client.get(f"/api/bookings/{booking.pk}/")
        self.assertEqual(by_id.status_code, 404)

    def test_guest_direct_cancellation_is_disabled_before_deadline(self):
        reference = self._create(
            check_in=(self.today + timedelta(days=5)).isoformat(),
            check_out=(self.today + timedelta(days=7)).isoformat(),
        ).json()["data"]["booking_reference"]
        response = self.client.post(f"/api/bookings/{reference}/cancel/",
                                    {"reason": "No longer travelling"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "CANCELLATION_NOT_ALLOWED")
        booking = Booking.objects.get(booking_reference=reference)
        self.assertEqual(booking.status, "PENDING")
        self.assertEqual(booking.cancellation_reason, "")

    def test_cancel_blocked_after_deadline(self):
        reference = self._create(
            check_in=(self.today + timedelta(days=1)).isoformat(),
            check_out=(self.today + timedelta(days=2)).isoformat(),
        ).json()["data"]["booking_reference"]
        response = self.client.post(f"/api/bookings/{reference}/cancel/")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "CANCELLATION_NOT_ALLOWED")

    def test_expired_pending_lazily_flipped_and_inventory_released(self):
        reference = self._create(rooms=2).json()["data"]["booking_reference"]
        booking = Booking.objects.get(booking_reference=reference)
        booking.expires_at = timezone.now() - timedelta(minutes=1)
        booking.save(update_fields=["expires_at"])
        board = self.client.get(f"/api/bookings/{reference}/")
        self.assertEqual(board.json()["data"]["status"], "EXPIRED")
        # A fresh availability search now sees both rooms again.
        availability = self.client.get("/api/rooms/availability/", {
            "check_in": (self.today + timedelta(days=5)).isoformat(),
            "check_out": (self.today + timedelta(days=7)).isoformat(),
            "room_type": self.room_type.slug,
        })
        self.assertEqual(availability.json()["data"]["results"][0]["available_rooms"], 2)

    def test_receipt_shape(self):
        reference = self._create().json()["data"]["booking_reference"]
        booking = Booking.objects.get(booking_reference=reference)
        booking.amount_paid = booking.total_amount
        booking.payment_status = Booking.PaymentStatus.PAID
        booking.status = Booking.Status.CONFIRMED
        booking.save()
        response = self.client.get(f"/api/bookings/{reference}/receipt/")
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["hotel"]["name"], "J-ONE HOTEL & LODGE")
        self.assertEqual(data["booking_reference"], reference)
        self.assertEqual(data["payment_status"], "PAID")

    def test_unknown_room_type_rejected(self):
        response = self.client.post("/api/bookings/", {
            "room_type": "no-such-type",
            "check_in": (self.today + timedelta(days=5)).isoformat(),
            "check_out": (self.today + timedelta(days=7)).isoformat(),
        }, format="json")
        self.assertEqual(response.status_code, 400)
