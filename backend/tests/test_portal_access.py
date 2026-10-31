"""Security and ownership coverage for verified-email guest portal sessions."""
from datetime import timedelta
from unittest import mock

from django.test import override_settings
from django.utils import timezone

from apps.bookings.models import Booking
from apps.portal.models import PortalAccessChallenge, PortalSession
from apps.portal.services import hash_secret
from apps.stays.models import Stay, StayRoom

from .base import BaseAPITestCase
from .factories import make_booking, make_guest, make_room, make_room_type
from .portal_stay_helpers import make_in_house_stay


class PortalAccessTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.email = "portal.guest@example.com"
        self.guest = make_guest(self.email, first_name="Portal", last_name="Guest")
        self.room_type = make_room_type("Portal suite", price="25000.00")
        self.room = make_room(self.room_type, "P01")
        self.booking = make_booking(self.guest, self.room_type, [self.room])
        # Portal access requires a live in-house stay (checked in, not checked out).
        self.stay = make_in_house_stay(self.booking)

    def _challenge(self, token="portal-magic-token-0123456789-abcdefghijklmnopqrstuvwxyz", *, expires_at=None):
        return PortalAccessChallenge.objects.create(
            email=self.email,
            token_hash=hash_secret(token),
            expires_at=expires_at or timezone.now() + timedelta(minutes=15),
        )

    def _consume(self, token="portal-magic-token-0123456789-abcdefghijklmnopqrstuvwxyz"):
        response = self.client.post("/api/portal/auth/consume/", {"token": token}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["data"]["session_token"]

    def test_request_is_non_enumerating_and_only_known_guest_email_creates_a_challenge(self):
        with mock.patch("apps.portal.services.queue_email"):
            known = self.client.post("/api/portal/auth/request/", {"email": self.email}, format="json")
            unknown = self.client.post(
                "/api/portal/auth/request/", {"email": "not-a-guest@example.com"}, format="json"
            )
        self.assertEqual(known.status_code, 202, known.content)
        self.assertEqual(unknown.status_code, 202, unknown.content)
        self.assertEqual(known.json(), unknown.json())
        self.assertEqual(PortalAccessChallenge.objects.filter(email=self.email).count(), 1)
        self.assertFalse(PortalAccessChallenge.objects.filter(email="not-a-guest@example.com").exists())

    def test_per_email_request_limit_does_not_change_the_public_response(self):
        with override_settings(PORTAL_CHALLENGES_PER_EMAIL_PER_HOUR=1), mock.patch(
            "apps.portal.services.queue_email"
        ):
            first = self.client.post("/api/portal/auth/request/", {"email": self.email}, format="json")
            second = self.client.post("/api/portal/auth/request/", {"email": self.email}, format="json")
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(PortalAccessChallenge.objects.filter(email=self.email).count(), 1)

    def test_magic_link_is_one_use_and_raw_secrets_are_never_stored(self):
        token = "portal-magic-token-0123456789-abcdefghijklmnopqrstuvwxyz"
        challenge = self._challenge(token)

        session_token = self._consume(token)
        challenge.refresh_from_db()
        session = PortalSession.objects.get(challenge=challenge)
        self.assertIsNotNone(challenge.consumed_at)
        self.assertNotEqual(challenge.token_hash, token)
        self.assertNotEqual(session.session_hash, session_token)
        self.assertEqual(session.email, self.email)
        self.assertEqual(session.authentication_method, PortalSession.AuthenticationMethod.EMAIL_MAGIC_LINK)

        retry = self.client.post("/api/portal/auth/consume/", {"token": token}, format="json")
        self.assertEqual(retry.status_code, 401, retry.content)
        self.assertEqual(PortalSession.objects.count(), 1)

    def test_expired_link_is_rejected_without_creating_a_session(self):
        token = "expired-portal-magic-token-0123456789-abcdefghijklmnop"
        self._challenge(token, expires_at=timezone.now() - timedelta(seconds=1))

        response = self.client.post("/api/portal/auth/consume/", {"token": token}, format="json")
        self.assertEqual(response.status_code, 401, response.content)
        self.assertEqual(PortalSession.objects.count(), 0)

    def test_portal_overview_is_scoped_to_verified_email_not_another_guest(self):
        other_guest = make_guest("other.portal.guest@example.com")
        other_room = make_room(self.room_type, "P02")
        other_booking = make_booking(other_guest, self.room_type, [other_room])
        token = "portal-magic-token-0123456789-abcdefghijklmnopqrstuvwxyz"
        self._challenge(token)
        session_token = self._consume(token)

        response = self.client.get("/api/portal/me/", HTTP_X_PORTAL_SESSION=session_token)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn("no-store", response["Cache-Control"])
        data = response.json()["data"]
        references = {row["booking_reference"] for row in data["bookings"]}
        self.assertIn(self.booking.booking_reference, references)
        self.assertNotIn(other_booking.booking_reference, references)
        self.assertEqual(data["email"], self.email)

    def test_overview_exposes_only_an_owned_in_house_stay_and_active_room_selection(self):
        stay = self.stay
        token = "portal-magic-token-0123456789-abcdefghijklmnopqrstuvwxyz"
        self._challenge(token)
        session_token = self._consume(token)

        overview = self.client.get("/api/portal/me/", HTTP_X_PORTAL_SESSION=session_token)
        self.assertEqual(overview.status_code, 200, overview.content)
        row = next(item for item in overview.json()["data"]["bookings"] if item["booking_reference"] == self.booking.booking_reference)
        self.assertEqual(row["in_house_stay"]["reference"], stay.reference)
        self.assertEqual(row["in_house_stay"]["rooms"], [{"id": self.room.pk, "room_number": self.room.room_number}])

    def test_room_occupant_does_not_see_other_rooms_or_booking_totals(self):
        from apps.bookings.models import BookingRoom, RoomOccupant
        other_room = make_room(self.room_type, "P02")
        assignment = BookingRoom.objects.create(
            booking=self.booking, room=other_room, check_in=self.booking.check_in,
            check_out=self.booking.check_out, checked_in_at=timezone.now(),
        )
        StayRoom.objects.create(stay=self.stay, room=other_room, booking_room=assignment,
                                actual_check_in_at=timezone.now())
        RoomOccupant.objects.create(booking_room=assignment, email="room@example.com", full_name="Room guest")
        token = "occupant-magic-token-0123456789-abcdefghijklmnopqrstuvwxyz"
        PortalAccessChallenge.objects.create(email="room@example.com", token_hash=hash_secret(token),
                                             expires_at=timezone.now() + timedelta(minutes=15))
        session_token = self._consume(token)
        response = self.client.get("/api/portal/me/", HTTP_X_PORTAL_SESSION=session_token)
        self.assertEqual(response.status_code, 200, response.content)
        row = response.json()["data"]["bookings"][0]
        self.assertEqual(row["room_numbers"], ["P02"])
        self.assertEqual(row["your_room_numbers"], ["P02"])
        self.assertIsNone(row["total_amount"])
        self.assertIsNone(row["amount_paid"])

    def test_logout_revokes_the_opaque_session_and_subsequent_access_fails(self):
        token = "portal-magic-token-0123456789-abcdefghijklmnopqrstuvwxyz"
        self._challenge(token)
        session_token = self._consume(token)

        logout = self.client.post("/api/portal/auth/logout/", {}, format="json", HTTP_X_PORTAL_SESSION=session_token)
        self.assertEqual(logout.status_code, 200, logout.content)
        self.assertEqual(self.client.get("/api/portal/me/", HTTP_X_PORTAL_SESSION=session_token).status_code, 401)

    def test_booking_specific_guest_token_is_not_accepted_as_a_portal_session(self):
        legacy_token = self.booking.issue_guest_access_token()
        response = self.client.get("/api/portal/me/", HTTP_X_PORTAL_SESSION=legacy_token)
        self.assertEqual(response.status_code, 401, response.content)
