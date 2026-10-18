# tests/test_payment_initialization_recovery.py
"""Phase 8 coverage for the reported HTTP 503 payment-initialization failure.

The reported incident: a public room booking saved a booking (reference
``J1-20261018-E013B22C``) and the browser got ``HTTP 503`` with
``{"success": false, "code": "PAYMENT_NOT_CONFIGURED"}`` from
``POST /api/payments/initialize/``.

These tests pin the behaviour that must hold on BOTH sides of the fix:

* the configuration gate refuses honestly (503, a stable code, no leaked
  secret, no orphan payment row) and never fakes a success;
* with a usable key the very same request reaches Paystack with the
  BACKEND's amount, in kobo, exactly once, in NGN, for the right booking;
* a failed/abandoned attempt can be retried without duplicating the booking,
  the room assignment or the money, and without reusing a successful
  reference;
* a browser redirect alone can never mark a booking paid, and an
  already-paid booking can never be charged again.

Nothing here touches the network: every Paystack call is mocked.
"""
import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.cache import cache
from django.test import override_settings

from apps.bookings.models import Booking
from apps.core.utils import hotel_today
from apps.payments.models import Payment

from .base import BaseAPITestCase
from .factories import (
    hotel_settings,
    make_room,
    make_room_type,
    make_user,
)

# A syntactically valid TEST key. It is never sent anywhere: every Paystack
# call in this file is mocked, and the value is not a real credential.
TEST_KEY = "sk_test_mock"


class PaymentInitializationBase(BaseAPITestCase):
    """A public, guest-facing booking over real HTTP (no JWT)."""

    price = "50000.00"
    nights = 2
    rooms = 1
    #: physical rooms held for this room type — enough for a second booking
    #: over the same dates, so a retry/reference test never collides with
    #: room availability.
    physical_rooms = 4

    def setUp(self):
        super().setUp()
        hotel_settings()
        self.room_type = make_room_type("Deluxe", price=self.price, max_guests=2)
        for index in range(1, self.physical_rooms + 1):
            make_room(self.room_type, f"2{index:02d}")
        self.today = hotel_today()

    def create_booking(self, *, rooms=None, email="guest@example.test"):
        rooms = rooms or self.rooms
        response = self.client.post("/api/bookings/", {
            "room_type": self.room_type.slug,
            "check_in": (self.today + timedelta(days=5)).isoformat(),
            "check_out": (self.today + timedelta(days=5 + self.nights)).isoformat(),
            "rooms": rooms, "adults": 2, "children": 0,
            "guest": {
                "first_name": "Test", "last_name": "Guest",
                "email": email, "phone": "08000000000",
            },
        }, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()["data"]
        self.guest_token = data["guest_access_token"]
        return Booking.objects.get(booking_reference=data["booking_reference"])

    def initialize(self, booking, payload=None):
        return self.client.post(
            "/api/payments/initialize/",
            payload if payload is not None else {"booking_reference": booking.booking_reference},
            format="json", HTTP_X_GUEST_ACCESS_TOKEN=self.guest_token,
        )

    def verify(self, reference):
        return self.client.get(
            f"/api/payments/verify/{reference}/",
            HTTP_X_GUEST_ACCESS_TOKEN=self.guest_token,
        )

    @staticmethod
    def _ok_initialize(reference):
        return {
            "authorization_url": f"https://checkout.paystack.com/{reference}",
            "access_code": "access-code",
            "reference": reference,
        }


# ---------------------------------------------------------------------------
# The 503 contract: honest, safe, and non-destructive
# ---------------------------------------------------------------------------
class PaymentNotConfiguredContractTests(PaymentInitializationBase):
    """A missing Paystack key must fail loudly and safely — never silently."""

    def test_the_503_keeps_the_existing_error_contract(self):
        booking = self.create_booking()
        with override_settings(PAYSTACK_SECRET_KEY=""):
            response = self.initialize(booking)
        self.assertEqual(response.status_code, 503, response.content)
        body = response.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["code"], "PAYMENT_NOT_CONFIGURED")
        self.assertEqual(body["message"], "Online payments are not configured. Please contact the hotel.")

    def test_the_503_never_leaks_a_credential_or_a_settings_dump(self):
        booking = self.create_booking()
        with override_settings(PAYSTACK_SECRET_KEY=""):
            response = self.initialize(booking)
        raw = response.content.decode()
        for leak in ("sk_live_", "sk_test_", "PAYSTACK_SECRET_KEY", "DJANGO_SECRET_KEY", "Traceback"):
            self.assertNotIn(leak, raw)

    def test_the_503_creates_no_payment_row_and_no_orphaned_attempt(self):
        booking = self.create_booking()
        with override_settings(PAYSTACK_SECRET_KEY=""):
            self.initialize(booking)
        self.assertEqual(Payment.objects.count(), 0)

    def test_the_booking_survives_the_503_with_its_hold_and_its_unpaid_status(self):
        """Phase 4: the hotel's policy is to keep the unpaid booking."""
        booking = self.create_booking(rooms=2)
        before = {
            "status": booking.status,
            "payment_status": booking.payment_status,
            "amount_due": booking.amount_due,
            "expires_at": booking.expires_at,
            "rooms": booking.room_assignments.count(),
        }
        with override_settings(PAYSTACK_SECRET_KEY=""):
            self.initialize(booking)

        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.PENDING)
        self.assertEqual(booking.payment_status, Booking.PaymentStatus.UNPAID)
        self.assertEqual(booking.amount_due, before["amount_due"])
        self.assertIsNotNone(booking.expires_at, "the room hold must survive a payment outage")
        self.assertEqual(booking.expires_at, before["expires_at"])
        self.assertEqual(booking.room_assignments.count(), before["rooms"])
        self.assertEqual(booking.payment_status, Booking.PaymentStatus.UNPAID)

    def test_a_public_key_in_the_secret_slot_is_also_a_configuration_refusal(self):
        booking = self.create_booking()
        with override_settings(PAYSTACK_SECRET_KEY="pk_test_abc"):
            response = self.initialize(booking)
        self.assertEqual(response.status_code, 503, response.content)
        self.assertEqual(response.json()["code"], "PAYMENT_NOT_CONFIGURED")
        self.assertEqual(Payment.objects.count(), 0)

    def test_a_whitespace_only_key_is_a_configuration_refusal(self):
        booking = self.create_booking()
        with override_settings(PAYSTACK_SECRET_KEY="   \n\t "):
            response = self.initialize(booking)
        self.assertEqual(response.status_code, 503, response.content)
        self.assertEqual(response.json()["code"], "PAYMENT_NOT_CONFIGURED")


