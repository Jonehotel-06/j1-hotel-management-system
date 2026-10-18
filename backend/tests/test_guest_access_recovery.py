# tests/test_guest_access_recovery.py
"""A guest must never be told "this device is not recognised" for a booking it just made.

The screenshot that prompted this work:

    Payment not completed
    This device is no longer recognised for your booking. Request a secure link below…

…on the phone that had created the booking minutes earlier. Two independent
defects can produce that sentence, and both are fixed here:

1. **A stale account session.** ``payments/initialize/`` and the guest booking
   endpoints are open to anonymous callers and authorize with the *booking*
   token, but DRF authenticates before the view runs — so an expired dashboard
   JWT left in the same browser answered ``401`` before the booking token was
   even read. A credential that cannot be used means "not signed in", not
   "denied".
2. **A lost booking token with no way back.** The browser that CREATED the
   booking still holds the random idempotency key it sent with the creation;
   presenting it is no more powerful than replaying the creation (which already
   returns a fresh token), so it can be exchanged for a new guest token without
   an email round-trip.
"""
import secrets
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import override_settings
from rest_framework_simplejwt.tokens import AccessToken

from apps.accounts.models import User
from apps.bookings.models import Booking
from apps.core.utils import hotel_today

from .base import BaseAPITestCase
from .factories import (
    hotel_settings,
    make_booking,
    make_guest,
    make_room,
    make_room_type,
    make_staff,
)


