# tests/test_email_notices.py
"""Tests for the shared branded notice emails + stale EmailLog resolution.

Covers the fixes made to the synchronous email system:

* every guest-facing notice (booking-pending, cancellation, password reset,
  refund updates, cancellation-request lifecycle) now carries a styled,
  email-safe HTML alternative built by the shared J1 notice builder;
* interrupted PENDING/SENDING rows (a deploy/timeout killed the request
  mid-send; no worker exists to resolve them) are resolved to FAILED and can
  no longer block the automatic receipt or the staff send-receipt endpoint
  forever.
"""
from datetime import timedelta
from decimal import Decimal

from django.core import mail
from django.utils import timezone

from apps.accounts.models import User
from apps.bookings.models import Booking
from apps.notifications.models import EmailLog
from apps.payments.models import Payment

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


class NoticeEmailStylingTests(BaseAPITestCase):
    """Guest-facing notices must be branded multipart (text + HTML)."""

    def setUp(self):
        super().setUp()
        hotel_settings(hotel_name="J-ONE HOTEL & LODGE", phone="0800", email="hotel@jone.test")

    def test_notice_builder_produces_branded_html_and_text(self):
        from apps.core.email_design import render_notice_email

        text, html = render_notice_email(
            category="Booking Update",
            title="Your booking has been cancelled",
            greeting="Hello Ada,",
            paragraphs=["Booking J1-X has been cancelled."],
            details=[{"label": "Booking reference", "value": "J1-X"}],
            cta_label="View Booking",
            cta_url="https://example.com/b",
            footnote="Questions? Call us.",
        )
        self.assertIn("<!DOCTYPE html", html)
        self.assertIn("J-ONE HOTEL &amp; LODGE", html)
        self.assertIn("cid:jone-logo", html)
        self.assertIn("Your booking has been cancelled", html)
        self.assertIn("https://example.com/b", html)
        # Table-based email-safe markup, no scripts.
        self.assertIn('role="presentation"', html)
        self.assertNotIn("<script", html.lower())
        # Text alternative carries the same facts.
        self.assertIn("Hello Ada,", text)
        self.assertIn("Booking reference: J1-X", text)
        self.assertIn("https://example.com/b", text)

    def test_notice_builder_escapes_injected_markup(self):
        from apps.core.email_design import render_notice_email

        _, html = render_notice_email(
            category="X", title="T", greeting="Hi",
            paragraphs=['<img src=x onerror=alert(1)>'],
        )
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;img", html)

    def test_password_reset_email_is_styled(self):
        make_user(email="resetme@example.com")
        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.post("/api/auth/password/reset/", {"email": "resetme@example.com"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertEqual(len(msg.alternatives), 1)
        html_body, html_type = msg.alternatives[0]
        self.assertEqual(html_type, "text/html")
        self.assertIn("<!DOCTYPE html", html_body)
        self.assertIn("Reset Password", html_body)
        self.assertIn("reset-password.html", html_body)
        # The token link must also be present in the plain text fallback.
        self.assertIn("reset-password.html", msg.body)

    def test_staff_cancellation_email_is_styled(self):
        from apps.bookings.services import booking_service

        rt = make_room_type()
        room = make_room(rt, 101)
        guest = make_guest(email="cancelme@example.com")
        booking = make_booking(guest, rt, rooms=[room], status=Booking.Status.CONFIRMED)
        staff = make_staff("mgr@jone.test", role=User.Role.MANAGER)
        with self.captureOnCommitCallbacks(execute=True):
            booking_service.cancel_booking(booking, staff=True, by_user=staff, reason="test")
        cancel_mails = [m for m in mail.outbox if "cancelled" in m.subject.lower()]
        self.assertEqual(len(cancel_mails), 1)
        msg = cancel_mails[0]
        self.assertEqual(len(msg.alternatives), 1)
        html_body = msg.alternatives[0][0]
        self.assertIn("<!DOCTYPE html", html_body)
        self.assertIn(booking.booking_reference, html_body)
        log = EmailLog.objects.get(kind=EmailLog.Kind.CANCELLATION)
        self.assertEqual(log.status, EmailLog.Status.SENT)
        self.assertTrue(log.html_body)

    def test_booking_pending_email_is_styled(self):
        rt = make_room_type()
        make_room(rt, 105)
        today = timezone.localdate()
        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.post("/api/bookings/", {
                "room_type": rt.slug,
                "check_in": (today + timedelta(days=5)).isoformat(),
                "check_out": (today + timedelta(days=7)).isoformat(),
                "rooms": 1, "adults": 1, "children": 0,
                "guest": {
                    "first_name": "Ada", "last_name": "Obi",
                    "email": "pendguest@example.com", "phone": "08030000000",
                },
            }, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        pending_mails = [m for m in mail.outbox if "Complete your booking" in m.subject]
        self.assertEqual(len(pending_mails), 1)
        msg = pending_mails[0]
        self.assertEqual(len(msg.alternatives), 1)
        html_body = msg.alternatives[0][0]
        self.assertIn("<!DOCTYPE html", html_body)
        self.assertIn("Complete Payment", html_body)
        log = EmailLog.objects.get(kind=EmailLog.Kind.BOOKING_PENDING)
        self.assertEqual(log.status, EmailLog.Status.SENT)


class StaleEmailLogResolutionTests(BaseAPITestCase):
    """Interrupted PENDING/SENDING rows must resolve, never block sends."""

    def setUp(self):
        super().setUp()
        hotel_settings(hotel_name="J-ONE HOTEL & LODGE", phone="0800", email="hotel@jone.test")
        self.staff = make_staff("recept@jone.test", role=User.Role.RECEPTIONIST)
        self.rt = make_room_type(name="Deluxe", price="25000.00")
        self.room = make_room(self.rt, 101)
        self.guest = make_guest(email="guest@example.com")
        self.booking = make_booking(
            self.guest, self.rt, rooms=[self.room],
            status=Booking.Status.CONFIRMED, amount_paid="25000.00", total="25000.00",
        )
        Payment.objects.create(
            booking=self.booking, reference="J1P-STALE-01", amount=Decimal("25000.00"),
            currency="NGN", status=Payment.Status.SUCCESS, provider="PAYSTACK",
            paid_at=timezone.now(),
        )

    def _stale_row(self, status):
        log = EmailLog.objects.create(
            to_email=self.guest.email,
            subject="Payment Receipt — stale",
            kind=EmailLog.Kind.RECEIPT,
            booking_reference=self.booking.booking_reference,
            payment_reference="J1P-STALE-01",
            booking_id=self.booking.id,
            status=status,
        )
        # Age it beyond the stale window (created_at is auto_now_add).
        EmailLog.objects.filter(pk=log.pk).update(
            created_at=timezone.now() - timedelta(hours=2)
        )
        return log

    def test_resolve_stale_marks_old_rows_failed(self):
        stale = self._stale_row(EmailLog.Status.SENDING)
        fresh = EmailLog.objects.create(
            to_email="a@example.com", subject="s", status=EmailLog.Status.PENDING,
        )
        resolved = EmailLog.resolve_stale()
        self.assertEqual(resolved, 1)
        stale.refresh_from_db()
        fresh.refresh_from_db()
        self.assertEqual(stale.status, EmailLog.Status.FAILED)
        self.assertEqual(stale.error_class, "StaleDelivery")
        self.assertIsNotNone(stale.failed_at)
        # A genuinely fresh row (send in flight right now) is never touched.
        self.assertEqual(fresh.status, EmailLog.Status.PENDING)

    def test_stale_row_does_not_block_staff_send_receipt(self):
        self._stale_row(EmailLog.Status.SENDING)
        self.auth(self.staff)
        res = self.client.post(
            f"/api/admin/bookings/{self.booking.booking_reference}/send-receipt/"
        )
        # Previously this returned "already being processed" forever; now the
        # stale row is resolved and a fresh synchronous send goes out.
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["data"]["status"], EmailLog.Status.SENT)
        self.assertEqual(len(mail.outbox), 1)

    def test_fresh_in_flight_row_still_blocks_duplicate_send(self):
        EmailLog.objects.create(
            to_email=self.guest.email,
            subject="Payment Receipt — in flight",
            kind=EmailLog.Kind.RECEIPT,
            booking_reference=self.booking.booking_reference,
            status=EmailLog.Status.SENDING,
        )
        self.auth(self.staff)
        res = self.client.post(
            f"/api/admin/bookings/{self.booking.booking_reference}/send-receipt/"
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["data"]["status"], "IN_PROGRESS")
        self.assertEqual(len(mail.outbox), 0)

    def test_stale_row_does_not_block_automatic_receipt(self):
        from apps.bookings.services.booking_service import _send_confirmation_email

        self._stale_row(EmailLog.Status.PENDING)
        with self.captureOnCommitCallbacks(execute=True):
            _send_confirmation_email(self.booking)
        sent = EmailLog.objects.filter(
            kind=EmailLog.Kind.RECEIPT,
            booking_reference=self.booking.booking_reference,
            status=EmailLog.Status.SENT,
        )
        self.assertEqual(sent.count(), 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_email_log_list_resolves_stale_rows_for_staff(self):
        stale = self._stale_row(EmailLog.Status.SENDING)
        self.auth(self.staff)
        res = self.client.get("/api/notifications/emails/")
        self.assertEqual(res.status_code, 200)
        stale.refresh_from_db()
        self.assertEqual(stale.status, EmailLog.Status.FAILED)
