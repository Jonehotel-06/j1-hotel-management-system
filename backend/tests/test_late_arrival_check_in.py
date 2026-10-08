# tests/test_late_arrival_check_in.py
# backend/tests/test_late_arrival_check_in.py
"""Late (missed first-night) arrivals must still be checkable in.

A multi-night guest who misses day one has not forfeited the stay: the front
desk must be able to welcome them on any later day of the booked range. These
tests pin both the derived queue and the rule the check-in service enforces,
so the restriction setting can never be widened to block late arrivals.
"""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from apps.accounts.models import User
from apps.bookings.models import Booking
from apps.bookings.services import booking_service
from apps.core.utils import hotel_today
from tests.base import BaseAPITestCase
from tests.factories import (hotel_settings, make_booking, make_guest, make_room,
                             make_room_type, make_staff)

LATE_URL = "/api/admin/bookings/late-arrivals/"


class LateArrivalQuerysetTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.today = hotel_today()
        self.rt = make_room_type(name="Standard", price="20000.00")
        self.rooms = [make_room(self.rt, n) for n in ("101", "102", "103", "104")]
        self.guest = make_guest()

    def booking(self, *, days_ago=2, nights=4, **kwargs):
        check_in = self.today - timedelta(days=days_ago)
        return make_booking(
            self.guest, self.rt, rooms=self.rooms[:1],
            check_in=check_in, check_out=check_in + timedelta(days=nights), **kwargs,
        )

    def refs(self):
        return {b.booking_reference
                for b in booking_service.late_arrival_bookings_queryset()}

    def test_confirmed_guest_inside_the_stay_is_listed(self):
        booking = self.booking()
        self.assertIn(booking.booking_reference, self.refs())

    def test_arrival_today_is_not_late_yet(self):
        booking = self.booking(days_ago=0)
        self.assertNotIn(booking.booking_reference, self.refs())

    def test_already_checked_in_is_not_listed(self):
        booking = self.booking(status=Booking.Status.CHECKED_IN)
        booking.checked_in_at = timezone.now()
        booking.save(update_fields=["checked_in_at"])
        self.assertNotIn(booking.booking_reference, self.refs())

    def test_stay_already_over_is_not_listed(self):
        booking = self.booking(days_ago=6, nights=4)
        self.assertNotIn(booking.booking_reference, self.refs())

    def test_last_day_of_the_stay_is_still_listed(self):
        """check_out == today still allows a walk-in; the rule is <= check_out."""
        booking = self.booking(days_ago=3, nights=3)
        self.assertEqual(booking.check_out, self.today)
        self.assertIn(booking.booking_reference, self.refs())

    def test_one_night_stays_are_excluded(self):
        """A single-night no-show has nothing left to check into."""
        booking = self.booking(days_ago=1, nights=1)
        self.assertNotIn(booking.booking_reference, self.refs())

    def test_cancelled_and_no_show_are_excluded(self):
        for status in (Booking.Status.CANCELLED, Booking.Status.NO_SHOW,
                       Booking.Status.EXPIRED, Booking.Status.PENDING):
            with self.subTest(status=status):
                booking = self.booking(status=status)
                self.assertNotIn(booking.booking_reference, self.refs())


class CanCheckInTodayTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.today = hotel_today()
        self.rt = make_room_type(name="Standard", price="20000.00")
        self.rooms = [make_room(self.rt, n) for n in ("201", "202")]
        self.guest = make_guest()

    def booking(self, *, start_offset, nights=4, **kwargs):
        check_in = self.today + timedelta(days=start_offset)
        return make_booking(
            self.guest, self.rt, rooms=self.rooms[:1],
            check_in=check_in, check_out=check_in + timedelta(days=nights), **kwargs,
        )

    def test_late_arrival_is_allowed_even_when_restriction_is_on(self):
        hotel_settings(restrict_check_in_to_booked_date=True)
        allowed, reason = booking_service.can_check_in_today(self.booking(start_offset=-2))
        self.assertTrue(allowed)
        self.assertEqual(reason, "")

    def test_on_time_arrival_is_allowed(self):
        hotel_settings(restrict_check_in_to_booked_date=True)
        allowed, _ = booking_service.can_check_in_today(self.booking(start_offset=0))
        self.assertTrue(allowed)

    def test_early_arrival_blocked_when_the_restriction_is_on(self):
        hotel_settings(restrict_check_in_to_booked_date=True)
        allowed, reason = booking_service.can_check_in_today(self.booking(start_offset=3))
        self.assertFalse(allowed)
        self.assertIn("scheduled", reason.lower())

    def test_early_arrival_allowed_when_the_restriction_is_off(self):
        hotel_settings(restrict_check_in_to_booked_date=False)
        allowed, _ = booking_service.can_check_in_today(self.booking(start_offset=3))
        self.assertTrue(allowed)

    def test_after_check_out_is_always_blocked(self):
        """Turning the restriction off relaxes EARLY arrivals only."""
        hotel_settings(restrict_check_in_to_booked_date=False)
        allowed, reason = booking_service.can_check_in_today(
            self.booking(start_offset=-10, nights=3))
        self.assertFalse(allowed)
        self.assertIn("no longer", reason.lower())

    def test_non_confirmed_bookings_are_blocked(self):
        allowed, reason = booking_service.can_check_in_today(
            self.booking(start_offset=-1, status=Booking.Status.PENDING))
        self.assertFalse(allowed)
        self.assertIn("CONFIRMED", reason)

    def test_already_checked_in_is_an_idempotent_yes(self):
        allowed, _ = booking_service.can_check_in_today(
            self.booking(start_offset=-1, status=Booking.Status.CHECKED_IN))
        self.assertTrue(allowed)


class LateArrivalEndpointTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.today = hotel_today()
        self.rt = make_room_type(name="Standard", price="20000.00")
        self.rooms = [make_room(self.rt, n) for n in ("301", "302")]
        self.guest = make_guest(first_name="Late", last_name="Guest")
        check_in = self.today - timedelta(days=2)
        self.booking = make_booking(
            self.guest, self.rt, rooms=self.rooms,
            check_in=check_in, check_out=check_in + timedelta(days=5),
            number_of_rooms=2, total="200000.00", amount_paid="150000.00",
        )
        self.staff = make_staff("desk@j1.test", role=User.Role.RECEPTIONIST)

    def rows(self, response):
        payload = response.data
        data = payload["data"] if isinstance(payload, dict) else payload
        return data["results"] if isinstance(data, dict) and "results" in data else data

    def test_endpoint_requires_staff(self):
        self.unauth()
        self.assertIn(self.client.get(LATE_URL).status_code, (401, 403))

    def test_row_carries_everything_the_desk_table_shows(self):
        self.auth(self.staff)
        rows = self.rows(self.client.get(LATE_URL))
        row = next(r for r in rows
                   if r["booking_reference"] == self.booking.booking_reference)
        self.assertEqual(row["nights_missed"], 2)
        self.assertEqual(row["nights"], 5)
        self.assertEqual(row["number_of_rooms"], 2)
        self.assertEqual(Decimal(row["amount_paid"]), Decimal("150000.00"))
        self.assertEqual(Decimal(row["total_amount"]), Decimal("200000.00"))
        self.assertTrue(row["can_check_in"])
        self.assertEqual(row["check_in_blocked_reason"], "")
        self.assertEqual(row["status"], Booking.Status.CONFIRMED)

    def test_checking_in_late_succeeds_and_clears_the_row(self):
        self.auth(self.staff)
        url = f"/api/admin/bookings/{self.booking.booking_reference}/check-in/"
        res = self.client.post(url, {}, format="json")
        self.assertEqual(res.status_code, 200)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, Booking.Status.CHECKED_IN)
        self.assertIsNotNone(self.booking.checked_in_at)
        refs = [r["booking_reference"] for r in self.rows(self.client.get(LATE_URL))]
        self.assertNotIn(self.booking.booking_reference, refs)

    def test_backend_rejects_an_early_check_in_the_ui_would_disable(self):
        hotel_settings(restrict_check_in_to_booked_date=True)
        check_in = self.today + timedelta(days=4)
        future = make_booking(self.guest, self.rt, rooms=self.rooms[:1],
                              check_in=check_in, check_out=check_in + timedelta(days=2))
        self.auth(self.staff)
        res = self.client.post(
            f"/api/admin/bookings/{future.booking_reference}/check-in/", {}, format="json")
        self.assertEqual(res.status_code, 409)
        future.refresh_from_db()
        self.assertEqual(future.status, Booking.Status.CONFIRMED)
