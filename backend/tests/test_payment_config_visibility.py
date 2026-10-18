# tests/test_payment_config_visibility.py
"""Online payments being switched OFF must be visible before a guest finds it.

The reported symptom was a guest completing a booking and every payment attempt
answering HTTP 503 `PAYMENT_NOT_CONFIGURED`, with the only clue in a server log.
A deployment with no Paystack secret key (or a public key pasted into the secret
slot) fails this way while the rest of the site looks healthy, so the state is
now reported to staff — on the settings endpoint and from a management command —
without ever exposing the key.
"""
import hashlib
import hmac
import io
from datetime import timedelta

from django.core.management import call_command
from django.test import override_settings

from apps.accounts.models import User
from apps.bookings.models import Booking
from apps.core.exceptions import PaymentNotConfiguredError
from apps.core.utils import hotel_today
from apps.payments.models import Payment
from apps.payments.services import payment_service
from apps.payments.services.paystack import (
    KEY_MODE_LIVE,
    KEY_MODE_NONE,
    KEY_MODE_TEST,
    PROBLEM_MISSING,
    PROBLEM_PUBLIC_KEY,
    PROBLEM_UNRECOGNIZED,
    secret_key_problem,
    secret_key_status,
)

from .base import BaseAPITestCase
from .factories import (
    hotel_settings,
    make_booking,
    make_guest,
    make_room,
    make_room_type,
    make_staff,
)

SETTINGS_URL = "/api/admin/settings/"


class SecretKeyStatusTests(BaseAPITestCase):
    def test_a_blank_key_is_not_configured(self):
        with override_settings(PAYSTACK_SECRET_KEY=""):
            self.assertEqual(secret_key_status(), (False, KEY_MODE_NONE))
        with override_settings(PAYSTACK_SECRET_KEY="   "):
            self.assertEqual(secret_key_status(), (False, KEY_MODE_NONE))

    def test_a_public_key_in_the_secret_slot_is_not_configured(self):
        for value in ("pk_live_abc", "pk_test_abc"):
            with override_settings(PAYSTACK_SECRET_KEY=value):
                self.assertEqual(secret_key_status(), (False, KEY_MODE_NONE), value)

    def test_an_unrecognisable_value_is_not_configured(self):
        with override_settings(PAYSTACK_SECRET_KEY="not-a-key"):
            self.assertEqual(secret_key_status(), (False, KEY_MODE_NONE))

    def test_live_and_test_keys_are_distinguished(self):
        with override_settings(PAYSTACK_SECRET_KEY="sk_live_abc123"):
            self.assertEqual(secret_key_status(), (True, KEY_MODE_LIVE))
        with override_settings(PAYSTACK_SECRET_KEY=" sk_test_abc123\n"):
            self.assertEqual(secret_key_status(), (True, KEY_MODE_TEST), "surrounding whitespace is tolerated")


class SettingsPayloadTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        hotel_settings()
        self.admin = make_staff("pay-admin@jone.test", role=User.Role.ADMIN)

    def _payload(self):
        self.auth(self.admin)
        response = self.client.get(SETTINGS_URL)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["data"]

    def test_the_settings_payload_reports_that_online_payments_are_off(self):
        with override_settings(PAYSTACK_SECRET_KEY=""):
            data = self._payload()
        self.assertFalse(data["online_payments_configured"])
        self.assertEqual(data["online_payments_mode"], "")

    def test_the_settings_payload_reports_a_live_key(self):
        with override_settings(PAYSTACK_SECRET_KEY="sk_live_secret_value"):
            data = self._payload()
        self.assertTrue(data["online_payments_configured"])
        self.assertEqual(data["online_payments_mode"], KEY_MODE_LIVE)

    def test_a_test_key_is_reported_as_test(self):
        with override_settings(PAYSTACK_SECRET_KEY="sk_test_secret_value"):
            data = self._payload()
        self.assertTrue(data["online_payments_configured"])
        self.assertEqual(data["online_payments_mode"], KEY_MODE_TEST)

    def test_the_key_never_appears_in_the_settings_payload(self):
        secret = "sk_live_do_not_leak_this_value"
        with override_settings(PAYSTACK_SECRET_KEY=secret):
            response_text = str(self._payload())
        self.assertNotIn(secret, response_text)
        self.assertNotIn("PAYSTACK_SECRET_KEY", response_text)


class CheckPaymentConfigCommandTests(BaseAPITestCase):
    def _run(self, *args):
        out = io.StringIO()
        call_command("check_payment_config", *args, stdout=out)
        return out.getvalue()

    def test_it_says_online_payments_are_off_when_the_key_is_missing(self):
        with override_settings(PAYSTACK_SECRET_KEY=""):
            output = self._run()
        self.assertIn("OFF", output)
        self.assertIn("PAYSTACK_SECRET_KEY", output)
        self.assertIn("503", output)

    def test_it_explains_a_public_key_instead_of_a_secret(self):
        with override_settings(PAYSTACK_SECRET_KEY="pk_test_abc"):
            output = self._run()
        self.assertIn("PUBLIC key", output)
        self.assertIn("sk_", output)

    def test_it_warns_that_a_test_key_takes_no_money(self):
        with override_settings(PAYSTACK_SECRET_KEY="sk_test_abc"):
            output = self._run()
        self.assertIn("TEST", output)
        self.assertIn("no money", output)

    def test_it_never_prints_the_key(self):
        secret = "sk_live_never_print_me"
        with override_settings(PAYSTACK_SECRET_KEY=secret):
            output = self._run()
        self.assertNotIn(secret, output)
        self.assertNotIn("never_print_me", output)

    def test_it_flags_a_callback_url_that_is_not_public_https(self):
        with override_settings(
            PAYSTACK_SECRET_KEY="sk_live_abc",
            PAYMENT_CALLBACK_URL="http://localhost:5500/payment-verify.html",
        ):
            output = self._run()
        self.assertIn("public HTTPS", output)

    def test_verify_is_skipped_without_a_key(self):
        with override_settings(PAYSTACK_SECRET_KEY=""):
            output = self._run("--verify")
        self.assertIn("skipped", output)


