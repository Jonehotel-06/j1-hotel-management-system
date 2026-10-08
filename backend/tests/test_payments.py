# tests/test_payments.py
"""PAYMENT tests (spec §32–§36, §88–§89): initialization, verified success,
failures, amount mismatch, webhooks + signatures, idempotency, offline records."""
import hashlib
import hmac
import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.cache import cache
from django.test import override_settings
from rest_framework.throttling import ScopedRateThrottle

from apps.bookings.models import Booking
from apps.core.utils import hotel_today
from apps.notifications.models import Notification
from apps.payments.models import Payment, Refund

from .base import BaseAPITestCase
from .factories import (
    hotel_settings,
    make_booking,
    make_guest,
    make_room,
    make_room_type,
    make_staff,
    make_user,
)
from apps.accounts.models import User


class PaymentFlowTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.room_type = make_room_type("Deluxe", price="25000.00")
        make_room(self.room_type, "201")
        self.user = make_user("payer@pay.dev", password="Str0ng!Pass")
        self.auth(self.user)
        self.today = hotel_today()
        response = self.client.post("/api/bookings/", {
            "room_type": self.room_type.slug,
            "check_in": (self.today + timedelta(days=5)).isoformat(),
            "check_out": (self.today + timedelta(days=7)).isoformat(),
            "rooms": 1, "adults": 2, "children": 0,
        }, format="json")
        self.assertEqual(response.status_code, 201, response.json())
        self.booking_ref = response.json()["data"]["booking_reference"]
        self.booking = Booking.objects.get(booking_reference=self.booking_ref)

    # --- Initialization ------------------------------------------------------
    def test_initialize_without_paystack_config_returns_503(self):
        # Explicitly unset the key: a developer's .env may legitimately carry a
        # real secret, and this test must not depend on the ambient env.
        with override_settings(PAYSTACK_SECRET_KEY=""):
            response = self.client.post("/api/payments/initialize/",
                                        {"booking_reference": self.booking_ref})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "PAYMENT_NOT_CONFIGURED")
        self.assertEqual(Payment.objects.count(), 0)  # rolled back, no orphan record

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_mock")
    @patch("apps.payments.services.paystack.initialize_transaction")
    def test_initialize_creates_payment_and_returns_checkout_payload(self, mock_init):
        mock_init.return_value = {
            "authorization_url": "https://checkout.paystack.com/abc",
            "access_code": "abc",
            "reference": None,  # replaced below with the generated local reference
        }
        mock_init.side_effect = lambda **kw: {
            "authorization_url": "https://checkout.paystack.com/abc",
            "access_code": "abc", "reference": kw["reference"],
        }
        response = self.client.post("/api/payments/initialize/",
                                    {"booking_reference": self.booking_ref})
        self.assertEqual(response.status_code, 201, response.json())
        data = response.json()["data"]
        self.assertEqual(data["authorization_url"], "https://checkout.paystack.com/abc")
        self.assertEqual(data["amount"], "50000.00")   # server-derived amount
        payment = Payment.objects.get()
        self.assertEqual(payment.amount, Decimal("50000.00"))
        # The amount sent to Paystack is the booking's, never client input.
        _, kwargs = mock_init.call_args
        self.assertEqual(kwargs["amount_kobo"], 5_000_000)

    # --- Verification --------------------------------------------------------
    def _make_payment(self):
        return Payment.objects.create(
            booking=self.booking, user=self.user, reference="J1P-TEST-0001",
            provider=Payment.Provider.PAYSTACK, amount=Decimal("50000.00"),
            currency="NGN", status=Payment.Status.PENDING,
        )

    def _paystack_payload(self, reference, amount_kobo=5_000_000, status_value="success"):
        return {
            "status": True,
            "message": "Verification successful",
            "data": {
                "status": status_value,
                "reference": reference,
                "amount": amount_kobo,
                "currency": "NGN",
                "channel": "card",
                "gateway_response": "Successful" if status_value == "success" else "Declined",
                "id": 99887766,
                "paid_at": "2026-09-09T10:00:00Z",
                "ip_address": "127.0.0.1",
            },
        }

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_mock")
    @patch("apps.payments.services.paystack.verify_transaction")
    def test_verify_success_confirms_booking(self, mock_verify):
        payment = self._make_payment()
        mock_verify.return_value = self._paystack_payload(payment.reference)
        response = self.client.get(f"/api/payments/verify/{payment.reference}/")
        self.assertEqual(response.status_code, 200, response.json())
        data = response.json()["data"]
        self.assertEqual(data["payment_status"], "PAID")
        self.assertEqual(data["booking_status"], "CONFIRMED")
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, "CONFIRMED")
        self.assertEqual(self.booking.amount_paid, Decimal("50000.00"))
        self.assertIsNone(self.booking.expires_at)  # hold released

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_mock")
    @patch("apps.payments.services.paystack.verify_transaction")
    def test_verify_is_idempotent(self, mock_verify):
        payment = self._make_payment()
        mock_verify.return_value = self._paystack_payload(payment.reference)
        first = self.client.get(f"/api/payments/verify/{payment.reference}/")
        self.assertEqual(first.status_code, 200)
        # Second call must NOT re-credit the booking or call the gateway again.
        second = self.client.get(f"/api/payments/verify/{payment.reference}/")
        self.assertEqual(second.status_code, 200)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.amount_paid, Decimal("50000.00"))
        self.assertEqual(mock_verify.call_count, 1)

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_mock")
    @patch("apps.payments.services.paystack.verify_transaction")
    def test_verify_failed_payment_does_not_confirm(self, mock_verify):
        payment = self._make_payment()
        mock_verify.return_value = self._paystack_payload(payment.reference, status_value="failed")
        response = self.client.get(f"/api/payments/verify/{payment.reference}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["transaction_status"], "failed")
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, "PENDING")
        self.assertEqual(self.booking.amount_paid, Decimal("0.00"))

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_mock")
    @patch("apps.payments.services.paystack.verify_transaction")
    def test_verify_amount_mismatch_rejected(self, mock_verify):
        payment = self._make_payment()
        mock_verify.return_value = self._paystack_payload(payment.reference, amount_kobo=10)
        response = self.client.get(f"/api/payments/verify/{payment.reference}/")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "PAYMENT_AMOUNT_MISMATCH")
        payment.refresh_from_db()
        self.assertEqual(payment.status, "PENDING")
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.amount_paid, Decimal("0.00"))

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_mock")
    @patch("apps.payments.services.paystack.verify_transaction")
    def test_verify_accepts_fee_inclusive_response_when_requested_amount_is_exact(self, mock_verify):
        payment = self._make_payment()
        payload = self._paystack_payload(payment.reference, amount_kobo=5_101_523)
        payload["data"]["requested_amount"] = 5_000_000
        payload["data"]["fees"] = 101_523
        mock_verify.return_value = payload
        response = self.client.get(f"/api/payments/verify/{payment.reference}/")
        self.assertEqual(response.status_code, 200, response.json())
        payment.refresh_from_db()
        self.booking.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.SUCCESS)
        self.assertEqual(payment.amount, Decimal("50000.00"))
        self.assertEqual(self.booking.amount_paid, Decimal("50000.00"))
        self.assertTrue(payment.metadata["paystack_reported_amount_includes_fee"])

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_mock")
    @patch("apps.payments.services.paystack.verify_transaction")
    def test_verify_accepts_exact_guest_amount_and_records_merchant_fee(self, mock_verify):
        payment = self._make_payment()
        payload = self._paystack_payload(payment.reference, amount_kobo=5_000_000)
        payload["data"]["fees"] = 75_000
        mock_verify.return_value = payload
        response = self.client.get(f"/api/payments/verify/{payment.reference}/")
        self.assertEqual(response.status_code, 200, response.json())
        self.assertIn("receipt", response.json()["data"])
        payment.refresh_from_db()
        self.assertEqual(payment.amount, Decimal("50000.00"))
        self.assertEqual(payment.metadata["paystack_fees_kobo"], 75_000)
        self.assertFalse(payment.metadata["paystack_reported_amount_includes_fee"])

    # --- Webhook -------------------------------------------------------------
    def _post_webhook(self, payload: dict, secret="sk_test_mock", sign=True):
        body = json.dumps(payload).encode()
        signature = hmac.new(secret.encode(), msg=body, digestmod=hashlib.sha512).hexdigest() if sign else "bogus"
        return self.client.post(
            "/api/payments/webhook/", data=body, content_type="application/json",
            HTTP_X_PAYSTACK_SIGNATURE=signature,
        )

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_mock")
    def test_webhook_rejects_invalid_signature(self):
        payment = self._make_payment()
        payload = {"event": "charge.success", "data": {"reference": payment.reference}}
        response = self._post_webhook(payload, sign=False)
        self.assertEqual(response.status_code, 401)
        payment.refresh_from_db()
        self.assertEqual(payment.status, "PENDING")

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_mock")
    @patch("apps.payments.services.paystack.verify_transaction")
    def test_webhook_success_processes_payment(self, mock_verify):
        payment = self._make_payment()
        mock_verify.return_value = self._paystack_payload(payment.reference)
        response = self._post_webhook({
            "event": "charge.success",
            "data": {"reference": payment.reference,
                     "amount": 5_000_000, "currency": "NGN", "status": "success"},
        })
        self.assertEqual(response.status_code, 200)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, "CONFIRMED")
        payment.refresh_from_db()
        self.assertEqual(payment.status, "SUCCESS")

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_mock")
    def test_webhook_unknown_reference_acknowledged(self):
        response = self._post_webhook({
            "event": "charge.success", "data": {"reference": "J1P-UNKNOWN-X"},
        })
        self.assertEqual(response.status_code, 200)

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_mock")
    def test_webhook_refund_processed_marks_payment_refunded(self):
        staff = make_staff("refunds@staff.dev", role=User.Role.ADMIN)
        payment = self._make_payment()
        payment.status = Payment.Status.SUCCESS
        payment.save(update_fields=["status"])
        self.booking.status = "CONFIRMED"
        self.booking.save(update_fields=["status"])
        response = self._post_webhook({
            "event": "refund.processed",
            "data": {"amount": 5_000_000, "transaction": {"reference": payment.reference}},
        })
        self.assertEqual(response.status_code, 200)
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.REFUNDED)
        # A refund does NOT auto-cancel the booking — staff judgment required.
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, "CONFIRMED")
        refund = Refund.objects.get(payment=payment)
        self.assertEqual(refund.status, Refund.Status.PROCESSED)
        self.assertEqual(self.booking.refund_amount, Decimal("50000.00"))
        Notification.objects.get(recipient=staff, type="REFUND_PROCESSED")
        # Idempotent: a duplicate delivery is acknowledged without side effects.
        response = self._post_webhook({
            "event": "refund.processed",
            "data": {"amount": 5_000_000, "transaction": {"reference": payment.reference}},
        })
        self.assertEqual(response.status_code, 200)
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.REFUNDED)
        self.assertEqual(Refund.objects.filter(payment=payment).count(), 1)

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_mock")
    def test_webhook_refund_pending_processing_failed_lifecycle(self):
        make_staff("refund-lifecycle@staff.dev", role=User.Role.ADMIN)
        payment = self._make_payment()
        payment.status = Payment.Status.SUCCESS
        payment.transaction_id = "123456789"
        payment.save(update_fields=["status", "transaction_id"])
        for event, expected in [
            ("refund.pending", Refund.Status.PENDING),
            ("refund.processing", Refund.Status.PROCESSING),
            ("refund.failed", Refund.Status.FAILED),
        ]:
            response = self._post_webhook({
                "event": event,
                "data": {
                    "id": 777, "status": expected.lower(), "amount": 2_500_000,
                    "transaction": {"reference": payment.reference, "id": payment.transaction_id},
                },
            })
            self.assertEqual(response.status_code, 200, response.json())
            self.assertEqual(Refund.objects.get(paystack_refund_id="777").status, expected)

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_mock")
    def test_webhook_dispute_create_notifies_staff_without_state_change(self):
        staff = make_staff("disputes@staff.dev", role=User.Role.ADMIN)
        payment = self._make_payment()
        payment.status = Payment.Status.SUCCESS
        payment.save(update_fields=["status"])
        response = self._post_webhook({
            "event": "charge.dispute.create",
            "data": {"reference": payment.reference, "amount": 5_000_000, "status": "pending"},
        })
        self.assertEqual(response.status_code, 200)  # must ACK, never raise
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.SUCCESS)  # untouched
        Notification.objects.get(recipient=staff, type="PAYMENT_DISPUTED")

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_mock")
    def test_webhook_throttled_after_burst(self):
        # DRF captures THROTTLE_RATES at class-definition time, so
        # override_settings(REST_FRAMEWORK=...) never reaches an already-imported
        # ScopedRateThrottle — patch the shared rates dict instead.
        cache.clear()  # isolate throttle history from earlier webhook tests
        try:
            with patch.dict(ScopedRateThrottle.THROTTLE_RATES, {"paystack_webhook": "3/min"}):
                payload = {"event": "charge.success", "data": {"reference": "J1P-BURST-1"}}
                for _ in range(3):
                    response = self._post_webhook(payload)
                    self.assertEqual(response.status_code, 200)
                response = self._post_webhook(payload)
                self.assertEqual(response.status_code, 429)
                self.assertEqual(response.json()["code"], "RATE_LIMITED")
        finally:
            cache.clear()

    # --- Access control ------------------------------------------------------
    def test_other_user_cannot_initialize_payment_for_my_booking(self):
        other = make_user("thief@pay.dev", password="Str0ng!Pass")
        self.auth(other)
        response = self.client.post("/api/payments/initialize/",
                                    {"booking_reference": self.booking_ref})
        self.assertEqual(response.status_code, 404)

    # --- Offline (staff) -----------------------------------------------------
    def test_staff_records_cash_payment_and_confirms_booking(self):
        receptionist = make_staff("desk@staff.dev", role=User.Role.RECEPTIONIST)
        self.auth(receptionist)
        response = self.client.post("/api/admin/payments/record/", {
            "booking_reference": self.booking_ref,
            "amount": "50000.00",
            "provider": "CASH",
            "notes": "Paid at front desk",
        })
        self.assertEqual(response.status_code, 201, response.json())
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, "CONFIRMED")
        self.assertEqual(self.booking.payment_status, "PAID")

    def test_offline_payment_above_balance_rejected(self):
        receptionist = make_staff("desk2@staff.dev", role=User.Role.RECEPTIONIST)
        self.auth(receptionist)
        response = self.client.post("/api/admin/payments/record/", {
            "booking_reference": self.booking_ref,
            "amount": "60000.00",
            "provider": "POS",
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "PAYMENT_FAILED")


class AnonymousGuestPaymentSecurityTests(BaseAPITestCase):
    """Guest checkout/payment contract: no JWT, token-scoped, authoritative."""

    def setUp(self):
        super().setUp()
        room_type = make_room_type("Guest Pay", price="30000.00")
        make_room(room_type, "GP1")
        today = hotel_today()
        response = self.client.post("/api/bookings/", {
            "room_type": room_type.slug,
            "check_in": (today + timedelta(days=5)).isoformat(),
            "check_out": (today + timedelta(days=7)).isoformat(),
            "rooms": 1, "adults": 1, "children": 0,
            "guest": {
                "first_name": "Guest", "last_name": "Payer",
                "email": "guestpayer@example.test", "phone": "08030000001",
            },
        }, format="json")
        self.assertEqual(response.status_code, 201, response.json())
        data = response.json()["data"]
        self.booking_ref = data["booking_reference"]
        self.token = data["guest_access_token"]
        self.headers = {"HTTP_X_GUEST_ACCESS_TOKEN": self.token}

    @staticmethod
    def _init_response(**kwargs):
        return {
            "authorization_url": "https://checkout.paystack.com/guest-code",
            "access_code": "guest-code",
            "reference": kwargs["reference"],
        }

    @override_settings(
        PAYSTACK_SECRET_KEY="sk_test_mock",
        PAYMENT_CALLBACK_URL="https://www.jonehotel.com/payment-verify.html",
    )
    @patch("apps.payments.services.paystack.initialize_transaction")
    def test_guest_initializes_without_jwt_and_amount_is_authoritative(self, mock_init):
        mock_init.side_effect = self._init_response
        response = self.client.post(
            "/api/payments/initialize/",
            {"booking_reference": self.booking_ref, "amount": "1.00", "currency": "USD"},
            format="json", **self.headers,
        )
        self.assertEqual(response.status_code, 201, response.json())
        payment = Payment.objects.get()
        self.assertEqual(payment.amount, Decimal("60000.00"))
        kwargs = mock_init.call_args.kwargs
        self.assertEqual(kwargs["amount_kobo"], 6_000_000)
        self.assertEqual(kwargs["callback_url"], "https://www.jonehotel.com/payment-verify.html")
        self.assertEqual(kwargs["email"], "guestpayer@example.test")
        self.assertNotIn("amount", kwargs["metadata"])
        self.assertNotIn("guest_access_token", kwargs["metadata"])

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_mock")
    @patch("apps.payments.services.paystack.initialize_transaction")
    def test_duplicate_initialize_reuses_one_gateway_transaction(self, mock_init):
        mock_init.side_effect = self._init_response
        first = self.client.post("/api/payments/initialize/", {"booking_reference": self.booking_ref},
                                 format="json", **self.headers)
        second = self.client.post("/api/payments/initialize/", {"booking_reference": self.booking_ref},
                                  format="json", **self.headers)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertEqual(first.json()["data"]["reference"], second.json()["data"]["reference"])
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(mock_init.call_count, 1)
        self.assertTrue(second.json()["data"]["reused"])

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_mock")
    @patch("apps.payments.services.paystack.initialize_transaction")
    def test_gateway_failure_can_retry_same_reference(self, mock_init):
        from apps.core.exceptions import PaymentGatewayError
        attempts = {"count": 0}
        def flaky(**kwargs):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise PaymentGatewayError()
            return self._init_response(**kwargs)
        mock_init.side_effect = flaky
        failed = self.client.post("/api/payments/initialize/", {"booking_reference": self.booking_ref},
                                  format="json", **self.headers)
        self.assertEqual(failed.status_code, 502)
        original = Payment.objects.get().reference
        retried = self.client.post("/api/payments/initialize/", {"booking_reference": self.booking_ref},
                                   format="json", **self.headers)
        self.assertEqual(retried.status_code, 201, retried.json())
        self.assertEqual(retried.json()["data"]["reference"], original)
        self.assertEqual(Payment.objects.count(), 1)

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_mock")
    @patch("apps.payments.services.paystack.initialize_transaction")
    def test_wrong_guest_token_cannot_initialize(self, mock_init):
        response = self.client.post(
            "/api/payments/initialize/", {"booking_reference": self.booking_ref}, format="json",
            HTTP_X_GUEST_ACCESS_TOKEN="wrong-token",
        )
        self.assertEqual(response.status_code, 404)
        mock_init.assert_not_called()
        self.assertEqual(Payment.objects.count(), 0)

    def _payment(self):
        return Payment.objects.create(
            booking=Booking.objects.get(booking_reference=self.booking_ref),
            reference="J1P-GUEST-SECURE-1", provider=Payment.Provider.PAYSTACK,
            amount=Decimal("60000.00"), currency="NGN", status=Payment.Status.PENDING,
        )

    def _verification(self, payment, *, status="success", amount=6_000_000,
                      currency="NGN", reference=None):
        return {"status": True, "message": "ok", "data": {
            "status": status, "reference": reference or payment.reference,
            "amount": amount, "currency": currency, "channel": "card",
            "gateway_response": "Successful", "id": 12345,
            "paid_at": "2026-09-13T10:00:00Z",
        }}

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_mock")
    @patch("apps.payments.services.paystack.verify_transaction")
    def test_guest_verification_success_and_refresh_are_idempotent(self, mock_verify):
        payment = self._payment()
        mock_verify.return_value = self._verification(payment)
        url = f"/api/payments/verify/{payment.reference}/"
        first = self.client.get(url, **self.headers)
        second = self.client.get(url, **self.headers)
        self.assertEqual(first.status_code, 200, first.json())
        self.assertEqual(first.json()["data"]["transaction_status"], "success")
        self.assertEqual(second.status_code, 200)
        self.assertEqual(mock_verify.call_count, 1)
        booking = Booking.objects.get(booking_reference=self.booking_ref)
        self.assertEqual(booking.amount_paid, Decimal("60000.00"))
        self.assertEqual(Payment.objects.filter(status=Payment.Status.SUCCESS).count(), 1)

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_mock")
    @patch("apps.payments.services.paystack.verify_transaction")
    def test_processing_status_remains_pending(self, mock_verify):
        payment = self._payment()
        mock_verify.return_value = self._verification(payment, status="processing")
        response = self.client.get(f"/api/payments/verify/{payment.reference}/", **self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["transaction_status"], "processing")
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.PENDING)
        self.assertEqual(payment.booking.status, Booking.Status.PENDING)

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_mock")
    @patch("apps.payments.services.paystack.verify_transaction")
    def test_reference_and_currency_mismatches_never_credit_booking(self, mock_verify):
        payment = self._payment()
        mock_verify.return_value = self._verification(payment, reference="DIFFERENT")
        response = self.client.get(f"/api/payments/verify/{payment.reference}/", **self.headers)
        self.assertEqual(response.status_code, 400)
        mock_verify.return_value = self._verification(payment, currency="USD")
        response = self.client.get(f"/api/payments/verify/{payment.reference}/", **self.headers)
        self.assertEqual(response.status_code, 400)
        payment.booking.refresh_from_db()
        self.assertEqual(payment.booking.amount_paid, Decimal("0.00"))

    def test_reference_without_guest_token_exposes_nothing(self):
        payment = self._payment()
        response = self.client.get(f"/api/payments/verify/{payment.reference}/")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("guestpayer", str(response.json()).lower())

    def test_cors_preflight_allows_guest_access_header(self):
        response = self.client.options(
            "/api/payments/initialize/",
            HTTP_ORIGIN="https://www.jonehotel.com",
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
            HTTP_ACCESS_CONTROL_REQUEST_HEADERS="content-type,x-guest-access-token",
        )
        allowed = response.get("Access-Control-Allow-Headers", "").lower()
        self.assertIn("x-guest-access-token", allowed)

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_mock")
    @patch("apps.payments.services.paystack.verify_transaction")
    def test_success_webhook_replay_has_no_duplicate_side_effects(self, mock_verify):
        staff = make_staff("webhook-replay@staff.test", role=User.Role.RECEPTIONIST)
        payment = self._payment()
        mock_verify.return_value = self._verification(payment)
        payload = {"event": "charge.success", "data": {"reference": payment.reference}}
        body = json.dumps(payload).encode()
        signature = hmac.new(b"sk_test_mock", msg=body, digestmod=hashlib.sha512).hexdigest()
        def send():
            return self.client.post(
                "/api/payments/webhook/", data=body, content_type="application/json",
                HTTP_X_PAYSTACK_SIGNATURE=signature,
            )
        self.assertEqual(send().status_code, 200)
        count_after_first = Notification.objects.filter(recipient=staff).count()
        self.assertEqual(send().status_code, 200)
        self.assertEqual(Notification.objects.filter(recipient=staff).count(), count_after_first)
        self.assertEqual(mock_verify.call_count, 1)


