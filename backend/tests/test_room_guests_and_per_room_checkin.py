"""Room guests and per-room check-in for multi-room bookings.

The request this covers, in the hotel's words:

    "There should be a provision for guests that booked many rooms to attach
     emails of those that are in the other room(s) in case they want to make a
     request for something, and for a proper record keeping if they actually
     checked in in all the rooms they booked."

So there are two promises, and both are tested here:

1. A person staying in room 2/3 of somebody else's booking has their own
   recorded email, can sign in to the guest portal with it, sees that booking,
   and can raise a request for THEIR room only.
2. The desk can record arrivals room by room, and the booking afterwards states
   exactly how many of the booked rooms were occupied — including a checkout
   that closes a room nobody ever arrived in.
"""
from datetime import timedelta

from django.utils import timezone

from apps.accounts.models import User
from apps.bookings.models import Booking, BookingRoom, RoomOccupant
from apps.core.utils import hotel_today
from apps.guest_services.models import ServiceRequest
from apps.portal.models import PortalAccessChallenge
from apps.portal.services import hash_secret
from apps.stays.models import Stay, StayRoom
from apps.rooms.models import Room

from .base import BaseAPITestCase
from .factories import make_booking, make_guest, make_room, make_room_type, make_staff


class RoomGuestTestCase(BaseAPITestCase):
    """Three physical rooms, one booking, three different people."""

    def setUp(self):
        super().setUp()
        self.receptionist = make_staff("roomguests.reception@staff.dev", role=User.Role.RECEPTIONIST)
        self.guest = make_guest("booker@example.com", first_name="Bola", last_name="Ade")
        self.room_type = make_room_type("Family wing", price="20000.00", max_guests=2)
        self.room_1 = make_room(self.room_type, "201")
        self.room_2 = make_room(self.room_type, "202")
        self.room_3 = make_room(self.room_type, "203")
        self.booking = make_booking(
            self.guest,
            self.room_type,
            [self.room_1, self.room_2, self.room_3],
            number_of_rooms=3,
            check_in=hotel_today(),
            check_out=hotel_today() + timedelta(days=3),
            total="60000.00",
        )
        self.assignment_1 = self.booking.room_assignments.get(room=self.room_1)
        self.assignment_2 = self.booking.room_assignments.get(room=self.room_2)
        self.assignment_3 = self.booking.room_assignments.get(room=self.room_3)

    def _put_room_guests(self, rooms):
        self.auth(self.receptionist)
        return self.client.put(
            f"/api/admin/bookings/{self.booking.booking_reference}/room-guests/",
            {"rooms": rooms},
            format="json",
        )

    def _check_in_room(self, assignment):
        self.auth(self.receptionist)
        return self.client.post(
            f"/api/admin/bookings/{self.booking.booking_reference}/rooms/{assignment.pk}/check-in/",
            {},
            format="json",
        )

    def _portal_session(self, email):
        """Mint a verified portal session for a room guest, as the email flow would."""
        # force_authenticate() short-circuits DRF's authentication classes, so
        # the staff identity used by the desk calls above has to be cleared
        # before the portal bearer is presented.
        self.unauth()
        token = f"room-guest-token-{email}-0123456789abcdefghijklmnop"
        PortalAccessChallenge.objects.create(
            email=email,
            token_hash=hash_secret(token),
            expires_at=timezone.now() + timedelta(minutes=15),
        )
        response = self.client.post("/api/portal/auth/consume/", {"token": token}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["data"]["session_token"]


class RoomGuestRecordingTests(RoomGuestTestCase):
    def test_the_desk_can_record_who_is_in_each_room(self):
        response = self._put_room_guests(
            [
                {"assignment_id": self.assignment_2.pk, "guests": [
                    {"email": "Ada.Obiora@Example.COM", "full_name": "Ada Obiora", "phone": "08030000001"},
                ]},
                {"assignment_id": self.assignment_3.pk, "guests": [
                    {"email": "chinedu@example.com", "full_name": "Chinedu Eze", "is_lead_occupant": True},
                ]},
            ]
        )
        self.assertEqual(response.status_code, 200, response.content)

        # Emails are stored lowercase so a portal lookup is an exact match.
        ada = RoomOccupant.objects.get(email="ada.obiora@example.com")
        self.assertEqual(ada.booking_room_id, self.assignment_2.pk)
        self.assertEqual(ada.full_name, "Ada Obiora")
        self.assertEqual(RoomOccupant.objects.filter(booking_room=self.assignment_3).count(), 1)

        detail = response.json()["data"]
        rooms = {row["room_number"]: row for row in detail["room_assignments"]}
        self.assertEqual([g["email"] for g in rooms["202"]["occupants"]], ["ada.obiora@example.com"])
        self.assertEqual(rooms["203"]["occupants"][0]["is_lead_occupant"], True)

    def test_saving_the_same_list_again_changes_nothing(self):
        payload = [{"assignment_id": self.assignment_2.pk, "guests": [{"email": "ada@example.com"}]}]
        self.assertEqual(self._put_room_guests(payload).status_code, 200)
        first = RoomOccupant.objects.get()
        self.assertEqual(self._put_room_guests(payload).status_code, 200)
        self.assertEqual(RoomOccupant.objects.count(), 1)
        # Same row, so any portal invitation already sent is not lost.
        self.assertEqual(RoomOccupant.objects.get().pk, first.pk)

    def test_removing_a_guest_from_the_list_removes_the_record(self):
        self._put_room_guests([
            {"assignment_id": self.assignment_2.pk, "guests": [{"email": "ada@example.com"}]},
            {"assignment_id": self.assignment_3.pk, "guests": [{"email": "chinedu@example.com"}]},
        ])
        self.assertEqual(RoomOccupant.objects.count(), 2)

        self._put_room_guests([
            {"assignment_id": self.assignment_2.pk, "guests": [{"email": "ada@example.com"}]},
        ])
        self.assertEqual(list(RoomOccupant.objects.values_list("email", flat=True)), ["ada@example.com"])

    def test_the_booking_contact_does_not_need_a_room_guest_row(self):
        response = self._put_room_guests([
            {"assignment_id": self.assignment_2.pk, "guests": [{"email": "BOOKER@example.com"}]},
        ])
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("booking contact", str(response.json()))
        self.assertFalse(RoomOccupant.objects.exists())

    def test_the_same_guest_cannot_hold_two_rooms(self):
        response = self._put_room_guests([
            {"assignment_id": self.assignment_2.pk, "guests": [{"email": "ada@example.com"}]},
            {"assignment_id": self.assignment_3.pk, "guests": [{"email": "ada@example.com"}]},
        ])
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("listed twice", str(response.json()))

    def test_a_room_from_another_booking_is_refused(self):
        other_booking = make_booking(
            make_guest("someone.else@example.com"), self.room_type,
            [make_room(self.room_type, "301")], total="20000.00",
        )
        foreign_assignment = other_booking.room_assignments.get()

        response = self._put_room_guests([
            {"assignment_id": foreign_assignment.pk, "guests": [{"email": "ada@example.com"}]},
        ])
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("does not belong to this booking", str(response.json()))

    def test_the_room_list_freezes_once_the_booking_is_closed(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.auth(self.receptionist)
            self.client.post(
                f"/api/admin/bookings/{self.booking.booking_reference}/check-in/", {}, format="json"
            )
            self.client.post(
                f"/api/admin/bookings/{self.booking.booking_reference}/check-out/",
                {"allow_balance_due": True},
                format="json",
            )
        response = self._put_room_guests([
            {"assignment_id": self.assignment_2.pk, "guests": [{"email": "ada@example.com"}]},
        ])
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()["code"], "INVALID_BOOKING_STATE")


class PerRoomCheckInTests(RoomGuestTestCase):
    def test_the_first_room_opens_the_stay_without_claiming_the_other_rooms(self):
        response = self._check_in_room(self.assignment_1)
        self.assertEqual(response.status_code, 200, response.content)

        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, Booking.Status.CHECKED_IN)
        self.assertIsNotNone(self.booking.checked_in_at)

        data = response.json()["data"]
        self.assertEqual(data["rooms_checked_in"], 1)
        self.assertEqual(data["rooms_awaiting_arrival"], 2)
        self.assertEqual(data["rooms_not_arrived"], 0)

        rooms = {row["room_number"]: row for row in data["room_assignments"]}
        self.assertEqual(rooms["201"]["arrival_status"], "CHECKED_IN")
        self.assertEqual(rooms["202"]["arrival_status"], "AWAITING_ARRIVAL")
        self.assertEqual(rooms["203"]["arrival_status"], "AWAITING_ARRIVAL")

        stay = Stay.objects.get(booking=self.booking)
        self.assertEqual(stay.status, Stay.Status.IN_HOUSE)
        self.assertEqual(
            list(StayRoom.objects.filter(stay=stay).values_list("room__room_number", flat=True)),
            ["201"],
        )
        self.room_1.refresh_from_db()
        self.room_2.refresh_from_db()
        self.assertEqual(self.room_1.status, Room.Status.OCCUPIED)
        self.assertEqual(self.room_2.status, Room.Status.AVAILABLE)  # never claimed as occupied

    def test_each_later_room_appends_its_own_occupancy_evidence(self):
        self._check_in_room(self.assignment_1)
        response = self._check_in_room(self.assignment_2)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["data"]["rooms_checked_in"], 2)
        self.assertEqual(response.json()["data"]["rooms_awaiting_arrival"], 1)

        stay = Stay.objects.get(booking=self.booking)
        self.assertEqual(
            sorted(StayRoom.objects.filter(stay=stay).values_list("room__room_number", flat=True)),
            ["201", "202"],
        )
        # Only ONE stay exists: a later arrival must not open a second stay.
        self.assertEqual(Stay.objects.filter(booking=self.booking).count(), 1)
        self.room_2.refresh_from_db()
        self.assertEqual(self.room_2.status, Room.Status.OCCUPIED)

    def test_checking_the_same_room_in_twice_is_a_no_op(self):
        self._check_in_room(self.assignment_1)
        again = self._check_in_room(self.assignment_1)
        self.assertEqual(again.status_code, 200, again.content)
        self.assertIn("already checked in", again.json()["message"])
        stay = Stay.objects.get(booking=self.booking)
        self.assertEqual(StayRoom.objects.filter(stay=stay, room=self.room_1).count(), 1)

    def test_the_whole_booking_check_in_completes_a_partial_arrival(self):
        self._check_in_room(self.assignment_1)
        self.auth(self.receptionist)
        response = self.client.post(
            f"/api/admin/bookings/{self.booking.booking_reference}/check-in/", {}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.content)
        detail = self.client.get(f"/api/admin/bookings/{self.booking.booking_reference}/").json()["data"]
        self.assertEqual(detail["rooms_checked_in"], 3)
        self.assertEqual(detail["rooms_awaiting_arrival"], 0)
        stay = Stay.objects.get(booking=self.booking)
        self.assertEqual(StayRoom.objects.filter(stay=stay).count(), 3)

    def test_the_whole_booking_check_in_still_stamps_every_room(self):
        self.auth(self.receptionist)
        response = self.client.post(
            f"/api/admin/bookings/{self.booking.booking_reference}/check-in/", {}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["data"]["status"], Booking.Status.CHECKED_IN)
        self.assertEqual(
            RoomOccupant.objects.count(), 0,
            "a booking with no recorded room guests must still check in normally",
        )
        self.assertEqual(
            Room.objects.filter(
                pk__in=[self.room_1.pk, self.room_2.pk, self.room_3.pk], status=Room.Status.OCCUPIED
            ).count(),
            3,
        )
        stay = Stay.objects.get(booking=self.booking)
        self.assertEqual(StayRoom.objects.filter(stay=stay).count(), 3)

    def test_a_room_out_of_service_blocks_only_that_room(self):
        self.room_2.status = Room.Status.MAINTENANCE
        self.room_2.save(update_fields=["status"])
        response = self._check_in_room(self.assignment_2)
        self.assertEqual(response.status_code, 409, response.content)
        self.assertIn("maintenance", str(response.json()).lower())
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, Booking.Status.CONFIRMED)

        # The other rooms are unaffected.
        self.assertEqual(self._check_in_room(self.assignment_1).status_code, 200)

    def test_checkout_records_rooms_nobody_arrived_in(self):
        self._check_in_room(self.assignment_1)
        self.auth(self.receptionist)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                f"/api/admin/bookings/{self.booking.booking_reference}/check-out/",
                {"allow_balance_due": True},
                format="json",
            )
        self.assertEqual(response.status_code, 200, response.content)

        self.assignment_1.refresh_from_db()
        self.assignment_2.refresh_from_db()
        self.assignment_3.refresh_from_db()
        self.assertIsNotNone(self.assignment_1.checked_in_at)
        self.assertIsNotNone(self.assignment_2.not_arrived_at)
        self.assertIsNotNone(self.assignment_3.not_arrived_at)
        self.assertEqual(self.assignment_2.arrival_status, "DID_NOT_ARRIVE")

        # The two untouched rooms are not sent to housekeeping as dirty.
        self.room_2.refresh_from_db()
        self.room_3.refresh_from_db()
        self.assertEqual(self.room_2.status, Room.Status.AVAILABLE)
        self.assertNotEqual(self.room_2.housekeeping_status, Room.HousekeepingStatus.DIRTY)
        # The room that WAS used is released dirty, as before.
        self.room_1.refresh_from_db()
        self.assertEqual(self.room_1.status, Room.Status.AVAILABLE)
        self.assertEqual(self.room_1.housekeeping_status, Room.HousekeepingStatus.DIRTY)


