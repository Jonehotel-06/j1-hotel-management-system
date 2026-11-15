"""Booking the FIRST room of a room type — the reported failure.

Reported symptom (verbatim from the desk):

    "When a guest attempts to book the first room in any room type the booking
     fails with 'The selected room is occupied', even though the room is free."

Root cause (reproduced by this module before the fix):
``_resolve_exact_room`` refused ANY walk-in/phone booking of a room whose
``Room.status`` flag was ``OCCUPIED``, without ever looking at the requested
dates. ``OCCUPIED`` means "a guest is physically in this room right now"; it is
not a reservation and must never override the booking calendar. Rooms are
allocated in ``room_number`` order, so the FIRST room of each type is the one
carrying that flag after a busy night — which is why the failure looked like
"the first room in every room type is occupied" while the availability calendar
answered "available".

Every scenario in the audit brief is pinned here:

 1. booking the first room in a room type;
 2. the second room;
 3. the last room;
 4. the first room of several different room types;
 5. a genuinely occupied room is still refused;
 6. an available room with existing reservations on non-overlapping dates;
 7. a room whose current guest checks out on the selected check-in date;
 8. multiple rooms in one booking;
 9. a booking after an expired pending hold;
10. two guests racing for the same room (no double booking);
11. refreshing availability before checkout (availability and creation agree);
12. completing a public (website) booking of an exact room.

The invariant behind all of them: **the availability answer and the
booking-creation answer are the same answer.**
"""
from datetime import timedelta

from django.utils import timezone

from apps.bookings.models import Booking, BookingRoom
from apps.bookings.services import availability
from apps.core.utils import hotel_today
from apps.rooms.models import Room
from tests.base import BaseAPITestCase
from tests.factories import (
    hotel_settings, make_booking, make_guest, make_room, make_room_type, make_staff,
)

TODAY = hotel_today()
CI = TODAY + timedelta(days=10)
CO = TODAY + timedelta(days=13)


