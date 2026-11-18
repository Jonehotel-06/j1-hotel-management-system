"""Batch 1 — Reception Event Management: registration, availability, payments,
accounting integration, cancellation, receipts and duplicate protection.

These are integration tests: they drive the real API routes, the real service
layer, the real finance ledger and the real database.
"""
from datetime import date, time, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework.exceptions import ValidationError

from apps.accounts.models import User
from apps.events.models import EventLocation, EventPayment, HotelEvent
from apps.events import services as event_services
from apps.finance.models import FinancialLine, FinancialTransaction
from apps.finance.services import accounting
from apps.bookings.models import Guest
from tests.base import BaseAPITestCase
from tests.factories import make_staff

EVENTS = "/api/admin/events/"


class EventFixtures:
    def event_setup(self):
        self.receptionist = make_staff("events-desk@staff.dev", role=User.Role.RECEPTIONIST)
        self.manager = make_staff("events-gm@staff.dev", role=User.Role.GENERAL_MANAGER)
        self.housekeeping = make_staff("events-hk@staff.dev", role=User.Role.HOUSEKEEPING)
        self.guest = Guest.objects.create(
            first_name="Chidi", last_name="Eze", email="chidi@example.com", phone="08030000001",
            country="Nigeria",
        )
        self.inside = EventLocation.objects.get(code="INSIDE_BAR")
        self.outside = EventLocation.objects.get(code="OUTSIDE_BAR")
        self.compound = EventLocation.objects.get(code="HOTEL_COMPOUND")
        self.day = date.today() + timedelta(days=20)

    def payload(self, **overrides):
        data = {
            "guest_id": self.guest.pk,
            "location_id": self.inside.pk,
            "event_type": "Birthday party",
            "event_date": str(self.day),
            "start_time": "18:00",
            "end_time": "22:00",
            "expected_guests": 40,
            "total_charged": "150000.00",
        }
        data.update(overrides)
        return data


