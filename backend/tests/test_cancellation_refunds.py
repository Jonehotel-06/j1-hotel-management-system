# tests/test_cancellation_refunds.py
"""Cancellation/refund workflow tests.

These guard the redesigned flow:
* guests submit structured Contact-page cancellation/refund requests;
* direct guest cancellation endpoints are non-destructive;
* staff approval cancels the booking but does not mark refunds complete;
* Paystack refund lifecycle rows are reconciled by provider webhooks.
"""
import hashlib
import hmac
import json
from decimal import Decimal
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from django.test import override_settings

from apps.accounts.models import User
from apps.bookings.models import Booking
from apps.enquiries.models import Enquiry
from apps.payments.models import Payment, Refund

from .base import BaseAPITestCase
from .factories import make_booking, make_guest, make_room, make_room_type, make_staff, make_user


class CancellationRefundWorkflowTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.room_type = make_room_type("Executive", price="50000.00")
        self.room = make_room(self.room_type, "301")
        self.guest_user = make_user("cancel.guest@example.com", password="Str0ng!Pass")
        self.guest = make_guest(email="cancel.guest@example.com", user=self.guest_user)
        self.booking = make_booking(
            self.guest,
            self.room_type,
            rooms=[self.room],
            status=Booking.Status.CONFIRMED,
            amount_paid="50000.00",
            total="50000.00",
        )
        self.booking.payment_status = Booking.PaymentStatus.PAID
        self.booking.save(update_fields=["payment_status"])
        self.payment = Payment.objects.create(
            booking=self.booking,
            user=self.guest_user,
            reference="J1P-CANCEL-0001",
            provider=Payment.Provider.PAYSTACK,
            amount=Decimal("50000.00"),
            currency="NGN",
            status=Payment.Status.SUCCESS,
            transaction_id="987654321",
        )

    def _submit_request(self):
        self.unauth()
        return self.client.post("/api/enquiries/", {
            "name": "Ada Obi",
            "email": self.guest.email,
            "phone": self.guest.phone,
            "subject": "Cancellation / refund request",
            "message": "Please cancel my stay because my travel plan changed.",
            "enquiry_type": "CANCELLATION",
            "booking_reference": self.booking.booking_reference,
            "payment_reference": self.payment.reference,
            "refund_requested": True,
            "preferred_contact_method": "EMAIL",
            "cancellation_reason": "Travel plan changed.",
        }, format="json")

    def _post_webhook(self, payload: dict, secret="sk_test_mock"):
        body = json.dumps(payload).encode()
        signature = hmac.new(secret.encode(), msg=body, digestmod=hashlib.sha512).hexdigest()
        return self.client.post(
            "/api/payments/webhook/", data=body, content_type="application/json",
            HTTP_X_PAYSTACK_SIGNATURE=signature,
        )

    def test_contact_cancellation_request_does_not_cancel_booking_and_has_private_status_link(self):
        response = self._submit_request()
        self.assertEqual(response.status_code, 201, response.json())
        data = response.json()["data"]
        self.assertIn("cancellation_reference", data)
        self.assertIn("status_url", data)

        enquiry = Enquiry.objects.get(cancellation_reference=data["cancellation_reference"])
        self.assertEqual(enquiry.enquiry_type, Enquiry.EnquiryType.CANCELLATION)
        self.assertEqual(enquiry.related_booking, self.booking)
        self.assertEqual(enquiry.related_payment, self.payment)

        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, Booking.Status.CONFIRMED)
        self.assertEqual(self.booking.payment_status, Booking.PaymentStatus.PAID)

        parsed = urlparse(data["status_url"])
        token = parse_qs(parsed.query)["token"][0]
        status_response = self.client.get(f"/api/enquiries/cancellation-status/{enquiry.cancellation_reference}/", {"token": token})
        self.assertEqual(status_response.status_code, 200, status_response.json())
        self.assertIn("not been cancelled", status_response.json()["data"]["message"])

    def test_legacy_guest_cancel_endpoint_is_non_destructive(self):
        self.auth(self.guest_user)
        response = self.client.post(
            f"/api/bookings/{self.booking.booking_reference}/cancel/",
            {"reason": "Changed plans"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "CANCELLATION_NOT_ALLOWED")
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, Booking.Status.CONFIRMED)
        self.assertEqual(self.booking.payment_status, Booking.PaymentStatus.PAID)
        self.assertEqual(Refund.objects.count(), 0)

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_mock")
    @patch("apps.payments.services.paystack.create_refund")
    def test_staff_approval_then_paystack_refund_webhook_completes_refund(self, mock_refund):
        self.assertEqual(self._submit_request().status_code, 201)
        enquiry = Enquiry.objects.get()
        manager = make_staff("refund.manager@staff.dev", role=User.Role.MANAGER)
        self.auth(manager)

        approve = self.client.post(
            f"/api/admin/enquiries/{enquiry.pk}/approve-cancellation/",
            {"notes": "Policy checked."},
            format="json",
        )
        self.assertEqual(approve.status_code, 200, approve.json())
        self.booking.refresh_from_db()
        self.payment.refresh_from_db()
        enquiry.refresh_from_db()
        self.assertEqual(self.booking.status, Booking.Status.CANCELLED)
        self.assertEqual(self.booking.payment_status, Booking.PaymentStatus.PAID)
        self.assertEqual(self.payment.status, Payment.Status.SUCCESS)
        self.assertEqual(enquiry.refund_status, Enquiry.RefundStatus.DUE)
        self.assertEqual(enquiry.calculated_refund_amount, Decimal("50000.00"))

        mock_refund.return_value = {
            "id": 888,
            "status": "pending",
            "amount": 5_000_000,
            "currency": "NGN",
            "reference": "RFND_888",
            "transaction": {"reference": self.payment.reference, "id": self.payment.transaction_id},
        }
        process = self.client.post(
            f"/api/admin/enquiries/{enquiry.pk}/process-refund/",
            {"customer_note": "Refund for approved cancellation."},
            format="json",
        )
        self.assertEqual(process.status_code, 202, process.json())
        _, kwargs = mock_refund.call_args
        self.assertEqual(kwargs["transaction"], self.payment.transaction_id)
        self.assertEqual(kwargs["amount_kobo"], 5_000_000)  # server-derived, never browser-supplied

        refund = Refund.objects.get(cancellation_request=enquiry)
        self.assertEqual(refund.status, Refund.Status.PENDING)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.SUCCESS)

        webhook = self._post_webhook({
            "event": "refund.processed",
            "data": {
                "id": 888,
                "status": "processed",
                "amount": 5_000_000,
                "reference": "RFND_888",
                "transaction": {"reference": self.payment.reference, "id": self.payment.transaction_id},
            },
        })
        self.assertEqual(webhook.status_code, 200, webhook.json())
        refund.refresh_from_db()
        self.payment.refresh_from_db()
        self.booking.refresh_from_db()
        enquiry.refresh_from_db()
        self.assertEqual(refund.status, Refund.Status.PROCESSED)
        self.assertEqual(self.payment.status, Payment.Status.REFUNDED)
        self.assertEqual(self.booking.status, Booking.Status.CANCELLED)
        self.assertEqual(self.booking.amount_paid, Decimal("50000.00"))
        self.assertEqual(self.booking.refund_amount, Decimal("50000.00"))
        self.assertEqual(self.booking.payment_status, Booking.PaymentStatus.REFUNDED)
        self.assertEqual(enquiry.refund_status, Enquiry.RefundStatus.PROCESSED)
        self.assertEqual(enquiry.cancellation_status, Enquiry.CancellationStatus.REFUNDED)
