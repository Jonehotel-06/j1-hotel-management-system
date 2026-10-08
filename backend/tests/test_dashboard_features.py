# tests/test_dashboard_features.py
"""DASHBOARD + BOOKING-FLOW FEATURE tests.

Covers the operational search, checkout search, payment validation, role
gates on room/room-type writes, receptionist report lockout, notification
ownership, guest-profile completeness, exact-room booking, the unavailable-room
fallback, public enquiry submission and payment/receipt integrity.
"""
import json
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from apps.accounts.models import User
from apps.bookings.models import Booking, BookingRoom
from apps.enquiries.models import Enquiry
from apps.core.utils import hotel_today
from apps.notifications.models import Notification
from apps.payments.models import Payment
from apps.rooms.models import Room

from .base import BaseAPITestCase
from .factories import (
    make_booking,
    make_guest,
    make_room,
    make_room_type,
    make_staff,
    make_user,
)


class OperationalSearchTests(BaseAPITestCase):
    """§3 — find a booking from any identifier a receptionist would type."""

    def setUp(self):
        super().setUp()
        self.room_type = make_room_type("Deluxe", price="25000.00", max_guests=2)
        self.room_203 = make_room(self.room_type, "203")
        self.room_204 = make_room(self.room_type, "204")
        self.other_type = make_room_type("Standard", price="15000.00", max_guests=2)
        self.room_101 = make_room(self.other_type, "101")
        self.staff = make_staff("desk@staff.dev", role=User.Role.RECEPTIONIST)
        self.auth(self.staff)
        self.today = hotel_today()
        self.guest = make_guest(
            "john.doe@example.com",
            first_name="John",
            last_name="Doe",
            phone="08012345678",
        )
        self.booking = make_booking(
            self.guest,
            self.room_type,
            rooms=[self.room_203],
            check_in=self.today + timedelta(days=3),
            check_out=self.today + timedelta(days=5),
            total="50000.00",
        )

    def search(self, term, **extra):
        params = {"search": term, "page_size": 20}
        params.update(extra)
        response = self.client.get("/api/admin/bookings/", params)
        self.assertEqual(response.status_code, 200, response.json())
        return response.json()["data"]

    def ids(self, rows):
        return {row["id"] for row in rows}

    def test_search_by_room_number(self):
        rows = self.search("203")
        self.assertIn(self.booking.id, self.ids(rows))
        self.assertEqual(rows[0]["room_numbers"], ["203"])

    def test_search_by_booking_reference(self):
        rows = self.search(self.booking.booking_reference)
        self.assertEqual(self.ids(rows), {self.booking.id})

    def test_search_by_partial_reference_is_case_insensitive(self):
        prefix = self.booking.booking_reference[:9]
        rows = self.search(prefix.lower())
        self.assertIn(self.booking.id, self.ids(rows))

    def test_search_by_first_name(self):
        self.assertIn(self.booking.id, self.ids(self.search("john")))

    def test_search_by_last_name(self):
        self.assertIn(self.booking.id, self.ids(self.search("doe")))

    def test_search_by_full_name(self):
        self.assertIn(self.booking.id, self.ids(self.search("john doe")))

    def test_search_by_email(self):
        self.assertIn(self.booking.id, self.ids(self.search("john.doe@example.com")))

    def test_search_by_phone(self):
        self.assertIn(self.booking.id, self.ids(self.search("08012345678")))

    def test_search_by_room_type(self):
        self.assertIn(self.booking.id, self.ids(self.search("deluxe")))

    def test_search_never_matches_a_different_room_number(self):
        rows = self.search("204")
        self.assertEqual(rows, [])

    def test_search_by_payment_reference(self):
        Payment.objects.create(
            booking=self.booking,
            user=self.staff,
            reference="J1P-20260101-ABCDE",
            provider=Payment.Provider.CASH,
            amount=Decimal("10000.00"),
            status=Payment.Status.SUCCESS,
        )
        self.assertIn(self.booking.id, self.ids(self.search("J1P-20260101-ABCDE")))

    def test_search_returns_distinct_rows(self):
        Payment.objects.create(
            booking=self.booking,
            user=self.staff,
            reference="J1P-SEARCH-000001",
            provider=Payment.Provider.POS,
            amount=Decimal("10000.00"),
            status=Payment.Status.SUCCESS,
        )
        Payment.objects.create(
            booking=self.booking,
            user=self.staff,
            reference="J1P-SEARCH-000002",
            provider=Payment.Provider.POS,
            amount=Decimal("10000.00"),
            status=Payment.Status.SUCCESS,
        )
        rows = self.search("J1P-SEARCH")
        self.assertEqual(len(rows), 1, rows)


class CheckoutSearchTests(BaseAPITestCase):
    """§4/§5 — the checkout screen searches server-side, never client-side."""

    def setUp(self):
        super().setUp()
        self.staff = make_staff("desk2@staff.dev", role=User.Role.RECEPTIONIST)
        self.auth(self.staff)
        self.today = hotel_today()
        self.room_type = make_room_type("Suite", price="40000.00", max_guests=3)
        self.room_301 = make_room(self.room_type, "301")
        self.room_302 = make_room(self.room_type, "302")
        self.checked_in = make_booking(
            make_guest("inhouse@example.com", first_name="Amina", last_name="Yusuf",
                       phone="08020000001"),
            self.room_type,
            rooms=[self.room_301],
            check_in=self.today - timedelta(days=1),
            check_out=self.today + timedelta(days=1),
            status=Booking.Status.CHECKED_IN,
            amount_paid="40000.00",
            total="80000.00",
        )
        self.upcoming = make_booking(
            make_guest("future@example.com", first_name="Chidi", last_name="Okafor",
                       phone="08020000002"),
            self.room_type,
            rooms=[self.room_302],
            check_in=self.today + timedelta(days=9),
            check_out=self.today + timedelta(days=11),
            status=Booking.Status.CONFIRMED,
            total="80000.00",
        )

    def checkout_search(self, term):
        response = self.client.get(
            "/api/admin/bookings/",
            {"status": "CHECKED_IN,CONFIRMED", "search": term, "page_size": 20},
        )
        self.assertEqual(response.status_code, 200, response.json())
        return {row["id"] for row in response.json()["data"]}

    def test_find_in_house_guest_by_room_number(self):
        self.assertEqual(self.checkout_search("301"), {self.checked_in.id})

    def test_find_in_house_guest_by_reference(self):
        self.assertEqual(
            self.checkout_search(self.checked_in.booking_reference), {self.checked_in.id}
        )

    def test_find_in_house_guest_by_phone(self):
        self.assertEqual(self.checkout_search("08020000001"), {self.checked_in.id})

    def test_find_in_house_guest_by_email(self):
        self.assertEqual(self.checkout_search("inhouse@example.com"), {self.checked_in.id})

    def test_find_in_house_guest_by_name(self):
        self.assertEqual(self.checkout_search("Amina"), {self.checked_in.id})

    def test_checkout_rows_carry_the_financial_fields(self):
        rows = self.client.get(
            "/api/admin/bookings/",
            {"status": "CHECKED_IN", "search": "301", "page_size": 20},
        ).json()["data"]
        row = rows[0]
        for field in ("total_amount", "amount_paid", "amount_due", "room_numbers",
                      "guest_name", "booking_reference", "check_out", "status"):
            self.assertIn(field, row)
        self.assertEqual(Decimal(row["amount_due"]), Decimal("40000.00"))


