# tests/test_receipt_pdf_design.py
"""Tests that the backend receipt PDF reproduces the rc-access design.

The canonical receipt design lives in ``frontend/js/receipt.js`` (the
``rc-access`` layout the guest sees on-page and downloads as an image/PDF). The
worker regenerates its own PDF from the database for the email attachment; these
tests lock in that the backend generator emits the SAME rows, wording, ordering
and formatting so the two never drift apart.

They avoid depending on any PDF-text-extraction library (none is a project
dependency): the row model — ``_normalize`` + ``_detail_rows`` — is the single
source of what gets drawn, so asserting on it verifies the content, while a
separate check confirms ``render_receipt_pdf`` emits valid, non-trivial PDF
bytes.
"""
from django.test import SimpleTestCase

from apps.bookings.services import receipt_pdf as R


SAMPLE = {
    "hotel": {
        "name": "J-ONE HOTEL & LODGE",
        "address": "Plot 566 Mgbowo Street, off Ezike Street",
        "city": "Enugu", "state": "Enugu State", "country": "",
        "phone": "+234 803 211 2874", "email": "jonathanonu76@gmail.com",
    },
    "guest": {
        "name": "Ada Obi", "email": "guest@example.com", "phone": "+234 802 000 1111",
        "address": "12 Rumuola Rd", "city": "Port Harcourt", "state": "Rivers", "country": "Nigeria",
    },
    "booking_reference": "J1B-2026-000123",
    "payment_status": "PARTIALLY_PAID",
    "room_type": "Deluxe Room", "room_numbers": [101, 102], "rooms": 2,
    "check_in": "2026-09-20", "check_out": "2026-09-23", "nights": 3,
    "number_of_guests": 2, "adults": 2, "children": 0,
    "total": "75000.00", "amount_paid": "50000.00", "amount_due": "25000.00", "currency": "NGN",
    "issued_at": "2026-09-15T14:32:05+01:00",
    "receipt_reference": "J1P-REC-0001",
    "latest_payment": {
        "reference": "J1P-REC-0001", "transaction_id": "TX-8891234455",
        "provider_reference": "paystk_ref_998", "amount": "50000.00",
        "provider": "PAYSTACK", "provider_label": "Paystack", "channel": "card",
        "status": "SUCCESS", "paid_at": "2026-09-15T14:32:05+01:00",
    },
    "payments": [{
        "reference": "J1P-REC-0001", "amount": "50000.00", "status": "SUCCESS",
        "provider": "PAYSTACK", "provider_label": "Paystack", "channel": "card",
        "paid_at": "2026-09-15T14:32:05+01:00",
    }],
}


class ReceiptPdfDesignTests(SimpleTestCase):
    def _rows(self, receipt=None):
        d = R._normalize(receipt or SAMPLE, {})
        return d, R._detail_rows(d)

    def test_row_order_matches_rc_access(self):
        """Same labels, in the same order, as detailRows() in receipt.js."""
        _, rows = self._rows()
        labels = [label for (label, *_rest) in rows]
        self.assertEqual(labels, [
            "Transaction Amount", "Transaction Type", "Transaction Date",
            "Sender", "Beneficiary", "Booking Details", "Remark",
            "Booking Reference", "Transaction Reference", "Session Id",
            "Transaction Status", "Payment Summary",
        ])

    def test_amount_uses_naira_symbol_and_thousands(self):
        d, rows = self._rows()
        amount_row = rows[0]
        self.assertEqual(amount_row[0], "Transaction Amount")
        self.assertEqual(amount_row[1], ["\u20a650,000"])  # ₦50,000, latest payment
        self.assertEqual(amount_row[2], "amount")

    def test_sender_and_beneficiary_lines(self):
        d, _ = self._rows()
        self.assertEqual(d["sender_lines"], ["Ada Obi", "+234 802 000 1111", "guest@example.com"])
        self.assertEqual(d["beneficiary_lines"][0], "J-ONE HOTEL & LODGE")
        self.assertIn("+234 803 211 2874", d["beneficiary_lines"])
        self.assertIn("jonathanonu76@gmail.com", d["beneficiary_lines"])

    def test_transaction_type_and_status(self):
        d, _ = self._rows()
        self.assertEqual(d["transaction_type"], "ONLINE PAYMENT / CARD")
        self.assertEqual(d["status"], "Partially paid")
        self.assertEqual(d["status_color"], R.C_STATUS_WARN)

    def test_status_colour_coding(self):
        for status, colour in [
            ("PAID", R.C_STATUS_OK), ("SUCCESS", R.C_STATUS_OK),
            ("FAILED", R.C_STATUS_DANGER), ("CANCELLED", R.C_STATUS_DANGER),
            ("UNPAID", R.C_STATUS_WARN), ("PARTIALLY_PAID", R.C_STATUS_WARN),
        ]:
            d = R._normalize({**SAMPLE, "payment_status": status, "latest_payment": None, "payments": []}, {})
            self.assertEqual(d["status_color"], colour, status)

    def test_payment_summary_lines(self):
        d, _ = self._rows()
        self.assertEqual(d["summary_lines"], [
            "Booking total: \u20a675,000",
            "Amount paid: \u20a650,000",
            "Outstanding balance: \u20a625,000",
        ])

    def test_generated_and_transaction_dates_formatting(self):
        d, _ = self._rows()
        # accessDateTime -> "YYYY-MM-DD HH:MM:SS"; generatedDateTime -> "DD/MM/YY HH:MM:SS"
        self.assertRegex(d["transaction_date"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        self.assertRegex(d["generated_at"], r"^\d{2}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}$")

    def test_booking_details_compacted(self):
        d, _ = self._rows()
        self.assertEqual(len(d["booking_details"]), 1)
        details = d["booking_details"][0]
        self.assertIn("Deluxe Room", details)
        self.assertIn("Room 101, 102", details)
        self.assertIn("3 nights", details)
        self.assertIn("2 guests", details)

    def test_renders_valid_pdf_bytes(self):
        pdf = R.render_receipt_pdf(SAMPLE)
        self.assertTrue(pdf[:5] == b"%PDF-")
        self.assertTrue(pdf.rstrip().endswith(b"%%EOF"))
        self.assertGreater(len(pdf), 2000)

    def test_render_is_deterministic_for_same_input(self):
        # Fixed issued_at/paid_at means the drawn content is identical; only the
        # PDF's internal timestamp differs, so compare a stable size window.
        a = R.render_receipt_pdf(SAMPLE)
        b = R.render_receipt_pdf(SAMPLE)
        self.assertAlmostEqual(len(a), len(b), delta=200)

    def test_missing_optional_fields_still_renders(self):
        minimal = {
            "hotel": {"name": "J-ONE HOTEL & LODGE"},
            "guest": {"name": "Guest"},
            "booking_reference": "J1B-X",
            "total": "10000.00", "amount_paid": "10000.00", "amount_due": "0.00",
            "payment_status": "PAID",
        }
        pdf = R.render_receipt_pdf(minimal)
        self.assertTrue(pdf[:5] == b"%PDF-")

    def test_long_field_wrapping_stays_within_value_column(self):
        R._ensure_fonts()
        long_email = "an.extremely.long.beneficiary.email.address.for.wrapping@subdomain.example.com"
        lines = R._wrap(long_email, R.VALUE_WIDTH, R._FONT_BOLD, 15)
        from reportlab.pdfbase.pdfmetrics import stringWidth
        for line in lines:
            self.assertLessEqual(stringWidth(line, R._FONT_BOLD, 15), R.VALUE_WIDTH + 0.5)
