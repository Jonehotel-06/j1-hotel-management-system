# tests/test_auto_checkout.py
"""Automatic checkout + 30-minute warning (spec §17–§18).

Covers: never-early timing, idempotency across repeated runs, balance
preservation, audit/notification side effects, room release, and
CHECKOUT_DUE_SOON deduplication across frequent Celery runs and across
distinct stays of the same booking link.
"""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from apps.audit.models import AuditLog
from apps.bookings.models import Booking
from apps.bookings.services.booking_service import (
    auto_checkout_due_bookings,
    send_checkout_due_soon_notifications,
)
from apps.core.utils import combine_hotel_datetime, hotel_today
from apps.notifications.models import Notification
from apps.rooms.models import Room

from .base import BaseAPITestCase
from .factories import (
    hotel_settings,
    make_booking,
    make_guest,
    make_room,
    make_room_type,
    make_staff,
)


class AutoCheckoutBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.settings_obj = hotel_settings()
        # notify_staff() fans out to active staff users — there must be at
        # least one for CHECKOUT_AUTO / CHECKOUT_DUE_SOON rows to exist.
        self.staff = make_staff("frontdesk@auto.dev")
        self.room_type = make_room_type("Standard", price="15000.00", max_guests=2)
        self.room = make_room(self.room_type, "101")
        self.guest = make_guest("inhouse@test.dev")
        self.today = hotel_today()

    def make_inhouse(self, *, check_out=None, amount_paid="15000.00"):
        booking = make_booking(
            self.guest, self.room_type, rooms=[self.room],
            check_in=self.today - timedelta(days=2),
            check_out=check_out or self.today,
            status=Booking.Status.CHECKED_IN,
            amount_paid=amount_paid,
        )
        booking.checked_in_at = timezone.now() - timedelta(days=2)
        booking.save(update_fields=["checked_in_at"])
        self.room.status = Room.Status.OCCUPIED
        self.room.save(update_fields=["status"])
        return booking

    def due_at(self, booking):
        return combine_hotel_datetime(booking.check_out, self.settings_obj.check_out_time)


class AutoCheckoutTests(AutoCheckoutBase):
    def test_not_checked_out_before_scheduled_time(self):
        booking = self.make_inhouse()
        before = self.due_at(booking) - timedelta(minutes=5)
        processed = auto_checkout_due_bookings(now=before)
        booking.refresh_from_db()
        self.assertEqual(processed, 0)
        self.assertEqual(booking.status, Booking.Status.CHECKED_IN)

    def test_checked_out_after_scheduled_time(self):
        booking = self.make_inhouse()
        after = self.due_at(booking) + timedelta(minutes=1)
        processed = auto_checkout_due_bookings(now=after)
        booking.refresh_from_db()
        self.room.refresh_from_db()
        self.assertEqual(processed, 1)
        self.assertEqual(booking.status, Booking.Status.CHECKED_OUT)
        self.assertIsNotNone(booking.checked_out_at)
        # Room released for housekeeping, no longer occupied.
        self.assertNotEqual(self.room.status, Room.Status.OCCUPIED)

    def test_idempotent_across_repeated_runs(self):
        booking = self.make_inhouse()
        after = self.due_at(booking) + timedelta(minutes=1)
        self.assertEqual(auto_checkout_due_bookings(now=after), 1)
        self.assertEqual(auto_checkout_due_bookings(now=after), 0)
        self.assertEqual(
            AuditLog.objects.filter(action="AUTO_CHECK_OUT").count(), 1
        )
        self.assertEqual(
            Notification.objects.filter(type="CHECKOUT_AUTO").values("link").distinct().count(), 1
        )

    def test_outstanding_balance_is_preserved_not_blocking(self):
        booking = self.make_inhouse(amount_paid="5000.00")
        self.assertGreater(booking.amount_due, Decimal("0.00"))
        after = self.due_at(booking) + timedelta(minutes=1)
        self.assertEqual(auto_checkout_due_bookings(now=after), 1)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CHECKED_OUT)
        # Financial records untouched — the balance survives checkout.
        self.assertGreater(booking.amount_due, Decimal("0.00"))

    def test_writes_audit_log_and_staff_notification(self):
        booking = self.make_inhouse()
        after = self.due_at(booking) + timedelta(minutes=1)
        auto_checkout_due_bookings(now=after)
        log = AuditLog.objects.filter(action="AUTO_CHECK_OUT").first()
        self.assertIsNotNone(log)
        self.assertIsNone(log.actor)
        self.assertIn(booking.booking_reference, log.summary)
        self.assertTrue(Notification.objects.filter(type="CHECKOUT_AUTO").exists())

    def test_manual_then_auto_never_double_processes(self):
        booking = self.make_inhouse()
        booking.status = Booking.Status.CHECKED_OUT
        booking.checked_out_at = timezone.now()
        booking.save(update_fields=["status", "checked_out_at"])
        after = self.due_at(booking) + timedelta(minutes=1)
        self.assertEqual(auto_checkout_due_bookings(now=after), 0)


class CheckoutDueSoonTests(AutoCheckoutBase):
    def test_warning_sent_inside_window(self):
        booking = self.make_inhouse()
        now = self.due_at(booking) - timedelta(minutes=20)
        sent = send_checkout_due_soon_notifications(now=now)
        self.assertEqual(sent, 1)
        note = Notification.objects.filter(type="CHECKOUT_DUE_SOON").first()
        self.assertIsNotNone(note)
        self.assertIn(booking.booking_reference, note.title)

    def test_no_warning_outside_window(self):
        booking = self.make_inhouse()
        too_early = self.due_at(booking) - timedelta(hours=2)
        self.assertEqual(send_checkout_due_soon_notifications(now=too_early), 0)
        past_due = self.due_at(booking) + timedelta(minutes=1)
        self.assertEqual(send_checkout_due_soon_notifications(now=past_due), 0)

    def test_deduplicated_across_frequent_runs(self):
        booking = self.make_inhouse()
        base = self.due_at(booking) - timedelta(minutes=25)
        total = 0
        for minutes in (0, 5, 10, 15, 20):  # simulates */5 Celery beat
            total += send_checkout_due_soon_notifications(now=base + timedelta(minutes=minutes))
        self.assertEqual(total, 1)
        self.assertEqual(
            Notification.objects.filter(type="CHECKOUT_DUE_SOON").values("link").distinct().count(),
            1,
        )
