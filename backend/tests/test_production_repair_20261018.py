# tests/test_production_repair_20261018.py
"""Regression tests for the 2026-10-18 production booking/payment failure.

Production sequence (Railway, 2026-10-18)::

    INFO    Booking J1-20261018-1C634806 created: standard-room x1
            2026-10-21 → 2026-10-23 total=50000.00 source=WEBSITE
    WARNING Payment init refused: no booking matches reference
            'J1-20261018-1C634806' (...)
    WARNING Guest access exchange rejected (reference=...,
            booking_found=False, ...)

Root cause chain proven by these tests:

1. ``create_booking`` is ``@transaction.atomic`` and, *inside* that
   transaction, calls ``notify_staff`` with a message containing "→"
   (U+2192, UTF-8 ``\\xE2\\x86\\x92``).
2. The production MySQL database/column/connection is not utf8mb4, so the
   notification INSERT fails with error 1366
   ("Incorrect string value: '\\xE2\\x86\\x92 ...'").
3. ``notify_users`` caught and logged that database error — but a database
   error caught inside ``atomic()`` without a savepoint marks the whole
   transaction broken (``needs_rollback``). The "Booking created" log line
   still printed (it runs before commit) and the view still returned 201
   from the in-memory instance, but exiting the atomic block rolled the
   ENTIRE booking back.
4. Every later request (payment initialize, guest-access exchange) then
   correctly reported the booking as not found.

A non-critical side effect (a staff notification, an audit row, an email
render) must never be able to erase a committed booking. These tests fail
if a booking disappears between requests, if the wrong guest credential
is accepted, or if ``transition_order`` is ever again callable outside a
transaction (the POS HTTP-500 in the same incident).
"""
import importlib
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.cache import cache
from django.db import connection, transaction
from django.test import SimpleTestCase, TransactionTestCase, override_settings

from apps.accounts.models import User
from apps.bookings.models import Booking, Guest
from apps.core.utils import hotel_today
from apps.notifications.models import Notification
from apps.notifications.services import notify_users
from apps.payments.models import Payment

from .base import BaseAPITestCase
from .factories import hotel_settings, make_room, make_room_type, make_staff


@contextmanager
def fail_notification_insert_at_db_level():
    """Make every Notification INSERT fail with a REAL database error.

    Simulates the production MySQL 1366 at the same layer: the real ORM
    INSERT (``bulk_create``, with its own ``atomic(savepoint=False)``
    wrapper) reaches the database and the database rejects it. Patching
    ``bulk_create`` with a Python exception would bypass that wrapper
    and would NOT reproduce Django's broken-transaction marking, so the
    failure is forced by a temporary BEFORE INSERT trigger instead.
    """
    trigger = "j1_fail_notification_insert"
    with connection.cursor() as cursor:
        if connection.vendor == "sqlite":
            cursor.execute(
                f"CREATE TRIGGER {trigger} BEFORE INSERT ON notifications_notification "
                "BEGIN SELECT RAISE(ABORT, 'simulated notification insert failure'); END"
            )
        elif connection.vendor == "mysql":
            cursor.execute(
                f"CREATE TRIGGER {trigger} BEFORE INSERT ON notifications_notification "
                "FOR EACH ROW SIGNAL SQLSTATE '45000' "
                "SET MESSAGE_TEXT = 'simulated notification insert failure'"
            )
        else:  # pragma: no cover - test suite runs on SQLite/MySQL
            raise RuntimeError(f"unsupported vendor for this helper: {connection.vendor}")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute(f"DROP TRIGGER IF EXISTS {trigger}")


def _public_booking_payload(room_type, today, **overrides):
    payload = {
        "room_type": room_type.slug,
        "check_in": (today + timedelta(days=3)).isoformat(),
        "check_out": (today + timedelta(days=5)).isoformat(),
        "rooms": 1,
        "adults": 1,
        "children": 0,
        "guest": {
            "first_name": "Ada",
            "last_name": "Obi",
            "email": "ada.repair@example.test",
            "phone": "+234 (0) 803 123 4567",
        },
    }
    payload.update(overrides)
    return payload