class EventLocationSeedTests(EventFixtures, BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.event_setup()
        self.auth(self.receptionist)

    def test_seeded_locations_are_the_three_configured_spaces(self):
        response = self.client.get(EVENTS + "locations/")
        self.assertEqual(response.status_code, 200)
        names = {row["name"] for row in response.data["data"]}
        self.assertTrue({"Inside Bar", "Outside Bar", "Hotel Compound"} <= names)

    def test_housekeeping_cannot_reach_event_endpoints(self):
        self.auth(self.housekeeping)
        self.assertEqual(self.client.get(EVENTS).status_code, 403)
        self.assertEqual(self.client.post(EVENTS, self.payload(), format="json").status_code, 403)


class EventRegistrationTests(EventFixtures, BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.event_setup()
        self.auth(self.receptionist)

    def test_unpaid_event_creates_a_receivable_and_no_collection(self):
        response = self.client.post(EVENTS, self.payload(), format="json")
        self.assertEqual(response.status_code, 201, response.data)
        body = response.data["data"]
        self.assertEqual(Decimal(body["total_charged"]), Decimal("150000.00"))
        self.assertEqual(Decimal(body["amount_paid"]), Decimal("0.00"))
        self.assertEqual(Decimal(body["outstanding_balance"]), Decimal("150000.00"))
        self.assertEqual(body["payment_status"], HotelEvent.PaymentStatus.UNPAID)

        event = HotelEvent.objects.get(reference=body["reference"])
        charge = event.charge_transaction
        self.assertEqual(charge.type, FinancialTransaction.Type.OTHER_CHARGE)
        self.assertEqual(charge.source_key, f"event-charge:{event.reference}")
        debits = {line.account_code: Decimal(line.amount) for line in charge.lines.filter(direction=FinancialLine.Direction.DEBIT)}
        credits = {line.account_code: Decimal(line.amount) for line in charge.lines.filter(direction=FinancialLine.Direction.CREDIT)}
        self.assertEqual(debits[accounting.ACCOUNTS_RECEIVABLE], Decimal("150000.00"))
        self.assertEqual(credits[accounting.OTHER_OPERATING_REVENUE], Decimal("150000.00"))
        # Unpaid charge must never appear as a collection.
        self.assertFalse(
            FinancialTransaction.objects.filter(
                type=FinancialTransaction.Type.PAYMENT_COLLECTION,
                source_reference=event.reference,
            ).exists()
        )

    def test_full_payment_at_registration_is_recorded_exactly_once(self):
        payload = self.payload(amount_paid="150000.00", payment_method="CASH",
                               payment_idempotency_key="init-full-1")
        response = self.client.post(EVENTS, payload, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        body = response.data["data"]
        self.assertEqual(body["payment_status"], HotelEvent.PaymentStatus.PAID)
        self.assertEqual(Decimal(body["outstanding_balance"]), Decimal("0.00"))

        event = HotelEvent.objects.get(reference=body["reference"])
        self.assertEqual(event.payments.count(), 1)
        payment = event.payments.get()
        self.assertEqual(payment.status, EventPayment.Status.SUCCESS)
        collection = payment.collection_transaction
        self.assertEqual(collection.type, FinancialTransaction.Type.PAYMENT_COLLECTION)
        debits = {line.account_code: Decimal(line.amount) for line in collection.lines.filter(direction=FinancialLine.Direction.DEBIT)}
        credits = {line.account_code: Decimal(line.amount) for line in collection.lines.filter(direction=FinancialLine.Direction.CREDIT)}
        self.assertEqual(debits[accounting.CASH_ON_HAND], Decimal("150000.00"))
        self.assertEqual(credits[accounting.ACCOUNTS_RECEIVABLE], Decimal("150000.00"))

    def test_partial_payment_then_subsequent_payment_settles_the_balance(self):
        response = self.client.post(
            EVENTS,
            self.payload(amount_paid="100000.00", payment_method="CASH", payment_idempotency_key="init-part-1"),
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        reference = response.data["data"]["reference"]
        self.assertEqual(response.data["data"]["payment_status"], HotelEvent.PaymentStatus.PARTIALLY_PAID)
        self.assertEqual(Decimal(response.data["data"]["outstanding_balance"]), Decimal("50000.00"))

        second = self.client.post(
            f"{EVENTS}{reference}/payments/",
            {"amount": "50000.00", "method": "BANK_TRANSFER", "external_reference": "TRF-EVT-0001",
             "idempotency_key": "follow-up-1"},
            format="json",
        )
        self.assertEqual(second.status_code, 201, second.data)

        event = HotelEvent.objects.get(reference=reference)
        event.refresh_from_db()
        self.assertEqual(Decimal(event.amount_paid), Decimal("150000.00"))
        self.assertEqual(event.payment_status, HotelEvent.PaymentStatus.PAID)
        self.assertEqual(Decimal(event.outstanding_balance), Decimal("0.00"))
        self.assertEqual(event.payments.count(), 2)
        # Two collections, each posted once.
        self.assertEqual(
            FinancialTransaction.objects.filter(
                type=FinancialTransaction.Type.PAYMENT_COLLECTION,
                source_reference=reference,
            ).count(),
            2,
        )

    def test_duplicate_payment_submission_is_rejected_as_a_replay(self):
        response = self.client.post(EVENTS, self.payload(), format="json")
        reference = response.data["data"]["reference"]
        payload = {"amount": "50000.00", "method": "CASH", "idempotency_key": "same-key-1"}
        first = self.client.post(f"{EVENTS}{reference}/payments/", payload, format="json")
        self.assertEqual(first.status_code, 201, first.data)
        second = self.client.post(f"{EVENTS}{reference}/payments/", payload, format="json")
        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(HotelEvent.objects.get(reference=reference).payments.count(), 1)
        self.assertFalse(second.data["data"] is None)

    def test_over_collection_is_refused(self):
        response = self.client.post(EVENTS, self.payload(), format="json")
        reference = response.data["data"]["reference"]
        over = self.client.post(
            f"{EVENTS}{reference}/payments/",
            {"amount": "200000.00", "method": "CASH", "idempotency_key": "over-1"},
            format="json",
        )
        self.assertEqual(over.status_code, 400)
        self.assertEqual(HotelEvent.objects.get(reference=reference).payments.count(), 0)

    def test_electronic_payment_requires_a_reference(self):
        response = self.client.post(EVENTS, self.payload(), format="json")
        reference = response.data["data"]["reference"]
        missing = self.client.post(
            f"{EVENTS}{reference}/payments/",
            {"amount": "1000.00", "method": "POS", "idempotency_key": "pos-noref"},
            format="json",
        )
        self.assertEqual(missing.status_code, 400)

    def test_unverified_paystack_reference_is_never_treated_as_paid(self):
        response = self.client.post(EVENTS, self.payload(), format="json")
        reference = response.data["data"]["reference"]
        with __import__("unittest").mock.patch(
            "apps.events.services._verify_paystack_reference",
            side_effect=ValidationError({"external_reference": "Paystack verification failed."}),
        ):
            failed = self.client.post(
                f"{EVENTS}{reference}/payments/",
                {"amount": "150000.00", "method": "PAYSTACK", "external_reference": "PSK-UNVERIFIED"},
                format="json",
            )
        self.assertEqual(failed.status_code, 400)
        event = HotelEvent.objects.get(reference=reference)
        self.assertEqual(Decimal(event.amount_paid), Decimal("0.00"))
        self.assertEqual(event.payment_status, HotelEvent.PaymentStatus.UNPAID)

    def test_new_guest_can_be_registered_with_the_event(self):
        payload = self.payload(guest_id=None, first_name="Ngozi", last_name="Umeh",
                              phone="08030000002", email="ngozi@example.com")
        payload.pop("guest_id")
        response = self.client.post(EVENTS, payload, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(Guest.objects.filter(email="ngozi@example.com").exists())

    def test_existing_guest_is_reused_not_duplicated(self):
        before = Guest.objects.count()
        response = self.client.post(EVENTS, self.payload(), format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Guest.objects.count(), before)


class EventAvailabilityTests(EventFixtures, BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.event_setup()
        self.auth(self.receptionist)

    def test_overlapping_event_in_the_same_space_is_refused(self):
        first = self.client.post(EVENTS, self.payload(), format="json")
        self.assertEqual(first.status_code, 201, first.data)
        clash = self.client.post(
            EVENTS,
            self.payload(event_type="Meeting", start_time="21:00", end_time="23:30"),
            format="json",
        )
        self.assertEqual(clash.status_code, 400)
        self.assertIn("already booked", str(clash.data).lower())

    def test_same_times_in_a_different_space_are_allowed(self):
        self.assertEqual(self.client.post(EVENTS, self.payload(), format="json").status_code, 201)
        other = self.client.post(
            EVENTS, self.payload(location_id=self.outside.pk, event_type="Meeting"), format="json"
        )
        self.assertEqual(other.status_code, 201, other.data)
        third = self.client.post(
            EVENTS, self.payload(location_id=self.compound.pk, event_type="Wedding"), format="json"
        )
        self.assertEqual(third.status_code, 201, third.data)
        self.assertEqual(HotelEvent.objects.filter(event_date=self.day).count(), 3)

    def test_back_to_back_events_do_not_conflict(self):
        self.assertEqual(self.client.post(EVENTS, self.payload(), format="json").status_code, 201)
        after = self.client.post(
            EVENTS, self.payload(start_time="22:00", end_time="23:55"), format="json"
        )
        self.assertEqual(after.status_code, 201, after.data)

    def test_receptionist_cannot_override_a_conflict(self):
        self.client.post(EVENTS, self.payload(), format="json")
        clash = self.client.post(
            EVENTS,
            self.payload(start_time="19:00", end_time="20:00", override=True, override_reason="VIP"),
            format="json",
        )
        self.assertEqual(clash.status_code, 400)

    def test_authorised_manager_can_override_with_a_recorded_reason(self):
        self.client.post(EVENTS, self.payload(), format="json")
        self.auth(self.manager)
        clash = self.client.post(
            EVENTS,
            self.payload(start_time="19:00", end_time="20:00", override=True,
                         override_reason="Same family event, approved by GM"),
            format="json",
        )
        self.assertEqual(clash.status_code, 201, clash.data)
        overridden = HotelEvent.objects.exclude(override_reason="").get()
        self.assertIn("approved by GM", overridden.override_reason)


class EventLifecycleTests(EventFixtures, BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.event_setup()
        self.auth(self.receptionist)

    def test_receipt_payload_carries_the_required_event_fields(self):
        created = self.client.post(
            EVENTS, self.payload(amount_paid="100000.00", payment_method="CASH",
                                 payment_idempotency_key="rcpt-1"),
            format="json",
        )
        reference = created.data["data"]["reference"]
        receipt = self.client.get(f"{EVENTS}{reference}/receipt/")
        self.assertEqual(receipt.status_code, 200)
        data = receipt.data["data"]
        self.assertEqual(data["event_reference"], reference)
        self.assertEqual(data["guest_name"], self.guest.full_name)
        self.assertEqual(data["event_location"], "Inside Bar")
        self.assertEqual(Decimal(data["total_charged"]), Decimal("150000.00"))
        self.assertEqual(Decimal(data["amount_paid"]), Decimal("100000.00"))
        self.assertEqual(Decimal(data["outstanding_balance"]), Decimal("50000.00"))
        self.assertEqual(data["payment_method"], "CASH")
        self.assertTrue(data["receipt_number"])
        self.assertTrue(data["date_issued"])
        self.assertEqual(len(data["payments"]), 1)

    def test_cancelling_preserves_the_financial_records(self):
        created = self.client.post(
            EVENTS, self.payload(amount_paid="100000.00", payment_method="CASH",
                                 payment_idempotency_key="cancel-1"),
            format="json",
        )
        reference = created.data["data"]["reference"]
        cancel = self.client.post(f"{EVENTS}{reference}/cancel/", {"reason": "Guest postponed"}, format="json")
        self.assertEqual(cancel.status_code, 200, cancel.data)
        event = HotelEvent.objects.get(reference=reference)
        self.assertEqual(event.status, HotelEvent.Status.CANCELLED)
        self.assertEqual(event.status, "CANCELLED")
        self.assertIsNotNone(event.charge_transaction_id)
        self.assertEqual(event.payments.count(), 1)
        self.assertEqual(Decimal(event.amount_paid), Decimal("100000.00"))

    def test_cancelled_event_refuses_new_payments(self):
        created = self.client.post(EVENTS, self.payload(), format="json")
        reference = created.data["data"]["reference"]
        self.client.post(f"{EVENTS}{reference}/cancel/", {"reason": "Double booked"}, format="json")
        blocked = self.client.post(
            f"{EVENTS}{reference}/payments/",
            {"amount": "1000.00", "method": "CASH", "idempotency_key": "after-cancel"},
            format="json",
        )
        self.assertEqual(blocked.status_code, 400)

    def test_filters_and_search_work(self):
        self.client.post(EVENTS, self.payload(), format="json")
        self.client.post(
            EVENTS, self.payload(location_id=self.compound.pk, event_type="Wedding reception"), format="json"
        )
        by_location = self.client.get(EVENTS, {"location_id": self.inside.pk})
        self.assertEqual(len(by_location.data["data"]), 1)
        by_search = self.client.get(EVENTS, {"search": "Wedding"})
        self.assertEqual(len(by_search.data["data"]), 1)
        unpaid = self.client.get(EVENTS, {"payment_status": "UNPAID"})
        self.assertEqual(len(unpaid.data["data"]), 2)
        by_date = self.client.get(EVENTS, {"date_from": str(self.day), "date_to": str(self.day)})
        self.assertEqual(len(by_date.data["data"]), 2)

    def test_service_rejects_an_end_time_before_the_start_time(self):
        with self.assertRaises((ValidationError, DjangoValidationError)):
            event_services.create_event(
                guest=self.guest, location=self.inside, event_type="Bad",
                event_date=self.day, start_time=time(20, 0), end_time=time(19, 0),
                expected_guests=5, total_charged=Decimal("1000.00"), actor=self.receptionist,
                idempotency_key="bad-window",
            )
        self.assertFalse(HotelEvent.objects.exists())