class FirstRoomBookingTests(BaseAPITestCase):
    """One room type with three rooms; the first room is "301"."""

    def setUp(self):
        super().setUp()
        # No tax/fee noise: the assertions below are about inventory, not money.
        hotel_settings(tax_rate="0.00", service_fee="0.00", pending_booking_minutes=30)
        self.room_type = make_room_type("First Room Standard", price="15000.00", max_guests=2)
        self.first = make_room(self.room_type, "301")
        self.second = make_room(self.room_type, "302")
        self.last = make_room(self.room_type, "303")
        self.staff = make_staff("first-room-desk@jone.test")
        self.auth(self.staff)

    # ------------------------------ helpers ---------------------------------
    def walk_in(self, **overrides):
        payload = {
            "room_type": self.room_type.slug,
            "check_in": CI.isoformat(),
            "check_out": CO.isoformat(),
            "rooms": 1,
            "adults": 2,
            "children": 0,
            "source": "WALK_IN",
            "status": "CONFIRMED",
            "guest": {
                "first_name": "Ngozi",
                "last_name": "Eze",
                "email": "ngozi.firstroom@jone.test",
                "phone": "08031234567",
            },
        }
        payload.update(overrides)
        return self.client.post("/api/admin/bookings/", payload, format="json")

    def public_booking(self, **overrides):
        self.unauth()   # a real website visitor is anonymous; re-authenticated below
        payload = {
            "room_type": self.room_type.slug,
            "check_in": CI.isoformat(),
            "check_out": CO.isoformat(),
            "rooms": 1,
            "adults": 2,
            "children": 0,
            "guest": {
                "first_name": "Website",
                "last_name": "Guest",
                "email": "website.firstroom@jone.test",
                "phone": "08031234568",
            },
        }
        payload.update(overrides)
        response = self.client.post("/api/bookings/", payload, format="json")
        self.auth(self.staff)     # restore the desk session for the next call
        return response

    def assigned_room_ids(self, response):
        ref = response.json()["data"]["booking_reference"]
        return set(
            Booking.objects.get(booking_reference=ref)
            .room_assignments.values_list("room_id", flat=True)
        )

    def block(self, room, check_in=CI, check_out=CO, status=Booking.Status.CONFIRMED):
        return make_booking(
            make_guest(email="blocker-%d@jone.test" % Booking.objects.count()),
            self.room_type, rooms=[room], check_in=check_in, check_out=check_out,
            number_of_rooms=1, status=status,
        )

    def public_room_availability(self, check_in=CI, check_out=CO):
        """{room_id: available} exactly as the public room picker sees it.

        The endpoint is AllowAny, so the authenticated desk session may read it
        too — which keeps this helper side-effect free for the caller.
        """
        res = self.client.get(
            f"/api/rooms/{self.room_type.slug}/rooms/",
            {"check_in": check_in.isoformat(), "check_out": check_out.isoformat()},
        )
        self.assertEqual(res.status_code, 200, res.content)
        rows = res.json()["data"]
        if isinstance(rows, dict):
            rows = rows.get("results", [])
        return {row["id"]: row["available"] for row in rows}

    # --------------------------- 1/2/3: order -------------------------------
    def test_case1_first_room_can_be_booked(self):
        res = self.walk_in(room_id=self.first.pk)
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(self.assigned_room_ids(res), {self.first.pk})

    def test_case2_second_room_can_be_booked(self):
        res = self.walk_in(room_id=self.second.pk)
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(self.assigned_room_ids(res), {self.second.pk})

    def test_case3_last_room_can_be_booked(self):
        res = self.walk_in(room_id=self.last.pk)
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(self.assigned_room_ids(res), {self.last.pk})

    def test_case4_first_room_of_every_room_type(self):
        """The reported shape: EVERY type failed on its first room."""
        for index in range(3):
            room_type = make_room_type(f"Type {index}", price="20000.00", max_guests=2)
            first_room = make_room(room_type, f"4{index}1")
            make_room(room_type, f"4{index}2")
            res = self.walk_in(room_type=room_type.slug, room_id=first_room.pk,
                               guest={
                                   "first_name": "Type", "last_name": str(index),
                                   "email": f"type{index}@jone.test",
                                   "phone": "08031234569",
                               })
            self.assertEqual(res.status_code, 201, res.content)
            self.assertEqual(self.assigned_room_ids(res), {first_room.pk})

    # ------------------ stale OCCUPIED flag (the actual bug) ----------------
    def test_stale_occupied_flag_does_not_block_a_future_stay(self):
        """A room left flagged OCCUPIED, with no reservation, is bookable."""
        self.first.status = Room.Status.OCCUPIED
        self.first.save(update_fields=["status"])
        # The availability calendar already said "available"…
        self.assertTrue(self.public_room_availability()[self.first.pk])
        # …so creation must say the same thing.
        res = self.walk_in(room_id=self.first.pk)
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(self.assigned_room_ids(res), {self.first.pk})

    def test_occupied_flag_refuses_only_the_night_the_room_is_in_use(self):
        """A stay that includes TONIGHT is refused while somebody is in it."""
        self.first.status = Room.Status.OCCUPIED
        self.first.save(update_fields=["status"])
        res = self.walk_in(
            room_id=self.first.pk,
            check_in=TODAY.isoformat(),
            check_out=(TODAY + timedelta(days=2)).isoformat(),
        )
        self.assertIn(res.status_code, (400, 409), res.content)
        self.assertIn("occupied", res.json()["message"].lower())
        self.assertEqual(Booking.objects.count(), 0)

    # -------------------- 5: genuine occupancy is protected -----------------
    def test_case5_genuinely_occupied_room_is_still_refused(self):
        self.block(self.first)
        res = self.walk_in(room_id=self.first.pk)
        self.assertIn(res.status_code, (400, 409), res.content)
        self.assertNotIn("occupied tonight", res.json()["message"])
        self.assertEqual(Booking.objects.count(), 1)
        # …and the availability engine agrees it is not free.
        self.assertFalse(self.public_room_availability()[self.first.pk])

    def test_case5b_checked_in_guest_blocks_the_room_for_overlapping_dates(self):
        booking = self.block(self.first, check_in=TODAY - timedelta(days=1),
                             check_out=TODAY + timedelta(days=3))
        booking.status = Booking.Status.CHECKED_IN
        booking.save(update_fields=["status"])
        self.first.status = Room.Status.OCCUPIED
        self.first.save(update_fields=["status"])
        self.assertFalse(self.public_room_availability(
            check_in=TODAY, check_out=TODAY + timedelta(days=1))[self.first.pk])
        res = self.walk_in(room_id=self.first.pk, check_in=TODAY.isoformat(),
                           check_out=(TODAY + timedelta(days=1)).isoformat())
        self.assertIn(res.status_code, (400, 409), res.content)

    # ---------------- 6: non-overlapping reservations allowed ---------------
    def test_case6_existing_reservations_on_other_dates_do_not_block(self):
        self.block(self.first, check_in=TODAY + timedelta(days=30),
                   check_out=TODAY + timedelta(days=33))
        res = self.walk_in(room_id=self.first.pk)
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(self.assigned_room_ids(res), {self.first.pk})

    # ------------------- 7: checkout day is not a blocked night -------------
    def test_case7_checkout_on_the_check_in_date_frees_the_room(self):
        departing = self.block(
            self.first,
            check_in=CI - timedelta(days=3),
            check_out=CI,          # the previous guest leaves on our arrival day
        )
        # The flag is still OCCUPIED until housekeeping releases it: the calendar
        # — not the flag — must decide, because tonight belongs to the new guest.
        self.first.status = Room.Status.OCCUPIED
        self.first.save(update_fields=["status"])
        self.assertTrue(self.public_room_availability()[self.first.pk])
        res = self.walk_in(room_id=self.first.pk)
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(self.assigned_room_ids(res), {self.first.pk})
        self.assertEqual(BookingRoom.objects.filter(booking=departing).count(), 1)

    # ------------------------------ 8: multi-room ---------------------------
    def test_case8_multiple_rooms_including_the_first(self):
        res = self.walk_in(rooms=3, adults=4)
        self.assertEqual(res.status_code, 201, res.content)
        booking = Booking.objects.get(booking_reference=res.json()["data"]["booking_reference"])
        assigned = sorted(a.room.room_number for a in booking.room_assignments.select_related("room"))
        self.assertEqual(assigned, ["301", "302", "303"])
        self.assertEqual(booking.number_of_rooms, 3)

    # --------------------------- 9: expired holds ---------------------------
    def test_case9_expired_pending_hold_does_not_block_the_first_room(self):
        self.block(self.first, status=Booking.Status.PENDING, check_in=CI, check_out=CO)
        hold = Booking.objects.filter(status=Booking.Status.PENDING).first()
        hold.expires_at = timezone.now() - timedelta(minutes=5)
        hold.save(update_fields=["expires_at"])
        self.assertTrue(self.public_room_availability()[self.first.pk])
        res = self.walk_in(room_id=self.first.pk)
        self.assertEqual(res.status_code, 201, res.content)

    def test_case9b_live_pending_hold_does_block_the_first_room(self):
        self.block(self.first, status=Booking.Status.PENDING, check_in=CI, check_out=CO)
        hold = Booking.objects.filter(status=Booking.Status.PENDING).first()
        hold.expires_at = timezone.now() + timedelta(minutes=20)
        hold.save(update_fields=["expires_at"])
        self.assertFalse(self.public_room_availability()[self.first.pk])
        res = self.walk_in(room_id=self.first.pk)
        self.assertIn(res.status_code, (400, 409), res.content)

    # ----------------------------- 10: no double booking --------------------
    def test_case10_second_booking_of_the_same_room_is_refused(self):
        first = self.walk_in(room_id=self.first.pk)
        self.assertEqual(first.status_code, 201, first.content)
        second = self.walk_in(room_id=self.first.pk, guest={
            "first_name": "Second", "last_name": "Guest",
            "email": "second.firstroom@jone.test", "phone": "08031234570",
        })
        self.assertIn(second.status_code, (400, 409), second.content)
        self.assertEqual(
            BookingRoom.objects.filter(room=self.first, booking__status__in=[
                Booking.Status.CONFIRMED, Booking.Status.CHECKED_IN,
                Booking.Status.PENDING,
            ]).count(),
            1,
        )

    # ------------------ 11: availability and creation agree ------------------
    def test_case11_availability_and_creation_agree_for_every_room(self):
        self.block(self.second)   # 302 is genuinely taken
        self.last.status = Room.Status.OCCUPIED  # 303 carries a stale flag
        self.last.save(update_fields=["status"])
        listed = self.public_room_availability()
        for room in (self.first, self.second, self.last):
            with self.subTest(room=room.room_number):
                res = self.walk_in(room_id=room.pk, guest={
                    "first_name": "Agree", "last_name": room.room_number,
                    "email": f"agree{room.room_number}@jone.test",
                    "phone": "08031234571",
                })
                created = res.status_code == 201
                self.assertEqual(
                    created, listed.get(room.pk, False),
                    f"availability said {listed.get(room.pk)} but creation answered "
                    f"{res.status_code}: {getattr(res, 'content', b'')[:200]}",
                )
                if created:
                    # Remove only the booking this sub-test created — the
                    # blocker on 302 must survive for the next iteration.
                    Booking.objects.filter(
                        booking_reference=res.json()["data"]["booking_reference"]
                    ).delete()

    # ----------------------- 12: public website booking ---------------------
    def test_case12_public_website_books_the_exact_first_room(self):
        res = self.public_booking(room_id=self.first.pk)
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(self.assigned_room_ids(res), {self.first.pk})

    def test_case12b_public_booking_discloses_a_substitution_or_refuses(self):
        """A taken room never becomes a silent different room.

        The public flow keeps its documented substitution policy: when an
        identical-rate room of the same type is free the booking succeeds and
        SAYS which room was assigned, otherwise it is refused. Both outcomes are
        acceptable; a silent swap is not.
        """
        self.block(self.first)
        before = Booking.objects.count()
        res = self.public_booking(room_id=self.first.pk)
        if res.status_code == 201:
            payload = res.json()["data"]
            substitution = payload.get("room_substitution") or {}
            self.assertEqual(substitution.get("requested_room_id"), self.first.pk,
                             "a substituted booking must disclose the room that was asked for")
            self.assertNotEqual(substitution.get("assigned_room_id"), self.first.pk)
            self.assertEqual(self.assigned_room_ids(res), {substitution["assigned_room_id"]})
            self.assertEqual(Booking.objects.count(), before + 1)
        else:
            self.assertIn(res.status_code, (400, 409), res.content)
            self.assertEqual(Booking.objects.count(), before)

    def test_case12c_public_booking_refuses_when_the_type_is_sold_out(self):
        for room in (self.first, self.second, self.last):
            self.block(room)
        before = Booking.objects.count()
        res = self.public_booking(room_id=self.first.pk)
        self.assertIn(res.status_code, (400, 409), res.content)
        self.assertEqual(Booking.objects.count(), before)


class AvailabilityAgreementUnitTests(BaseAPITestCase):
    """Direct checks on the availability engine that underpins the above."""

    def test_blocked_room_ids_ignore_the_occupied_status_flag(self):
        """Only live assignments block; the physical flag never does."""
        room_type = make_room_type("Unit Agree", price="10000.00", max_guests=2)
        room = make_room(room_type, "801", status=Room.Status.OCCUPIED)
        blocked = set(availability.blocked_room_ids(
            room_type_id=room_type.pk, check_in=CI, check_out=CO,
        ))
        self.assertEqual(blocked, set())
        free = list(availability.available_rooms_queryset(
            room_type=room_type, check_in=CI, check_out=CO,
        ))
        self.assertEqual([r.pk for r in free], [room.pk])
