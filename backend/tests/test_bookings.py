# tests/test_bookings.py
"""BOOKING ENGINE tests: creation, double-booking protection, validation,
cancellation, expiration, guest record isolation (IDOR)."""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from apps.bookings.models import Booking, Guest
from apps.core.utils import hotel_today

from .base import BaseAPITestCase
from .factories import hotel_settings, make_room, make_room_type, make_user, make_staff


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


class WalkInBookingLifecycleTests(BaseAPITestCase):
    """End-to-end tests for staff walk-in/phone booking creation (POST /api/admin/bookings/)."""

    def setUp(self):
        super().setUp()
        hotel_settings(tax_rate="0.00", service_fee="0.00", pending_booking_minutes=30)
        self.room_type = make_room_type("WalkIn Standard", price="15000.00", max_guests=2)
        self.room1 = make_room(self.room_type, "301")
        self.room2 = make_room(self.room_type, "302")
        self.other_type = make_room_type("WalkIn Suite", price="35000.00", max_guests=2)
        self.other_room = make_room(self.other_type, "401")
        self.staff = make_staff("reception-walkin@jone.dev")
        self.auth(self.staff)
        self.today = hotel_today()

    def _walk_in_payload(self, **overrides):
        base = {
            "room_type": self.room_type.slug,
            "check_in": (self.today + timedelta(days=3)).isoformat(),
            "check_out": (self.today + timedelta(days=5)).isoformat(),
            "rooms": 1,
            "adults": 2,
            "children": 0,
            "source": "WALK_IN",
            "status": "CONFIRMED",
            "special_requests": "Quiet floor near elevator",
            "internal_notes": "VIP walk-in guest — verified ID at front desk",
            "guest": {
                "first_name": "Ngozi",
                "last_name": "Eze",
                "email": "ngozi.walkin@example.test",
                "phone": "+234 (0) 803 123 4567",
            },
        }
        base.update(overrides)
        return base

    def test_walk_in_booking_creates_guest_room_assignment_and_keeps_internal_notes_staff_only(self):
        response = self.client.post("/api/admin/bookings/", self._walk_in_payload(), format="json")
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()["data"]
        ref = data["booking_reference"]
        self.assertTrue(ref.startswith("J1-"))
        self.assertEqual(data["source"], "WALK_IN")
        self.assertEqual(data["status"], "CONFIRMED")
        self.assertEqual(data["special_requests"], "Quiet floor near elevator")
        self.assertEqual(data["internal_notes"], "VIP walk-in guest — verified ID at front desk")
        self.assertEqual(len(data["room_assignments"]), 1)
        self.assertEqual(data["guest"]["phone"], "+234(0)8031234567")

        booking = Booking.objects.get(booking_reference=ref)
        self.assertEqual(booking.internal_notes, "VIP walk-in guest — verified ID at front desk")
        token = booking.issue_guest_access_token()

        # Public/guest endpoints must NEVER expose internal_notes
        self.client.credentials()
        public_detail = self.client.get(
            f"/api/bookings/{ref}/",
            HTTP_X_GUEST_ACCESS_TOKEN=token,
        )
        self.assertEqual(public_detail.status_code, 200, public_detail.content)
        self.assertNotIn("internal_notes", public_detail.json()["data"])

    def test_walk_in_booking_validation_errors_return_400_field_errors_never_500(self):
        # Overlong phone (>20 non-space characters) returns 400 on guest.phone, never 500 DataError
        bad_phone = self._walk_in_payload(
            guest={
                "first_name": "Ngozi",
                "last_name": "Eze",
                "email": "ngozi.walkin@example.test",
                "phone": "08031234567/08021234567/08091234567",
            }
        )
        r_phone = self.client.post("/api/admin/bookings/", bad_phone, format="json")
        self.assertEqual(r_phone.status_code, 400, r_phone.content)
        self.assertEqual(r_phone.json()["code"], "VALIDATION_ERROR")
        self.assertIn("phone", r_phone.json()["errors"].get("guest", {}))

        # Invalid email returns 400 on guest.email
        bad_email = self._walk_in_payload(
            guest={
                "first_name": "Ngozi",
                "last_name": "Eze",
                "email": "not-an-email",
                "phone": "08031234567",
            }
        )
        r_email = self.client.post("/api/admin/bookings/", bad_email, format="json")
        self.assertEqual(r_email.status_code, 400, r_email.content)
        self.assertIn("email", r_email.json()["errors"].get("guest", {}))

        # room_id belonging to another room_type returns 400 on room_id
        bad_room = self._walk_in_payload(room_id=self.other_room.pk)
        r_room = self.client.post("/api/admin/bookings/", bad_room, format="json")
        self.assertEqual(r_room.status_code, 400, r_room.content)
        self.assertIn("room_id", r_room.json()["errors"])

    def test_walk_in_booking_succeeds_even_if_confirmation_email_preparation_fails(self):
        from unittest.mock import patch

        with patch(
            "apps.bookings.serializers.ReceiptSerializer.to_representation",
            side_effect=RuntimeError("simulated receipt failure"),
        ):
            response = self.client.post("/api/admin/bookings/", self._walk_in_payload(), format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertTrue(response.json()["data"]["booking_reference"].startswith("J1-"))



class PublicBookingPhoneNormalisationTests(BaseAPITestCase):
    """Regression pin for the PUBLIC checkout phone contract (2026-10-18 audit).

    The public booking form (booking-review.html) explicitly accepts formatted
    numbers — ``pattern="[0-9+\\(\\)\\s\\-]{7,}"`` — and sends them trimmed but
    NOT compacted. The staff walk-in serializer and ``upsert_guest`` already
    compact spaces and enforce the Guest.phone 20-char column, but the public
    ``GuestWriteSerializer`` / ``RoomGuestInputSerializer`` used to hit the raw
    model MaxLengthValidator first, so a guest typing the very common
    "+234 (0) 803 123 4567" (21 chars) had the online booking rejected with
    "Ensure this field has no more than 20 characters." — the reported
    "online bookings are not completing successfully" failure.
    """

    def setUp(self):
        super().setUp()
        hotel_settings(tax_rate="0.00", service_fee="0.00", pending_booking_minutes=30)
        self.room_type = make_room_type("Public Standard", price="15000.00", max_guests=2)
        self.room_a = make_room(self.room_type, "201")
        self.room_b = make_room(self.room_type, "202")
        self.today = hotel_today()
        self.unauth()

    def _public_payload(self, **overrides):
        payload = {
            "room_type": self.room_type.slug,
            "check_in": (self.today + timedelta(days=5)).isoformat(),
            "check_out": (self.today + timedelta(days=7)).isoformat(),
            "rooms": 1, "adults": 2, "children": 0,
            "guest": {
                "first_name": "Amaka",
                "last_name": "Testguest",
                "email": "amaka.public@example.test",
                "phone": "+234 (0) 803 123 4567",
            },
        }
        payload.update(overrides)
        return payload

    def test_public_booking_compacts_formatted_guest_phone(self):
        response = self.client.post("/api/bookings/", self._public_payload(), format="json")
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()["data"]
        self.assertEqual(data["guest"]["phone"], "+234(0)8031234567")
        booking = Booking.objects.get(booking_reference=data["booking_reference"])
        self.assertEqual(booking.guest.phone, "+234(0)8031234567")

    def test_public_booking_rejects_phone_still_over_column_limit_after_compaction(self):
        payload = self._public_payload()
        payload["guest"]["phone"] = "+234-803-123-4567-9988-77"  # 25 chars, no spaces to compact
        response = self.client.post("/api/bookings/", payload, format="json")
        self.assertEqual(response.status_code, 400, response.content)
        errors = response.json()["errors"]
        self.assertEqual(
            errors["guest"]["phone"], ["Phone number must be 20 characters or fewer."]
        )
        self.assertEqual(Booking.objects.count(), 0)

    def test_public_booking_rejects_phone_with_too_few_digits(self):
        payload = self._public_payload()
        payload["guest"]["phone"] = "12345"
        response = self.client.post("/api/bookings/", payload, format="json")
        self.assertEqual(response.status_code, 400, response.content)
        errors = response.json()["errors"]
        self.assertEqual(
            errors["guest"]["phone"], ["Enter a valid phone number (at least 7 digits)."]
        )

    def test_multi_room_booking_compacts_room_guest_phone(self):
        from apps.bookings.models import RoomOccupant

        payload = self._public_payload(rooms=2, adults=3)
        payload["room_guests"] = [{
            "email": "sister.occupant@example.test",
            "full_name": "Sister Test",
            "phone": "+234 (0) 805 987 6543",
        }]
        response = self.client.post("/api/bookings/", payload, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        occupant = RoomOccupant.objects.get(email="sister.occupant@example.test")
        self.assertEqual(occupant.phone, "+234(0)8059876543")

    def test_room_guest_phone_over_limit_after_compaction_is_rejected_with_field_error(self):
        payload = self._public_payload(rooms=2, adults=3)
        payload["room_guests"] = [{
            "email": "sister.occupant@example.test",
            "full_name": "Sister Test",
            "phone": "+234-805-987-6543-1122-33",
        }]
        response = self.client.post("/api/bookings/", payload, format="json")
        self.assertEqual(response.status_code, 400, response.content)
        errors = response.json()["errors"]
        self.assertEqual(
            errors["room_guests"]["0"]["phone"],
            ["Phone number must be 20 characters or fewer."],
        )
        self.assertEqual(Booking.objects.count(), 0)

    def test_admin_guest_update_compacts_formatted_phone(self):
        from apps.bookings.models import Guest

        guest = Guest.objects.create(
            first_name="Chidi", last_name="Existing", email="chidi.guest@example.test",
            phone="08031230000",
        )
        staff = make_staff("guest-desk@jone.dev")
        self.auth(staff)
        response = self.client.patch(
            f"/api/admin/guests/{guest.pk}/",
            {"phone": "+234 (0) 803 555 1234"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        guest.refresh_from_db()
        self.assertEqual(guest.phone, "+234(0)8035551234")
