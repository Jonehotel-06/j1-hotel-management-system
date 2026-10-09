# tests/test_guest_access.py
"""Guest access-token security: expiry, cross-booking isolation, capability-scoped
staff JWT access, and the production CORS preflight contract.

These lock in the guest-checkout authorization architecture:

* the raw token is returned exactly once at booking creation;
* an expired token is worthless (404, nothing leaked);
* guest A's token can never act on guest B's booking (bookings OR payments);
* staff JWTs need an explicit booking-read/manage capability on guest-facing endpoints;
* a generic staff account alone never bypasses booking-level authorization;
* a guest token grants nothing on staff endpoints;
* the browser preflight from a deployed frontend origin must be allowed to
  send X-Guest-Access-Token (the production failure this suite guards).
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.bookings.models import Booking
from apps.payments.models import Payment
from apps.core.utils import hotel_today

from .base import BaseAPITestCase
from .factories import make_room, make_room_type, make_staff


class GuestAccessTokenSecurityTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.room_type = make_room_type("Access Suite", price="20000.00", max_guests=2)
        make_room(self.room_type, "AS1")
        make_room(self.room_type, "AS2")
        today = hotel_today()
        self.check_in = (today + timedelta(days=4)).isoformat()
        self.check_out = (today + timedelta(days=6)).isoformat()
        self.booking_a = self._create_booking("guest-a@example.test")
        self.booking_b = self._create_booking("guest-b@example.test")

    def _create_booking(self, email):
        response = self.client.post("/api/bookings/", {
            "room_type": self.room_type.slug,
            "check_in": self.check_in,
            "check_out": self.check_out,
            "rooms": 1, "adults": 1, "children": 0,
            "guest": {
                "first_name": "Guest", "last_name": email.split("@")[0].title(),
                "email": email, "phone": "08030000000",
            },
        }, format="json")
        self.assertEqual(response.status_code, 201, response.json())
        return response.json()["data"]

    # --- expiry ---------------------------------------------------------------
    def test_expired_token_is_rejected_with_404(self):
        ref = self.booking_a["booking_reference"]
        token = self.booking_a["guest_access_token"]
        booking = Booking.objects.get(booking_reference=ref)
        booking.guest_access_expires_at = timezone.now() - timedelta(minutes=1)
        booking.save(update_fields=["guest_access_expires_at"])
        response = self.client.get(f"/api/bookings/{ref}/", HTTP_X_GUEST_ACCESS_TOKEN=token)
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("guest", str(response.json()).lower().replace("guest_", ""))

    def test_expired_token_cannot_initialize_payment(self):
        ref = self.booking_a["booking_reference"]
        token = self.booking_a["guest_access_token"]
        booking = Booking.objects.get(booking_reference=ref)
        booking.guest_access_expires_at = timezone.now() - timedelta(minutes=1)
        booking.save(update_fields=["guest_access_expires_at"])
        response = self.client.post(
            "/api/payments/initialize/", {"booking_reference": ref},
            format="json", HTTP_X_GUEST_ACCESS_TOKEN=token,
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(Payment.objects.count(), 0)

    # --- cross-booking isolation ------------------------------------------------
    def test_guest_a_token_cannot_read_guest_b_booking(self):
        response = self.client.get(
            f"/api/bookings/{self.booking_b['booking_reference']}/",
            HTTP_X_GUEST_ACCESS_TOKEN=self.booking_a["guest_access_token"],
        )
        self.assertEqual(response.status_code, 404)

    def test_guest_a_token_cannot_pay_for_guest_b_booking(self):
        response = self.client.post(
            "/api/payments/initialize/",
            {"booking_reference": self.booking_b["booking_reference"]},
            format="json",
            HTTP_X_GUEST_ACCESS_TOKEN=self.booking_a["guest_access_token"],
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(Payment.objects.count(), 0)

    def test_guest_a_token_cannot_verify_guest_b_payment(self):
        payment = Payment.objects.create(
            booking=Booking.objects.get(booking_reference=self.booking_b["booking_reference"]),
            reference="J1P-CROSS-1", provider=Payment.Provider.PAYSTACK,
            amount=Decimal("40000.00"), currency="NGN", status=Payment.Status.PENDING,
        )
        response = self.client.get(
            f"/api/payments/verify/{payment.reference}/",
            HTTP_X_GUEST_ACCESS_TOKEN=self.booking_a["guest_access_token"],
        )
        self.assertEqual(response.status_code, 404)

    def test_guest_a_token_cannot_cancel_or_read_receipt_of_guest_b(self):
        ref_b = self.booking_b["booking_reference"]
        token_a = self.booking_a["guest_access_token"]
        self.assertEqual(
            self.client.post(f"/api/bookings/{ref_b}/cancel/", {}, format="json",
                             HTTP_X_GUEST_ACCESS_TOKEN=token_a).status_code, 404)
        self.assertEqual(
            self.client.get(f"/api/bookings/{ref_b}/receipt/",
                            HTTP_X_GUEST_ACCESS_TOKEN=token_a).status_code, 404)

    # --- staff JWT keeps working on guest endpoints -------------------------------
    def test_staff_jwt_with_booking_capability_reads_booking_without_guest_token(self):
        staff = make_staff("frontdesk@staff.test", role=User.Role.RECEPTIONIST)
        self.auth(staff)
        response = self.client.get(f"/api/bookings/{self.booking_a['booking_reference']}/")
        self.assertEqual(response.status_code, 200)

    def test_department_staff_without_booking_capability_cannot_read_guest_records(self):
        for role, email in (
            (User.Role.CASHIER, "cashier-no-booking@staff.test"),
            (User.Role.HOUSEKEEPING, "housekeeping-no-booking@staff.test"),
            (User.Role.MAINTENANCE, "maintenance-no-booking@staff.test"),
        ):
            with self.subTest(role=role):
                staff = make_staff(email, role=role)
                self.auth(staff)
                reference = self.booking_a["booking_reference"]
                self.assertEqual(self.client.get(f"/api/bookings/{reference}/").status_code, 404)
                self.assertEqual(self.client.get(f"/api/bookings/{reference}/receipt/").status_code, 404)

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_mock")
    @patch("apps.payments.services.paystack.initialize_transaction")
    def test_staff_jwt_can_initialize_payment_for_a_guest(self, mock_init):
        mock_init.side_effect = lambda **kwargs: {
            "authorization_url": "https://checkout.paystack.com/staff-code",
            "access_code": "staff-code", "reference": kwargs["reference"],
        }
        staff = make_staff("cashier@staff.test", role=User.Role.RECEPTIONIST)
        self.auth(staff)
        response = self.client.post(
            "/api/payments/initialize/",
            {"booking_reference": self.booking_a["booking_reference"]}, format="json",
        )
        self.assertEqual(response.status_code, 201, response.json())

    # --- guest token grants nothing on staff endpoints ---------------------------
    def test_guest_token_is_rejected_on_staff_endpoints(self):
        token = self.booking_a["guest_access_token"]
        response = self.client.get("/api/admin/bookings/", HTTP_X_GUEST_ACCESS_TOKEN=token)
        self.assertIn(response.status_code, (401, 403))
        staff_receipt = self.client.get(
            f"/api/admin/bookings/{self.booking_a['booking_reference']}/receipt/",
            HTTP_X_GUEST_ACCESS_TOKEN=token,
        )
        self.assertIn(staff_receipt.status_code, (401, 403))

    # --- CORS: production-like preflight -----------------------------------------
    def test_preflight_from_frontend_origin_allows_guest_header(self):
        """OPTIONS /api/payments/initialize/ from a deployed frontend must
        allow Content-Type and X-Guest-Access-Token or the browser blocks the
        request before Django ever sees the POST."""
        response = self.client.options(
            "/api/payments/initialize/",
            HTTP_ORIGIN="https://hotel.example.test",
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
            HTTP_ACCESS_CONTROL_REQUEST_HEADERS="content-type,x-guest-access-token",
        )
        allowed = response.get("Access-Control-Allow-Headers", "").lower()
        self.assertIn("x-guest-access-token", allowed)
        self.assertIn("content-type", allowed)
        methods = response.get("Access-Control-Allow-Methods", "").upper()
        self.assertIn("POST", methods)

    def test_preflight_on_bookings_allows_guest_header(self):
        response = self.client.options(
            "/api/bookings/X/",
            HTTP_ORIGIN="https://hotel.example.test",
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="GET",
            HTTP_ACCESS_CONTROL_REQUEST_HEADERS="x-guest-access-token",
        )
        allowed = response.get("Access-Control-Allow-Headers", "").lower()
        self.assertIn("x-guest-access-token", allowed)
