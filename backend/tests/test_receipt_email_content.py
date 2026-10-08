# tests/test_receipt_email_content.py
"""Content/presentation tests for the HTML receipt email builder.

These lock in the guest-facing formatting rules the redesign introduced:
Naira money, human-readable dates, an obvious accessible payment status, a
de-duplicated payment history, and correct HTML auto-escaping of dynamic
values. They exercise the real ``ReceiptSerializer`` payload — no hard-coded
demo data.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.test import SimpleTestCase
from django.utils import timezone

from apps.accounts.models import User
from apps.bookings.models import Booking
from apps.bookings.serializers import ReceiptSerializer
from apps.bookings.services.receipt_email import (_DEFAULT_STATUS, _STATUS_PRESENTATION,
                                                   _status_presentation, render_receipt_email)
from apps.core.formatting import format_date, format_datetime, format_money
from apps.payments.models import Payment

from .base import BaseAPITestCase
from .factories import (
    hotel_settings,
    make_booking,
    make_guest,
    make_room,
    make_room_type,
)


class FormattingTests(BaseAPITestCase):
    def test_money_is_naira_with_grouping(self):
        self.assertEqual(format_money("100000.00"), "₦100,000.00")
        self.assertEqual(format_money(Decimal("0")), "₦0.00")
        self.assertEqual(format_money("1500000.5"), "₦1,500,000.50")

    def test_money_unknown_currency_keeps_code(self):
        self.assertEqual(format_money("100.00", "USD"), "$100.00")
        self.assertEqual(format_money("100.00", "XOF"), "XOF 100.00")

    def test_money_bad_input_is_safe(self):
        self.assertEqual(format_money(None), "₦0.00")
        self.assertEqual(format_money("not-a-number"), "₦0.00")

    def test_dates_are_human_readable(self):
        self.assertEqual(format_date("2026-09-12"), "12 September 2026")
        human = format_datetime("2026-09-12T19:09:51.773242+00:00")
        # Rendered in hotel-local time (Africa/Lagos, +01:00) and human form.
        self.assertIn("12 September 2026", human)
        self.assertNotIn("T", human)
        self.assertNotIn("+00:00", human)

    def test_date_has_no_leading_zero_on_single_digit_day(self):
        # The whole point of the (previously Unix-only) %-d: no leading zero.
        self.assertEqual(format_date("2026-09-04"), "4 September 2026")
        self.assertEqual(format_date(date(2026, 1, 1)), "1 January 2026")

    def test_datetime_12_hour_clock_edges(self):
        # Midnight and noon must read 12 AM / 12 PM, not 0/24.
        self.assertEqual(
            format_datetime("2026-09-04T00:05:00+01:00"), "4 September 2026, 12:05 AM"
        )
        self.assertEqual(
            format_datetime("2026-09-04T12:00:00+01:00"), "4 September 2026, 12:00 PM"
        )
        self.assertEqual(
            format_datetime("2026-09-04T13:07:00+01:00"), "4 September 2026, 1:07 PM"
        )

    def test_date_formatting_uses_no_unix_only_directives(self):
        """Regression guard for the Windows ``ValueError: Invalid format string``.

        The formatting helpers must build the day/hour from integer components,
        never from the glibc-only ``%-d`` / ``%-I`` directives (which raise on
        Windows). We assert the source contains no such directive so the bug
        cannot silently return.
        """
        import inspect

        from apps.core import formatting

        source = inspect.getsource(formatting)
        # Strip docstrings/comments mentioning the directive on purpose; check
        # only executable ``strftime("...")`` / ``strftime('...')`` calls.
        import re

        for call in re.findall(r"strftime\(\s*[\"']([^\"']*)[\"']", source):
            self.assertNotIn("%-", call, f"Unix-only directive in strftime({call!r})")
            self.assertNotIn("%#", call, f"Windows-only directive in strftime({call!r})")

    def test_empty_and_bad_dates_are_safe(self):
        self.assertEqual(format_date(None), "")
        self.assertEqual(format_date(""), "")
        self.assertEqual(format_date("not-a-date"), "")
        self.assertEqual(format_datetime(None), "")


class ReceiptEmailBuilderTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        hotel_settings(hotel_name="J-ONE HOTEL & LODGE", phone="0800", email="hotel@jone.test")
        self.rt = make_room_type(name="Executive Suite", price="50000.00")
        self.room = make_room(self.rt, 301)
        self.guest = make_guest(email="guest@example.com")

    def _receipt(self, booking):
        return ReceiptSerializer().to_representation(booking)

    def test_paid_receipt_naira_and_status(self):
        booking = make_booking(
            self.guest, self.rt, rooms=[self.room],
            status=Booking.Status.CONFIRMED, amount_paid="150000.00", total="150000.00",
        )
        Payment.objects.create(
            booking=booking, reference="J1P-REC-PAID", amount=Decimal("150000.00"),
            currency="NGN", status=Payment.Status.SUCCESS, provider="PAYSTACK",
            paid_at=timezone.now(),
        )
        booking.payment_status = Booking.PaymentStatus.PAID
        booking.save()
        subject, text, html = render_receipt_email(self._receipt(booking))

        self.assertEqual(subject, f"Payment Receipt — {booking.booking_reference}")
        self.assertIn("₦150,000.00", html)
        self.assertIn("₦0.00", html)  # outstanding
        self.assertIn("PAID", html)
        self.assertIn("Paid in full", html)
        # No raw currency-code money and no raw ISO timestamp leaks.
        self.assertNotIn("150000.00 NGN", html)
        self.assertNotIn("+00:00", html)
        # Plain-text fallback carries the same facts.
        self.assertIn("₦150,000.00", text)
        self.assertIn(booking.booking_reference, text)

    def test_partial_payment_shows_outstanding(self):
        booking = make_booking(
            self.guest, self.rt, rooms=[self.room],
            status=Booking.Status.CONFIRMED, amount_paid="75000.00", total="150000.00",
        )
        Payment.objects.create(
            booking=booking, reference="J1P-REC-PART", amount=Decimal("75000.00"),
            currency="NGN", status=Payment.Status.SUCCESS, provider="PAYSTACK",
            paid_at=timezone.now(),
        )
        booking.payment_status = Booking.PaymentStatus.PARTIALLY_PAID
        booking.save()
        _, _, html = render_receipt_email(self._receipt(booking))
        self.assertIn("PARTIALLY PAID", html)
        self.assertIn("₦75,000.00", html)

    def test_multiple_payments_render_history_without_duplicates(self):
        booking = make_booking(
            self.guest, self.rt, rooms=[self.room],
            status=Booking.Status.CONFIRMED, amount_paid="150000.00", total="150000.00",
        )
        Payment.objects.create(
            booking=booking, reference="J1P-REC-A", amount=Decimal("75000.00"),
            currency="NGN", status=Payment.Status.SUCCESS, provider="PAYSTACK",
            channel="card", paid_at=timezone.now() - timedelta(days=1),
        )
        Payment.objects.create(
            booking=booking, reference="J1P-REC-B", amount=Decimal("75000.00"),
            currency="NGN", status=Payment.Status.SUCCESS, provider="CASH",
            channel="cash", paid_at=timezone.now(),
        )
        booking.payment_status = Booking.PaymentStatus.PAID
        booking.save()
        _, _, html = render_receipt_email(self._receipt(booking))
        self.assertIn("Payment History", html)
        self.assertIn("J1P-REC-A", html)
        self.assertIn("J1P-REC-B", html)
        # Each reference appears exactly once (no duplicate rows).
        self.assertEqual(html.count("J1P-REC-A"), 1)
        self.assertEqual(html.count("J1P-REC-B"), 1)

    def test_dynamic_values_are_html_escaped(self):
        self.guest.first_name = "<b>Ada</b>"
        self.guest.last_name = "\"><script>alert(1)</script>"
        self.guest.save()
        booking = make_booking(
            self.guest, self.rt, rooms=[self.room],
            status=Booking.Status.CONFIRMED, amount_paid="150000.00", total="150000.00",
        )
        Payment.objects.create(
            booking=booking, reference="J1P-REC-XSS", amount=Decimal("150000.00"),
            currency="NGN", status=Payment.Status.SUCCESS, provider="PAYSTACK",
            paid_at=timezone.now(),
        )
        _, _, html = render_receipt_email(self._receipt(booking))
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertNotIn("<b>Ada</b>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_long_reference_has_wrap_hint(self):
        booking = make_booking(
            self.guest, self.rt, rooms=[self.room],
            status=Booking.Status.CONFIRMED, amount_paid="150000.00", total="150000.00",
        )
        Payment.objects.create(
            booking=booking, reference="J1P-VERYLONGREFERENCE-0123456789ABCDEF0123456789",
            amount=Decimal("150000.00"), currency="NGN", status=Payment.Status.SUCCESS,
            provider="PAYSTACK", paid_at=timezone.now(),
        )
        _, _, html = render_receipt_email(self._receipt(booking))
        self.assertIn("J1P-VERYLONGREFERENCE-0123456789ABCDEF0123456789", html)
        self.assertIn("word-break:break-all", html)


class StatusPresentationTests(SimpleTestCase):
    """Every real status maps to a badge; unknown keys degrade safely.

    The map is keyed on Booking.PaymentStatus / Booking.Status values. If one
    is ever renamed without updating the map the receipt silently prints a
    meaningless "STATUS" chip, so both directions are pinned here.
    """

    def test_every_payment_status_has_its_own_presentation(self):
        from apps.bookings.models import Booking

        for value, _label in Booking.PaymentStatus.choices:
            with self.subTest(payment_status=value):
                status = _status_presentation({"payment_status": value})
                self.assertNotEqual(status["badge"], _DEFAULT_STATUS[1])
                self.assertTrue(status["label"])
                self.assertTrue(status["glyph"])

    def test_cancelled_booking_overrides_a_lingering_payment_status(self):
        status = _status_presentation(
            {"booking_status": "CANCELLED", "payment_status": "PAID"}
        )
        self.assertEqual(status["badge"], "CANCELLED")
        self.assertEqual(status["tone"], "cancelled")

    def test_unknown_status_falls_back_without_raising(self):
        status = _status_presentation({"payment_status": "SOMETHING_NEW"})
        self.assertEqual(status["badge"], _DEFAULT_STATUS[1])

    def test_missing_status_falls_back_without_raising(self):
        self.assertEqual(_status_presentation({})["badge"], _DEFAULT_STATUS[1])

    def test_status_glyphs_are_email_safe(self):
        """Glyphs must come from the widely-supported WGL-4 repertoire.

        Symbols outside it (U+25D1 circle-half, U+21BA arrow-loop) render as
        tofu boxes in Outlook and several Android clients. Status is also
        carried by the text badge, so a glyph is never the only signal.
        """
        allowed = {"\u2713", "\u2715", "\u2717", "\u2190", "\u00bd", "\u2022", "\u2014"}
        for key, (_label, _badge, glyph, _tone) in _STATUS_PRESENTATION.items():
            with self.subTest(status=key):
                self.assertIn(glyph, allowed, f"{key}: {glyph!r} is not email-safe")
