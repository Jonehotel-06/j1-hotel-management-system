# tests/test_guest_checkout.py
from datetime import timedelta

from apps.bookings.models import Booking, Guest
from apps.core.utils import hotel_today

from .base import BaseAPITestCase
from .factories import make_room, make_room_type


class AnonymousGuestCheckoutTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.room_type = make_room_type("Anonymous Standard", price="12000.00", max_guests=2)
        make_room(self.room_type, "A1")
        today = hotel_today()
        self.payload = {
            "room_type": self.room_type.slug,
            "check_in": (today + timedelta(days=5)).isoformat(),
            "check_out": (today + timedelta(days=7)).isoformat(),
            "rooms": 1,
            "adults": 1,
            "children": 0,
            "guest": {
                "first_name": "Amina",
                "last_name": "Guest",
                "email": "amina@example.test",
                "phone": "08030000000",
            },
        }

    def test_anonymous_checkout_creates_standalone_guest_and_secure_token(self):
        response = self.client.post("/api/bookings/", self.payload, format="json")
        self.assertEqual(response.status_code, 201, response.json())
        data = response.json()["data"]
        self.assertTrue(data["guest_access_token"])
        booking = Booking.objects.get(booking_reference=data["booking_reference"])
        self.assertIsNone(booking.guest.user_id)
        self.assertTrue(booking.guest_access_token_hash)
        self.assertEqual(Guest.objects.get(pk=booking.guest_id).email, "amina@example.test")

    def test_reference_alone_does_not_expose_booking(self):
        response = self.client.post("/api/bookings/", self.payload, format="json")
        reference = response.json()["data"]["booking_reference"]
        self.assertEqual(self.client.get(f"/api/bookings/{reference}/").status_code, 404)

    def test_secure_token_scopes_booking_access(self):
        response = self.client.post("/api/bookings/", self.payload, format="json")
        data = response.json()["data"]
        reference = data["booking_reference"]
        token = data["guest_access_token"]
        self.assertEqual(
            self.client.get(
                f"/api/bookings/{reference}/",
                HTTP_X_GUEST_ACCESS_TOKEN=token,
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(
                f"/api/bookings/{reference}/",
                HTTP_X_GUEST_ACCESS_TOKEN="not-the-token",
            ).status_code,
            404,
        )

    def test_guest_checkout_does_not_create_an_account(self):
        self.client.post("/api/bookings/", self.payload, format="json")
        self.assertFalse(Guest.objects.get(email="amina@example.test").user_id)
