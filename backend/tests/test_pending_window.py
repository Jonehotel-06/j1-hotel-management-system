# tests/test_pending_window.py
"""End-to-end tests for the ADMIN-CONFIGURABLE PENDING WINDOW.

The requirement: "Pending window (min)" on the settings page must genuinely
control how long a newly created unpaid PENDING booking holds inventory:

* PATCH /api/admin/settings/ saves + reloads the value, and the settings cache
  is invalidated immediately (no stale five-minute window);
* a new pending booking receives expires_at = creation time + configured
  minutes (server clock, never the browser's);
* changing the setting does NOT retroactively move existing holds;
* successful payment before the deadline confirms the booking and clears
  expires_at; the expiration sweep can then never touch it (idempotent);
* an unpaid booking past its deadline flips to EXPIRED, releases the room, and
  can no longer accept/confirm a payment — including the race where Paystack
  verification lands after the hold has lapsed.
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.bookings.models import Booking
from apps.bookings.services import booking_service
from apps.core.utils import hotel_today
from apps.hotel.models import HotelSettings
from apps.payments.models import Payment

from .base import BaseAPITestCase
from .factories import make_room, make_room_type, make_staff, make_user


class PendingWindowSettingTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.admin = make_staff("admin@j1.dev", role=User.Role.ADMIN)
        self.room_type = make_room_type("Classic", price="20000.00")
        make_room(self.room_type, "301")
        self.today = hotel_today()

    def _patch_setting(self, minutes):
        self.auth(self.admin)
        response = self.client.patch(
            "/api/admin/settings/", {"pending_booking_minutes": minutes}, format="json"
        )
        return response

    def _create_pending_booking(self, day_offset=5):
        guest = make_user(f"guest{Booking.objects.count()}@pw.dev")
        self.auth(guest)
        response = self.client.post("/api/bookings/", {
            "room_type": self.room_type.slug,
            "check_in": (self.today + timedelta(days=day_offset)).isoformat(),
            "check_out": (self.today + timedelta(days=day_offset + 2)).isoformat(),
            "rooms": 1, "adults": 2, "children": 0,
        }, format="json")
        self.assertEqual(response.status_code, 201, response.json())
        return Booking.objects.get(
            booking_reference=response.json()["data"]["booking_reference"]
        )

    def _assert_window(self, booking, minutes):
        self.assertEqual(booking.status, Booking.Status.PENDING)
        self.assertIsNotNone(booking.expires_at)
        expected = timezone.now() + timedelta(minutes=minutes)
        delta = abs((booking.expires_at - expected).total_seconds())
        self.assertLess(delta, 60, f"expires_at ≠ now + {minutes}min (off by {delta:.0f}s)")

    # --- Save / reload / immediate effect ------------------------------------
    def test_patch_saves_reloads_and_applies_to_new_bookings_immediately(self):
        for offset, minutes in ((5, 5), (10, 15)):
            with self.subTest(minutes=minutes):
                response = self._patch_setting(minutes)
                self.assertEqual(response.status_code, 200, response.json())
                self.assertEqual(response.json()["data"]["pending_booking_minutes"], minutes)

                # Reload via GET — the saved value comes back.
                reload = self.client.get("/api/admin/settings/")
                self.assertEqual(reload.json()["data"]["pending_booking_minutes"], minutes)

                # The cached settings accessor must already see it (save()
                # deletes the cache key) — no waiting out a cache TTL.
                self.assertEqual(
                    HotelSettings.get_settings().pending_booking_minutes, minutes
                )

                # And a brand-new pending booking uses it (each subtest books
                # different dates so the single room never blocks itself).
                booking = self._create_pending_booking(day_offset=offset)
                self._assert_window(booking, minutes)

    def test_zero_minutes_is_rejected(self):
        response = self._patch_setting(0)
        self.assertEqual(response.status_code, 400)

    def test_non_admin_cannot_change_setting(self):
        receptionist = make_staff("reception@j1.dev", role=User.Role.RECEPTIONIST)
        self.auth(receptionist)
        response = self.client.patch(
            "/api/admin/settings/", {"pending_booking_minutes": 5}, format="json"
        )
        self.assertIn(response.status_code, (403, 404))
        self.assertNotEqual(
            HotelSettings.get_settings().pending_booking_minutes, 5
        )

    # --- Existing holds keep their deadline -----------------------------------
    def test_changing_setting_does_not_move_existing_holds(self):
        self._patch_setting(30)
        booking = self._create_pending_booking()
        original_expiry = booking.expires_at
        self._patch_setting(5)
        booking.refresh_from_db()
        self.assertEqual(booking.expires_at, original_expiry)

    # --- Payment clears the hold; expiry then never touches it ----------------
    def test_paid_booking_is_confirmed_and_immune_to_expiration(self):
        self._patch_setting(5)
        booking = self._create_pending_booking()
        booking_service.register_successful_payment(booking, booking.required_payment)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CONFIRMED)
        self.assertIsNone(booking.expires_at)

        # Even a sweep at a much later "now" cannot expire a confirmed booking.
        count = booking_service.expire_stale_pending_bookings(
            now=timezone.now() + timedelta(days=1)
        )
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CONFIRMED)
        self.assertEqual(count, 0)

    # --- Unpaid booking expires and releases the room --------------------------
    def test_unpaid_booking_expires_and_room_becomes_available(self):
        self._patch_setting(5)
        booking = self._create_pending_booking()

        # Simulate the deadline passing (controlled time, no waiting).
        booking.expires_at = timezone.now() - timedelta(seconds=1)
        booking.save(update_fields=["expires_at"])

        count = booking_service.expire_stale_pending_bookings()
        self.assertEqual(count, 1)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.EXPIRED)

        # Idempotent: a second sweep (and a direct second call) is a no-op.
        self.assertEqual(booking_service.expire_stale_pending_bookings(), 0)
        booking_service.expire_pending_booking(booking)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.EXPIRED)

        # The room is available again for the same dates.
        self.unauth()
        response = self.client.get("/api/rooms/availability/", {
            "check_in": (self.today + timedelta(days=5)).isoformat(),
            "check_out": (self.today + timedelta(days=7)).isoformat(),
            "guests": 2, "rooms": 1,
        })
        self.assertEqual(response.status_code, 200)
        results = response.json()["data"]["results"]
        classic = next(r for r in results if r["room_type"]["slug"] == self.room_type.slug)
        self.assertGreaterEqual(classic["available_rooms"], 1)

    def test_expired_booking_cannot_initialize_payment(self):
        self._patch_setting(5)
        booking = self._create_pending_booking()
        booking.expires_at = timezone.now() - timedelta(minutes=1)
        booking.save(update_fields=["expires_at"])

        with override_settings(PAYSTACK_SECRET_KEY="sk_test_mock"):
            response = self.client.post(
                "/api/payments/initialize/",
                {"booking_reference": booking.booking_reference},
            )
        self.assertGreaterEqual(response.status_code, 400)
        self.assertEqual(Payment.objects.filter(booking=booking).count(), 0)
        # The persisted status may still be PENDING here (the lazy flip rolls
        # back with the refused initialization transaction); what matters is
        # that the hold is dead: no payment was opened and the sweep finishes
        # the job.
        booking.refresh_from_db()
        self.assertNotEqual(booking.status, Booking.Status.CONFIRMED)
        booking_service.expire_stale_pending_bookings()
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.EXPIRED)

    # --- Race: verification vs. expiration -------------------------------------
    def _paystack_payload(self, reference, paid_at, amount_kobo):
        return {
            "status": True,
            "message": "Verification successful",
            "data": {
                "status": "success", "reference": reference,
                "amount": amount_kobo, "currency": "NGN", "channel": "card",
                "gateway_response": "Successful", "id": 424242,
                "paid_at": paid_at,
            },
        }

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_mock")
    @patch("apps.payments.services.paystack.verify_transaction")
    def test_charge_within_window_confirms_even_if_sweep_is_late(self, mock_verify):
        """Guest paid BEFORE the deadline; verification arriving after it must
        still confirm (the hold blocked inventory the whole time)."""
        self._patch_setting(5)
        booking = self._create_pending_booking()
        payment = Payment.objects.create(
            booking=booking, reference="J1P-RACE-0001",
            provider=Payment.Provider.PAYSTACK, amount=booking.required_payment,
            currency="NGN", status=Payment.Status.PENDING,
        )
        paid_at = (booking.expires_at - timedelta(minutes=1)).isoformat()
        # Deadline passes before verification lands.
        booking.expires_at = timezone.now() - timedelta(seconds=1)
        booking.save(update_fields=["expires_at"])
        paid_at = (timezone.now() - timedelta(minutes=1)).isoformat()

        mock_verify.return_value = self._paystack_payload(
            payment.reference, paid_at,
            int(booking.required_payment * 100),
        )
        response = self.client.get(f"/api/payments/verify/{payment.reference}/")
        self.assertEqual(response.status_code, 200, response.json())
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CONFIRMED)
        self.assertIsNone(booking.expires_at)

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_mock")
    @patch("apps.payments.services.paystack.verify_transaction")
    def test_charge_after_window_cannot_resurrect_expired_hold(self, mock_verify):
        """Charged AFTER the deadline: the released hold must never be
        re-confirmed behind the guest's (and the next guest's) back."""
        self._patch_setting(5)
        booking = self._create_pending_booking()
        payment = Payment.objects.create(
            booking=booking, reference="J1P-RACE-0002",
            provider=Payment.Provider.PAYSTACK, amount=booking.required_payment,
            currency="NGN", status=Payment.Status.PENDING,
        )
        booking.expires_at = timezone.now() - timedelta(minutes=10)
        booking.save(update_fields=["expires_at"])
        paid_at = timezone.now().isoformat()  # after the deadline

        mock_verify.return_value = self._paystack_payload(
            payment.reference, paid_at,
            int(booking.required_payment * 100),
        )
        response = self.client.get(f"/api/payments/verify/{payment.reference}/")
        self.assertGreaterEqual(response.status_code, 400)
        booking.refresh_from_db()
        self.assertNotEqual(booking.status, Booking.Status.CONFIRMED)
        self.assertEqual(booking.amount_paid, Decimal("0.00"))
        payment.refresh_from_db()
        self.assertNotEqual(payment.status, Payment.Status.SUCCESS)

    def test_expire_pending_booking_backs_off_if_confirmed_meanwhile(self):
        """The sweep re-checks under the row lock: a stale in-memory PENDING
        snapshot must never overwrite a booking confirmed in between."""
        self._patch_setting(5)
        booking = self._create_pending_booking()
        booking.expires_at = timezone.now() - timedelta(seconds=1)
        booking.save(update_fields=["expires_at"])

        stale_snapshot = Booking.objects.get(pk=booking.pk)  # sweep's view
        # Payment wins the race first.
        booking_service.register_successful_payment(booking, booking.required_payment)
        # Sweep then runs with its stale snapshot.
        booking_service.expire_pending_booking(stale_snapshot)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CONFIRMED)

    def test_admin_bookings_list_lazily_expires_stale_holds(self):
        """When Celery beat is unavailable, the staff bookings screen still
        shows the truth: stale holds are swept (throttled) on list load."""
        self._patch_setting(5)
        booking = self._create_pending_booking()
        booking.expires_at = timezone.now() - timedelta(minutes=1)
        booking.save(update_fields=["expires_at"])

        self.auth(make_staff("desk@j1.dev", role=User.Role.RECEPTIONIST))
        response = self.client.get("/api/admin/bookings/")
        self.assertEqual(response.status_code, 200)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.EXPIRED)
