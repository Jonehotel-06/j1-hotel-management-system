# tests/test_synchronous_email.py
"""Payment-verification email: synchronous delivery after Paystack success.

Covers the required behaviour after the email queue was removed:

* a successful Paystack verification triggers the receipt email DIRECTLY — no
  Celery task, no broker, no worker (the SMTP attempt happens inside the
  verification flow, right after the payment transaction commits);
* EmailLog records the REAL SMTP result (SENT only when the backend accepted
  the message);
* an SMTP failure marks the email FAILED but NEVER alters the verified,
  successful payment state.
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock
from unittest.mock import patch

from django.core import mail
from django.test import override_settings

from apps.bookings.models import Booking
from apps.core.utils import hotel_today
from apps.core.emails import queue_email
from apps.notifications.models import EmailLog
from apps.payments.models import Payment
from apps.payments.services import payment_service

from .base import BaseAPITestCase
from .factories import make_room, make_room_type, make_user


class VerificationEmailTests(BaseAPITestCase):
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
        self.booking = Booking.objects.get(
            booking_reference=response.json()["data"]["booking_reference"]
        )
        self.payment = Payment.objects.create(
            booking=self.booking, user=self.user, reference="J1P-SYNC-0001",
            provider=Payment.Provider.PAYSTACK, amount=Decimal("50000.00"),
            currency="NGN", status=Payment.Status.PENDING,
        )
        mail.outbox.clear()

    def _payload(self, status_value="success"):
        return {
            "status": True,
            "message": "Verification successful",
            "data": {
                "status": status_value,
                "reference": self.payment.reference,
                "amount": 5_000_000,
                "currency": "NGN",
                "channel": "card",
                "gateway_response": "Successful",
                "id": 99887766,
                "paid_at": "2026-09-09T10:00:00Z",
            },
        }

    def _verify(self):
        """Run verification and flush the on-commit email callback."""
        # In production the process runs in autocommit at this point, so the
        # on-commit callback runs immediately; TestCase holds an outer
        # transaction, so capture+execute reproduces the request behaviour.
        with self.captureOnCommitCallbacks(execute=True):
            return payment_service.process_verification(reference=self.payment.reference)

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_mock")
    @patch("apps.payments.services.paystack.verify_transaction")
    def test_successful_verification_sends_email_synchronously(self, mock_verify):
        mock_verify.return_value = self._payload()
        result = self._verify()
        self.assertEqual(result["transaction_status"], "success")

        self.payment.refresh_from_db()
        self.booking.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.SUCCESS)
        self.assertEqual(self.booking.status, Booking.Status.CONFIRMED)

        # The receipt email was delivered DURING the verification flow — the
        # row is final (SENT), not PENDING/QUEUED.
        log = EmailLog.objects.filter(
            kind=EmailLog.Kind.RECEIPT, booking_reference=self.booking.booking_reference
        ).latest("id")
        self.assertEqual(log.status, EmailLog.Status.SENT)
        self.assertIsNotNone(log.sent_at)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(self.booking.booking_reference, mail.outbox[0].subject)
        # The receipt PDF is attached exactly as before.
        pdfs = [
            a for a in mail.outbox[0].attachments
            if getattr(a, "filename", "") and a.filename.endswith(".pdf")
        ]
        self.assertTrue(pdfs, "receipt PDF must remain attached")

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_mock")
    @patch("apps.payments.services.paystack.verify_transaction")
    def test_verification_uses_no_celery_for_email(self, mock_verify):
        mock_verify.return_value = self._payload()
        import apps.notifications.tasks as tasks_module

        # The email task is gone entirely; anything queue-shaped would raise.
        self.assertFalse(hasattr(tasks_module, "send_email_task"))
        with mock.patch(
            "apps.notifications.tasks.deliver_email_log",
            wraps=tasks_module.deliver_email_log,
        ) as spy:
            self._verify()
        self.assertTrue(spy.called)

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_mock")
    @patch("apps.payments.services.paystack.verify_transaction")
    def test_smtp_failure_keeps_payment_success_marks_email_failed(self, mock_verify):
        import smtplib

        mock_verify.return_value = self._payload()
        with mock.patch(
            "apps.notifications.providers.EmailMessage.send",
            side_effect=smtplib.SMTPAuthenticationError(535, b"bad creds"),
        ):
            result = self._verify()

        # Email FAILED, payment/booking untouched — success stays success.
        self.assertEqual(result["transaction_status"], "success")
        self.payment.refresh_from_db()
        self.booking.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.SUCCESS)
        self.assertEqual(self.booking.status, Booking.Status.CONFIRMED)
        self.assertEqual(self.booking.amount_paid, Decimal("50000.00"))

        log = EmailLog.objects.filter(
            booking_reference=self.booking.booking_reference
        ).latest("id")
        self.assertEqual(log.status, EmailLog.Status.FAILED)
        self.assertEqual(log.failure_stage, EmailLog.FailureStage.SMTP)
        self.assertNotIn("bad creds", log.error_message)
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_mock")
    @patch("apps.payments.services.paystack.verify_transaction")
    def test_failed_payment_sends_no_receipt_email(self, mock_verify):
        mock_verify.return_value = self._payload(status_value="failed")
        self._verify()
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.FAILED)
        self.assertFalse(
            EmailLog.objects.filter(kind=EmailLog.Kind.RECEIPT).exists()
        )


class DirectDeliveryGuardTests(BaseAPITestCase):
    """The request path's helpers must never reach for a broker."""

    def test_core_emails_module_imports_no_celery(self):
        import inspect

        import apps.core.emails as emails_module

        source = inspect.getsource(emails_module)
        self.assertNotIn("import celery", source.lower())
        self.assertNotIn("from celery", source.lower())
        self.assertNotIn(".delay(", source)
        self.assertNotIn(".apply_async(", source)
        self.assertNotIn("send_email_task", source)

    def test_queue_email_is_commit_deferred_direct_delivery(self):
        from django.core import mail as django_mail
        from django.db import transaction

        with self.captureOnCommitCallbacks(execute=True):
            with transaction.atomic():
                queue_email("Hi", "Body", ["guest@example.com"], kind="GENERIC")
        self.assertEqual(len(django_mail.outbox), 1)
        log = EmailLog.objects.latest("id")
        self.assertEqual(log.status, EmailLog.Status.SENT)