class PaystackWrapperContractTests(BaseAPITestCase):
    @override_settings(PAYSTACK_SECRET_KEY="sk_test_secret")
    @patch("apps.payments.services.paystack.requests.post")
    def test_initialize_uses_bearer_auth_and_validates_required_response(self, mock_post):
        from apps.payments.services import paystack
        response = mock_post.return_value
        response.status_code = 200
        response.json.return_value = {
            "status": True,
            "data": {
                "authorization_url": "https://checkout.paystack.com/code",
                "access_code": "code", "reference": "J1P-CONTRACT-1",
            },
        }
        data = paystack.initialize_transaction(
            email="payer@example.test", amount_kobo=6_000_000,
            reference="J1P-CONTRACT-1",
            callback_url="https://www.jonehotel.com/payment-verify.html",
            metadata={"booking_reference": "J1-TEST"},
        )
        self.assertEqual(data["reference"], "J1P-CONTRACT-1")
        kwargs = mock_post.call_args.kwargs
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer sk_test_secret")
        self.assertEqual(kwargs["json"]["amount"], 6_000_000)
        self.assertEqual(kwargs["json"]["currency"], "NGN")
        self.assertNotIn("sk_test_secret", str(kwargs["json"]))

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_secret")
    @patch("apps.payments.services.paystack.requests.post")
    def test_initialize_rejects_http_200_without_complete_success_data(self, mock_post):
        from apps.core.exceptions import PaymentGatewayError
        from apps.payments.services import paystack
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"status": True, "data": {}}
        with self.assertRaises(PaymentGatewayError):
            paystack.initialize_transaction(
                email="payer@example.test", amount_kobo=100,
                reference="J1P-BAD", callback_url="https://example.test/verify",
            )


