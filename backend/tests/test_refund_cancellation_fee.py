# tests/test_refund_cancellation_fee.py
"""Regression tests: the cancellation fee must actually be deducted from the
amount sent to Paystack, and refunds must stay inside safe bounds.

These guard the exact gap reported in production: a refund completed, but the
guest received the full amount paid because the fee was never applied to the
Paystack request.
"""
from decimal import Decimal
from unittest.mock import patch

from django.test import override_settings

from apps.accounts.models import User
from apps.bookings.models import Booking
from apps.core.exceptions import PaymentError
from apps.enquiries.models import Enquiry
from apps.payments.models import Payment, Refund
from apps.payments.services import payment_service

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


class RefundCancellationFeeTests(BaseAPITestCase):
    """10% cancellation fee on a ₦50,000 payment ⇒ ₦45,000 refunded."""

    def setUp(self):
        super().setUp()
        hotel_settings(cancellation_fee_percent=Decimal("10.00"))
        self.room_type = make_room_type("Executive", price="50000.00")
        self.room = make_room(self.room_type, "401")
        self.guest_user = make_user("fee.guest@example.com", password="Str0ng!Pass")
        self.guest = make_guest(email="fee.guest@example.com", user=self.guest_user)
        self.booking = make_booking(
            self.guest, self.room_type, rooms=[self.room],
            status=Booking.Status.CONFIRMED, amount_paid="50000.00", total="50000.00",
        )
        self.booking.payment_status = Booking.PaymentStatus.PAID
        self.booking.save(update_fields=["payment_status"])
        self.payment = Payment.objects.create(
            booking=self.booking, user=self.guest_user, reference="J1P-FEE-0001",
            provider=Payment.Provider.PAYSTACK, amount=Decimal("50000.00"),
            currency="NGN", status=Payment.Status.SUCCESS, transaction_id="555000111",
        )
        self.manager = make_staff("fee.manager@staff.dev", role=User.Role.MANAGER)

    # --- helpers ---------------------------------------------------------
    def _approved_enquiry(self):
        self.unauth()
        response = self.client.post("/api/enquiries/", {
            "name": "Ada Obi", "email": self.guest.email, "phone": self.guest.phone,
            "subject": "Cancellation / refund request",
            "message": "Please cancel my stay, my travel plans changed.",
            "enquiry_type": "CANCELLATION",
            "booking_reference": self.booking.booking_reference,
            "payment_reference": self.payment.reference,
            "refund_requested": True, "preferred_contact_method": "EMAIL",
            "cancellation_reason": "Travel plan changed.",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.json())
        enquiry = Enquiry.objects.get()
        self.auth(self.manager)
        approve = self.client.post(
            f"/api/admin/enquiries/{enquiry.pk}/approve-cancellation/",
            {"notes": "Policy checked."}, format="json",
        )
        self.assertEqual(approve.status_code, 200, approve.json())
        enquiry.refresh_from_db()
        return enquiry

    @staticmethod
    def _paystack_response(amount_kobo):
        return {
            "id": 4321, "status": "pending", "amount": amount_kobo, "currency": "NGN",
            "reference": "RFND_4321",
            "transaction": {"reference": "J1P-FEE-0001", "id": "555000111"},
        }

    # --- tests -----------------------------------------------------------
    def test_policy_records_fee_and_net_refund_on_approval(self):
        enquiry = self._approved_enquiry()
        self.assertEqual(enquiry.calculated_cancellation_fee, Decimal("5000.00"))
        self.assertEqual(enquiry.calculated_refund_amount, Decimal("45000.00"))

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_mock")
    @patch("apps.payments.services.paystack.create_refund")
    def test_cancellation_fee_is_deducted_from_paystack_refund_amount(self, mock_refund):
        enquiry = self._approved_enquiry()
        mock_refund.return_value = self._paystack_response(4_500_000)

        response = self.client.post(
            f"/api/admin/enquiries/{enquiry.pk}/process-refund/", {}, format="json"
        )
        self.assertEqual(response.status_code, 202, response.json())

        # THE regression: Paystack must receive the NET amount, in kobo.
        _, kwargs = mock_refund.call_args
        self.assertEqual(kwargs["amount_kobo"], 4_500_000)

        refund = Refund.objects.get(cancellation_request=enquiry)
        self.assertEqual(refund.amount, Decimal("45000.00"))

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_mock")
    @patch("apps.payments.services.paystack.create_refund")
    def test_stale_gross_snapshot_is_corrected_to_net_before_submission(self, mock_refund):
        """A snapshot that never had the fee applied must not reach Paystack."""
        enquiry = self._approved_enquiry()
        # Simulate a stale/gross figure (e.g. written before the fee existed).
        enquiry.calculated_refund_amount = Decimal("50000.00")
        enquiry.save(update_fields=["calculated_refund_amount"])

        mock_refund.return_value = self._paystack_response(4_500_000)
        response = self.client.post(
            f"/api/admin/enquiries/{enquiry.pk}/process-refund/", {}, format="json"
        )
        self.assertEqual(response.status_code, 202, response.json())

        _, kwargs = mock_refund.call_args
        self.assertEqual(kwargs["amount_kobo"], 4_500_000)   # NOT 5,000,000
        self.assertEqual(Refund.objects.get().amount, Decimal("45000.00"))
        enquiry.refresh_from_db()
        self.assertEqual(enquiry.calculated_refund_amount, Decimal("45000.00"))
        self.assertEqual(enquiry.calculated_cancellation_fee, Decimal("5000.00"))

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_mock")
    @patch("apps.payments.services.paystack.create_refund")
    def test_fee_is_never_deducted_twice(self, mock_refund):
        """A snapshot already net of the fee stays exactly as-is."""
        enquiry = self._approved_enquiry()
        self.assertEqual(enquiry.calculated_refund_amount, Decimal("45000.00"))

        mock_refund.return_value = self._paystack_response(4_500_000)
        self.client.post(f"/api/admin/enquiries/{enquiry.pk}/process-refund/", {}, format="json")

        _, kwargs = mock_refund.call_args
        self.assertEqual(kwargs["amount_kobo"], 4_500_000)   # not 4,050,000

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_mock")
    @patch("apps.payments.services.paystack.create_refund")
    def test_refund_cannot_exceed_refundable_balance(self, mock_refund):
        """An already-processed refund shrinks what remains available."""
        enquiry = self._approved_enquiry()
        Refund.objects.create(
            booking=self.booking, payment=self.payment, amount=Decimal("44000.00"),
            currency="NGN", status=Refund.Status.PROCESSED,
            paystack_transaction_reference=self.payment.reference,
            paystack_refund_id="prior-1",
        )
        mock_refund.return_value = self._paystack_response(100_000)

        self.client.post(f"/api/admin/enquiries/{enquiry.pk}/process-refund/", {}, format="json")
        _, kwargs = mock_refund.call_args
        # 50,000 paid − 44,000 already refunded = 6,000 available; policy wants
        # 45,000, so the smaller bound wins.
        self.assertEqual(kwargs["amount_kobo"], 600_000)

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_mock")
    @patch("apps.payments.services.paystack.create_refund")
    def test_full_fee_leaves_nothing_to_refund(self, mock_refund):
        hotel_settings(cancellation_fee_percent=Decimal("100.00"))
        enquiry = self._approved_enquiry()
        with self.assertRaises(PaymentError):
            payment_service.initiate_cancellation_refund(
                enquiry=enquiry, staff_user=self.manager,
            )
        mock_refund.assert_not_called()
        self.assertFalse(Refund.objects.filter(cancellation_request=enquiry).exists())

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_mock")
    @patch("apps.payments.services.paystack.create_refund")
    def test_duplicate_refund_submission_is_prevented(self, mock_refund):
        enquiry = self._approved_enquiry()
        mock_refund.return_value = self._paystack_response(4_500_000)

        first = self.client.post(f"/api/admin/enquiries/{enquiry.pk}/process-refund/", {}, format="json")
        self.assertEqual(first.status_code, 202, first.json())

        # A second submission is refused by the backend state guard rather than
        # creating a second Paystack refund.
        second = self.client.post(f"/api/admin/enquiries/{enquiry.pk}/process-refund/", {}, format="json")
        self.assertEqual(second.status_code, 400, second.json())
        self.assertEqual(second.json()["code"], "PAYMENT_FAILED")

        # Exactly ONE Paystack call and ONE refund row.
        self.assertEqual(mock_refund.call_count, 1)
        self.assertEqual(Refund.objects.filter(cancellation_request=enquiry).count(), 1)

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_mock")
    @patch("apps.payments.services.paystack.create_refund")
    def test_zero_fee_policy_refunds_the_full_amount(self, mock_refund):
        hotel_settings(cancellation_fee_percent=Decimal("0.00"))
        enquiry = self._approved_enquiry()
        mock_refund.return_value = self._paystack_response(5_000_000)

        self.client.post(f"/api/admin/enquiries/{enquiry.pk}/process-refund/", {}, format="json")
        _, kwargs = mock_refund.call_args
        self.assertEqual(kwargs["amount_kobo"], 5_000_000)
        self.assertEqual(Refund.objects.get().amount, Decimal("50000.00"))
