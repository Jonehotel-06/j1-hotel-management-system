# tests/test_multi_room_booking.py
"""Multi-room booking tests: the same room type, booked in multiples.

The frontend now sends an explicit ``rooms`` quantity for the whole guest
journey (availability → quote → create), so these tests pin the AUTHORITATIVE
inventory guarantees that make that safe:

* creation consumes exactly ``rooms`` physical rooms of that type;
* pricing is per-night × nights × rooms, backend-computed only;
* every other room type / date stays untouched;
* failing to satisfy the full quantity fails LOUDLY (HTTP 409) and leaves
  nothing behind — the count is never silently downgraded;
* two racing requests for the last rooms cannot overlap → exactly one winner.

SQLite note: the in-memory test database. SQLite has no row locking, so
Django's ``select_for_update`` in ``create_booking`` is a no-op here and the
two racing requests serialise on the SHARED table lock instead — the losing
request ends in ``OperationalError: database table is locked`` (500) rather
than the DomainError → 409 contract MySQL produces. Either way the invariant
under test is identical and asserted for every engine: NO OVERBOOKING and at
most one winner; the stay is afterwards provably fully booked (a sequential
attempt gets the honest 409 + ROOM_UNAVAILABLE contract error).
"""
import threading
from datetime import timedelta

from django.test import tag

from apps.bookings.models import Booking, BookingRoom
from apps.core.utils import hotel_today
from tests.base import BaseAPITestCase
from tests.factories import make_booking, make_guest, make_room, make_room_type

# Relative to the hotel's today: fixed literals silently rot into "check-in in
# the past" and fail the whole module once that date passes.
CI = (hotel_today() + timedelta(days=10)).isoformat()
CO = (hotel_today() + timedelta(days=13)).isoformat()   # 3 hotel nights
GUEST = {
    "first_name": "Multi",
    "last_name": "Room",
    "email": "multi.room@example.com",
    "phone": "+2348012345678",
}


