# tests/test_email_pipeline.py
"""Tests for the synchronous, truth-tracking email delivery service.

Covers the architecture after the email queue was removed:

* delivery is DIRECT — no Celery task, no broker, no Redis, no ``.delay()``;
* commit semantics: booking-path emails only go out when the surrounding
  transaction actually commits (a rolled-back booking sends nothing);
* the Brevo HTTPS transport: success (200/201 + messageId), authentication
  failure, sender rejection, invalid recipient, rate limiting, server errors
  and network timeouts are all recorded truthfully on the EmailLog row;
* a missing BREVO_API_KEY / invalid DEFAULT_FROM_EMAIL fails loudly, never
  silently.

All Brevo interactions are mocked — no test needs a real API key and no
secret value is ever printed.
"""
from unittest import mock

from django.core import mail
from django.test import TestCase, override_settings

import requests

from apps.core.emails import queue_email, send_email_safe
from apps.notifications.models import EmailLog
from apps.notifications.providers import BREVO_API_URL
from apps.notifications.tasks import active_transport


def _brevo_response(status_code=201, body=None, text=""):
    """A minimal fake requests.Response for the Brevo API."""
    response = mock.Mock()
    response.status_code = status_code
    if body is None:
        response.json.side_effect = ValueError("no json")
        response.text = text
    else:
        response.json.return_value = body
        response.text = text or str(body)
    return response


