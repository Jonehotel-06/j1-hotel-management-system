# tests/test_availability.py
"""AVAILABILITY ENGINE tests (spec §23–§24): overlap boundaries, operational
blocks, expired holds, empty results."""
from datetime import timedelta

from django.utils import timezone

from apps.bookings.models import Booking
from apps.core.utils import hotel_today
from apps.rooms.models import Room

from .base import BaseAPITestCase
from .factories import make_booking, make_guest, make_room, make_room_type


class AvailabilityTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.room_type = make_room_type("Standard", price="15000.00")
        self.room_a = make_room(self.room_type, "101")
        self.room_b = make_room(self.room_type, "102")
        self.guest = make_guest()
        self.today = hotel_today()

    def _search(self, check_in, check_out, **extra):
        params = {"check_in": check_in.isoformat(), "check_out": check_out.isoformat(), **extra}
        return self.client.get("/api/rooms/availability/", params)

    def test_all_rooms_free(self):
        response = self._search(self.today + timedelta(days=5), self.today + timedelta(days=8))
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        result = next(r for r in data["results"] if r["room_type"]["id"] == self.room_type.pk)
        self.assertEqual(result["available_rooms"], 2)
        self.assertTrue(result["bookable"])
        self.assertEqual(data["nights"], 3)

    def test_overlap_rules_block_occupied_dates(self):
        make_booking(self.guest, self.room_type, rooms=[self.room_a],
                     check_in=self.today + timedelta(days=10),
                     check_out=self.today + timedelta(days=15))
        # 10→12 overlaps the existing 10→15 booking: only room_b is free.
        response = self._search(self.today + timedelta(days=10), self.today + timedelta(days=12))
        result = response.json()["data"]["results"][0]
        self.assertEqual(result["available_rooms"], 1)

    def test_checkout_day_is_free_for_next_guest(self):
        """Boundary rule (spec §23): existing 10→15 must NOT block 15→20."""
        make_booking(self.guest, self.room_type, rooms=[self.room_a, self.room_b],
                     check_in=self.today + timedelta(days=10),
                     check_out=self.today + timedelta(days=15))
        response = self._search(self.today + timedelta(days=15), self.today + timedelta(days=20))
        result = response.json()["data"]["results"][0]
        self.assertEqual(result["available_rooms"], 2)
        self.assertTrue(result["bookable"])

    def test_day_before_checkin_is_free(self):
        make_booking(self.guest, self.room_type, rooms=[self.room_a, self.room_b],
                     check_in=self.today + timedelta(days=10),
                     check_out=self.today + timedelta(days=15))
        response = self._search(self.today + timedelta(days=5), self.today + timedelta(days=10))
        result = response.json()["data"]["results"][0]
        self.assertEqual(result["available_rooms"], 2)

    def test_maintenance_rooms_are_never_available(self):
        self.room_b.status = Room.Status.MAINTENANCE
        self.room_b.save()
        response = self._search(self.today + timedelta(days=5), self.today + timedelta(days=8))
        result = response.json()["data"]["results"][0]
        self.assertEqual(result["available_rooms"], 1)

    def test_expired_pending_holds_do_not_block(self):
        make_booking(
            self.guest, self.room_type, rooms=[self.room_a, self.room_b],
            check_in=self.today + timedelta(days=5), check_out=self.today + timedelta(days=8),
            status=Booking.Status.PENDING,
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        result = self._search(self.today + timedelta(days=5), self.today + timedelta(days=8)).json()["data"]["results"][0]
        self.assertEqual(result["available_rooms"], 2)

    def test_live_pending_hold_blocks_inventory(self):
        make_booking(
            self.guest, self.room_type, rooms=[self.room_a, self.room_b],
            check_in=self.today + timedelta(days=5), check_out=self.today + timedelta(days=8),
            status=Booking.Status.PENDING,
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        result = self._search(self.today + timedelta(days=5), self.today + timedelta(days=8)).json()["data"]["results"][0]
        self.assertEqual(result["available_rooms"], 0)
        self.assertFalse(result["bookable"])

    def test_empty_result_is_a_valid_200(self):
        make_booking(
            self.guest, self.room_type, rooms=[self.room_a, self.room_b],
            check_in=self.today + timedelta(days=5), check_out=self.today + timedelta(days=8),
        )
        response = self._search(self.today + timedelta(days=6), self.today + timedelta(days=7),
                                room_type=str(self.room_type.pk))
        self.assertEqual(response.status_code, 200)
        result = response.json()["data"]["results"][0]
        self.assertEqual(result["available_rooms"], 0)
        self.assertFalse(result["bookable"])

    def test_invalid_dates_rejected_with_code(self):
        response = self._search(self.today + timedelta(days=8), self.today + timedelta(days=5))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "INVALID_DATES")

    def test_public_physical_room_options_share_one_authoritative_window(self):
        check_in = self.today + timedelta(days=5)
        check_out = self.today + timedelta(days=8)
        make_booking(self.guest, self.room_type, rooms=[self.room_a],
                     check_in=check_in, check_out=check_out)

        response = self.client.get(
            f"/api/rooms/{self.room_type.slug}/rooms/",
            {"check_in": check_in.isoformat(), "check_out": check_out.isoformat()},
        )

        self.assertEqual(response.status_code, 200, response.json())
        options = {row["id"]: row["available"] for row in response.json()["data"]}
        self.assertFalse(options[self.room_a.pk])
        self.assertTrue(options[self.room_b.pk])

    def test_missing_params_returns_validation_envelope(self):
        response = self.client.get("/api/rooms/availability/")
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body["code"], "VALIDATION_ERROR")
        self.assertIn("check_in", body["errors"])