class OfflinePaymentValidationTests(BaseAPITestCase):
    """§2 — the server decides what may be recorded, never the browser."""

    def setUp(self):
        super().setUp()
        self.staff = make_staff("cashier@staff.dev", role=User.Role.RECEPTIONIST)
        self.auth(self.staff)
        self.today = hotel_today()
        self.room_type = make_room_type("Classic", price="20000.00")
        self.room = make_room(self.room_type, "110")
        self.booking = make_booking(
            make_guest("payer@example.com"),
            self.room_type,
            rooms=[self.room],
            check_in=self.today + timedelta(days=2),
            check_out=self.today + timedelta(days=4),
            status=Booking.Status.CONFIRMED,
            total="40000.00",
        )

    def record(self, amount, provider="CASH", **extra):
        payload = {"booking_reference": self.booking.booking_reference,
                   "amount": amount, "provider": provider}
        payload.update(extra)
        return self.client.post("/api/admin/payments/record/", payload, format="json")

    def test_partial_payment_recorded_and_booking_updated(self):
        response = self.record("15000.00")
        self.assertEqual(response.status_code, 201, response.json())
        body = response.json()["data"]
        self.assertEqual(Decimal(body["booking"]["amount_paid"]), Decimal("15000.00"))
        self.assertEqual(Decimal(body["booking"]["amount_due"]), Decimal("25000.00"))
        self.assertEqual(body["booking"]["payment_status"], Booking.PaymentStatus.PARTIALLY_PAID)
        self.assertEqual(body["receipt_reference"], body["reference"])
        self.assertTrue(body["has_receipt"])
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.amount_paid, Decimal("15000.00"))

    def test_full_payment_marks_booking_paid(self):
        response = self.record("40000.00")
        self.assertEqual(response.status_code, 201, response.json())
        self.assertEqual(
            response.json()["data"]["booking"]["payment_status"], Booking.PaymentStatus.PAID
        )

    def test_over_payment_is_rejected(self):
        response = self.record("40000.01")
        self.assertEqual(response.status_code, 400, response.json())
        self.assertEqual(Payment.objects.count(), 0)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.amount_paid, Decimal("0.00"))

    def test_zero_and_negative_amounts_are_rejected(self):
        for amount in ("0.00", "-500.00"):
            response = self.record(amount)
            self.assertEqual(response.status_code, 400, amount)
        self.assertEqual(Payment.objects.count(), 0)

    def test_unknown_booking_is_404(self):
        response = self.client.post(
            "/api/admin/payments/record/",
            {"booking_reference": "J1-NOPE", "amount": "100.00", "provider": "CASH"},
            format="json",
        )
        self.assertEqual(response.status_code, 404)

    def test_second_payment_settles_the_balance(self):
        self.record("15000.00")
        self.record("25000.00")
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.amount_paid, Decimal("40000.00"))
        self.assertEqual(self.booking.amount_due, Decimal("0.00"))
        self.assertEqual(self.booking.payment_status, Booking.PaymentStatus.PAID)

    def test_payment_is_rejected_on_a_checked_out_booking(self):
        self.booking.status = Booking.Status.CHECKED_OUT
        self.booking.save(update_fields=["status"])
        response = self.record("1000.00")
        self.assertEqual(response.status_code, 409, response.json())


class RoomAndRoomTypePermissionTests(BaseAPITestCase):
    """§7/§9 — writes are gated server-side, reads stay open to all staff."""

    def setUp(self):
        super().setUp()
        self.room_type = make_room_type("Garden", price="18000.00")
        self.room = make_room(self.room_type, "401")
        self.admin = make_staff("boss@staff.dev", role=User.Role.ADMIN)
        self.manager = make_staff("mgr@staff.dev", role=User.Role.MANAGER)
        self.receptionist = make_staff("rec@staff.dev", role=User.Role.RECEPTIONIST)

    def test_receptionist_can_read_rooms_and_types(self):
        self.auth(self.receptionist)
        self.assertEqual(self.client.get("/api/admin/rooms/").status_code, 200)
        self.assertEqual(self.client.get("/api/admin/room-types/").status_code, 200)

    def test_receptionist_cannot_create_or_edit_a_room_type(self):
        self.auth(self.receptionist)
        self.assertEqual(
            self.client.post("/api/admin/room-types/", {"name": "Sneaky"}, format="json").status_code,
            403,
        )
        self.assertEqual(
            self.client.patch(
                f"/api/admin/room-types/{self.room_type.pk}/",
                {"base_price": "1.00"}, format="json",
            ).status_code,
            403,
        )

    def test_receptionist_cannot_deactivate_a_room_type(self):
        self.auth(self.receptionist)
        response = self.client.delete(f"/api/admin/room-types/{self.room_type.pk}/")
        self.assertEqual(response.status_code, 403)
        self.room_type.refresh_from_db()
        self.assertTrue(self.room_type.is_active)

    def test_receptionist_cannot_edit_or_delete_a_room(self):
        self.auth(self.receptionist)
        self.assertEqual(
            self.client.patch(
                f"/api/admin/rooms/{self.room.pk}/", {"status": "MAINTENANCE"}, format="json"
            ).status_code,
            403,
        )
        self.assertEqual(self.client.delete(f"/api/admin/rooms/{self.room.pk}/").status_code, 403)
        self.room.refresh_from_db()
        self.assertTrue(self.room.is_active)

    def test_manager_can_edit_and_soft_deactivate(self):
        self.auth(self.manager)
        response = self.client.patch(
            f"/api/admin/room-types/{self.room_type.pk}/",
            {"base_price": "19000.00"}, format="json",
        )
        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(self.client.delete(f"/api/admin/rooms/{self.room.pk}/").status_code, 204)
        self.room.refresh_from_db()
        self.assertFalse(self.room.is_active)

    def test_admin_can_everything(self):
        self.auth(self.admin)
        response = self.client.post(
            "/api/admin/room-types/",
            {"name": "Penthouse", "base_price": "90000.00", "max_guests": 4},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.json())
        created = response.json()["data"]["id"]
        self.assertEqual(self.client.delete(f"/api/admin/room-types/{created}/").status_code, 204)

    def test_rooms_can_be_filtered_by_room_type_for_the_view_rooms_action(self):
        other = make_room_type("Cottage", price="12000.00")
        make_room(other, "900")
        self.auth(self.receptionist)
        rows = self.client.get(
            "/api/admin/rooms/", {"room_type": self.room_type.pk, "page_size": 50}
        ).json()["data"]
        self.assertEqual([r["room_number"] for r in rows], ["401"])