class QueueEmailCommitSemanticsTests(TestCase):
    """queue_email must defer to the surrounding transaction's commit."""

    def test_rolled_back_transaction_sends_nothing(self):
        from django.db import transaction

        with transaction.atomic():
            queue_email(
                "Subject", "Body", ["guest@example.com"], kind="BOOKING_PENDING"
            )
            transaction.set_rollback(True)
        self.assertEqual(EmailLog.objects.count(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_committed_transaction_delivers_synchronously(self):
        from django.db import transaction

        with self.captureOnCommitCallbacks(execute=True):
            with transaction.atomic():
                queue_email(
                    "Complete your booking", "Body", ["guest@example.com"],
                    kind="BOOKING_PENDING", booking_reference="J1-TEST-2",
                )
        log = EmailLog.objects.get(booking_reference="J1-TEST-2")
        self.assertEqual(log.status, EmailLog.Status.SENT)
        self.assertEqual(len(mail.outbox), 1)


class DirectDeliveryTests(TestCase):
    """send_email_safe delivers inline through the Django backend in tests."""

    def test_send_email_safe_delivers_inline_and_records_sent(self):
        log = send_email_safe("Subject", "Body text", ["guest@example.com"], kind="GENERIC")
        self.assertEqual(log.status, EmailLog.Status.SENT)
        self.assertIsNotNone(log.sent_at)
        self.assertEqual(len(mail.outbox), 1)

    def test_invalid_recipients_are_skipped_entirely(self):
        log = send_email_safe("Subject", "Body", ["not-an-email", "", None])
        self.assertIsNone(log)
        self.assertEqual(EmailLog.objects.count(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_backend_failure_is_recorded_failed(self):
        import smtplib

        with mock.patch(
            "apps.notifications.providers.EmailMessage.send",
            side_effect=smtplib.SMTPAuthenticationError(535, b"bad creds"),
        ):
            log = send_email_safe("Subject", "Body", ["guest@example.com"])
        self.assertEqual(log.status, EmailLog.Status.FAILED)
        self.assertEqual(log.error_class, "SMTPAuthenticationError")
        self.assertNotIn("bad creds", log.error_message)
        self.assertEqual(log.failure_stage, EmailLog.FailureStage.PROVIDER)
        self.assertEqual(len(mail.outbox), 0)

    def test_no_celery_no_delay_no_broker_in_email_modules(self):
        import inspect

        import apps.core.emails as emails_module
        import apps.notifications.tasks as tasks_module

        for module in (emails_module, tasks_module):
            source = inspect.getsource(module)
            self.assertNotIn("import celery", source.lower())
            self.assertNotIn("from celery", source.lower())
            self.assertNotIn(".delay(", source)
            self.assertNotIn(".apply_async(", source)
            self.assertNotIn("send_email_task", source)
            self.assertNotIn("shared_task", source)
        self.assertFalse(hasattr(tasks_module, "send_email_task"))


@override_settings(EMAIL_PROVIDER="brevo", BREVO_API_KEY="test-key-not-a-real-secret",
                   DEFAULT_FROM_EMAIL="J-one hotel & lodge <agbo33010@gmail.com>")
class BrevoTransportTests(TestCase):
    """The Brevo HTTPS API path: request format, truth-tracking, error surfacing."""

    def _send(self, **kwargs):
        return send_email_safe(
            "Receipt", "Plain body", ["guest@example.com"],
            html_message=kwargs.pop("html_message", "<p>Hello</p>"),
            **kwargs,
        )

    def test_transport_selection_is_brevo(self):
        self.assertEqual(active_transport(), "brevo")

    def test_success_201_records_sent_with_provider_message_id(self):
        with mock.patch(
            "requests.post",
            return_value=_brevo_response(201, {"messageId": "<202609.abc@smtp-relay>"}),
        ) as post:
            log = self._send()
        self.assertEqual(log.status, EmailLog.Status.SENT)
        self.assertEqual(log.provider_message_id, "<202609.abc@smtp-relay>")
        self.assertIsNotNone(log.sent_at)
        # Request format: correct endpoint, api-key header, sender/to/subject.
        args, kwargs = post.call_args
        self.assertEqual(args[0], BREVO_API_URL)
        self.assertEqual(kwargs["headers"]["api-key"], "test-key-not-a-real-secret")
        payload = kwargs["json"]
        self.assertEqual(payload["sender"]["email"], "agbo33010@gmail.com")
        self.assertEqual(payload["sender"]["name"], "J-one hotel & lodge")
        self.assertEqual(payload["to"], [{"email": "guest@example.com"}])
        self.assertEqual(payload["textContent"], "Plain body")
        self.assertIn("htmlContent", payload)
        # Bounded timeout.
        self.assertGreater(kwargs["timeout"], 0)
        # Nothing went through the Django backend.
        self.assertEqual(len(mail.outbox), 0)

    def test_success_200_also_accepted(self):
        with mock.patch(
            "requests.post", return_value=_brevo_response(200, {"messageId": "id-200"})
        ):
            log = self._send()
        self.assertEqual(log.status, EmailLog.Status.SENT)
        self.assertEqual(log.provider_message_id, "id-200")

    def test_plaintext_only_email_has_no_html_content(self):
        with mock.patch(
            "requests.post", return_value=_brevo_response(201, {"messageId": "x"})
        ) as post:
            send_email_safe("Plain", "Only text", ["guest@example.com"])
        payload = post.call_args.kwargs["json"]
        self.assertNotIn("htmlContent", payload)
        self.assertEqual(payload["textContent"], "Only text")

    def test_authentication_failure_401_is_failed_with_status(self):
        with mock.patch(
            "requests.post",
            return_value=_brevo_response(401, {"message": "Key not found", "code": "unauthorized"}),
        ):
            log = self._send()
        self.assertEqual(log.status, EmailLog.Status.FAILED)
        self.assertEqual(log.error_class, "EmailProviderError")
        self.assertIn("401", log.error_message)
        self.assertIn("Key not found", log.error_message)
        self.assertNotIn("test-key", log.error_message)

    def test_sender_rejection_400_is_failed_with_reason(self):
        with mock.patch(
            "requests.post",
            return_value=_brevo_response(400, {"message": "Sender not valid", "code": "invalid_parameter"}),
        ):
            log = self._send()
        self.assertEqual(log.status, EmailLog.Status.FAILED)
        self.assertIn("400", log.error_message)
        self.assertIn("Sender not valid", log.error_message)

    def test_invalid_recipient_400_is_failed(self):
        with mock.patch(
            "requests.post",
            return_value=_brevo_response(400, {"message": "Invalid email address", "code": "invalid_parameter"}),
        ):
            log = self._send()
        self.assertEqual(log.status, EmailLog.Status.FAILED)
        self.assertIn("Invalid email address", log.error_message)

    def test_rate_limit_429_is_failed_with_status(self):
        with mock.patch(
            "requests.post",
            return_value=_brevo_response(429, {"message": "Too many requests"}),
        ):
            log = self._send()
        self.assertEqual(log.status, EmailLog.Status.FAILED)
        self.assertIn("429", log.error_message)

    def test_server_error_5xx_is_failed_with_status(self):
        with mock.patch(
            "requests.post", return_value=_brevo_response(503, None, text="upstream down")
        ):
            log = self._send()
        self.assertEqual(log.status, EmailLog.Status.FAILED)
        self.assertIn("503", log.error_message)

    def test_network_timeout_is_failed(self):
        with mock.patch(
            "requests.post", side_effect=requests.exceptions.Timeout("timed out")
        ):
            log = self._send()
        self.assertEqual(log.status, EmailLog.Status.FAILED)
        self.assertEqual(log.error_class, "TimeoutError")
        self.assertIn("timed out", log.error_message.lower())

    def test_connection_error_is_failed(self):
        with mock.patch(
            "requests.post",
            side_effect=requests.exceptions.ConnectionError("no route to host"),
        ):
            log = self._send()
        self.assertEqual(log.status, EmailLog.Status.FAILED)
        self.assertEqual(log.error_class, "ConnectionError")

    def test_success_without_message_id_is_not_reported_sent(self):
        with mock.patch("requests.post", return_value=_brevo_response(201, {})):
            log = self._send()
        self.assertEqual(log.status, EmailLog.Status.FAILED)

    @override_settings(BREVO_API_KEY="")
    def test_missing_api_key_fails_loudly(self):
        log = self._send()
        self.assertEqual(log.status, EmailLog.Status.FAILED)
        self.assertEqual(log.error_class, "EmailConfigurationError")
        self.assertIn("EMAIL_API_KEY", log.error_message)

    @override_settings(DEFAULT_FROM_EMAIL="not-an-address")
    def test_invalid_default_from_email_fails_loudly(self):
        log = self._send()
        self.assertEqual(log.status, EmailLog.Status.FAILED)
        self.assertEqual(log.error_class, "EmailConfigurationError")
        self.assertIn("DEFAULT_FROM_EMAIL", log.error_message)

    def test_no_redis_and_no_celery_touched(self):
        """A Brevo delivery must complete without any broker interaction."""
        with mock.patch("celery.app.task.Task.apply_async",
                        side_effect=AssertionError("Celery must not be used for email")), \
             mock.patch("requests.post",
                        return_value=_brevo_response(201, {"messageId": "ok"})):
            log = self._send()
        self.assertEqual(log.status, EmailLog.Status.SENT)


@override_settings(EMAIL_PROVIDER="")
class TransportSelectionTests(TestCase):
    """Provider selection is deterministic and can never pick SMTP by accident
    in production-like configurations."""

    @override_settings(BREVO_API_KEY="some-key")
    def test_auto_prefers_brevo_when_key_present(self):
        self.assertEqual(active_transport(), "brevo")

    @override_settings(BREVO_API_KEY="")
    def test_auto_falls_back_to_django_backend_without_key(self):
        self.assertEqual(active_transport(), "django")

    @override_settings(EMAIL_PROVIDER="brevo", BREVO_API_KEY="")
    def test_explicit_brevo_wins_even_without_key(self):
        # Explicit means explicit: the send fails loudly about the missing
        # key rather than silently degrading to (blocked) SMTP.
        self.assertEqual(active_transport(), "brevo")

    @override_settings(EMAIL_PROVIDER="django", BREVO_API_KEY="some-key")
    def test_explicit_django_wins_even_with_key(self):
        self.assertEqual(active_transport(), "django")