class MultiRoomBookingTests(BaseAPITestCase):
    """Case scenarios on a hotel with exactly 5 physical rooms of one type."""

    def setUp(self):
        super().setUp()
        self.rt = make_room_type(name="Multi Deluxe", price="8000.00", slug="multi-deluxe")
        self.rooms = [make_room(self.rt, "M10%d" % (i + 1)) for i in range(5)]

    # -- helpers -------------------------------------------------------------
    def book(self, *, rooms, key):
        return self.client.post(
            "/api/bookings/",
            {
                "room_type": self.rt.slug,
                "check_in": CI,
                "check_out": CO,
                "rooms": rooms,
                "adults": rooms + 1,
                "children": 0,
                "guest": dict(GUEST),
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY=key,
        )

    def occupy(self, physical_rooms):
        """Block an overlapping stay on the given physical rooms."""
        return make_booking(
            make_guest(email="blocker-%d@example.com" % Booking.objects.count()),
            self.rt, rooms=physical_rooms, check_in=CI, check_out=CO,
            number_of_rooms=len(physical_rooms),
        )

    # -- cases ---------------------------------------------------------------
    def test_case1_guest_takes_2_of_5(self):
        """2 of 5 available → succeeds, consumes EXACTLY 2 rooms of the type."""
        res = self.book(rooms=2, key="multi-2of5-0001")
        self.assertEqual(res.status_code, 201, res.json())
        body = res.json()["data"]

        b = Booking.objects.get(booking_reference=body["booking_reference"])
        self.assertEqual(b.number_of_rooms, 2)
        self.assertEqual(b.room_type, self.rt)

        assignments = b.room_assignments.all()
        self.assertEqual(assignments.count(), 2)                         # one row per physical room
        self.assertEqual(len({a.room_id for a in assignments}), 2)       # distinct rooms
        self.assertEqual({a.room_id for a in assignments} <= {aroom.pk for aroom in self.rooms}, True)

        # Other rooms of the type remain free; nothing else was touched.
        remaining = BookingRoom.objects.exclude(booking=b).count()
        self.assertEqual(remaining, 0)

        # Backend-computed pricing: per-night × nights × rooms.
        self.assertEqual(str(b.total_amount), "48000.00")                # 8000 × 3 × 2

    def test_case2_block_3_leave_2_guest_takes_2_of_2(self):
        """3 rooms pre-booked + request 2 → succeeds on the 2 remaining."""
        blocker = make_booking(make_guest(email="block3@example.com"), self.rt,
                               rooms=self.rooms[:3], check_in=CI, check_out=CO, number_of_rooms=3)

        res = self.book(rooms=2, key="multi-2of2-0002")
        self.assertEqual(res.status_code, 201, res.json())
        b = Booking.objects.get(booking_reference=res.json()["data"]["booking_reference"])
        assigned = {a.room_id for a in b.room_assignments.all()}
        self.assertEqual(assigned, {self.rooms[3].pk, self.rooms[4].pk})  # exactly the free two

    def test_case3_four_blocked_two_requested_fails_clearly(self):
        """4 blocked, ask for 2 → loud 409, nothing created, nothing changed."""
        blocker = make_booking(make_guest(email="block4@example.com"), self.rt,
                               rooms=self.rooms[:4], check_in=CI, check_out=CO, number_of_rooms=4)

        res = self.book(rooms=2, key="multi-need2-0003")
        self.assertEqual(res.status_code, 409, res.json())
        body = res.json()
        self.assertEqual(body.get("success"), False)
        self.assertEqual(body.get("code"), "ROOM_UNAVAILABLE")
        # Clear, actionable overbooking message stating the real remaining
        # stock — never a silent quantity downgrade.
        self.assertIn("Only 1 room", body.get("message", ""))

        # Atomic rollback: no booking, no assignment rows.
        self.assertEqual(Booking.objects.filter(guest__email=GUEST["email"]).count(), 0)
        self.assertEqual(BookingRoom.objects.count(), 4)                 # only the blocker's rows
        self.assertEqual(Booking.objects.count(), 1)                     # only the blocker

    def test_case4_guest_takes_all_5(self):
        """Exactly-5 request succeeds and exhausts the type for those dates."""
        res = self.book(rooms=5, key="multi-5of5-0004")
        self.assertEqual(res.status_code, 201, res.json())
        b = Booking.objects.get(booking_reference=res.json()["data"]["booking_reference"])
        self.assertEqual(b.room_assignments.count(), 5)
        self.assertEqual(str(b.total_amount), "120000.00")               # 8000 × 3 × 5

        # The type is now fully booked for the range — one more must 409.
        res2 = self.book(rooms=1, key="multi-plus1-0005")
        self.assertEqual(res2.status_code, 409, res2.json())
        self.assertEqual(res2.json().get("code"), "ROOM_UNAVAILABLE")

    def test_case5_guest_takes_6_of_5_fails(self):
        """6 requested from 5 → loud 409; never downgraded to 5-for-6."""
        res = self.book(rooms=6, key="multi-6of5-0006")
        self.assertEqual(res.status_code, 409, res.json())
        self.assertIn("Only 5 room", res.json().get("message", ""))
        self.assertEqual(Booking.objects.count(), 0)
        self.assertEqual(BookingRoom.objects.count(), 0)


@tag("concurrency")
class OverbookingRaceTests(BaseAPITestCase):
    """Case 6: two racing requests must not consume the same last room.

    The service locks the room-type row (``select_for_update``) and
    re-computes availability inside the transaction before assigning rooms,
    so the second racer sees the first racer's committed state and fails with
    RoomUnavailableError → 409 ROOM_UNAVAILABLE (production MySQL behaviour).

    The ABSOLUTE safety invariant asserted for every engine: no physical room
    ever serves two overlapping bookings from this race (no overbooking), and
    at most one winner is created.

    Engine notes: SQLite (this sandbox/CI DB) verifies the invariant by
    serialising writers itself — a racer may surface its lock-busy outcome as
    a 5xx ("database table is locked") and create NOTHING. That is the same
    safe failure (no record, no overbooking), surfaced differently; the
    strict "exactly one 201" semantics apply on engines with row locking."""

    def setUp(self):
        super().setUp()
        self.rt = make_room_type(name="Race King", price="5000.00", slug="race-king")
        self.room = make_room(self.rt, "RC1")
        self.ci = (hotel_today() + timedelta(days=21)).isoformat()
        self.co = (hotel_today() + timedelta(days=23)).isoformat()

    def test_two_racers_one_room_no_overbooking(self):
        payload = {
            "room_type": self.rt.slug,
            "check_in": self.ci,
            "check_out": self.co,
            "rooms": 1,
            "adults": 2,
            "children": 0,
        }
        results = []
        barrier = threading.Barrier(2)

        def attempt(key):
            barrier.wait(timeout=20)
            res = self.client.post(
                "/api/bookings/", dict(payload, guest=dict(GUEST)), format="json",
                HTTP_IDEMPOTENCY_KEY=key,
            )
            results.append((res.status_code, res.json()))

        threads = [
            threading.Thread(target=attempt, args=("race-winner-001",), daemon=True),
            threading.Thread(target=attempt, args=("race-winner-002",), daemon=True),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        codes = [r[0] for r in results]
        winners = [r for r in results if r[0] == 201]

        # ---- ABSOLUTE invariant (every engine): no overbooking, ≤1 winner.
        self.assertLessEqual(len(winners), 1, f"at most one winner allowed, got {codes}")
        self.assertLessEqual(Booking.objects.count(), 1)
        self.assertLessEqual(
            BookingRoom.objects.filter(room=self.room, check_in__lt=self.co, check_out__gt=self.ci).count(),
            1,
        )

        from django.db import connection
        if connection.vendor != "sqlite":
            # Row-locking engines (production MySQL): exactly one winner and
            # the loser gets the honest 409 + ROOM_UNAVAILABLE contract error.
            self.assertEqual(len(winners), 1, f"exactly one racer may win, got {codes}")
            losers = [r for r in results if r[0] != 201]
            self.assertEqual(len(losers), 1)
            self.assertEqual(losers[0][0], 409)
            self.assertEqual(losers[0][1].get("code"), "ROOM_UNAVAILABLE")
        else:
            # SQLite shared-cache CI: writer serialisation shows up as either
            # a 201+409/500 pair or (under contention) two failed attempts.
            # Either way the hotel stays consistent and the stay must remain
            # bookable immediately afterwards — prove recovery with a
            # sequential request.
            if Booking.objects.count() == 0:
                res = self.client.post(
                    "/api/bookings/",
                    dict(payload, guest=dict(GUEST)), format="json",
                    HTTP_IDEMPOTENCY_KEY="race-recovery-001",
                )
                self.assertEqual(res.status_code, 201, res.json())
            # One more concurrent-style attempt at the fully booked stay must
            # fail — never a silent downgrade, never another winner.
            res = self.client.post(
                "/api/bookings/",
                dict(payload, guest=dict(GUEST)), format="json",
                HTTP_IDEMPOTENCY_KEY="race-afterward-001",
            )
            self.assertEqual(res.status_code, 409, res.json())
            self.assertEqual(res.json().get("code"), "ROOM_UNAVAILABLE")
            self.assertEqual(Booking.objects.count(), 1)
            self.assertLessEqual(
                BookingRoom.objects.filter(room=self.room, check_in__lt=self.co, check_out__gt=self.ci).count(),
                1,
            )