# ---------------------------------------------------------------------------
# Amount integrity: the backend's amount, in kobo, exactly once, in NGN
# ---------------------------------------------------------------------------
class PaymentAmountIntegrityTests(PaymentInitializationBase):
    rooms = 2

    @override_settings(PAYSTACK_SECRET_KEY=TEST_KEY)
    @patch("apps.payments.services.paystack.initialize_transaction")
    def test_a_multi_room_booking_is_charged_its_full_total_in_kobo_exactly_once(self, mock_init):
        booking = self.create_booking(rooms=2)
        # 2 rooms x 2 nights x 50,000 = 200,000 naira = 20,000,000 kobo.
        self.assertEqual(booking.total_amount, Decimal("200000.00"))
        mock_init.side_effect = lambda **kw: self._ok_initialize(kw["reference"])

        response = self.initialize(booking)

        self.assertEqual(response.status_code, 201, response.content)
        kwargs = mock_init.call_args.kwargs
        self.assertEqual(kwargs["amount_kobo"], 20_000_000)
        # Not naira-as-kobo (200,000) and not double-converted (2,000,000,000).
        self.assertNotEqual(kwargs["amount_kobo"], 200_000)
        self.assertNotEqual(kwargs["amount_kobo"], 2_000_000_000)
        self.assertEqual(response.json()["data"]["amount"], "200000.00")
        self.assertEqual(response.json()["data"]["currency"], "NGN")

    @override_settings(PAYSTACK_SECRET_KEY=TEST_KEY)
    @patch("apps.payments.services.paystack.initialize_transaction")
    def test_the_payment_row_and_booking_stay_in_ngn(self, mock_init):
        booking = self.create_booking()
        mock_init.side_effect = lambda **kw: self._ok_initialize(kw["reference"])
        self.initialize(booking)
        payment = Payment.objects.get()
        self.assertEqual(payment.currency, "NGN")
        self.assertEqual(payment.amount, booking.total_amount)
        self.assertEqual(booking.currency, "NGN")

    @override_settings(PAYSTACK_SECRET_KEY=TEST_KEY)
    @patch("apps.payments.services.paystack.initialize_transaction")
    def test_a_client_supplied_amount_is_ignored(self, mock_init):
        """The browser must never be able to choose what it is charged."""
        booking = self.create_booking()
        mock_init.side_effect = lambda **kw: self._ok_initialize(kw["reference"])
        response = self.initialize(booking, {
            "booking_reference": booking.booking_reference,
            "amount": "1.00", "amount_kobo": 100, "currency": "USD",
        })
        self.assertEqual(response.status_code, 201, response.content)
        kwargs = mock_init.call_args.kwargs
        # The 2-room booking total (200,000 naira) is what is charged — the
        # client's "amount": "1.00" is nowhere in the request.
        self.assertEqual(kwargs["amount_kobo"], 20_000_000)
        self.assertEqual(Payment.objects.get().amount, Decimal("200000.00"))

    @override_settings(PAYSTACK_SECRET_KEY=TEST_KEY)
    @patch("apps.payments.services.paystack.initialize_transaction")
    def test_the_initialization_call_carries_the_guest_email_reference_and_callback(self, mock_init):
        booking = self.create_booking(email="payer@example.test")
        mock_init.side_effect = lambda **kw: self._ok_initialize(kw["reference"])

        self.initialize(booking)

        kwargs = mock_init.call_args.kwargs
        self.assertEqual(kwargs["email"], "payer@example.test")
        self.assertEqual(kwargs["reference"], Payment.objects.get().reference)
        self.assertTrue(kwargs["callback_url"].endswith("/payment-verify.html"))
        self.assertEqual(kwargs["metadata"]["booking_reference"], booking.booking_reference)
        self.assertEqual(kwargs["metadata"]["payment_reference"], kwargs["reference"])


