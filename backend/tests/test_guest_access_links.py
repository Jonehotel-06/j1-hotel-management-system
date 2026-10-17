# tests/test_guest_access_links.py
"""Emailed booking links must work on any device, and payment refusals must say why.

Two problems this suite pins down:

1. A guest token lives in one browser. A guest who opens the reservation from
   their email on a phone has no credential, so ``POST /api/payments/initialize/``
   answers 404 even though the booking is real and payable. The emailed link now
   carries a signed ``access`` value that the page exchanges for a token.
2. A 404 alone cannot distinguish "no such booking" from "credential rejected".
   The response must stay identical (references are not probe tokens) but the
   server log must name the reason.
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core import mail
from django.test import override_settings

from apps.accounts.models import User
from apps.bookings.models import Booking
from apps.bookings.services import booking_service
from apps.core.utils import hotel_today

from .base import BaseAPITestCase
from .factories import hotel_settings, make_booking, make_guest, make_room, make_room_type


class AccessLinkTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        hotel_settings()
        self.room_type = make_room_type("Access Wing", price="20000.00")
        self.room = make_room(self.room_type, "901")
        self.guest = make_guest("access.link@example.test")
        self.today = hotel_today()
        self.booking = make_booking(
            self.guest, self.room_type, [self.room],
            check_in=self.today + timedelta(days=4),
            check_out=self.today + timedelta(days=6),
            total="40000.00",
        )

    # --- the link itself -----------------------------------------------------
    def test_every_emailed_link_carries_a_signed_access_value(self):
        link = booking_service.guest_booking_link(self.booking)
        self.assertIn(f"ref={self.booking.booking_reference}", link)
        self.assertIn("access=", link)

        from apps.bookings.access_links import read_access_link_token

        value = link.split("access=", 1)[1].split("&", 1)[0]
        self.assertTrue(read_access_link_token(value, self.booking.booking_reference))

    def test_the_link_stays_reference_only_when_signing_fails(self):
        """A broken SECRET_KEY must degrade to today's behaviour, not break email."""
        with patch("apps.bookings.access_links.issue_access_link_token", side_effect=RuntimeError("boom")):
            link = booking_service.guest_booking_link(self.booking)
        self.assertIn(f"ref={self.booking.booking_reference}", link)
        self.assertNotIn("access=", link)

    # --- exchanging it -------------------------------------------------------
    def test_exchange_returns_a_token_that_can_read_the_booking(self):
        access = self._access_value()
        response = self.client.post(
            f"/api/bookings/{self.booking.booking_reference}/access/",
            {"access": access}, format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        token = response.json()["data"]["guest_access_token"]
        self.assertTrue(token)

        detail = self.client.get(
            f"/api/bookings/{self.booking.booking_reference}/",
            HTTP_X_GUEST_ACCESS_TOKEN=token,
        )
        self.assertEqual(detail.status_code, 200, detail.content)
        self.assertEqual(
            detail.json()["data"]["booking_reference"], self.booking.booking_reference
        )

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_mock")
    @patch("apps.payments.services.paystack.initialize_transaction")
    def test_an_exchanged_token_can_start_a_payment(self, mock_init):
        """The whole point: a guest on a fresh device reaches the Paystack URL."""
        mock_init.side_effect = lambda **kw: {
            "authorization_url": "https://checkout.paystack.com/ok",
            "access_code": "ok",
            "reference": kw["reference"],
        }
        response = self.client.post(
            f"/api/bookings/{self.booking.booking_reference}/access/",
            {"access": self._access_value()}, format="json",
        )
        token = response.json()["data"]["guest_access_token"]
        payment = self.client.post(
            "/api/payments/initialize/",
            {"booking_reference": self.booking.booking_reference},
            format="json", HTTP_X_GUEST_ACCESS_TOKEN=token,
        )
        self.assertEqual(payment.status_code, 201, payment.content)
        self.assertEqual(
            payment.json()["data"]["authorization_url"], "https://checkout.paystack.com/ok"
        )

    def test_a_tampered_value_is_rejected(self):
        access = self._access_value()
        response = self.client.post(
            f"/api/bookings/{self.booking.booking_reference}/access/",
            {"access": access[:-3] + "abc"}, format="json",
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "RESOURCE_NOT_FOUND")

    def test_an_expired_value_is_rejected(self):
        with override_settings(BOOKING_ACCESS_LINK_MAX_AGE=-1):
            response = self.client.post(
                f"/api/bookings/{self.booking.booking_reference}/access/",
                {"access": self._access_value()}, format="json",
            )
        self.assertEqual(response.status_code, 404)

    def test_a_value_for_one_booking_cannot_unlock_another(self):
        other_room = make_room(self.room_type, "902")
        other = make_booking(
            make_guest("other@example.test"), self.room_type, [other_room],
            check_in=self.today + timedelta(days=8),
            check_out=self.today + timedelta(days=9), total="20000.00",
        )
        response = self.client.post(
            f"/api/bookings/{other.booking_reference}/access/",
            {"access": self._access_value()}, format="json",
        )
        self.assertEqual(response.status_code, 404)

    def test_an_unknown_reference_gets_the_same_404(self):
        response = self.client.post(
            "/api/bookings/J1-NOT-A-BOOKING/access/",
            {"access": self._access_value()}, format="json",
        )
        self.assertEqual(response.status_code, 404)

    # --- re-sending ----------------------------------------------------------
    def test_resend_reports_success_without_revealing_whether_the_booking_exists(self):
        response = self.client.post(
            "/api/bookings/J1-DOES-NOT-EXIST/access-link/", {}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["data"], {"sent": True})
        self.assertEqual(len(mail.outbox), 0)

    def test_resend_emails_the_address_on_the_reservation(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                f"/api/bookings/{self.booking.booking_reference}/access-link/",
                {}, format="json",
            )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.guest.email])
        self.assertIn("access=", mail.outbox[0].body)

    def test_resend_is_capped_per_booking(self):
        for _ in range(5):
            with self.captureOnCommitCallbacks(execute=True):
                self.client.post(
                    f"/api/bookings/{self.booking.booking_reference}/access-link/",
                    {}, format="json",
                )
        self.assertEqual(len(mail.outbox), 3)

    # --- the failure this replaced -------------------------------------------
    def test_a_reference_alone_still_cannot_start_a_payment(self):
        """Regression guard: adding recovery must not make references payable."""
        response = self.client.post(
            "/api/payments/initialize/",
            {"booking_reference": self.booking.booking_reference}, format="json",
        )
        self.assertEqual(response.status_code, 404)

    def test_the_refusal_is_logged_with_the_reason(self):
        with self.assertLogs("apps", level="WARNING") as captured:
            self.client.post(
                "/api/payments/initialize/",
                {"booking_reference": self.booking.booking_reference}, format="json",
            )
        joined = "\n".join(captured.output)
        self.assertIn("credential rejected", joined)
        self.assertIn(self.booking.booking_reference, joined)
        self.assertIn("guest_token_present=False", joined)

    def test_an_unknown_reference_is_logged_as_not_found(self):
        with self.assertLogs("apps", level="WARNING") as captured:
            self.client.post(
                "/api/payments/initialize/",
                {"booking_reference": "J1-20260101-00000000"}, format="json",
            )
        self.assertIn("no booking matches reference", "\n".join(captured.output))

    # --- helpers -------------------------------------------------------------
    def _access_value(self):
        from apps.bookings.access_links import issue_access_link_token

        return issue_access_link_token(self.booking)
