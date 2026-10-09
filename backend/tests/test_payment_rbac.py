"""Payment/receipt endpoints use least-privilege capabilities, not staff-wide gates."""
from decimal import Decimal

from django.utils import timezone

from apps.accounts.models import User
from apps.bookings.models import Booking
from apps.payments.models import Payment, Refund

from .base import BaseAPITestCase
from .factories import make_booking, make_guest, make_room, make_room_type, make_staff


class PaymentCapabilityTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.room_type = make_room_type("Finance RBAC", price="10000.00")
        self.room = make_room(self.room_type, "FIN-R1")
        self.guest = make_guest("finance-rbac@guest.test")
        self.booking = make_booking(
            self.guest,
            self.room_type,
            rooms=[self.room],
            status=Booking.Status.CONFIRMED,
            amount_paid="10000.00",
            total="30000.00",
        )
        self.payment = Payment.objects.create(
            booking=self.booking,
            reference="J1P-FIN-RBAC-001",
            provider=Payment.Provider.CASH,
            amount=Decimal("10000.00"),
            currency="NGN",
            status=Payment.Status.SUCCESS,
            paid_at=timezone.now(),
        )
        self.refund = Refund.objects.create(
            booking=self.booking,
            payment=self.payment,
            amount=Decimal("1000.00"),
            currency="NGN",
            paystack_transaction_reference=self.payment.reference,
        )

    def test_accountant_can_read_payment_refund_and_staff_receipt_but_not_full_booking(self):
        accountant = make_staff("finance-reader@staff.test", role=User.Role.ACCOUNTANT)
        self.auth(accountant)

        responses = (
            self.client.get("/api/admin/payments/"),
            self.client.get(f"/api/admin/payments/{self.payment.pk}/"),
            self.client.get("/api/admin/payments/refunds/"),
            self.client.get(f"/api/admin/payments/refunds/{self.refund.pk}/"),
            self.client.get(f"/api/admin/bookings/{self.booking.booking_reference}/receipt/"),
        )
        for response in responses:
            self.assertEqual(response.status_code, 200, response.content)

        # payment.read is not a blanket booking-read grant; a distinct staff
        # receipt projection serves finance without exposing booking actions.
        public_receipt = self.client.get(f"/api/bookings/{self.booking.booking_reference}/receipt/")
        self.assertEqual(public_receipt.status_code, 404)
        booking_detail = self.client.get(f"/api/bookings/{self.booking.booking_reference}/")
        self.assertEqual(booking_detail.status_code, 404)
        self.assertEqual(self.client.get("/api/admin/bookings/").status_code, 403)
        self.assertEqual(
            self.client.get(f"/api/admin/bookings/{self.booking.booking_reference}/").status_code,
            403,
        )

        self.assertEqual(
            self.client.post(
                f"/api/admin/bookings/{self.booking.booking_reference}/send-receipt/", {}, format="json"
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                "/api/admin/payments/record/",
                {"booking_reference": self.booking.booking_reference, "amount": "100.00", "provider": "CASH"},
                format="json",
            ).status_code,
            403,
        )

    def test_cashier_collection_capability_does_not_grant_booking_payment_or_ledger_access(self):
        cashier = make_staff("cashier-rbac@staff.test", role=User.Role.CASHIER)
        self.auth(cashier)

        self.assertEqual(self.client.get("/api/admin/payments/").status_code, 403)
        self.assertEqual(
            self.client.get(f"/api/admin/bookings/{self.booking.booking_reference}/receipt/").status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                "/api/admin/payments/record/",
                {"booking_reference": self.booking.booking_reference, "amount": "100.00", "provider": "CASH"},
                format="json",
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                "/api/payments/initialize/", {"booking_reference": self.booking.booking_reference}, format="json"
            ).status_code,
            404,
        )
        self.assertEqual(self.client.get(f"/api/payments/verify/{self.payment.reference}/").status_code, 404)
        self.assertEqual(self.client.get(f"/api/bookings/{self.booking.booking_reference}/").status_code, 404)
        self.assertEqual(Payment.objects.count(), 1)

    def test_front_desk_supervisor_may_record_a_booking_payment_by_capability(self):
        supervisor = make_staff("frontdesk-supervisor@staff.test", role=User.Role.FRONT_DESK_SUPERVISOR)
        self.auth(supervisor)

        response = self.client.post(
            "/api/admin/payments/record/",
            {"booking_reference": self.booking.booking_reference, "amount": "500.00", "provider": "CASH"},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(Payment.objects.filter(booking=self.booking).count(), 2)