class _BookingFixture(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        hotel_settings()
        self.room_type = make_room_type("Recovery Wing", price="20000.00")
        self.room = make_room(self.room_type, "951")
        self.guest = make_guest("recovery@example.test")
        today = hotel_today()
        self.booking = make_booking(
            self.guest, self.room_type, [self.room],
            check_in=today + timedelta(days=4),
            check_out=today + timedelta(days=6),
            total="40000.00",
        )
        self.booking.idempotency_key = "idem-" + secrets.token_hex(8)
        self.booking.save(update_fields=["idempotency_key"])
        self.detail_url = f"/api/bookings/{self.booking.booking_reference}/"
        self.exchange_url = f"/api/bookings/{self.booking.booking_reference}/access/"

    def _guest_headers(self, token):
        return {"HTTP_X_GUEST_ACCESS_TOKEN": token}


class IdempotencyKeyReclaimTests(_BookingFixture):
    def test_the_browser_that_created_the_booking_can_reclaim_its_token(self):
        response = self.client.post(
            self.exchange_url, {"idempotency_key": self.booking.idempotency_key}, format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        token = response.json()["data"]["guest_access_token"]
        self.assertTrue(token)

        detail = self.client.get(self.detail_url, **self._guest_headers(token))
        self.assertEqual(detail.status_code, 200, detail.content)

    def test_the_reclaimed_token_can_start_a_payment(self):
        token = self.client.post(
            self.exchange_url, {"idempotency_key": self.booking.idempotency_key}, format="json",
        ).json()["data"]["guest_access_token"]
        with override_settings(PAYSTACK_SECRET_KEY="sk_test_recovery"):
            with patch("apps.payments.services.paystack.initialize_transaction") as gateway:
                gateway.side_effect = lambda **kw: {
                    "authorization_url": "https://checkout.paystack.test/recovered",
                    "access_code": "recovered",
                    "reference": kw["reference"],
                }
                response = self.client.post(
                    "/api/payments/initialize/",
                    {"booking_reference": self.booking.booking_reference},
                    format="json", **self._guest_headers(token),
                )
        self.assertEqual(response.status_code, 201, response.content)

    def test_a_wrong_key_is_the_same_404_as_every_other_booking_endpoint(self):
        response = self.client.post(
            self.exchange_url, {"idempotency_key": "not-the-key"}, format="json",
        )
        self.assertEqual(response.status_code, 404, response.content)

    def test_another_bookings_key_cannot_unlock_this_one(self):
        other = make_booking(
            self.guest, self.room_type, [self.room],
            check_in=hotel_today() + timedelta(days=10),
            check_out=hotel_today() + timedelta(days=12),
            total="40000.00",
        )
        other.idempotency_key = "idem-other-booking"
        other.save(update_fields=["idempotency_key"])
        response = self.client.post(
            self.exchange_url, {"idempotency_key": "idem-other-booking"}, format="json",
        )
        self.assertEqual(response.status_code, 404, response.content)

    def test_a_booking_without_a_stored_key_cannot_be_reclaimed(self):
        Booking.objects.filter(pk=self.booking.pk).update(idempotency_key="")
        response = self.client.post(
            self.exchange_url, {"idempotency_key": self.booking.idempotency_key}, format="json",
        )
        self.assertEqual(response.status_code, 404, response.content)

    def test_an_empty_request_is_rejected_rather_than_guessed(self):
        response = self.client.post(self.exchange_url, {}, format="json")
        self.assertEqual(response.status_code, 400, response.content)

    def test_reclaiming_rotates_the_previous_token(self):
        first = self.client.post(
            self.exchange_url, {"idempotency_key": self.booking.idempotency_key}, format="json",
        ).json()["data"]["guest_access_token"]
        second = self.client.post(
            self.exchange_url, {"idempotency_key": self.booking.idempotency_key}, format="json",
        ).json()["data"]["guest_access_token"]
        self.assertNotEqual(first, second)
        # One live credential at a time: the superseded token is refused.
        stale = self.client.get(self.detail_url, **self._guest_headers(first))
        self.assertEqual(stale.status_code, 404, stale.content)
        fresh = self.client.get(self.detail_url, **self._guest_headers(second))
        self.assertEqual(fresh.status_code, 200, fresh.content)


class RealCreationPathTests(BaseAPITestCase):
    """The reclaim path must work against a booking made by POST /api/bookings/.

    The unit tests above set ``idempotency_key`` by hand; this one proves the
    creation flow actually persists the plaintext key the browser sent, which is
    the whole basis of in-place recovery.
    """

    def setUp(self):
        super().setUp()
        hotel_settings()
        self.room_type = make_room_type("Recovery Deluxe", price="20000.00", max_guests=2)
        self.room = make_room(self.room_type, "952")
        today = hotel_today()
        self.payload = {
            "room_type": self.room_type.slug,
            "check_in": (today + timedelta(days=12)).isoformat(),
            "check_out": (today + timedelta(days=14)).isoformat(),
            "rooms": 1,
            "adults": 2,
            "children": 0,
            "guest": {
                "first_name": "Amaka",
                "last_name": "Recovery",
                "email": "amaka.recovery@example.test",
                "phone": "08030000001",
            },
        }

    def test_a_booking_created_over_the_api_can_be_reclaimed_with_its_creation_key(self):
        key = "web-" + secrets.token_hex(8)
        created = self.client.post(
            "/api/bookings/", self.payload, format="json", HTTP_IDEMPOTENCY_KEY=key,
        )
        self.assertEqual(created.status_code, 201, created.content)
        reference = created.json()["data"]["booking_reference"]

        # What the browser keeps for later: the reference and its own key.
        self.assertEqual(Booking.objects.get(booking_reference=reference).idempotency_key, key)

        # Simulate the phone that paid nothing and lost its token: no guest
        # token, no bearer, only the values it generated itself.
        reclaimed = self.client.post(
            f"/api/bookings/{reference}/access/", {"idempotency_key": key}, format="json",
        )
        self.assertEqual(reclaimed.status_code, 200, reclaimed.content)
        token = reclaimed.json()["data"]["guest_access_token"]
        self.assertTrue(token)

        detail = self.client.get(
            f"/api/bookings/{reference}/", HTTP_X_GUEST_ACCESS_TOKEN=token,
        )
        self.assertEqual(detail.status_code, 200, detail.content)
        self.assertEqual(detail.json()["data"]["booking_reference"], reference)

    def test_a_key_from_creation_is_still_refused_for_a_different_booking(self):
        first = self.client.post(
            "/api/bookings/", self.payload, format="json",
            HTTP_IDEMPOTENCY_KEY="web-" + secrets.token_hex(8),
        )
        self.assertEqual(first.status_code, 201, first.content)
        other_reference = first.json()["data"]["booking_reference"]

        second_payload = dict(self.payload)
        second_payload["check_in"] = (hotel_today() + timedelta(days=20)).isoformat()
        second_payload["check_out"] = (hotel_today() + timedelta(days=22)).isoformat()
        second_payload["guest"] = dict(self.payload["guest"], email="other.recovery@example.test")
        other_key = "web-" + secrets.token_hex(8)
        second = self.client.post(
            "/api/bookings/", second_payload, format="json", HTTP_IDEMPOTENCY_KEY=other_key,
        )
        self.assertEqual(second.status_code, 201, second.content)

        response = self.client.post(
            f"/api/bookings/{other_reference}/access/", {"idempotency_key": other_key},
            format="json",
        )
        self.assertEqual(response.status_code, 404, response.content)


class StaleAccountSessionTests(_BookingFixture):
    """A dead dashboard JWT in the same browser must not break a guest."""

    def setUp(self):
        super().setUp()
        self.staff_user = make_staff("stale.session@jone.test", role=User.Role.RECEPTIONIST)
        self.expired = AccessToken.for_user(self.staff_user)
        self.expired.set_exp(lifetime=timedelta(seconds=-60))
        self.token = self.client.post(
            self.exchange_url, {"idempotency_key": self.booking.idempotency_key}, format="json",
        ).json()["data"]["guest_access_token"]

    def _headers(self):
        return {
            "HTTP_AUTHORIZATION": f"Bearer {self.expired}",
            **self._guest_headers(self.token),
        }

    def test_an_expired_bearer_does_not_break_the_booking_page(self):
        response = self.client.get(self.detail_url, **self._headers())
        self.assertEqual(response.status_code, 200, response.content)

    def test_an_expired_bearer_does_not_break_payment_initialization(self):
        with override_settings(PAYSTACK_SECRET_KEY=""):
            response = self.client.post(
                "/api/payments/initialize/",
                {"booking_reference": self.booking.booking_reference},
                format="json", **self._headers(),
            )
        # It must reach the view: the answer is the honest "payments are not
        # configured", never 401 UNAUTHORIZED.
        self.assertEqual(response.status_code, 503, response.content)
        self.assertEqual(response.json()["code"], "PAYMENT_NOT_CONFIGURED")

    def test_a_garbage_bearer_is_treated_as_anonymous_too(self):
        response = self.client.get(
            self.detail_url,
            HTTP_AUTHORIZATION="Bearer not-a-jwt",
            **self._guest_headers(self.token),
        )
        self.assertEqual(response.status_code, 200, response.content)

    def test_a_valid_staff_session_still_works(self):
        from rest_framework_simplejwt.tokens import RefreshToken

        good = RefreshToken.for_user(self.staff_user).access_token
        response = self.client.get(
            self.detail_url, HTTP_AUTHORIZATION=f"Bearer {good}",
        )
        self.assertEqual(response.status_code, 200, response.content)

    def test_a_bad_bearer_and_no_guest_token_is_still_a_refusal(self):
        """Leniency must not become access: no credential still means 404."""
        response = self.client.get(
            self.detail_url, HTTP_AUTHORIZATION=f"Bearer {self.expired}",
        )
        self.assertEqual(response.status_code, 404, response.content)


class ErrorEnvelopeMessageTests(BaseAPITestCase):
    """A 401 must read like a sentence, not like a Python dict repr."""

    def test_an_invalid_bearer_returns_a_readable_message(self):
        response = self.client.get(
            "/api/auth/profile/", HTTP_AUTHORIZATION="Bearer not-a-jwt",
        )
        self.assertEqual(response.status_code, 401, response.content)
        message = response.json()["message"]
        self.assertIsInstance(message, str)
        self.assertNotIn("{", message)
        self.assertNotIn("ErrorDetail", message)
        self.assertNotIn("code=", message)
        self.assertTrue(message.strip())


class MultiBookingAndAccessLinkRecoveryTests(BaseAPITestCase):
    """End-to-end reproduction and recovery for multi-booking payment initialization."""

    @staticmethod
    def _guest_headers(token: str) -> dict:
        return {"HTTP_X_GUEST_ACCESS_TOKEN": token}

    def setUp(self):
        super().setUp()
        hotel_settings()
        self.room_type = make_room_type("Audit Deluxe", price="25000.00", max_guests=2)
        self.room1 = make_room(self.room_type, "961")
        self.room2 = make_room(self.room_type, "962")

    def _create_online_booking(self, *, days_offset: int, idem_key: str, email: str):
        today = hotel_today()
        return self.client.post(
            "/api/bookings/",
            {
                "room_type": self.room_type.slug,
                "check_in": (today + timedelta(days=days_offset)).isoformat(),
                "check_out": (today + timedelta(days=days_offset + 2)).isoformat(),
                "rooms": 1,
                "adults": 2,
                "children": 0,
                "guest": {
                    "first_name": "Chinedu",
                    "last_name": "Okafor",
                    "email": email,
                    "phone": "08031234567",
                },
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY=idem_key,
        )

    def test_booking_creation_returns_signed_access_link_token_usable_for_recovery_and_payment(self):
        from unittest.mock import patch

        created = self._create_online_booking(
            days_offset=20,
            idem_key="idem-audit-link-0001",
            email="link-recovery@example.test",
        )
        self.assertEqual(created.status_code, 201, created.content)
        data = created.json()["data"]
        ref = data["booking_reference"]
        access_link_token = data.get("access_link_token")
        self.assertTrue(access_link_token, "access_link_token must be returned on creation")

        # Presenting the signed access_link_token directly or via exchange unlocks the booking
        detail = self.client.get(
            f"/api/bookings/{ref}/",
            **self._guest_headers(access_link_token),
        )
        self.assertEqual(detail.status_code, 200, detail.content)

        exchanged = self.client.post(
            f"/api/bookings/{ref}/access/",
            {"access": access_link_token},
            format="json",
        )
        self.assertEqual(exchanged.status_code, 200, exchanged.content)
        fresh_token = exchanged.json()["data"]["guest_access_token"]

        with override_settings(PAYSTACK_SECRET_KEY="sk_test_valid_secret"), patch(
            "apps.payments.services.paystack.initialize_transaction",
            side_effect=lambda **kw: {
                "authorization_url": "https://checkout.paystack.com/audit-ok",
                "access_code": "ac_1",
                "reference": kw["reference"],
            },
        ):
            pay_res = self.client.post(
                "/api/payments/initialize/",
                {"booking_reference": ref},
                format="json",
                **self._guest_headers(fresh_token),
            )
        self.assertEqual(pay_res.status_code, 201, pay_res.content)
        self.assertEqual(
            pay_res.json()["data"]["authorization_url"],
            "https://checkout.paystack.com/audit-ok",
        )

    def test_stale_prior_booking_token_is_recovered_via_second_booking_idempotency_key(self):
        from unittest.mock import patch

        b1 = self._create_online_booking(
            days_offset=25,
            idem_key="idem-prior-booking-01",
            email="guest-b1@example.test",
        ).json()["data"]
        b2 = self._create_online_booking(
            days_offset=28,
            idem_key="idem-second-booking-02",
            email="guest-b2@example.test",
        ).json()["data"]

        # Presenting Booking 1's token for Booking 2 is rejected with 404
        wrong_init = self.client.post(
            "/api/payments/initialize/",
            {"booking_reference": b2["booking_reference"]},
            format="json",
            **self._guest_headers(b1["guest_access_token"]),
        )
        self.assertEqual(wrong_init.status_code, 404, wrong_init.content)

        # Reclaiming Booking 2 via its idempotency_key restores a valid token and initializes payment
        reclaimed = self.client.post(
            f"/api/bookings/{b2['booking_reference']}/access/",
            {"idempotency_key": "idem-second-booking-02"},
            format="json",
        )
        self.assertEqual(reclaimed.status_code, 200, reclaimed.content)
        recovered_token = reclaimed.json()["data"]["guest_access_token"]

        with override_settings(PAYSTACK_SECRET_KEY="sk_test_valid_secret"), patch(
            "apps.payments.services.paystack.initialize_transaction",
            side_effect=lambda **kw: {
                "authorization_url": "https://checkout.paystack.com/recovered-b2",
                "access_code": "ac_2",
                "reference": kw["reference"],
            },
        ):
            ok_init = self.client.post(
                "/api/payments/initialize/",
                {"booking_reference": b2["booking_reference"]},
                format="json",
                **self._guest_headers(recovered_token),
            )
        self.assertEqual(ok_init.status_code, 201, ok_init.content)
        self.assertEqual(
            ok_init.json()["data"]["authorization_url"],
            "https://checkout.paystack.com/recovered-b2",
        )