class ReceptionistReportLockoutTests(BaseAPITestCase):
    """§8 — reports are invisible AND unreachable for receptionists."""

    def setUp(self):
        super().setUp()
        self.receptionist = make_staff("frontdesk@staff.dev", role=User.Role.RECEPTIONIST)
        self.manager = make_staff("gm@staff.dev", role=User.Role.MANAGER)
        self.admin = make_staff("owner@staff.dev", role=User.Role.ADMIN)
        self.today = hotel_today()
        self.params = {
            "start_date": (self.today - timedelta(days=7)).isoformat(),
            "end_date": self.today.isoformat(),
        }
        self.urls = [
            "/api/admin/reports/revenue/",
            "/api/admin/reports/occupancy/",
            "/api/admin/reports/bookings/",
        ]

    def test_receptionist_gets_403_on_every_report(self):
        self.auth(self.receptionist)
        for url in self.urls:
            response = self.client.get(url, self.params)
            self.assertEqual(response.status_code, 403, url)

    def test_manager_and_admin_can_read_reports(self):
        for user in (self.manager, self.admin):
            self.auth(user)
            for url in self.urls:
                self.assertEqual(self.client.get(url, self.params).status_code, 200, url)

    def test_anonymous_is_rejected(self):
        self.unauth()
        self.assertEqual(
            self.client.get("/api/admin/reports/revenue/", self.params).status_code, 401
        )


class NotificationDetailTests(BaseAPITestCase):
    """§13 — a user can only ever read their own notifications."""

    def setUp(self):
        super().setUp()
        self.user = make_user("notify@guest.dev")
        self.other = make_user("other@guest.dev")
        self.mine = Notification.objects.create(
            recipient=self.user, type=Notification.Type.BOOKING_CREATED,
            title="Booking J1-TEST-00001 created", message="New reservation",
            link="/dashboard/booking-details.html?ref=J1-TEST-00001",
        )
        self.theirs = Notification.objects.create(
            recipient=self.other, type=Notification.Type.PAYMENT_SUCCESS,
            title="Payment received", message="Someone paid",
        )

    def test_owner_can_read_the_detail(self):
        self.auth(self.user)
        response = self.client.get(f"/api/notifications/{self.mine.pk}/")
        self.assertEqual(response.status_code, 200, response.json())
        data = response.json()["data"]
        self.assertEqual(data["title"], "Booking J1-TEST-00001 created")
        self.assertEqual(data["related"]["kind"], "booking")
        self.assertEqual(data["related"]["reference"], "J1-TEST-00001")

    def test_opening_marks_it_read_and_returns_the_fresh_count(self):
        self.auth(self.user)
        self.assertEqual(self.client.get("/api/notifications/unread-count/").json()["data"]["unread_count"], 1)
        data = self.client.get(f"/api/notifications/{self.mine.pk}/").json()["data"]
        self.assertTrue(data["is_read"])
        self.assertEqual(data["unread_count"], 0)
        self.mine.refresh_from_db()
        self.assertTrue(self.mine.is_read)

    def test_other_users_notification_is_404_not_a_leak(self):
        self.auth(self.user)
        response = self.client.get(f"/api/notifications/{self.theirs.pk}/")
        self.assertEqual(response.status_code, 404)

    def test_unknown_id_is_404(self):
        self.auth(self.user)
        self.assertEqual(self.client.get("/api/notifications/999999/").status_code, 404)

    def test_anonymous_is_401(self):
        self.unauth()
        self.assertEqual(self.client.get(f"/api/notifications/{self.mine.pk}/").status_code, 401)