class RoomGuestPortalTests(RoomGuestTestCase):
    def setUp(self):
        super().setUp()
        self._put_room_guests([
            {"assignment_id": self.assignment_2.pk, "guests": [
                {"email": "ada.obiora@example.com", "full_name": "Ada Obiora"},
            ]},
            {"assignment_id": self.assignment_3.pk, "guests": [
                {"email": "chinedu@example.com", "full_name": "Chinedu Eze"},
            ]},
        ])
        self._check_in_room(self.assignment_1)
        self._check_in_room(self.assignment_2)

    def test_a_recorded_room_guest_can_request_a_sign_in_link(self):
        from unittest import mock

        with mock.patch("apps.portal.services.queue_email") as send:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(
                    "/api/portal/auth/request/", {"email": "ada.obiora@example.com"}, format="json"
                )
        self.assertEqual(response.status_code, 202, response.content)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(PortalAccessChallenge.objects.filter(email="ada.obiora@example.com").count(), 1)

    def test_an_unrecorded_email_still_gets_the_generic_no_challenge_answer(self):
        response = self.client.post(
            "/api/portal/auth/request/", {"email": "stranger@example.com"}, format="json"
        )
        self.assertEqual(response.status_code, 202)
        self.assertFalse(PortalAccessChallenge.objects.filter(email="stranger@example.com").exists())

    def test_the_room_guest_sees_the_booking_but_only_their_own_room(self):
        session = self._portal_session("ada.obiora@example.com")
        response = self.client.get("/api/portal/me/", HTTP_X_PORTAL_SESSION=session)
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        row = next(
            item for item in data["bookings"]
            if item["booking_reference"] == self.booking.booking_reference
        )
        self.assertFalse(row["is_booking_contact"])
        self.assertEqual(row["your_room_numbers"], ["202"])
        self.assertEqual(
            [room["room_number"] for room in row["in_house_stay"]["rooms"]], ["202"],
            "a room guest must not be offered a room that belongs to somebody else's party",
        )

    def test_a_room_guest_cannot_see_a_booking_they_are_not_part_of(self):
        other_booking = make_booking(
            make_guest("other.party@example.com"), self.room_type,
            [make_room(self.room_type, "401")], total="20000.00",
        )
        session = self._portal_session("ada.obiora@example.com")
        response = self.client.get("/api/portal/me/", HTTP_X_PORTAL_SESSION=session)
        references = {row["booking_reference"] for row in response.json()["data"]["bookings"]}
        self.assertNotIn(other_booking.booking_reference, references)

    def test_a_room_guest_raises_a_request_for_their_room_and_is_named_on_it(self):
        stay = Stay.objects.get(booking=self.booking)
        session = self._portal_session("ada.obiora@example.com")
        response = self.client.post(
            "/api/portal/requests/",
            {
                "stay_reference": stay.reference,
                "room_id": self.room_2.pk,
                "category": ServiceRequest.Category.HOUSEKEEPING,
                "priority": "NORMAL",
                "summary": "Extra towels please",
            },
            format="json",
            HTTP_X_PORTAL_SESSION=session,
        )
        self.assertEqual(response.status_code, 201, response.content)

        service_request = ServiceRequest.objects.get()
        self.assertEqual(service_request.room_id, self.room_2.pk)
        self.assertEqual(service_request.portal_email, "ada.obiora@example.com")
        self.assertIsNotNone(service_request.occupant_id)
        self.assertEqual(service_request.occupant.email, "ada.obiora@example.com")
        # It still belongs to the booking for folio/billing purposes.
        self.assertEqual(service_request.guest_id, self.guest.pk)

    def test_a_room_guest_cannot_raise_a_request_against_another_room(self):
        # Room 3 is genuinely in house — so the refusal below is the ownership
        # rule, not "that room has no active stay".
        self._check_in_room(self.assignment_3)
        stay = Stay.objects.get(booking=self.booking)
        session = self._portal_session("ada.obiora@example.com")
        response = self.client.post(
            "/api/portal/requests/",
            {
                "stay_reference": stay.reference,
                "room_id": self.room_3.pk,
                "category": ServiceRequest.Category.HOUSEKEEPING,
                "priority": "NORMAL",
                "summary": "Wrong room",
            },
            format="json",
            HTTP_X_PORTAL_SESSION=session,
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("your own room", str(response.json()))

    def test_the_room_guest_can_see_the_request_they_raised(self):
        stay = Stay.objects.get(booking=self.booking)
        session = self._portal_session("ada.obiora@example.com")
        self.client.post(
            "/api/portal/requests/",
            {
                "stay_reference": stay.reference,
                "room_id": self.room_2.pk,
                "category": ServiceRequest.Category.AMENITY,
                "priority": "NORMAL",
                "summary": "Bottled water",
            },
            format="json",
            HTTP_X_PORTAL_SESSION=session,
        )
        listing = self.client.get("/api/portal/requests/", HTTP_X_PORTAL_SESSION=session)
        self.assertEqual(listing.status_code, 200, listing.content)
        payload = listing.json()["data"]
        rows = payload["results"] if isinstance(payload, dict) else payload
        self.assertEqual([row["summary"] for row in rows], ["Bottled water"])

    def test_the_booking_contact_still_sees_every_room(self):
        session = self._portal_session("booker@example.com")
        response = self.client.get("/api/portal/me/", HTTP_X_PORTAL_SESSION=session)
        row = next(
            item for item in response.json()["data"]["bookings"]
            if item["booking_reference"] == self.booking.booking_reference
        )
        self.assertTrue(row["is_booking_contact"])
        self.assertEqual(sorted(row["your_room_numbers"]), ["201", "202", "203"])
        self.assertEqual(
            sorted(room["room_number"] for room in row["in_house_stay"]["rooms"]),
            ["201", "202"],
        )


class MultiRoomBookingCreationTests(BaseAPITestCase):
    """The guest attaches the other rooms' emails while booking, not only at the desk."""

    def setUp(self):
        super().setUp()
        self.room_type = make_room_type("Group chalet", price="12000.00", max_guests=2)
        for number in ("D1", "D2", "D3"):
            make_room(self.room_type, number)
        self.today = hotel_today()

    def _create(self, **overrides):
        payload = {
            "room_type": self.room_type.slug,
            "check_in": (self.today + timedelta(days=6)).isoformat(),
            "check_out": (self.today + timedelta(days=8)).isoformat(),
            "rooms": 3, "adults": 4, "children": 0,
            "guest": {
                "first_name": "Bola", "last_name": "Ade",
                "email": "bola.ade@example.com", "phone": "08031234567",
            },
        }
        payload.update(overrides)
        return self.client.post("/api/bookings/", payload, format="json")

    def test_other_room_emails_are_recorded_at_booking_time(self):
        response = self._create(room_guests=[
            {"email": "ada@example.com", "full_name": "Ada"},
            {"email": "chinedu@example.com", "full_name": "Chinedu"},
        ])
        self.assertEqual(response.status_code, 201, response.content)
        booking = Booking.objects.get(booking_reference=response.json()["data"]["booking_reference"])

        rooms = list(booking.room_assignments.order_by("room__room_number"))
        self.assertEqual(len(rooms), 3)
        self.assertEqual(list(rooms[1].occupants.values_list("email", flat=True)), ["ada@example.com"])
        self.assertEqual(list(rooms[2].occupants.values_list("email", flat=True)), ["chinedu@example.com"])
        self.assertFalse(rooms[0].occupants.exists(), "the booking contact is room 1 by convention")

    def test_a_room_index_pins_a_guest_to_the_room_they_named(self):
        response = self._create(room_guests=[
            {"email": "ada@example.com", "room_index": 0},
        ])
        self.assertEqual(response.status_code, 201, response.content)
        booking = Booking.objects.get(booking_reference=response.json()["data"]["booking_reference"])
        first_room = booking.room_assignments.order_by("room__room_number").first()
        self.assertEqual(list(first_room.occupants.values_list("email", flat=True)), ["ada@example.com"])

    def test_room_emails_are_refused_on_a_single_room_booking(self):
        response = self._create(rooms=1, room_guests=[{"email": "ada@example.com"}])
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("more than one room", str(response.json()))

    def test_the_booking_contact_cannot_also_be_a_room_guest(self):
        response = self._create(room_guests=[{"email": "bola.ade@example.com"}])
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("booking contact", str(response.json()))

    def test_duplicate_room_emails_are_refused(self):
        response = self._create(room_guests=[
            {"email": "ada@example.com"}, {"email": "ADA@example.com"},
        ])
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("listed twice", str(response.json()))

    def test_the_detail_endpoint_reports_which_rooms_are_still_expected(self):
        response = self._create(room_guests=[{"email": "ada@example.com"}])
        self.assertEqual(response.status_code, 201, response.content)
        booking = Booking.objects.get(booking_reference=response.json()["data"]["booking_reference"])
        self.auth(make_staff("roomguests.detail@staff.dev", role=User.Role.RECEPTIONIST))
        detail = self.client.get(f"/api/admin/bookings/{booking.booking_reference}/")
        self.assertEqual(detail.status_code, 200, detail.content)
        data = detail.json()["data"]
        self.assertEqual(data["rooms_checked_in"], 0)
        self.assertEqual(data["rooms_awaiting_arrival"], 3)
        statuses = {row["arrival_status"] for row in data["room_assignments"]}
        self.assertEqual(statuses, {"EXPECTED"})


class BookingListRoomCountTests(RoomGuestTestCase):
    """The arrivals list must state room-level progress without an N+1."""

    def test_the_list_row_carries_room_arrival_counts(self):
        self._check_in_room(self.assignment_1)
        self.auth(self.receptionist)
        response = self.client.get("/api/admin/bookings/?page_size=20")
        self.assertEqual(response.status_code, 200, response.content)
        rows = response.json()["data"]["results"] if isinstance(response.json()["data"], dict) else response.json()["data"]
        row = next(r for r in rows if r["booking_reference"] == self.booking.booking_reference)
        self.assertEqual(row["rooms_checked_in"], 1)
        self.assertEqual(row["rooms_awaiting_arrival"], 2)
        self.assertEqual(row["number_of_rooms"], 3)

    def test_a_booking_with_no_arrivals_reports_zero_in_house(self):
        self.auth(self.receptionist)
        response = self.client.get("/api/admin/bookings/?page_size=20")
        rows = response.json()["data"]["results"] if isinstance(response.json()["data"], dict) else response.json()["data"]
        row = next(r for r in rows if r["booking_reference"] == self.booking.booking_reference)
        self.assertEqual(row["rooms_checked_in"], 0)
        self.assertEqual(row["rooms_awaiting_arrival"], 3)

    def test_the_counts_do_not_add_a_query_per_row(self):
        """One page = one query plan, whether it holds one booking or four."""
        from django.test.utils import CaptureQueriesContext
        from django.db import connection

        self.auth(self.receptionist)
        # Warm-up: hotel settings and the stale-hold sweep are cached on the
        # first request, and would otherwise make the first page look smaller.
        self.client.get("/api/admin/bookings/?page_size=20")
        with CaptureQueriesContext(connection) as small:
            self.client.get("/api/admin/bookings/?page_size=20")

        extra_guest = make_guest("bulk@example.com")
        for index in range(3):
            make_booking(
                make_guest(f"bulk{index}@example.com"), self.room_type,
                [make_room(self.room_type, f"9{index}0")], total="20000.00",
            )
        with CaptureQueriesContext(connection) as large:
            self.client.get("/api/admin/bookings/?page_size=20")

        self.assertEqual(
            len(large), len(small),
            f"the bookings list grew by {len(large) - len(small)} quer(ies) for 3 extra rows",
        )


class OccupancyCountsArrivedRoomsTests(BaseAPITestCase):
    """Occupancy keeps counting rooms SOLD; arrivals are reported beside it."""

    def setUp(self):
        super().setUp()
        self.room_type = make_room_type("Occupancy wing", price="18000.00")
        self.room_a = make_room(self.room_type, "801")
        self.room_b = make_room(self.room_type, "802")
        self.guest = make_guest("occupancy@example.com")
        self.today = hotel_today()

    def _booking(self, start_offset=-2, nights=3):
        check_in = self.today + timedelta(days=start_offset)
        return make_booking(
            self.guest, self.room_type, [self.room_a, self.room_b],
            number_of_rooms=2, check_in=check_in,
            check_out=check_in + timedelta(days=nights),
            status=Booking.Status.CHECKED_IN, total="36000.00",
        )

    def _report(self):
        from apps.reports.services.reports import occupancy_report

        return occupancy_report(self.today + timedelta(days=-2), self.today)

    def test_a_sold_room_nobody_arrived_in_is_not_counted_as_arrived(self):
        booking = self._booking()
        assignment_a = booking.room_assignments.get(room=self.room_a)
        assignment_b = booking.room_assignments.get(room=self.room_b)
        arrival = timezone.now() - timedelta(days=2)
        assignment_a.checked_in_at = arrival
        assignment_a.save(update_fields=["checked_in_at"])
        assignment_b.not_arrived_at = timezone.now()
        assignment_b.save(update_fields=["not_arrived_at"])

        report = self._report()
        self.assertEqual(report["occupied_room_nights"], 6)   # 2 rooms × 3 nights sold
        self.assertEqual(report["arrived_room_nights"], 3)    # only room 801
        self.assertEqual(report["unarrived_room_nights"], 3)  # only room 802
        self.assertEqual(report["average_arrival_percent"], 50.0)

    def test_a_room_arriving_late_only_counts_from_the_night_it_arrived(self):
        booking = self._booking()
        second_night = timezone.now() - timedelta(days=1)
        for assignment in booking.room_assignments.all():
            assignment.checked_in_at = second_night
            assignment.save(update_fields=["checked_in_at"])

        report = self._report()
        by_day = {row["date"]: row for row in report["by_day"]}
        days = sorted(by_day)
        self.assertEqual(report["occupied_room_nights"], 6)
        self.assertEqual(by_day[days[0]]["arrived_rooms"], 0, "night one had nobody in the rooms")
        self.assertEqual(by_day[days[-1]]["arrived_rooms"], 2)
        self.assertEqual(report["arrived_room_nights"], 4)

    def test_a_booking_that_is_still_expected_is_not_labelled_a_no_show(self):
        check_in = self.today + timedelta(days=1)
        make_booking(
            self.guest, self.room_type, [self.room_a], number_of_rooms=1,
            check_in=check_in, check_out=check_in + timedelta(days=2), total="18000.00",
        )
        report = self._report()
        self.assertEqual(report["unarrived_room_nights"], 0)
        self.assertEqual(report["arrived_room_nights"], 0)

    def test_the_report_is_exposed_by_the_api(self):
        from apps.accounts.models import User

        self._booking()
        staff = make_staff("occupancy.manager@staff.dev", role=User.Role.MANAGER)
        self.auth(staff)
        response = self.client.get(
            f"/api/admin/reports/occupancy/?start_date={self.today - timedelta(days=2)}&end_date={self.today}"
        )
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        for key in ("arrived_room_nights", "unarrived_room_nights", "average_arrival_percent", "definitions"):
            self.assertIn(key, data)
        self.assertIn("arrived_rooms", data["by_day"][0])