class SecretKeyProblemTests(BaseAPITestCase):
    """The operator-facing reason category behind a 503 (never the value)."""

    def test_categories_distinguish_the_three_fixes(self):
        cases = [
            ("", PROBLEM_MISSING),
            ("   ", PROBLEM_MISSING),
            ("pk_live_abc", PROBLEM_PUBLIC_KEY),
            ("pk_test_abc", PROBLEM_PUBLIC_KEY),
            ("not-a-key", PROBLEM_UNRECOGNIZED),
        ]
        for value, expected in cases:
            with override_settings(PAYSTACK_SECRET_KEY=value):
                self.assertEqual(secret_key_problem(), expected, value)

    def test_a_usable_key_has_no_problem(self):
        for value in ("sk_live_abc123", "sk_test_abc123", " sk_test_abc123\n"):
            with override_settings(PAYSTACK_SECRET_KEY=value):
                self.assertEqual(secret_key_problem(), "", value)


class InitializationRefusalLoggingTests(BaseAPITestCase):
    """A refused initialization must tell the operator WHY, without leaking.

    Before this, the 503 reached the guest and the access log, but nothing
    recorded whether the deployment key was missing, a public key, or junk —
    the three states need different fixes.
    """

    def setUp(self):
        super().setUp()
        hotel_settings()
        room_type = make_room_type("Config Log Wing", price="20000.00")
        room = make_room(room_type, "CL1")
        guest = make_guest("configlog@example.test")
        today = hotel_today()
        self.booking = make_booking(
            guest, room_type, [room],
            check_in=today + timedelta(days=4),
            check_out=today + timedelta(days=6),
            status=Booking.Status.PENDING, total="40000.00",
        )

    def _refuse(self, key_value):
        with override_settings(PAYSTACK_SECRET_KEY=key_value):
            with self.assertLogs("apps", level="ERROR") as logs:
                with self.assertRaises(PaymentNotConfiguredError):
                    payment_service.initialize_booking_payment(
                        booking=self.booking, user=None
                    )
        return "\n".join(logs.output)

    def test_missing_key_is_logged_as_missing(self):
        output = self._refuse("")
        self.assertIn(PROBLEM_MISSING, output)
        self.assertIn("PAYSTACK_SECRET_KEY", output)

    def test_public_key_is_logged_as_public_key(self):
        output = self._refuse("pk_test_do_not_log_me")
        self.assertIn(PROBLEM_PUBLIC_KEY, output)
        self.assertNotIn("do_not_log_me", output)

    def test_unrecognised_key_is_logged_without_its_value(self):
        output = self._refuse("junk-value-do-not-log")
        self.assertIn(PROBLEM_UNRECOGNIZED, output)
        self.assertNotIn("junk-value-do-not-log", output)

    def test_refusal_preserves_the_unpaid_booking_and_creates_no_payment(self):
        self._refuse("")
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, Booking.Status.PENDING)
        self.assertEqual(self.booking.payment_status, Booking.PaymentStatus.UNPAID)
        self.assertEqual(Payment.objects.count(), 0)


class WebhookSignatureNormalisationTests(BaseAPITestCase):
    """Webhook HMAC must use the key exactly as the API path normalises it.

    Initialization strips surrounding whitespace from the stored key; the
    webhook verifier did not, so a key pasted with a trailing newline made
    every genuine Paystack webhook fail signature validation (401) while
    browser verification kept working.
    """

    BODY = b'{"event":"charge.success","data":{"reference":"J1P-UNKNOWN-REF"}}'

    def _sign(self, secret="sk_test_mock"):
        return hmac.new(
            secret.encode(), msg=self.BODY, digestmod=hashlib.sha512
        ).hexdigest()

    @override_settings(PAYSTACK_SECRET_KEY=" sk_test_mock\n")
    def test_genuine_signature_validates_with_a_padded_stored_key(self):
        self.assertTrue(
            payment_service.verify_webhook_signature(self.BODY, self._sign())
        )
        self.assertFalse(
            payment_service.verify_webhook_signature(self.BODY, "bogus")
        )

    @override_settings(PAYSTACK_SECRET_KEY=" sk_test_mock\n")
    def test_webhook_endpoint_accepts_genuine_signature_with_padded_key(self):
        response = self.client.post(
            "/api/payments/webhook/", data=self.BODY,
            content_type="application/json",
            HTTP_X_PAYSTACK_SIGNATURE=self._sign(),
        )
        # Unknown reference is acknowledged once the signature has validated.
        self.assertEqual(response.status_code, 200, response.content)

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_mock")
    def test_webhook_endpoint_still_rejects_a_bad_signature(self):
        response = self.client.post(
            "/api/payments/webhook/", data=self.BODY,
            content_type="application/json",
            HTTP_X_PAYSTACK_SIGNATURE="bogus",
        )
        self.assertEqual(response.status_code, 401)