class StaffProfileTests(BaseAPITestCase):
    """§6 — complete staff profile, credential-free, role-gated."""

    def setUp(self):
        super().setUp()
        self.admin = make_staff("root@staff.dev", role=User.Role.ADMIN)
        self.manager = make_staff("mgr2@staff.dev", role=User.Role.MANAGER)
        self.receptionist = make_staff("rec2@staff.dev", role=User.Role.RECEPTIONIST)

    def test_admin_gets_the_full_profile(self):
        self.auth(self.admin)
        data = self.client.get(f"/api/admin/users/staff/{self.receptionist.pk}/").json()["data"]
        for field in ("id", "email", "first_name", "last_name", "full_name", "phone",
                      "role", "role_label", "is_active", "email_verified", "date_joined",
                      "last_login", "stats", "can_manage"):
            self.assertIn(field, data)
        self.assertTrue(data["can_manage"])
        self.assertEqual(data["role"], "RECEPTIONIST")
        for credential in ("password", "password_hash", "refresh"):
            self.assertNotIn(credential, data)

    def test_manager_can_view_any_staff_profile(self):
        self.auth(self.manager)
        response = self.client.get(f"/api/admin/users/staff/{self.receptionist.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["data"]["can_manage"])

    def test_receptionist_can_view_own_profile_only(self):
        self.auth(self.receptionist)
        self.assertEqual(
            self.client.get(f"/api/admin/users/staff/{self.receptionist.pk}/").status_code, 200
        )
        self.assertEqual(
            self.client.get(f"/api/admin/users/staff/{self.admin.pk}/").status_code, 403
        )

    def test_receptionist_cannot_activate_or_deactivate_anyone(self):
        self.auth(self.receptionist)
        self.assertEqual(
            self.client.patch(
                f"/api/admin/users/{self.receptionist.pk}/", {"is_active": False}, format="json"
            ).status_code,
            403,
        )

    def test_admin_can_deactivate_a_staff_account(self):
        self.auth(self.admin)
        response = self.client.patch(
            f"/api/admin/users/{self.receptionist.pk}/", {"is_active": False}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.json())
        self.receptionist.refresh_from_db()
        self.assertFalse(self.receptionist.is_active)


class GuestProfileCompletenessTests(BaseAPITestCase):
    """§14 — every stored, non-sensitive guest field reaches the dashboard."""

    def setUp(self):
        super().setUp()
        self.staff = make_staff("guestview@staff.dev", role=User.Role.RECEPTIONIST)
        self.manager = make_staff("guestmgr@staff.dev", role=User.Role.MANAGER)
        self.room_type = make_room_type("Business", price="30000.00")
        self.room = make_room(self.room_type, "555")
        self.guest = make_guest(
            "complete@example.com",
            first_name="Ngozi",
            last_name="Eze",
            phone="08031112222",
            address="12 Awolowo Road",
            city="Enugu",
            state="Enugu",
            country="Nigeria",
            identification_type="NATIONAL_ID",
            identification_number="12345678901",
            special_requests="Late arrival",
        )
        self.booking = make_booking(
            self.guest,
            self.room_type,
            rooms=[self.room],
            check_in=hotel_today() + timedelta(days=1),
            check_out=hotel_today() + timedelta(days=3),
            status=Booking.Status.CONFIRMED,
            amount_paid="10000.00",
            total="60000.00",
        )

    def test_detail_contains_every_stored_field(self):
        self.auth(self.staff)
        data = self.client.get(f"/api/admin/guests/{self.guest.pk}/").json()["data"]
        for field in ("id", "first_name", "last_name", "full_name", "email", "phone",
                      "address", "city", "state", "country", "identification_type",
                      "identification_type_label", "identification_number",
                      "special_requests", "bookings", "bookings_count", "last_booking_at",
                      "has_account", "created_at", "updated_at"):
            self.assertIn(field, data)
        self.assertEqual(data["city"], "Enugu")
        self.assertEqual(data["identification_type_label"], "National ID")

    def test_history_rows_include_room_number_and_money(self):
        self.auth(self.staff)
        data = self.client.get(f"/api/admin/guests/{self.guest.pk}/").json()["data"]
        row = data["bookings"][0]
        self.assertEqual(row["room_numbers"], ["555"])
        self.assertEqual(row["room_type_name"], "Business")
        self.assertEqual(Decimal(row["total_amount"]), Decimal("60000.00"))
        self.assertEqual(Decimal(row["amount_due"]), Decimal("50000.00"))
        self.assertIn("payment_status", row)
        self.assertEqual(data["bookings_count"], 1)
        self.assertEqual(Decimal(data["outstanding_balance"]), Decimal("50000.00"))

    def test_id_number_is_masked_for_receptionists_but_full_for_managers(self):
        self.auth(self.staff)
        masked = self.client.get(f"/api/admin/guests/{self.guest.pk}/").json()["data"]
        self.assertTrue(masked["identification_number"].endswith("8901"))
        self.assertTrue(masked["identification_number"].startswith("*"))

        self.auth(self.manager)
        full = self.client.get(f"/api/admin/guests/{self.guest.pk}/").json()["data"]
        self.assertEqual(full["identification_number"], "12345678901")


class ExactRoomBookingTests(BaseAPITestCase):
    """§15–§19 — "Book this room" selects the exact room, with a safe fallback."""

    def setUp(self):
        super().setUp()
        self.room_type = make_room_type("Deluxe", price="25000.00", max_guests=2)
        self.room_203 = make_room(self.room_type, "203")
        self.room_204 = make_room(self.room_type, "204")
        self.user = make_user("booker@guest.dev")
        self.auth(self.user)
        self.today = hotel_today()
        self.check_in = (self.today + timedelta(days=20)).isoformat()
        self.check_out = (self.today + timedelta(days=22)).isoformat()

    def create(self, **extra):
        payload = {
            "room_type": self.room_type.slug,
            "check_in": self.check_in,
            "check_out": self.check_out,
            "rooms": 1,
            "adults": 2,
            "children": 0,
            "guest": {
                "first_name": "Temi", "last_name": "Ade",
                "email": "temi@example.com", "phone": "08055550000",
            },
        }
        payload.update(extra)
        return self.client.post("/api/bookings/", payload, format="json")

    def test_public_room_type_rooms_endpoint_is_readable(self):
        self.unauth()
        response = self.client.get(f"/api/rooms/{self.room_type.slug}/rooms/")
        self.assertEqual(response.status_code, 200, response.json())
        data = response.json()["data"]
        self.assertEqual([r["room_number"] for r in data], ["203", "204"])
        self.assertTrue(all("notes" not in r for r in data))

    def test_exact_room_is_assigned_when_free(self):
        response = self.create(room_id=self.room_203.pk)
        self.assertEqual(response.status_code, 201, response.json())
        data = response.json()["data"]
        self.assertEqual(data["room_assignments"][0]["room_number"], "203")
        self.assertIsNone(data.get("room_substitution"))
        self.assertEqual(data["room_type_name"], "Deluxe")

    def test_unavailable_exact_room_falls_back_to_same_type_and_is_reported(self):
        # Take 203 out of inventory for the requested nights.
        blocker = make_booking(
            make_guest("blocker@example.com"),
            self.room_type,
            rooms=[self.room_203],
            check_in=self.today + timedelta(days=20),
            check_out=self.today + timedelta(days=22),
            status=Booking.Status.CONFIRMED,
            total="50000.00",
        )
        self.assertIsNotNone(blocker.pk)
        response = self.create(room_id=self.room_203.pk)
        self.assertEqual(response.status_code, 201, response.json())
        data = response.json()["data"]
        self.assertEqual(data["room_assignments"][0]["room_number"], "204")
        substitution = data["room_substitution"]
        self.assertEqual(substitution["requested_room_number"], "203")
        self.assertEqual(substitution["assigned_room_number"], "204")
        self.assertFalse(substitution["price_changed"])
        self.assertIn("204", substitution["reason"])
        # Both rooms are the same type, so the total is untouched.
        self.assertEqual(Decimal(data["total_amount"]), Decimal("50000.00"))

    def test_no_silent_switch_when_nothing_equivalent_is_free(self):
        other_guest = make_guest("squatter@example.com")
        make_booking(
            other_guest,
            self.room_type,
            rooms=[self.room_203, self.room_204],
            check_in=self.today + timedelta(days=20),
            check_out=self.today + timedelta(days=22),
            status=Booking.Status.CONFIRMED,
            number_of_rooms=2,
            total="100000.00",
        )
        response = self.create(room_id=self.room_203.pk)
        self.assertEqual(response.status_code, 409, response.json())
        self.assertEqual(response.json()["code"], "ROOM_UNAVAILABLE")
        self.assertEqual(Booking.objects.filter(guest__email="temi@example.com").count(), 0)

    def test_room_from_another_type_is_rejected(self):
        other_type = make_room_type("Standard", price="15000.00")
        foreign_room = make_room(other_type, "900")
        response = self.create(room_id=foreign_room.pk)
        self.assertEqual(response.status_code, 409, response.json())

    def test_out_of_service_room_is_never_assigned(self):
        self.room_204.status = Room.Status.MAINTENANCE
        self.room_204.save(update_fields=["status"])
        make_booking(
            make_guest("blocker2@example.com"),
            self.room_type,
            rooms=[self.room_203],
            check_in=self.today + timedelta(days=20),
            check_out=self.today + timedelta(days=22),
            status=Booking.Status.CONFIRMED,
            total="50000.00",
        )
        response = self.create(room_id=self.room_203.pk)
        self.assertEqual(response.status_code, 409, response.json())

    def test_confirmation_uses_the_actually_assigned_room(self):
        response = self.create(room_id=self.room_204.pk)
        data = response.json()["data"]
        booking = Booking.objects.get(booking_reference=data["booking_reference"])
        self.assertEqual(
            [a.room.room_number for a in booking.room_assignments.all()], ["204"]
        )
        receipt = self.client.get(f"/api/bookings/{booking.booking_reference}/receipt/").json()["data"]
        self.assertEqual(receipt["room_numbers"], ["204"])


class ReceiptIntegrityTests(BaseAPITestCase):
    """§10/§27 — the receipt mirrors the real booking + payment state."""

    def setUp(self):
        super().setUp()
        self.staff = make_staff("receipts@staff.dev", role=User.Role.RECEPTIONIST)
        self.room_type = make_room_type("Royal", price="45000.00", max_guests=2)
        self.room = make_room(self.room_type, "701")
        self.booking = make_booking(
            make_guest("receipt@example.com", first_name="Bala", last_name="Musa"),
            self.room_type,
            rooms=[self.room],
            check_in=hotel_today() + timedelta(days=1),
            check_out=hotel_today() + timedelta(days=3),
            status=Booking.Status.CONFIRMED,
            total="90000.00",
        )

    def receipt(self):
        return self.client.get(
            f"/api/bookings/{self.booking.booking_reference}/receipt/"
        ).json()["data"]

    def test_receipt_reports_the_unpaid_state_honestly(self):
        self.auth(self.staff)
        data = self.receipt()
        self.assertEqual(data["payment_status"], "UNPAID")
        self.assertEqual(Decimal(data["amount_paid"]), Decimal("0.00"))
        self.assertEqual(Decimal(data["amount_due"]), Decimal("90000.00"))
        self.assertIsNone(data["latest_payment"])
        self.assertEqual(data["payments"], [])

    def test_receipt_reflects_partial_then_full_payment(self):
        self.auth(self.staff)
        self.client.post(
            "/api/admin/payments/record/",
            {"booking_reference": self.booking.booking_reference,
             "amount": "30000.00", "provider": "CASH"},
            format="json",
        )
        data = self.receipt()
        self.assertEqual(data["payment_status"], "PARTIALLY_PAID")
        self.assertEqual(Decimal(data["amount_paid"]), Decimal("30000.00"))
        self.assertEqual(data["latest_payment"]["reference"], data["receipt_reference"])
        self.assertEqual(data["latest_payment"]["provider_label"], "Cash")

        self.client.post(
            "/api/admin/payments/record/",
            {"booking_reference": self.booking.booking_reference,
             "amount": "60000.00", "provider": "POS"},
            format="json",
        )
        data = self.receipt()
        self.assertEqual(data["payment_status"], "PAID")
        self.assertEqual(Decimal(data["amount_due"]), Decimal("0.00"))
        self.assertEqual(len(data["payments"]), 2)
        self.assertEqual(Decimal(data["previous_payments_total"]), Decimal("30000.00"))
        self.assertEqual(data["room_numbers"], ["701"])
        self.assertEqual(data["nights"], 2)
        self.assertEqual(data["guest"]["name"], "Bala Musa")
        self.assertEqual(data["number_of_guests"], 2)

    def test_receipt_can_be_emailed_to_the_guest(self):
        self.auth(self.staff)
        response = self.client.post(
            f"/api/admin/bookings/{self.booking.booking_reference}/send-receipt/", {}
        )
        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(response.json()["data"]["recipient"], "receipt@example.com")


class EnquirySubmissionTests(BaseAPITestCase):
    """§31–§38 — the public contact form must actually persist an enquiry.

    The regression this guards: `website` (the anti-spam honeypot) was declared
    as a writable serializer field, and `write_only` only hides a field on
    OUTPUT — it still lands in `validated_data`, so `Enquiry.objects.create()`
    raised `TypeError: Enquiry() got unexpected keyword arguments: 'website'`.
    Every contact submission therefore died with an opaque HTTP 500 and NOTHING
    was ever saved.
    """

    VALID = {
        "name": "Grace Okoro",
        "email": "grace.okoro@example.com",
        "phone": "+234 805 123 4567",
        "subject": "Booking enquiry",
        "message": "I would like to reserve a deluxe room for three nights next month.",
    }

    def setUp(self):
        super().setUp()
        self.staff = make_staff("enq-desk@staff.dev", role=User.Role.RECEPTIONIST)
        self.admin = make_staff("enq-root@staff.dev", role=User.Role.ADMIN)

    def submit(self, **overrides):
        payload = dict(self.VALID)
        payload.update(overrides)
        return self.client.post("/api/enquiries/", payload, format="json")

    def test_endpoint_is_public_no_authentication_required(self):
        self.unauth()
        response = self.submit()
        self.assertEqual(response.status_code, 201, response.json())
        self.assertTrue(response.json()["success"])

    def test_valid_enquiry_is_persisted_with_every_field(self):
        self.unauth()
        self.assertEqual(self.submit().status_code, 201)
        enquiry = Enquiry.objects.get()
        self.assertEqual(enquiry.name, self.VALID["name"])
        self.assertEqual(enquiry.email, self.VALID["email"])
        self.assertEqual(enquiry.phone, self.VALID["phone"])
        self.assertEqual(enquiry.subject, self.VALID["subject"])
        self.assertEqual(enquiry.message, self.VALID["message"])
        self.assertEqual(enquiry.status, Enquiry.Status.NEW)

    def test_honeypot_submission_creates_no_record(self):
        self.unauth()
        response = self.submit(website="http://spam.example.com")
        # The bot is told it succeeded; nothing is stored.
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Enquiry.objects.count(), 0)

    def test_honeypot_blank_string_is_treated_as_a_real_visitor(self):
        self.unauth()
        self.assertEqual(self.submit(website="").status_code, 201)
        self.assertEqual(Enquiry.objects.count(), 1)

    def test_short_message_returns_a_useful_validation_message(self):
        self.unauth()
        response = self.submit(message="hi")
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body["code"], "VALIDATION_ERROR")
        self.assertEqual(body["errors"]["message"], ["Please enter a more detailed message."])
        self.assertEqual(Enquiry.objects.count(), 0)

    def test_missing_required_fields_are_rejected(self):
        self.unauth()
        response = self.client.post(
            "/api/enquiries/", {"message": "A perfectly long enough message."}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        errors = response.json()["errors"]
        self.assertIn("name", errors)
        self.assertIn("email", errors)
        self.assertIn("subject", errors)
        self.assertEqual(Enquiry.objects.count(), 0)

    def test_html_in_the_message_is_stripped(self):
        self.unauth()
        self.submit(message="<script>alert('x')</script> Please call me about a booking.")
        enquiry = Enquiry.objects.get()
        self.assertNotIn("<script>", enquiry.message)
        self.assertIn("Please call me about a booking.", enquiry.message)

    def test_staff_are_notified_without_breaking_the_submission(self):
        self.unauth()
        self.assertEqual(self.submit().status_code, 201)
        self.assertEqual(Enquiry.objects.count(), 1)
        notified = Notification.objects.filter(type=Notification.Type.ENQUIRY_NEW)
        self.assertEqual(notified.count(), 2)  # both active staff accounts
        self.assertTrue(notified.first().link.endswith("/dashboard/enquiries.html"))

    def test_a_failing_notification_does_not_lose_the_enquiry(self):
        """Side effects must never roll back a valid enquiry."""
        self.unauth()
        with mock.patch(
            "apps.enquiries.services.notify_staff", side_effect=RuntimeError("fan-out down")
        ):
            self.assertEqual(self.submit().status_code, 201)
        self.assertEqual(Enquiry.objects.count(), 1)

    def test_submissions_are_idempotent_per_request_not_per_click(self):
        """Two genuine submissions create two rows; the guard is client-side."""
        self.unauth()
        self.submit()
        self.submit()
        self.assertEqual(Enquiry.objects.count(), 2)


class NotificationDetailContractTests(BaseAPITestCase):
    """§2–§5 — the notification detail contract the detail page consumes."""

    def setUp(self):
        super().setUp()
        self.admin = make_staff("nd-admin@staff.dev", role=User.Role.ADMIN)
        self.manager = make_staff("nd-mgr@staff.dev", role=User.Role.MANAGER)
        self.receptionist = make_staff("nd-rec@staff.dev", role=User.Role.RECEPTIONIST)
        self.guest = make_user("nd-guest@guest.dev")
        self.note = Notification.objects.create(
            recipient=self.receptionist,
            type=Notification.Type.PAYMENT_SUCCESS,
            title="Payment received",
            message="Payment of NGN 50,000 has been recorded.",
            link="/dashboard/booking-details.html?ref=J1-20260913-0001",
        )

    def detail(self, pk):
        response = self.client.get(f"/api/notifications/{pk}/")
        self.assertEqual(response.status_code, 200, response.json())
        return response.json()["data"]

    def test_detail_returns_every_field_the_page_renders(self):
        self.auth(self.receptionist)
        data = self.detail(self.note.pk)
        for field in ("id", "type", "type_label", "title", "message", "link",
                      "is_read", "created_at", "related", "unread_count"):
            self.assertIn(field, data)
        self.assertEqual(data["type_label"], "Payment received")
        self.assertEqual(data["message"], "Payment of NGN 50,000 has been recorded.")
        # Opening the detail marks it read, so the very first fetch already
        # reports is_read=True with the remaining unread count at zero.
        self.assertTrue(data["is_read"])
        self.assertEqual(data["unread_count"], 0)

    def test_related_link_keeps_the_backend_root_relative_path(self):
        """The frontend must not receive a mangled 'dashboard/…' path."""
        self.auth(self.receptionist)
        related = self.detail(self.note.pk)["related"]
        self.assertEqual(related["link"], "/dashboard/booking-details.html?ref=J1-20260913-0001")
        self.assertEqual(related["kind"], "booking")
        self.assertEqual(related["reference"], "J1-20260913-0001")
        self.assertTrue(related["link"].startswith("/dashboard/"))

    def test_every_staff_role_can_open_its_own_notification(self):
        for user in (self.admin, self.manager, self.receptionist):
            with self.subTest(role=user.role):
                note = Notification.objects.create(
                    recipient=user, type=Notification.Type.SYSTEM,
                    title="Own notice", message="Only for " + user.email,
                )
                self.auth(user)
                data = self.detail(note.pk)
                self.assertEqual(data["title"], "Own notice")
                self.assertTrue(data["is_read"])

    def test_guest_cannot_read_a_staff_notification(self):
        self.auth(self.guest)
        self.assertEqual(self.client.get(f"/api/notifications/{self.note.pk}/").status_code, 404)

    def test_receptionist_cannot_read_another_members_notification(self):
        self.auth(self.manager)
        self.assertEqual(self.client.get(f"/api/notifications/{self.note.pk}/").status_code, 404)

    def test_unread_count_decreases_after_opening(self):
        second = Notification.objects.create(
            recipient=self.receptionist, type=Notification.Type.CHECK_IN,
            title="Guest checked in", message="Room 203 occupied.",
        )
        self.auth(self.receptionist)
        self.assertEqual(
            self.client.get("/api/notifications/unread-count/").json()["data"]["unread_count"], 2
        )
        self.assertTrue(self.detail(self.note.pk)["is_read"])
        self.assertEqual(
            self.client.get("/api/notifications/unread-count/").json()["data"]["unread_count"], 1
        )
        self.auth(self.receptionist)
        response = self.client.post(f"/api/notifications/{second.pk}/read/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["unread_count"], 0)
        second.refresh_from_db()
        self.assertTrue(second.is_read)

    def test_mark_all_read_clears_the_badge(self):
        Notification.objects.create(
            recipient=self.receptionist, type=Notification.Type.SYSTEM,
            title="Another", message="Notice body.",
        )
        self.auth(self.receptionist)
        response = self.client.post("/api/notifications/read-all/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["unread_count"], 0)
        self.assertEqual(
            Notification.objects.filter(recipient=self.receptionist, is_read=False).count(), 0
        )


class StaffProfileContractTests(BaseAPITestCase):
    """§6–§9 — the staff profile endpoint behind the Profile button."""

    def setUp(self):
        super().setUp()
        self.admin = make_staff("sp-admin@staff.dev", role=User.Role.ADMIN)
        self.manager = make_staff("sp-mgr@staff.dev", role=User.Role.MANAGER)
        self.receptionist = make_staff("sp-rec@staff.dev", role=User.Role.RECEPTIONIST)
        self.guest = make_user("sp-guest@guest.dev")

    def profile(self, pk):
        response = self.client.get(f"/api/admin/users/staff/{pk}/")
        return response

    def test_profile_returns_the_full_credential_free_card(self):
        self.auth(self.admin)
        response = self.profile(self.receptionist.pk)
        self.assertEqual(response.status_code, 200, response.json())
        data = response.json()["data"]
        for field in ("id", "email", "first_name", "last_name", "full_name", "phone",
                      "role", "role_label", "profile_image_url", "is_active",
                      "email_verified", "is_staff", "is_admin", "is_staff_member",
                      "date_joined", "last_login", "updated_at", "stats", "can_manage"):
            self.assertIn(field, data)
        self.assertEqual(data["role_label"], "Receptionist")
        self.assertTrue(data["is_staff_member"])
        self.assertFalse(data["is_admin"])
        stats = data["stats"]
        for key in ("bookings_created", "payments_recorded", "last_payment_at",
                    "actions_logged", "last_action_at"):
            self.assertIn(key, stats)

    def test_profile_never_exposes_credentials(self):
        self.auth(self.admin)
        payload = json.dumps(self.profile(self.receptionist.pk).json())
        for needle in ("password", "pbkdf2", "bcrypt", "secret", "token", "refresh"):
            self.assertNotIn(needle, payload.lower())

    def test_manager_may_view_but_not_manage(self):
        self.auth(self.manager)
        response = self.profile(self.receptionist.pk)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["data"]["can_manage"])

    def test_receptionist_sees_only_their_own_profile(self):
        self.auth(self.receptionist)
        self.assertEqual(self.profile(self.receptionist.pk).status_code, 200)
        self.assertEqual(self.profile(self.admin.pk).status_code, 403)
        self.assertEqual(self.profile(self.manager.pk).status_code, 403)

    def test_guest_account_is_rejected(self):
        self.auth(self.guest)
        self.assertEqual(self.profile(self.admin.pk).status_code, 403)

    def test_anonymous_is_rejected(self):
        self.unauth()
        self.assertIn(self.profile(self.admin.pk).status_code, (401, 403))

    def test_unknown_staff_id_is_404(self):
        self.auth(self.admin)
        self.assertEqual(self.profile(999999).status_code, 404)

    def test_staff_list_can_be_filtered_to_staff_roles_only(self):
        """The staff console must never have to download guest accounts."""
        self.auth(self.admin)
        make_user("sp-guest2@guest.dev", role=User.Role.GUEST)
        response = self.client.get("/api/admin/users/", {"role__in": "ADMIN,MANAGER,RECEPTIONIST"})
        self.assertEqual(response.status_code, 200)
        rows = response.json()["data"]
        self.assertTrue(rows)
        self.assertTrue(all(row["role"] != "GUEST" for row in rows))


class BookingRoomTypeSelectionTests(BaseAPITestCase):
    """§24–§30 — the room TYPE chosen upstream is the one that gets booked."""

    def setUp(self):
        super().setUp()
        self.deluxe = make_room_type("Deluxe", price="25000.00", max_guests=2)
        self.standard = make_room_type("Standard", price="15000.00", max_guests=2)
        make_room(self.deluxe, "201")
        make_room(self.deluxe, "202")
        make_room(self.standard, "101")
        self.user = make_user("selector@guest.dev")
        self.auth(self.user)
        self.today = hotel_today()
        self.check_in = (self.today + timedelta(days=30)).isoformat()
        self.check_out = (self.today + timedelta(days=32)).isoformat()

    def book(self, slug, **extra):
        payload = {
            "room_type": slug,
            "check_in": self.check_in,
            "check_out": self.check_out,
            "rooms": 1,
            "adults": 2,
            "children": 0,
            "guest": {"first_name": "Sade", "last_name": "Ojo",
                      "email": "sade@example.com", "phone": "08011112222"},
        }
        payload.update(extra)
        return self.client.post("/api/bookings/", payload, format="json")

    def test_requested_room_type_is_the_one_that_is_booked(self):
        response = self.book(self.standard.slug)
        self.assertEqual(response.status_code, 201, response.json())
        data = response.json()["data"]
        self.assertEqual(data["room_type_name"], "Standard")
        booking = Booking.objects.get(booking_reference=data["booking_reference"])
        self.assertEqual(booking.room_type_id, self.standard.pk)
        self.assertEqual(
            [a.room.room_number for a in booking.room_assignments.all()], ["101"]
        )

    def test_a_room_number_is_never_accepted_as_a_room_type(self):
        """A physical-room identifier must fail loudly in the `room_type`
        slot instead of silently booking something unrelated."""
        response = self.book("201")
        self.assertEqual(response.status_code, 400, response.json())
        self.assertEqual(Booking.objects.count(), 0)

    def test_an_unknown_room_type_is_rejected(self):
        response = self.book("no-such-room-type")
        self.assertEqual(response.status_code, 400, response.json())
        self.assertEqual(Booking.objects.count(), 0)

    def test_pricing_follows_the_selected_room_type(self):
        deluxe = self.book(self.deluxe.slug).json()["data"]
        standard = self.book(self.standard.slug).json()["data"]
        self.assertEqual(Decimal(deluxe["total_amount"]), Decimal("50000.00"))
        self.assertEqual(Decimal(standard["total_amount"]), Decimal("30000.00"))

    def test_availability_reports_the_room_type_by_slug(self):
        response = self.client.get(
            "/api/rooms/availability/",
            {"check_in": self.check_in, "check_out": self.check_out, "rooms": 1, "adults": 2},
        )
        self.assertEqual(response.status_code, 200)
        slugs = {row["room_type"]["slug"] for row in response.json()["data"]["results"]}
        self.assertIn(self.deluxe.slug, slugs)
        self.assertIn(self.standard.slug, slugs)


class ReceiptFinancialTests(BaseAPITestCase):
    """§14–§23 — the compact receipt renders real money, not client maths."""

    def setUp(self):
        super().setUp()
        self.room_type = make_room_type("Deluxe", price="25000.00", max_guests=2)
        self.room = make_room(self.room_type, "801")
        self.staff = make_staff("rc-desk@staff.dev", role=User.Role.RECEPTIONIST)
        self.today = hotel_today()
        self.booking = make_booking(
            make_guest("rc-guest@example.com", first_name="Bala", last_name="Musa"),
            self.room_type,
            rooms=[self.room],
            check_in=self.today + timedelta(days=5),
            check_out=self.today + timedelta(days=8),
            total="75000.00",
        )

    def receipt(self):
        self.auth(self.staff)
        response = self.client.get(f"/api/bookings/{self.booking.booking_reference}/receipt/")
        self.assertEqual(response.status_code, 200, response.json())
        return response.json()["data"]

    def record(self, amount, provider="CASH"):
        self.auth(self.staff)
        response = self.client.post(
            "/api/admin/payments/record/",
            {"booking_reference": self.booking.booking_reference,
             "amount": amount, "provider": provider},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.json())
        return response.json()["data"]

    def test_receipt_before_any_payment_shows_the_full_outstanding_balance(self):
        data = self.receipt()
        self.assertIsNone(data["latest_payment"])
        self.assertEqual(Decimal(data["total"]), Decimal("75000.00"))
        self.assertEqual(Decimal(data["amount_paid"]), Decimal("0.00"))
        self.assertEqual(Decimal(data["amount_due"]), Decimal("75000.00"))
        self.assertEqual(data["payment_status"], "UNPAID")
        self.assertEqual(Decimal(data["previous_payments_total"]), Decimal("0.00"))

    def test_partial_payment_is_reported_exactly(self):
        self.record("20000.00")
        data = self.receipt()
        self.assertEqual(data["payment_status"], "PARTIALLY_PAID")
        self.assertEqual(Decimal(data["amount_paid"]), Decimal("20000.00"))
        self.assertEqual(Decimal(data["amount_due"]), Decimal("55000.00"))
        self.assertEqual(Decimal(data["latest_payment"]["amount"]), Decimal("20000.00"))
        # One payment so far — nothing counted as "previous".
        self.assertEqual(Decimal(data["previous_payments_total"]), Decimal("0.00"))

    def test_multiple_payments_split_current_and_previous(self):
        self.record("20000.00")
        self.record("10000.00")
        data = self.receipt()
        self.assertEqual(Decimal(data["latest_payment"]["amount"]), Decimal("10000.00"))
        self.assertEqual(Decimal(data["previous_payments_total"]), Decimal("20000.00"))
        self.assertEqual(Decimal(data["amount_paid"]), Decimal("30000.00"))
        self.assertEqual(Decimal(data["amount_due"]), Decimal("45000.00"))
        self.assertEqual(len(data["payments"]), 2)

    def test_full_payment_clears_the_outstanding_balance(self):
        self.record("30000.00")
        self.record("45000.00")
        data = self.receipt()
        self.assertEqual(data["payment_status"], "PAID")
        self.assertEqual(Decimal(data["amount_due"]), Decimal("0.00"))
        self.assertEqual(Decimal(data["amount_paid"]), Decimal("75000.00"))
        self.assertEqual(Decimal(data["previous_payments_total"]), Decimal("30000.00"))

    def test_receipt_never_invents_a_payment_reference(self):
        self.record("5000.00")
        data = self.receipt()
        self.assertTrue(data["receipt_reference"])
        self.assertEqual(data["receipt_reference"], data["latest_payment"]["reference"])
        self.assertTrue(data["latest_payment"]["reference"].startswith("J1P-"))

    def test_receipt_carries_the_hotel_identity_for_the_header(self):
        data = self.receipt()
        self.assertIn("hotel", data)
        self.assertTrue(data["hotel"]["name"])
        self.assertEqual(data["room_numbers"], ["801"])
        self.assertEqual(data["nights"], 3)