# ---------------------------------------------------------------------------
# Reference integrity and retry safety
# ---------------------------------------------------------------------------
class PaymentRetrySafetyTests(PaymentInitializationBase):
    @override_settings(PAYSTACK_SECRET_KEY=TEST_KEY)
    @patch("apps.payments.services.paystack.initialize_transaction")
    def test_a_gateway_failure_can_be_retried_on_the_same_booking_and_reference(self, mock_init):
        from apps.core.exceptions import PaymentGatewayError

        attempts = {"count": 0}

        def flaky(**kwargs):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise PaymentGatewayError()
            return self._ok_initialize(kwargs["reference"])

        mock_init.side_effect = flaky
        booking = self.create_booking()

        failed = self.initialize(booking)
        self.assertEqual(failed.status_code, 502, failed.content)
        original_reference = Payment.objects.get().reference

        retried = self.initialize(booking)
        self.assertEqual(retried.status_code, 201, retried.content)
        self.assertEqual(retried.json()["data"]["reference"], original_reference)
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(Booking.objects.count(), 1)
        self.assertEqual(booking.room_assignments.count(), self.rooms)

    @override_settings(PAYSTACK_SECRET_KEY=TEST_KEY)
    @patch("apps.payments.services.paystack.initialize_transaction")
    def test_a_new_attempt_after_a_failed_payment_gets_a_new_reference_and_no_new_booking(self, mock_init):
        """An abandoned/failed attempt must not be re-charged on its old reference."""
        booking = self.create_booking()
        failed = Payment.objects.create(
            booking=booking, reference="J1P-ABANDONED-1",
            provider=Payment.Provider.PAYSTACK, amount=booking.total_amount,
            currency="NGN", status=Payment.Status.FAILED,
        )
        mock_init.side_effect = lambda **kw: self._ok_initialize(kw["reference"])

        response = self.initialize(booking)

        self.assertEqual(response.status_code, 201, response.content)
        new_reference = response.json()["data"]["reference"]
        self.assertNotEqual(new_reference, failed.reference)
        self.assertEqual(Payment.objects.count(), 2)
        self.assertEqual(Booking.objects.count(), 1)
        self.assertEqual(booking.room_assignments.count(), self.rooms)

    @override_settings(PAYSTACK_SECRET_KEY=TEST_KEY)
    @patch("apps.payments.services.paystack.initialize_transaction")
    def test_a_successful_reference_is_never_reused_for_a_new_charge(self, mock_init):
        booking = self.create_booking()
        mock_init.side_effect = lambda **kw: self._ok_initialize(kw["reference"])
        first = self.initialize(booking)
        first_reference = first.json()["data"]["reference"]

        # A second booking for the same stay must not reuse the first reference.
        other = self.create_booking(email="other@example.test")
        second = self.initialize(other)
        self.assertNotEqual(second.json()["data"]["reference"], first_reference)
        self.assertEqual(Payment.objects.filter(reference=first_reference).count(), 1)

    def test_the_booking_reference_format_is_preserved(self):
        booking = self.create_booking()
        self.assertRegex(booking.booking_reference, r"^J1-\d{8}-[0-9A-F]{8}$")

    def test_payment_references_are_unique_and_paystack_safe(self):
        booking = self.create_booking()
        references = {Payment.objects.create(
            booking=booking, reference=f"J1P-UNIQUE-{index}",
            provider=Payment.Provider.PAYSTACK, amount=Decimal("1.00"),
            currency="NGN", status=Payment.Status.PENDING,
        ).reference for index in range(5)}
        self.assertEqual(len(references), 5)
        for reference in references:
            self.assertLessEqual(len(reference), 40)


