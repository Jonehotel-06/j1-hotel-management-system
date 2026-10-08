# tests/test_booking_idempotency.py
"""Idempotent booking creation: a lost 201 response must never produce a
duplicate reservation when the user presses Retry.

Covers the client-generated ``Idempotency-Key`` mechanism on
POST /api/bookings/ (Part: safe retries after client timeouts).
"""
from datetime import timedelta
from unittest import mock

from apps.bookings.models import Booking
from apps.core.utils import hotel_today

from .base import BaseAPITestCase
from .factories import make_room, make_room_type


class BookingIdempotencyTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.room_type = make_room_type("Idem Deluxe", price="20000.00", max_guests=2)
        make_room(self.room_type, "I1")
        make_room(self.room_type, "I2")
        today = hotel_today()
        self.payload = {
            "room_type": self.room_type.slug,
            "check_in": (today + timedelta(days=9)).isoformat(),
            "check_out": (today + timedelta(days=11)).isoformat(),
            "rooms": 1,
            "adults": 2,
            "children": 0,
            "guest": {
                "first_name": "Chidi",
                "last_name": "Retry",
                "email": "chidi@example.test",
                "phone": "08030000000",
            },
        }

    def test_first_request_creates_booking_with_201(self):
        res = self.client.post(
            "/api/bookings/", self.payload, format="json",
            HTTP_IDEMPOTENCY_KEY="client-key-abc123",
        )
        self.assertEqual(res.status_code, 201, res.json())
        self.assertFalse(res.json()["data"].get("idempotent_replay"))
        self.assertEqual(Booking.objects.count(), 1)

    def test_retry_with_same_key_returns_original_booking_not_duplicate(self):
        first = self.client.post(
            "/api/bookings/", self.payload, format="json",
            HTTP_IDEMPOTENCY_KEY="client-key-abc123",
        )
        self.assertEqual(first.status_code, 201)
        reference = first.json()["data"]["booking_reference"]

        # The "retry" — same logical submission, same key (this is exactly the
        # scenario where the browser timed out but the server had created the
        # booking).
        second = self.client.post(
            "/api/bookings/", self.payload, format="json",
            HTTP_IDEMPOTENCY_KEY="client-key-abc123",
        )
        self.assertEqual(second.status_code, 200)
        data = second.json()["data"]
        self.assertEqual(data["booking_reference"], reference)
        self.assertTrue(data["idempotent_replay"])
        # No duplicate reservation, no second inventory hold.
        self.assertEqual(Booking.objects.count(), 1)

    def test_replay_reissues_fresh_guest_access_token(self):
        first = self.client.post(
            "/api/bookings/", self.payload, format="json",
            HTTP_IDEMPOTENCY_KEY="client-key-abc123",
        )
        token1 = first.json()["data"]["guest_access_token"]
        second = self.client.post(
            "/api/bookings/", self.payload, format="json",
            HTTP_IDEMPOTENCY_KEY="client-key-abc123",
        )
        token2 = second.json()["data"]["guest_access_token"]
        # The first token was lost with the timed-out response; the replay
        # rotates it so the guest can still access their booking.
        self.assertTrue(token2)
        self.assertNotEqual(token1, token2)

        reference = second.json()["data"]["booking_reference"]
        res = self.client.get(
            f"/api/bookings/{reference}/", HTTP_X_GUEST_ACCESS_TOKEN=token2
        )
        self.assertEqual(res.status_code, 200)

    def test_different_key_creates_new_booking(self):
        self.client.post("/api/bookings/", self.payload, format="json",
                         HTTP_IDEMPOTENCY_KEY="key-one-111111")
        self.client.post("/api/bookings/", self.payload, format="json",
                         HTTP_IDEMPOTENCY_KEY="key-two-222222")
        self.assertEqual(Booking.objects.count(), 2)

    def test_invalid_key_is_rejected(self):
        res = self.client.post(
            "/api/bookings/", self.payload, format="json",
            HTTP_IDEMPOTENCY_KEY="bad key with spaces!",
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(Booking.objects.count(), 0)

    def test_missing_key_still_works(self):
        res = self.client.post("/api/bookings/", self.payload, format="json")
        self.assertEqual(res.status_code, 201)
        self.assertEqual(Booking.objects.count(), 1)

    def test_racing_duplicate_key_returns_existing_booking(self):
        # Two concurrent requests with the same key: one wins the insert, the
        # loser hits the unique constraint and must return the winner's booking
        # instead of raising a 500 or creating a duplicate.
        from django.db import IntegrityError

        with mock.patch(
            "apps.bookings.services.booking_service.Booking.objects.create",
            side_effect=IntegrityError("UNIQUE constraint failed: idempotency_key"),
        ):
            res = self.client.post(
                "/api/bookings/", self.payload, format="json",
                HTTP_IDEMPOTENCY_KEY="race-key-999999",
            )
        # No pre-existing row with that key: the constraint failure must be
        # something else entirely — re-raised honestly (500, logged) rather
        # than mis-reported as a replay.
        self.assertEqual(res.status_code, 500)
        self.assertEqual(Booking.objects.count(), 0)

        # Now create one for real, then simulate the losing racer.
        self.client.post("/api/bookings/", self.payload, format="json",
                         HTTP_IDEMPOTENCY_KEY="race-key-999999")
        with mock.patch(
            "apps.bookings.services.booking_service.Booking.objects.create",
            side_effect=IntegrityError("UNIQUE constraint failed: idempotency_key"),
        ):
            res = self.client.post(
                "/api/bookings/", self.payload, format="json",
                HTTP_IDEMPOTENCY_KEY="race-key-999999",
            )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["data"]["idempotent_replay"])
        self.assertEqual(Booking.objects.count(), 1)
