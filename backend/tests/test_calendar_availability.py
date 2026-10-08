# tests/test_calendar_availability.py
"""Calendar availability tests for GET /api/rooms/{slug}/unavailable-dates/.

The endpoint powers the room-type-aware booking calendar. Every scenario maps
to a real inventory question the guest's calendar must answer correctly:

* one booked room must NOT sell out the whole type (physical inventory),
* a date is unavailable only when EVERY sellable room is blocked,
* nights are half-open [check_in, check_out) — the checkout day is a valid
  new check-in,
* cancelled / expired reservations never block; live pending holds do,
* maintenance rooms are not sellable inventory.
"""
from datetime import timedelta

from django.utils import timezone

from apps.bookings.models import Booking
from apps.rooms.models import Room

from .base import BaseAPITestCase
from .factories import make_booking, make_guest, make_room, make_room_type


class CalendarAvailabilityTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.room_type = make_room_type("Superior", price="60000.00")
        self.room_101 = make_room(self.room_type, "101")
        self.room_102 = make_room(self.room_type, "102")
        self.room_103 = make_room(self.room_type, "103")
        self.guest = make_guest()
        self.today = timezone.localdate()

    def _calendar(self, slug=None, **params):
        slug = slug or self.room_type.slug
        return self.client.get(f"/api/rooms/{slug}/unavailable-dates/", params)

    def _dates(self, **params):
        response = self._calendar(**params)
        self.assertEqual(response.status_code, 200)
        return response.json()["data"]

    # -- Response shape -----------------------------------------------------

    def test_response_shape_and_complete_dates_map(self):
        start, end = self.today + timedelta(days=10), self.today + timedelta(days=13)
        data = self._dates(start_date=start.isoformat(), end_date=end.isoformat())
        self.assertEqual(data["room_type"]["slug"], self.room_type.slug)
        self.assertEqual(data["room_type"]["name"], "Superior")
        self.assertEqual(data["total_rooms"], 3)
        self.assertEqual(data["from"], start.isoformat())
        self.assertEqual(data["through"], (end - timedelta(days=1)).isoformat())
        # one entry per date in [start, end)
        self.assertEqual(list(data["dates"].keys()),
                         [(start + timedelta(days=i)).isoformat() for i in range(3)])
        self.assertTrue(all(v["available"] for v in data["dates"].values()))
        self.assertEqual(data["unavailable_dates"], [])

    def test_legacy_days_parameter_still_works(self):
        data = self._dates(days=10)
        self.assertEqual(data["from"], self.today.isoformat())
        self.assertEqual(data["through"], (self.today + timedelta(days=9)).isoformat())
        self.assertEqual(len(data["dates"]), 10)

    def test_lookup_by_numeric_id(self):
        data = self._dates(slug=str(self.room_type.pk), days=5)
        self.assertEqual(data["room_type"]["id"], self.room_type.pk)

    def test_unknown_room_type_is_404(self):
        response = self._calendar(slug="does-not-exist", days=5)
        self.assertEqual(response.status_code, 404)

    # -- Scenario A: one room booked, type still available -------------------

    def test_one_booked_room_does_not_sell_out_the_type(self):
        ci, co = self.today + timedelta(days=5), self.today + timedelta(days=8)
        make_booking(self.guest, self.room_type, rooms=[self.room_101], check_in=ci, check_out=co)
        data = self._dates(start_date=ci.isoformat(), end_date=co.isoformat())
        for day in (ci, ci + timedelta(days=1), ci + timedelta(days=2)):
            info = data["dates"][day.isoformat()]
            self.assertTrue(info["available"])
            self.assertEqual(info["available_rooms"], 2)
        self.assertEqual(data["unavailable_dates"], [])

    # -- Scenario B: every physical room booked → date unavailable ------------

    def test_all_rooms_booked_marks_nights_unavailable(self):
        ci, co = self.today + timedelta(days=5), self.today + timedelta(days=8)
        for room in (self.room_101, self.room_102, self.room_103):
            make_booking(self.guest, self.room_type, rooms=[room], check_in=ci, check_out=co)
        data = self._dates(start_date=ci.isoformat(), end_date=(co + timedelta(days=1)).isoformat())
        for day in (ci, ci + timedelta(days=1), ci + timedelta(days=2)):
            self.assertFalse(data["dates"][day.isoformat()]["available"])
        # The checkout day is requested too and must stay available.
        self.assertTrue(data["dates"][co.isoformat()]["available"])
        self.assertEqual(
            data["unavailable_dates"],
            [(ci + timedelta(days=i)).isoformat() for i in range(3)],
        )

    # -- Scenario C/D/E: half-open night boundaries ---------------------------

    def test_checkout_day_is_a_valid_checkin_for_the_next_guest(self):
        ci, co = self.today + timedelta(days=5), self.today + timedelta(days=8)
        for room in (self.room_101, self.room_102, self.room_103):
            make_booking(self.guest, self.room_type, rooms=[room], check_in=ci, check_out=co)
        data = self._dates(start_date=ci.isoformat(), end_date=co.isoformat())
        self.assertIn((co - timedelta(days=1)).isoformat(), data["unavailable_dates"])
        # co itself is outside [ci, co) so it is not even in this window;
        # request it explicitly and it must be free.
        data2 = self._dates(start_date=co.isoformat(), end_date=(co + timedelta(days=1)).isoformat())
        self.assertTrue(data2["dates"][co.isoformat()]["available"])
        self.assertEqual(data2["dates"][co.isoformat()]["available_rooms"], 3)

    def test_back_to_back_bookings_on_one_room(self):
        # 101 occupied 5→8 and 8→10; the merge must keep 101 blocked exactly
        # on nights 5..9 without double counting the shared boundary.
        first_ci = self.today + timedelta(days=5)
        make_booking(self.guest, self.room_type, rooms=[self.room_101],
                     check_in=first_ci, check_out=first_ci + timedelta(days=3))
        make_booking(self.guest, self.room_type, rooms=[self.room_101],
                     check_in=first_ci + timedelta(days=3), check_out=first_ci + timedelta(days=5))
        data = self._dates(start_date=first_ci.isoformat(),
                           end_date=(first_ci + timedelta(days=6)).isoformat())
        for i in range(5):
            self.assertEqual(data["dates"][(first_ci + timedelta(days=i)).isoformat()]["available_rooms"], 2)
        self.assertEqual(data["dates"][(first_ci + timedelta(days=5)).isoformat()]["available_rooms"], 3)

    # -- Scenario F/G/H: reservation statuses ---------------------------------

    def test_cancelled_booking_does_not_block(self):
        ci, co = self.today + timedelta(days=5), self.today + timedelta(days=8)
        for room in (self.room_101, self.room_102, self.room_103):
            make_booking(self.guest, self.room_type, rooms=[room],
                         check_in=ci, check_out=co, status=Booking.Status.CANCELLED)
        data = self._dates(start_date=ci.isoformat(), end_date=co.isoformat())
        self.assertEqual(data["unavailable_dates"], [])
        self.assertTrue(all(v["available_rooms"] == 3 for v in data["dates"].values()))

    def test_expired_pending_hold_does_not_block(self):
        ci, co = self.today + timedelta(days=5), self.today + timedelta(days=8)
        for room in (self.room_101, self.room_102, self.room_103):
            make_booking(self.guest, self.room_type, rooms=[room],
                         check_in=ci, check_out=co, status=Booking.Status.PENDING,
                         expires_at=timezone.now() - timedelta(minutes=1))
        data = self._dates(start_date=ci.isoformat(), end_date=co.isoformat())
        self.assertEqual(data["unavailable_dates"], [])

    def test_live_pending_hold_blocks(self):
        ci, co = self.today + timedelta(days=5), self.today + timedelta(days=8)
        for room in (self.room_101, self.room_102):
            make_booking(self.guest, self.room_type, rooms=[room], check_in=ci, check_out=co)
        make_booking(self.guest, self.room_type, rooms=[self.room_103],
                     check_in=ci, check_out=co, status=Booking.Status.PENDING,
                     expires_at=timezone.now() + timedelta(minutes=15))
        data = self._dates(start_date=ci.isoformat(), end_date=co.isoformat())
        self.assertEqual(data["unavailable_dates"],
                         [(ci + timedelta(days=i)).isoformat() for i in range(3)])

    def test_checked_out_and_no_show_do_not_block(self):
        ci, co = self.today + timedelta(days=5), self.today + timedelta(days=8)
        for i, room in enumerate((self.room_101, self.room_102, self.room_103)):
            status = Booking.Status.CHECKED_OUT if i == 0 else Booking.Status.NO_SHOW
            make_booking(self.guest, self.room_type, rooms=[room],
                         check_in=ci, check_out=co, status=status)
        data = self._dates(start_date=ci.isoformat(), end_date=co.isoformat())
        self.assertEqual(data["unavailable_dates"], [])

    # -- Maintenance inventory ------------------------------------------------

    def test_maintenance_rooms_are_not_sellable_inventory(self):
        self.room_103.status = Room.Status.MAINTENANCE
        self.room_103.save()
        ci, co = self.today + timedelta(days=5), self.today + timedelta(days=8)
        # Only two sellable rooms; booking both sells the type out.
        make_booking(self.guest, self.room_type, rooms=[self.room_101], check_in=ci, check_out=co)
        make_booking(self.guest, self.room_type, rooms=[self.room_102], check_in=ci, check_out=co)
        data = self._dates(start_date=ci.isoformat(), end_date=co.isoformat())
        self.assertEqual(data["total_rooms"], 2)
        self.assertEqual(data["unavailable_dates"],
                         [(ci + timedelta(days=i)).isoformat() for i in range(3)])

    def test_type_with_no_sellable_rooms_is_fully_unavailable(self):
        for room in (self.room_101, self.room_102, self.room_103):
            room.status = Room.Status.OUT_OF_SERVICE
            room.save()
        data = self._dates(days=5)
        self.assertEqual(data["total_rooms"], 0)
        self.assertEqual(len(data["unavailable_dates"]), 5)

    # -- Parameter validation --------------------------------------------------

    def test_start_without_end_is_rejected(self):
        response = self._calendar(start_date=self.today.isoformat())
        self.assertEqual(response.status_code, 400)
        self.assertIn("start_date", response.json()["errors"])

    def test_malformed_dates_are_rejected(self):
        response = self._calendar(start_date="18-09-2026", end_date="2026-09-20")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "VALIDATION_ERROR")

    def test_end_before_start_is_rejected(self):
        response = self._calendar(start_date="2026-09-10", end_date="2026-09-01")
        self.assertEqual(response.status_code, 400)
        self.assertIn("end_date", response.json()["errors"])

    def test_window_larger_than_cap_is_rejected(self):
        response = self._calendar(start_date="2026-01-01", end_date="2027-06-01")
        self.assertEqual(response.status_code, 400)
        self.assertIn("end_date", response.json()["errors"])

    def test_non_numeric_days_is_rejected(self):
        response = self._calendar(days="soon")
        self.assertEqual(response.status_code, 400)
        self.assertIn("days", response.json()["errors"])