# ---------------------------------------------------------------------------
# Verification safety: a redirect is not proof of payment
# ---------------------------------------------------------------------------
class PaymentVerificationSafetyTests(PaymentInitializationBase):
    @override_settings(PAYSTACK_SECRET_KEY=TEST_KEY)
    @patch("apps.payments.services.paystack.verify_transaction")
    def test_a_browser_redirect_without_a_reference_cannot_mark_the_booking_paid(self, mock_verify):
        booking = self.create_booking()
        response = self.client.get("/api/payments/verify/")
        self.assertEqual(response.status_code, 404, response.content)
        mock_verify.assert_not_called()
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.PENDING)
        self.assertEqual(booking.payment_status, Booking.PaymentStatus.UNPAID)
        self.assertEqual(booking.amount_paid, Decimal("0.00"))

    @override_settings(PAYSTACK_SECRET_KEY=TEST_KEY)
    @patch("apps.payments.services.paystack.verify_transaction")
    def test_an_unknown_reference_never_credits_a_booking(self, mock_verify):
        booking = self.create_booking()
        response = self.client.get("/api/payments/verify/J1P-DOES-NOT-EXIST/")
        self.assertEqual(response.status_code, 404, response.content)
        mock_verify.assert_not_called()
        booking.refresh_from_db()
        self.assertEqual(booking.amount_paid, Decimal("0.00"))

    @override_settings(PAYSTACK_SECRET_KEY=TEST_KEY)
    @patch("apps.payments.services.paystack.verify_transaction")
    def test_a_gateway_rejection_during_verification_leaves_the_booking_unpaid(self, mock_verify):
        from apps.core.exceptions import PaymentError

        booking = self.create_booking()
        payment = Payment.objects.create(
            booking=booking, reference="J1P-VERIFY-REJECTED",
            provider=Payment.Provider.PAYSTACK, amount=booking.total_amount,
            currency="NGN", status=Payment.Status.PENDING,
        )
        mock_verify.side_effect = PaymentError("The transaction could not be verified with the payment provider.")

        response = self.verify(payment.reference)

        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()["code"], "PAYMENT_FAILED")
        payment.refresh_from_db()
        booking.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.PENDING)
        self.assertEqual(booking.amount_paid, Decimal("0.00"))
        self.assertEqual(booking.payment_status, Booking.PaymentStatus.UNPAID)

    @override_settings(PAYSTACK_SECRET_KEY=TEST_KEY)
    @patch("apps.payments.services.paystack.initialize_transaction")
    def test_an_already_paid_booking_cannot_be_charged_again(self, mock_init):
        booking = self.create_booking()
        booking.amount_paid = booking.total_amount
        booking.payment_status = Booking.PaymentStatus.PAID
        booking.status = Booking.Status.CONFIRMED
        booking.save(update_fields=["amount_paid", "payment_status", "status"])

        response = self.initialize(booking)

        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()["code"], "PAYMENT_ALREADY_COMPLETED")
        mock_init.assert_not_called()
        self.assertEqual(Payment.objects.count(), 0)

    @override_settings(PAYSTACK_SECRET_KEY=TEST_KEY)
    @patch("apps.payments.services.paystack.verify_transaction")
    def test_repeated_verification_credits_the_booking_only_once(self, mock_verify):
        booking = self.create_booking()
        payment = Payment.objects.create(
            booking=booking, reference="J1P-REPEAT-VERIFY",
            provider=Payment.Provider.PAYSTACK, amount=booking.total_amount,
            currency="NGN", status=Payment.Status.PENDING,
        )
        mock_verify.return_value = {
            "status": True, "message": "Verification successful",
            "data": {
                "status": "success", "reference": payment.reference,
                "amount": 10_000_000, "currency": "NGN", "channel": "card",
                "gateway_response": "Successful", "id": 424242,
                "paid_at": "2026-10-18T10:00:00Z",
            },
        }

        for _ in range(3):
            response = self.verify(payment.reference)
            self.assertEqual(response.status_code, 200, response.content)

        booking.refresh_from_db()
        self.assertEqual(booking.amount_paid, Decimal("100000.00"))
        self.assertEqual(booking.payment_status, Booking.PaymentStatus.PAID)
        self.assertEqual(mock_verify.call_count, 1)

    @override_settings(PAYSTACK_SECRET_KEY=TEST_KEY)
    @patch("apps.payments.services.paystack.verify_transaction")
    def test_a_currency_mismatch_never_credits_the_booking(self, mock_verify):
        booking = self.create_booking()
        payment = Payment.objects.create(
            booking=booking, reference="J1P-WRONG-CURRENCY",
            provider=Payment.Provider.PAYSTACK, amount=booking.total_amount,
            currency="NGN", status=Payment.Status.PENDING,
        )
        mock_verify.return_value = {
            "status": True, "message": "Verification successful",
            "data": {
                "status": "success", "reference": payment.reference,
                "amount": 10_000_000, "currency": "GHS", "channel": "card",
                "gateway_response": "Successful", "id": 424243,
                "paid_at": "2026-10-18T10:00:00Z",
            },
        }

        response = self.verify(payment.reference)

        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()["code"], "PAYMENT_AMOUNT_MISMATCH")
        booking.refresh_from_db()
        self.assertEqual(booking.amount_paid, Decimal("0.00"))
        self.assertEqual(booking.payment_status, Booking.PaymentStatus.UNPAID)


