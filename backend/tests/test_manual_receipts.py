# tests/test_manual_receipts.py
"""Receipts must be truthful for EVERY payment type.

Online (Paystack) and manual/walk-in (CASH / POS / BANK TRANSFER) payments all
flow through the same authoritative ReceiptSerializer and are rendered by the
same rc-access design (frontend/js/receipt.js on screen, receipt_pdf.py in the
email attachment). These tests lock in:

* the serializer emits the same structure for all four provider types;
* provider/channel/recorded_by identify manual payments correctly;
* a cash/POS/transfer receipt never carries fabricated Paystack fields —
  no invented gateway "Session Id", no ONLINE PAYMENT label;
* a Paystack receipt keeps its transaction id / online label.
"""
from decimal import Decimal

from django.utils import timezone

from apps.accounts.models import User
from apps.bookings.models import Booking
from apps.bookings.serializers import ReceiptSerializer
from apps.bookings.services import receipt_pdf as R
from apps.payments.models import Payment
from apps.payments.services import payment_service

from .base import BaseAPITestCase
from .factories import make_booking, make_guest, make_room, make_room_type, make_staff


class ManualReceiptTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.staff = make_staff("frontdesk@j1.dev", role=User.Role.RECEPTIONIST)
        self.room_type = make_room_type("Suite", price="30000.00")
        self.room = make_room(self.room_type, "401")

    def _booking(self):
        guest = make_guest(email=f"walkin{Booking.objects.count()}@guest.dev")
        return make_booking(
            guest, self.room_type, rooms=[self.room],
            status=Booking.Status.PENDING, total="60000.00",
        )

    def _receipt_for(self, provider):
        booking = self._booking()
        payment_service.record_offline_payment(
            booking=booking, staff_user=self.staff,
            amount=Decimal("60000.00"), provider=provider, notes="Front desk",
        )
        booking.refresh_from_db()
        return booking, ReceiptSerializer().to_representation(booking)

    EXPECTED_KEYS = {
        "hotel", "booking_reference", "booking_status", "payment_status",
        "guest", "room_type", "room_numbers", "rooms", "check_in", "check_out",
        "nights", "price_per_night", "subtotal", "discount", "tax", "fees",
        "total", "amount_paid", "amount_due", "currency", "issued_at",
        "receipt_reference", "latest_payment", "payments",
    }

    def test_all_manual_providers_share_the_receipt_structure(self):
        for provider in (Payment.Provider.CASH, Payment.Provider.POS,
                         Payment.Provider.BANK_TRANSFER):
            with self.subTest(provider=provider):
                booking, receipt = self._receipt_for(provider)
                self.assertTrue(self.EXPECTED_KEYS.issubset(receipt.keys()),
                                self.EXPECTED_KEYS - set(receipt.keys()))
                latest = receipt["latest_payment"]
                self.assertEqual(latest["provider"], provider)
                self.assertEqual(latest["status"], "SUCCESS")
                self.assertEqual(latest["recorded_by"], self.staff.email)
                self.assertEqual(latest["notes"], "Front desk")
                self.assertEqual(latest["provider_reference"], "")  # not Paystack
                self.assertEqual(latest["transaction_id"], "")      # no gateway id
                self.assertEqual(receipt["amount_paid"], "60000.00")
                self.assertEqual(receipt["amount_due"], "0.00")
                self.assertEqual(receipt["booking_status"], "CONFIRMED")

    def test_manual_receipt_renderer_rows_identify_the_real_method(self):
        labels_by_provider = {
            Payment.Provider.CASH: "CASH",
            Payment.Provider.POS: "POS TERMINAL",
            Payment.Provider.BANK_TRANSFER: "BANK TRANSFER",
        }
        for provider, expected in labels_by_provider.items():
            with self.subTest(provider=provider):
                _, receipt = self._receipt_for(provider)
                d = R._normalize(receipt, {})
                rows = dict((label, lines) for (label, lines, *_r) in R._detail_rows(d))
                self.assertEqual(d["transaction_type"], expected)
                self.assertNotIn("ONLINE PAYMENT", d["transaction_type"])
                # No fabricated gateway session for an offline payment.
                self.assertNotIn("Session Id", rows)
                # Staff attribution is present.
                self.assertEqual(rows.get("Recorded By"), [self.staff.email])
                # The real internal payment reference is still shown.
                self.assertEqual(rows["Transaction Reference"], [receipt["receipt_reference"]])

    def test_paystack_receipt_keeps_online_labels_and_session_id(self):
        booking = self._booking()
        Payment.objects.create(
            booking=booking, reference="J1P-ONL-0001",
            provider=Payment.Provider.PAYSTACK, amount=Decimal("60000.00"),
            currency="NGN", status=Payment.Status.SUCCESS, channel="card",
            transaction_id="TX-1234567", paid_at=timezone.now(),
        )
        from apps.bookings.services import booking_service
        booking_service.register_successful_payment(booking, Decimal("60000.00"))
        booking.refresh_from_db()
        receipt = ReceiptSerializer().to_representation(booking)
        d = R._normalize(receipt, {})
        rows = dict((label, lines) for (label, lines, *_r) in R._detail_rows(d))
        self.assertEqual(d["transaction_type"], "ONLINE PAYMENT / CARD")
        self.assertEqual(rows["Session Id"], ["TX-1234567"])
        self.assertNotIn("Recorded By", rows)  # payer ≠ staff attribution

    def test_manual_receipt_pdf_renders(self):
        _, receipt = self._receipt_for(Payment.Provider.CASH)
        pdf = R.render_receipt_pdf(receipt)
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertGreater(len(pdf), 1000)

    def test_receipt_endpoint_returns_manual_payment_truthfully(self):
        booking, _ = self._receipt_for(Payment.Provider.POS)
        self.auth(self.staff)
        response = self.client.get(f"/api/bookings/{booking.booking_reference}/receipt/")
        self.assertEqual(response.status_code, 200)
        latest = response.json()["data"]["latest_payment"]
        self.assertEqual(latest["provider"], "POS")
        self.assertEqual(latest["provider_label"], "POS Terminal")
        self.assertEqual(latest["recorded_by"], self.staff.email)