class OfflinePaymentStateGuardTests(BaseAPITestCase):
    """Task 17 (server side): the dashboard hides "Record payment" once a
    booking is settled, but the endpoint — not the UI — is the authority.

    A stale tab, a double submit or a direct API call must never be able to
    add another offline payment to a booking whose payment record is already
    PAID, REFUNDED or PARTIALLY_REFUNDED.
    """

    def setUp(self):
        super().setUp()
        hotel_settings()
        self.room_type = make_room_type("Guarded", price="20000.00")
        self.room = make_room(self.room_type, "G01")
        self.guest = make_guest("guarded@example.com")
        self.staff = make_staff("guard.desk@staff.dev", role=User.Role.RECEPTIONIST)
        self.auth(self.staff)

    def _booking(self, **kwargs):
        return make_booking(self.guest, self.room_type, [self.room], total="20000.00", **kwargs)

    def _record(self, booking, amount="1000.00"):
        return self.client.post("/api/admin/payments/record/", {
            "booking_reference": booking.booking_reference,
            "amount": amount,
            "provider": "CASH",
        })

    def test_fully_paid_booking_rejects_further_offline_payment(self):
        booking = self._booking(amount_paid="20000.00")
        booking.payment_status = Booking.PaymentStatus.PAID
        booking.save(update_fields=["payment_status"])

        response = self._record(booking)

        self.assertEqual(response.status_code, 409, response.json())
        self.assertEqual(response.json()["code"], "PAYMENT_ALREADY_COMPLETED")
        self.assertEqual(booking.payments.count(), 0)

    def test_refunded_booking_rejects_offline_payment_even_with_balance_due(self):
        """A refund leaves amount_due > 0 again; the payment state must still
        block a new front-desk payment rather than silently re-opening it."""
        booking = self._booking(amount_paid="0.00")
        booking.payment_status = Booking.PaymentStatus.REFUNDED
        booking.save(update_fields=["payment_status"])
        self.assertGreater(booking.amount_due, 0)

        response = self._record(booking)

        self.assertEqual(response.status_code, 409, response.json())
        self.assertEqual(response.json()["code"], "PAYMENT_ALREADY_COMPLETED")
        self.assertEqual(booking.payments.count(), 0)

    def test_partially_refunded_booking_rejects_offline_payment(self):
        booking = self._booking(amount_paid="5000.00")
        booking.payment_status = Booking.PaymentStatus.PARTIALLY_REFUNDED
        booking.save(update_fields=["payment_status"])

        response = self._record(booking)

        self.assertEqual(response.status_code, 409, response.json())
        self.assertEqual(response.json()["code"], "PAYMENT_ALREADY_COMPLETED")

    def test_unpaid_and_partially_paid_bookings_still_accept_payment(self):
        """The guard must not block the normal front-desk flow."""
        unpaid = self._booking(amount_paid="0.00")
        self.assertEqual(self._record(unpaid, "20000.00").status_code, 201)

        partial = self._booking(amount_paid="5000.00")
        partial.payment_status = Booking.PaymentStatus.PARTIALLY_PAID
        partial.save(update_fields=["payment_status"])
        response = self._record(partial, "15000.00")
        self.assertEqual(response.status_code, 201, response.json())
        partial.refresh_from_db()
        self.assertEqual(partial.payment_status, "PAID")