# ---------------------------------------------------------------------------
# The webhook trust boundary
# ---------------------------------------------------------------------------
class WebhookTrustBoundaryTests(PaymentInitializationBase):
    """Paystack signs with the SECRET key.

    A Paystack PUBLIC key (pk_…) is public by design — it is embedded in
    frontend checkout code — so a signature computed with it proves nothing.
    The signature layer must fail closed on exactly the same predicate the
    initialization path uses, otherwise one mistyped deployment variable
    silently weakens the webhook as well as the checkout.
    """

    BODY = json.dumps({
        "event": "charge.success",
        "data": {"reference": "J1P-SIGNATURE-PROBE"},
    }).encode()

    def _signature(self, key):
        import hashlib
        import hmac

        return hmac.new(key.encode(), msg=self.BODY, digestmod=hashlib.sha512).hexdigest()

    def _post(self, key, signature=None):
        return self.client.post(
            "/api/payments/webhook/", data=self.BODY, content_type="application/json",
            HTTP_X_PAYSTACK_SIGNATURE=self._signature(key) if signature is None else signature,
        )

    def test_a_genuine_signature_computed_with_the_secret_key_is_accepted(self):
        with override_settings(PAYSTACK_SECRET_KEY=TEST_KEY):
            response = self._post(TEST_KEY)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["success"])

    def test_a_signature_computed_with_a_public_key_is_rejected(self):
        """A public key is public: anyone could forge this signature."""
        with override_settings(PAYSTACK_SECRET_KEY="pk_test_public"):
            response = self._post("pk_test_public")
        self.assertEqual(response.status_code, 401, response.content)
        self.assertEqual(response.json()["code"], "INVALID_SIGNATURE")

    def test_a_missing_key_rejects_every_webhook(self):
        with override_settings(PAYSTACK_SECRET_KEY=""):
            response = self._post(TEST_KEY)
        self.assertEqual(response.status_code, 401, response.content)
        self.assertEqual(response.json()["code"], "INVALID_SIGNATURE")

    def test_a_whitespace_padded_stored_key_still_accepts_a_genuine_signature(self):
        """The stored value is normalised the same way the API calls normalise it."""
        with override_settings(PAYSTACK_SECRET_KEY=f"  {TEST_KEY}\n"):
            response = self._post(TEST_KEY)
        self.assertEqual(response.status_code, 200, response.content)

    def test_a_tampered_body_is_rejected(self):
        import hashlib
        import hmac

        with override_settings(PAYSTACK_SECRET_KEY=TEST_KEY):
            response = self.client.post(
                "/api/payments/webhook/",
                data=b'{"event":"charge.success","data":{"reference":"J1P-TAMPERED"}}',
                content_type="application/json",
                HTTP_X_PAYSTACK_SIGNATURE=hmac.new(
                    TEST_KEY.encode(), msg=self.BODY, digestmod=hashlib.sha512).hexdigest(),
            )
        self.assertEqual(response.status_code, 401, response.content)
