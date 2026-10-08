# tests/test_occupancy_and_missed.py
"""Occupancy calendar, missed (no-show) bookings and rescheduling.

These three staff features all derive their state from the booking/room
assignment tables, never from a client-supplied flag, so the pins below are
about DERIVATION correctness:

* the calendar shows one entry per physical room per night, so a multi-room
  booking appears in every one of its rooms;
* a stay occupies [check_in, check_out) — the departure day is free again;
* cancelled/expired/no-show bookings never occupy a room;
* the missed list only contains bookings whose arrival deadline passed with no
  check-in, and a checked-in guest can never appear;
* a reschedule keeps the same booking (and its payments), re-checks
  availability, and re-prices on the backend.
"""
from datetime import timedelta
from decimal import Decimal

from apps.accounts.models import User
from apps.bookings.models import Booking, BookingRoom
from apps.bookings.services import booking_service
from apps.core.utils import hotel_today

from .base import BaseAPITestCase
from .factories import (hotel_settings, make_booking, make_guest, make_room,
                        make_room_type, make_staff)

CALENDAR = "/api/admin/bookings/calendar/"
MISSED = "/api/admin/bookings/missed/"


class OccupancyCalendarTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.staff = make_staff("cal.desk@staff.dev", role=User.Role.RECEPTIONIST)
        self.auth(self.staff)
        self.room_type = make_room_type("Calendar Deluxe", price="40000.00", max_guests=4)
        self.r1 = make_room(self.room_type, "C101")
        self.r2 = make_room(self.room_type, "C102")
        self.r3 = make_room(self.room_type, "C103")
        self.guest = make_guest("cal.guest@test.dev")
        self.today = hotel_today()
        # A 3-night stay starting the 10th of next month keeps the whole stay
        # inside one calendar month regardless of "today".
        self.start = (self.today.replace(day=1) + timedelta(days=40)).replace(day=10)
        self.end = self.start + timedelta(days=3)

    def get(self, **params):
        params.setdefault("year", self.start.year)
        params.setdefault("month", self.start.month)
        return self.client.get(CALENDAR, params)

    def day(self, payload, date):
        return payload["data"]["days"].get(date.isoformat(), [])

    def test_multi_room_booking_appears_once_per_room_per_night(self):
        make_booking(self.guest, self.room_type, rooms=[self.r1, self.r2],
                     check_in=self.start, check_out=self.end, number_of_rooms=2)
        res = self.get()
        self.assertEqual(res.status_code, 200, res.json())
        for offset in range(3):
            date = self.start + timedelta(days=offset)
            entries = self.day(res.json(), date)
            self.assertEqual(
                sorted(e["room_number"] for e in entries), ["C101", "C102"],
                f"both rooms must be occupied on {date}",
            )

    def test_checkout_day_is_free_again(self):
        make_booking(self.guest, self.room_type, rooms=[self.r1],
                     check_in=self.start, check_out=self.end)
        payload = self.get().json()
        self.assertTrue(self.day(payload, self.end - timedelta(days=1)))
        self.assertEqual(self.day(payload, self.end), [])

    def test_cancelled_expired_and_no_show_bookings_never_occupy_a_room(self):
        for status in (Booking.Status.CANCELLED, Booking.Status.EXPIRED,
                       Booking.Status.NO_SHOW):
            with self.subTest(status=status):
                Booking.objects.all().delete()
                make_booking(self.guest, self.room_type, rooms=[self.r1],
                             check_in=self.start, check_out=self.end, status=status)
                self.assertEqual(self.day(self.get().json(), self.start), [])

    def test_checked_in_and_checked_out_stays_are_shown(self):
        for status in (Booking.Status.CHECKED_IN, Booking.Status.CHECKED_OUT):
            with self.subTest(status=status):
                Booking.objects.all().delete()
                make_booking(self.guest, self.room_type, rooms=[self.r1],
                             check_in=self.start, check_out=self.end, status=status)
                entries = self.day(self.get().json(), self.start)
                self.assertEqual(len(entries), 1)
                self.assertEqual(entries[0]["status"], status)

    def test_entries_carry_what_the_modal_needs(self):
        booking = make_booking(self.guest, self.room_type, rooms=[self.r1],
                               check_in=self.start, check_out=self.end)
        entry = self.day(self.get().json(), self.start)[0]
        self.assertEqual(entry["booking_reference"], booking.booking_reference)
        self.assertEqual(entry["guest_name"], self.guest.full_name)
        self.assertEqual(entry["room_number"], "C101")
        self.assertEqual(entry["room_type_name"], self.room_type.name)
        self.assertEqual(entry["booking_id"], booking.pk)
        self.assertEqual(entry["check_in"], self.start.isoformat())

    def test_stay_spanning_a_month_boundary_is_clipped_to_the_requested_month(self):
        first_of_month = self.start.replace(day=1)
        spanning_start = first_of_month - timedelta(days=2)
        make_booking(self.guest, self.room_type, rooms=[self.r1],
                     check_in=spanning_start, check_out=first_of_month + timedelta(days=2))
        payload = self.get().json()
        self.assertEqual(payload["data"]["start_date"], first_of_month.isoformat())
        self.assertTrue(self.day(payload, first_of_month))
        # Nothing outside the requested month leaks into the response.
        for key in payload["data"]["days"]:
            self.assertTrue(key.startswith(f"{self.start.year:04d}-{self.start.month:02d}"))

    def test_month_is_validated(self):
        self.assertEqual(self.client.get(CALENDAR, {"month": 13, "year": 2026}).status_code, 400)
        self.assertEqual(self.client.get(CALENDAR, {"month": "abc"}).status_code, 400)

    def test_calendar_is_one_query_regardless_of_booking_count(self):
        for i in range(6):
            guest = make_guest(f"cal-load-{i}@test.dev")
            make_booking(guest, self.room_type, rooms=[self.r3],
                         check_in=self.start + timedelta(days=i * 2),
                         check_out=self.start + timedelta(days=i * 2 + 1))
        with self.assertNumQueries(1):
            booking_service.occupancy_calendar(year=self.start.year, month=self.start.month)

    def test_guest_account_cannot_read_the_calendar(self):
        self.unauth()
        self.assertIn(self.client.get(CALENDAR).status_code, (401, 403))


class MissedBookingTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        hotel_settings(cancellation_fee_percent=Decimal("10.00"))
        self.staff = make_staff("missed.desk@staff.dev", role=User.Role.RECEPTIONIST)
        self.auth(self.staff)
        self.room_type = make_room_type("Missed Standard", price="20000.00")
        self.room = make_room(self.room_type, "M201")
        self.guest = make_guest("missed.guest@test.dev")
        self.today = hotel_today()

    def past_booking(self, *, days_ago=3, status=Booking.Status.CONFIRMED, **extra):
        return make_booking(
            self.guest, self.room_type, rooms=[self.room],
            check_in=self.today - timedelta(days=days_ago),
            check_out=self.today - timedelta(days=days_ago - 2),
            status=status, **extra,
        )

    def refs(self, res):
        return {row["booking_reference"] for row in res.json()["data"]}

    def test_confirmed_booking_with_no_arrival_is_listed(self):
        booking = self.past_booking()
        res = self.client.get(MISSED)
        self.assertEqual(res.status_code, 200, res.json())
        self.assertIn(booking.booking_reference, self.refs(res))

    def test_checked_in_guest_is_never_listed(self):
        booking = self.past_booking(status=Booking.Status.CHECKED_IN)
        booking.checked_in_at = booking.created_at
        booking.save(update_fields=["checked_in_at"])
        self.assertNotIn(booking.booking_reference, self.refs(self.client.get(MISSED)))

    def test_cancelled_and_future_bookings_are_excluded(self):
        cancelled = self.past_booking(status=Booking.Status.CANCELLED)
        future = make_booking(self.guest, self.room_type, rooms=[self.room],
                              check_in=self.today + timedelta(days=5),
                              check_out=self.today + timedelta(days=7))
        listed = self.refs(self.client.get(MISSED))
        self.assertNotIn(cancelled.booking_reference, listed)
        self.assertNotIn(future.booking_reference, listed)

    def test_already_flagged_no_show_stays_visible(self):
        booking = self.past_booking(status=Booking.Status.NO_SHOW)
        self.assertIn(booking.booking_reference, self.refs(self.client.get(MISSED)))

    def test_row_exposes_the_refundable_amount_net_of_the_cancellation_fee(self):
        booking = self.past_booking(amount_paid="20000.00", total="20000.00")
        row = next(r for r in self.client.get(MISSED).json()["data"]
                   if r["booking_reference"] == booking.booking_reference)
        self.assertEqual(Decimal(row["cancellation_fee"]), Decimal("2000.00"))
        self.assertEqual(Decimal(row["refundable_amount"]), Decimal("18000.00"))

    def test_list_is_paginated_and_searchable(self):
        booking = self.past_booking()
        res = self.client.get(MISSED, {"search": self.guest.email})
        self.assertIn("pagination", res.json())
        self.assertIn(booking.booking_reference, self.refs(res))
        empty = self.client.get(MISSED, {"search": "nobody@nowhere.dev"})
        self.assertEqual(empty.json()["data"], [])

    def test_anonymous_access_is_refused(self):
        self.unauth()
        self.assertIn(self.client.get(MISSED).status_code, (401, 403))


class RescheduleBookingTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        hotel_settings(tax_rate_percent=Decimal("0.00"), service_fee=Decimal("0.00"))
        self.manager = make_staff("resched.manager@staff.dev", role=User.Role.MANAGER)
        self.receptionist = make_staff("resched.desk@staff.dev", role=User.Role.RECEPTIONIST)
        self.auth(self.manager)
        self.room_type = make_room_type("Reschedule King", price="50000.00", max_guests=4)
        self.r1 = make_room(self.room_type, "R301")
        self.r2 = make_room(self.room_type, "R302")
        self.guest = make_guest("resched.guest@test.dev")
        self.today = hotel_today()
        self.booking = make_booking(
            self.guest, self.room_type, rooms=[self.r1],
            check_in=self.today - timedelta(days=2),
            check_out=self.today - timedelta(days=1),
            status=Booking.Status.NO_SHOW, amount_paid="50000.00", total="50000.00",
        )

    def url(self, booking=None):
        return f"/api/admin/bookings/{(booking or self.booking).booking_reference}/reschedule/"

    def post(self, *, days_out=14, nights=1, booking=None):
        check_in = self.today + timedelta(days=days_out)
        return self.client.post(self.url(booking), {
            "check_in": check_in.isoformat(),
            "check_out": (check_in + timedelta(days=nights)).isoformat(),
        }, format="json")

    def test_reschedule_moves_the_same_booking_and_keeps_payments(self):
        payments_before = self.booking.amount_paid
        res = self.post()
        self.assertEqual(res.status_code, 200, res.json())
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.check_in, self.today + timedelta(days=14))
        self.assertEqual(self.booking.status, Booking.Status.CONFIRMED)
        self.assertEqual(self.booking.amount_paid, payments_before)
        self.assertEqual(Booking.objects.count(), 1, "no duplicate booking is created")

    def test_rooms_are_reassigned_for_the_new_dates(self):
        self.post()
        assignments = BookingRoom.objects.filter(booking=self.booking)
        self.assertEqual(assignments.count(), 1)
        assignment = assignments.first()
        self.assertEqual(assignment.check_in, self.today + timedelta(days=14))
        self.assertEqual(assignment.check_out, self.today + timedelta(days=15))

    def test_reschedule_is_refused_when_no_room_is_free(self):
        other = make_guest("resched.blocker@test.dev")
        target = self.today + timedelta(days=21)
        for room in (self.r1, self.r2):
            make_booking(other, self.room_type, rooms=[room],
                         check_in=target, check_out=target + timedelta(days=2))
        res = self.post(days_out=21)
        self.assertEqual(res.status_code, 409, res.json())
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, Booking.Status.NO_SHOW)

    def test_longer_stay_is_repriced_and_reports_the_balance_due(self):
        res = self.post(nights=3)
        self.assertEqual(res.status_code, 200, res.json())
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.total_amount, Decimal("150000.00"))
        self.assertEqual(Decimal(res.json()["data"]["reschedule"]["balance_due"]),
                         Decimal("100000.00"))

    def test_identical_dates_are_rejected(self):
        check_in = self.today + timedelta(days=9)
        self.client.post(self.url(), {
            "check_in": check_in.isoformat(),
            "check_out": (check_in + timedelta(days=1)).isoformat(),
        }, format="json")
        repeat = self.client.post(self.url(), {
            "check_in": check_in.isoformat(),
            "check_out": (check_in + timedelta(days=1)).isoformat(),
        }, format="json")
        self.assertEqual(repeat.status_code, 400, repeat.json())

    def test_checkout_before_checkin_is_rejected(self):
        check_in = self.today + timedelta(days=14)
        res = self.client.post(self.url(), {
            "check_in": check_in.isoformat(),
            "check_out": (check_in - timedelta(days=1)).isoformat(),
        }, format="json")
        self.assertEqual(res.status_code, 400, res.json())

    def test_cancelled_booking_cannot_be_rescheduled(self):
        self.booking.status = Booking.Status.CANCELLED
        self.booking.save(update_fields=["status"])
        res = self.post()
        # Project-wide contract: an illegal state transition is 409, not 400.
        self.assertEqual(res.status_code, 409, res.json())
        self.assertEqual(res.json()["code"], "INVALID_BOOKING_STATE")

    def test_receptionist_cannot_reschedule(self):
        self.auth(self.receptionist)
        self.assertEqual(self.post().status_code, 403)

    def test_reschedule_is_audited(self):
        from apps.audit.models import AuditLog

        self.post()
        entry = AuditLog.objects.filter(action="BOOKING_RESCHEDULED").first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.actor, self.manager)