class BookingSurvivesNotificationFailureTests(BaseAPITestCase):
    """The exact production sequence: create -> access -> pay -> verify."""

    def setUp(self):
        super().setUp()
        hotel_settings(tax_rate="0.00", service_fee="0.00", pending_booking_minutes=30)
        self.room_type = make_room_type("Standard Room", price="25000.00", max_guests=2)
        make_room(self.room_type, "101")
        make_room(self.room_type, "102")
        # A staff recipient must exist or notify_staff is a no-op and the
        # production failure cannot be reproduced.
        self.staff = make_staff("repair-desk@jone.dev", role=User.Role.RECEPTIONIST)
        self.today = hotel_today()

    def test_booking_survives_notification_db_failure_and_can_be_paid(self):
        with fail_notification_insert_at_db_level():
            response = self.client.post(
                "/api/bookings/",
                _public_booking_payload(self.room_type, self.today),
                format="json",
                HTTP_IDEMPOTENCY_KEY="repair-20261018-booking-1",
            )
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()["data"]
        reference = data["booking_reference"]
        token = data["guest_access_token"]
        self.assertTrue(token)
        self.assertEqual(data["total_amount"], "50000.00")  # 25,000 x 2 nights

        # The booking must REALLY be in the database after the request —
        # this is the assertion the production incident violated.
        booking = Booking.objects.get(booking_reference=reference)
        self.assertEqual(booking.status, Booking.Status.PENDING)

        # Guest access with the returned credential works.
        detail = self.client.get(
            f"/api/bookings/{reference}/", HTTP_X_GUEST_ACCESS_TOKEN=token
        )
        self.assertEqual(detail.status_code, 200, detail.content)

        # A wrong credential must never be accepted (and must not leak).
        wrong = self.client.get(
            f"/api/bookings/{reference}/",
            HTTP_X_GUEST_ACCESS_TOKEN="not-the-guests-token",
        )
        self.assertEqual(wrong.status_code, 404)

        # The browser that created the booking can recover via its
        # idempotency key (the exchange that returned 404 in production).
        exchange = self.client.post(
            f"/api/bookings/{reference}/access/",
            {"idempotency_key": "repair-20261018-booking-1"},
            format="json",
        )
        self.assertEqual(exchange.status_code, 200, exchange.content)
        recovered_token = exchange.json()["data"]["guest_access_token"]

        # Payment initialization finds the booking (mocked Paystack: the
        # amount/reference association is what is under test).
        with override_settings(PAYSTACK_SECRET_KEY="sk_test_repair"):
            with patch(
                "apps.payments.services.paystack.initialize_transaction"
            ) as gateway:
                gateway.side_effect = lambda **kw: {
                    "authorization_url": "https://checkout.paystack.test/repair",
                    "access_code": "repair",
                    "reference": kw["reference"],
                }
                init = self.client.post(
                    "/api/payments/initialize/",
                    {"booking_reference": reference},
                    format="json",
                    HTTP_X_GUEST_ACCESS_TOKEN=recovered_token,
                )
        self.assertEqual(init.status_code, 201, init.content)
        payment = Payment.objects.get(booking=booking)
        self.assertEqual(payment.amount, Decimal("50000.00"))
        self.assertEqual(payment.status, Payment.Status.PENDING)
        # Initialization must NOT mark the booking paid.
        booking.refresh_from_db()
        self.assertEqual(booking.payment_status, Booking.PaymentStatus.UNPAID)

        # Verification against a mocked successful gateway response marks
        # the payment and booking consistently; a repeat is idempotent.
        paystack_payload = {
            "status": True,
            "message": "Verification successful",
            "data": {
                "status": "success",
                "reference": payment.reference,
                "amount": 5_000_000,  # kobo
                "currency": "NGN",
                "channel": "card",
                "id": 424242,
                "paid_at": "2026-10-18T10:00:00Z",
                "gateway_response": "Successful",
            },
        }
        with patch(
            "apps.payments.services.paystack.verify_transaction",
            return_value=paystack_payload,
        ):
            verify = self.client.get(
                f"/api/payments/verify/{payment.reference}/",
                HTTP_X_GUEST_ACCESS_TOKEN=recovered_token,
            )
            self.assertEqual(verify.status_code, 200, verify.content)
            verify_again = self.client.get(
                f"/api/payments/verify/{payment.reference}/",
                HTTP_X_GUEST_ACCESS_TOKEN=recovered_token,
            )
            self.assertEqual(verify_again.status_code, 200, verify_again.content)
        booking.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.SUCCESS)
        self.assertEqual(booking.payment_status, Booking.PaymentStatus.PAID)
        self.assertEqual(booking.amount_paid, Decimal("50000.00"))
        self.assertEqual(Payment.objects.filter(booking=booking).count(), 1)

        # And the booking is STILL retrievable afterwards (refresh path).
        again = self.client.get(
            f"/api/bookings/{reference}/", HTTP_X_GUEST_ACCESS_TOKEN=recovered_token
        )
        self.assertEqual(again.status_code, 200, again.content)

    def test_failed_verification_never_marks_booking_paid(self):
        response = self.client.post(
            "/api/bookings/",
            _public_booking_payload(self.room_type, self.today),
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()["data"]
        reference, token = data["booking_reference"], data["guest_access_token"]
        payment = Payment.objects.create(
            booking=Booking.objects.get(booking_reference=reference),
            reference="J1P-REPAIR-FAILED-VERIFY",
            amount=Decimal("50000.00"),
            currency="NGN",
            provider=Payment.Provider.PAYSTACK,
            status=Payment.Status.PENDING,
        )
        with patch(
            "apps.payments.services.paystack.verify_transaction",
            return_value={
                "status": True,
                "message": "Verification successful",
                "data": {
                    "status": "failed",
                    "reference": payment.reference,
                    "amount": 5_000_000,
                    "currency": "NGN",
                    "gateway_response": "Declined",
                },
            },
        ):
            verify = self.client.get(
                f"/api/payments/verify/{payment.reference}/",
                HTTP_X_GUEST_ACCESS_TOKEN=token,
            )
        self.assertEqual(verify.status_code, 200, verify.content)
        booking = Booking.objects.get(booking_reference=reference)
        self.assertNotEqual(booking.payment_status, Booking.PaymentStatus.PAID)
        self.assertEqual(booking.amount_paid, Decimal("0.00"))

    def test_payment_init_for_unknown_reference_is_404_not_500(self):
        response = self.client.post(
            "/api/payments/initialize/",
            {"booking_reference": "J1-20261018-DOESNOTEXIST"},
            format="json",
            HTTP_X_GUEST_ACCESS_TOKEN="irrelevant",
        )
        self.assertEqual(response.status_code, 404)


class BookingSurvivesEmailRenderFailureTests(BaseAPITestCase):
    """A pending-booking email render failure must not erase the booking."""

    def setUp(self):
        super().setUp()
        hotel_settings(tax_rate="0.00", service_fee="0.00", pending_booking_minutes=30)
        self.room_type = make_room_type("Email Wing", price="25000.00", max_guests=2)
        make_room(self.room_type, "201")
        self.today = hotel_today()

    def test_render_failure_still_returns_persisted_booking(self):
        with patch(
            "apps.core.email_design.render_notice_email",
            side_effect=RuntimeError("template boom"),
        ):
            response = self.client.post(
                "/api/bookings/",
                _public_booking_payload(self.room_type, self.today),
                format="json",
            )
        self.assertEqual(response.status_code, 201, response.content)
        reference = response.json()["data"]["booking_reference"]
        self.assertTrue(Booking.objects.filter(booking_reference=reference).exists())


class NotifyUsersSavepointTests(TransactionTestCase):
    """Direct proof: a notification DB error cannot poison the caller's txn.

    Uses TransactionTestCase so the outer ``atomic()`` below is a REAL
    outermost transaction (as in production), not a test savepoint.
    """

    def setUp(self):
        cache.clear()
        super().setUp()

    def test_marker_row_survives_notification_failure_inside_same_transaction(self):
        staff = User.objects.create_user(
            email="marker-staff@repair.dev", password="Passw0rd!234",
            first_name="Marker", last_name="Staff", role=User.Role.RECEPTIONIST,
        )
        with transaction.atomic():
            Guest.objects.create(
                first_name="Marker", last_name="Guest",
                email="marker@repair.dev", phone="08031234567",
            )
            # Real database failure inside notify_users; must be contained
            # by a savepoint so the caller's transaction stays committable.
            with fail_notification_insert_at_db_level():
                notify_users(
                    [staff], type="BOOKING_CREATED",
                    title="New booking", message="Ada booked 2026-10-21 \u2192 2026-10-23",
                )
            # The transaction must still be usable immediately afterwards.
            self.assertTrue(Guest.objects.filter(email="marker@repair.dev").exists())
        # ...and the marker must actually have committed.
        self.assertTrue(Guest.objects.filter(email="marker@repair.dev").exists())

    def test_unicode_notification_round_trips(self):
        staff = User.objects.create_user(
            email="unicode@repair.dev", password="Passw0rd!234",
            first_name="Uni", last_name="Code", role=User.Role.RECEPTIONIST,
        )
        message = "Ada Obi booked 1 × Standard Room, 2026-10-21 → 2026-10-23 · ₦50,000 ✓"
        notify_users(
            [staff], type="BOOKING_CREATED", title="New booking → test", message=message
        )
        saved = Notification.objects.get(recipient=staff)
        self.assertEqual(saved.message, message)
        self.assertIn("→", saved.title)


class PosTransitionTransactionTests(TransactionTestCase):
    """POS: transition_order must own its transaction (production HTTP 500).

    ``select_for_update`` outside a transaction is invisible to the
    TestCase suite (which always runs inside a transaction) — hence
    TransactionTestCase here, calling the service in autocommit exactly
    as the production view does.
    """

    def setUp(self):
        cache.clear()
        super().setUp()
        from apps.pos.models import MenuCategory, MenuItem

        self.actor = User.objects.create_user(
            email="pos-repair@jone.dev", password="Passw0rd!234",
            first_name="Pos", last_name="Repair", role=User.Role.CASHIER,
        )
        category = MenuCategory.objects.create(name="Repair Kitchen", slug="repair-kitchen")
        self.item = MenuItem.objects.create(
            category=category, name="Repair Jollof", sku="REPAIR-JOLLOF",
            base_price=Decimal("2500.00"), tax_rate_percent=Decimal("0.00"),
        )

    def test_transition_order_works_in_autocommit_and_is_idempotent(self):
        from django.core.exceptions import ValidationError

        from apps.pos.models import PosOrder
        from apps.pos.services.order_service import create_order, submit_order, transition_order

        order, created = create_order(
            mode=PosOrder.Mode.TAKEAWAY,
            actor=self.actor,
            lines=[{"menu_item_id": self.item.pk, "quantity": 1, "modifier_ids": [], "notes": ""}],
            guest_name="Repair Guest",
            idempotency_key="pos-repair-20261018-1",
        )
        self.assertTrue(created)
        submit_order(order=order, actor=self.actor)

        # Called in AUTOCOMMIT (no surrounding transaction) — this is the
        # production call that raised TransactionManagementError on MySQL.
        # SQLite cannot reproduce that error (it has no select_for_update),
        # so probe the contract directly: order events are written at the
        # END of the transition and must observe an open transaction.
        from apps.pos.models import PosOrderEvent

        observed = {}
        manager_class = type(PosOrderEvent.objects)
        original_create = manager_class.create

        def probing_create(self, **kwargs):
            if self.model is PosOrderEvent:
                observed["in_atomic_block"] = connection.in_atomic_block
            return original_create(self, **kwargs)

        with patch.object(manager_class, "create", probing_create):
            preparing = transition_order(
                order=order, target_status=PosOrder.Status.PREPARING, actor=self.actor
            )
        self.assertEqual(preparing.status, PosOrder.Status.PREPARING)
        self.assertTrue(
            observed.get("in_atomic_block"),
            "transition_order must execute inside transaction.atomic() "
            "(select_for_update fails outside a transaction on MySQL)",
        )

        # Duplicate request for the same status is a safe no-op.
        again = transition_order(
            order=order, target_status=PosOrder.Status.PREPARING, actor=self.actor
        )
        self.assertEqual(again.status, PosOrder.Status.PREPARING)

        # Invalid transition is refused and changes nothing.
        with self.assertRaises(ValidationError):
            transition_order(
                order=order, target_status=PosOrder.Status.DRAFT, actor=self.actor
            )
        order.refresh_from_db()
        self.assertEqual(order.status, PosOrder.Status.PREPARING)

        ready = transition_order(
            order=order, target_status=PosOrder.Status.READY, actor=self.actor
        )
        self.assertEqual(ready.status, PosOrder.Status.READY)


class WalkInRepairTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        hotel_settings(tax_rate="0.00", service_fee="0.00", pending_booking_minutes=30)
        self.room_type = make_room_type("Repair Standard", price="15000.00", max_guests=2)
        self.room = make_room(self.room_type, "301")
        self.other_type = make_room_type("Repair Suite", price="35000.00", max_guests=2)
        self.other_room = make_room(self.other_type, "401")
        self.staff = make_staff("walkin-repair@jone.dev")
        self.auth(self.staff)
        self.today = hotel_today()

    def _payload(self, **overrides):
        base = {
            "room_type": self.room_type.slug,
            "check_in": (self.today + timedelta(days=3)).isoformat(),
            "check_out": (self.today + timedelta(days=5)).isoformat(),
            "rooms": 1, "adults": 1, "children": 0,
            "source": "WALK_IN", "status": "CONFIRMED",
            "guest": {
                "first_name": "Ngozi", "last_name": "Eze",
                "email": "ngozi.repair@example.test",
                "phone": "+234 (0) 803 123 4567",
            },
        }
        base.update(overrides)
        return base

    def test_formatted_nigerian_phone_is_normalised_before_storage(self):
        response = self.client.post("/api/admin/bookings/", self._payload(), format="json")
        self.assertEqual(response.status_code, 201, response.content)
        booking = Booking.objects.get(
            booking_reference=response.json()["data"]["booking_reference"]
        )
        self.assertLessEqual(len(booking.guest.phone), 20)
        self.assertNotIn(" ", booking.guest.phone)

    def test_room_from_wrong_type_is_a_field_error_not_500(self):
        response = self.client.post(
            "/api/admin/bookings/",
            self._payload(room_id=self.other_room.pk),
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("room_id", response.json().get("errors", {}))
        self.assertEqual(Booking.objects.count(), 0)

    def test_walk_in_survives_notification_db_failure(self):
        with fail_notification_insert_at_db_level():
            response = self.client.post(
                "/api/admin/bookings/", self._payload(), format="json"
            )
        self.assertEqual(response.status_code, 201, response.content)
        reference = response.json()["data"]["booking_reference"]
        self.assertTrue(Booking.objects.filter(booking_reference=reference).exists())


class Utf8mb4MigrationTransactionTests(SimpleTestCase):
    """Railway/MariaDB deployment regression, 2026-10-18.

    The utf8mb4 migrations execute ``ALTER TABLE`` via the schema editor.
    MariaDB/MySQL DDL implicitly commits and cannot be rolled back, so
    Django correctly raises ``TransactionManagementError`` if those
    migrations run under Django's default ``atomic=True``. The failed
    Railway deploy stopped at ``audit.0003`` before any DDL ran. Both
    conversion migrations must therefore remain explicitly non-atomic;
    conversion itself is idempotent if a later table needs a retry.
    """

    def test_utf8mb4_conversion_migrations_are_non_atomic(self):
        for module_name in (
            "apps.audit.migrations.0003_utf8mb4_auditlog_table",
            "apps.notifications.migrations.0016_utf8mb4_notification_tables",
        ):
            with self.subTest(module=module_name):
                migration = importlib.import_module(module_name).Migration
                self.assertIs(migration.atomic, False)
