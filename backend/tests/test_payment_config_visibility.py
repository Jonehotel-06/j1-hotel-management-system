# tests/test_payment_config_visibility.py
"""Online payments being switched OFF must be visible before a guest finds it.

The reported symptom was a guest completing a booking and every payment attempt
answering HTTP 503 `PAYMENT_NOT_CONFIGURED`, with the only clue in a server log.
A deployment with no Paystack secret key (or a public key pasted into the secret
slot) fails this way while the rest of the site looks healthy, so the state is
now reported to staff — on the settings endpoint and from a management command —
without ever exposing the key.
"""
import io

from django.core.management import call_command
from django.test import override_settings

from apps.accounts.models import User
from apps.payments.services.paystack import (
    KEY_MODE_LIVE,
    KEY_MODE_NONE,
    KEY_MODE_TEST,
    secret_key_status,
)

from .base import BaseAPITestCase
from .factories import hotel_settings, make_staff

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
