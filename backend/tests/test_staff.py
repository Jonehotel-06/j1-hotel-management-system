# tests/test_staff.py
"""STAFF OPERATIONS tests (spec §41–§57, §84): role gates, dashboard, front-desk
actions, manual bookings, room assignment, settings, users admin, audit trail."""
from datetime import timedelta
from decimal import Decimal

from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.bookings.models import Booking
from apps.core.utils import hotel_today
from apps.payments.models import Payment as PaymentModel
from apps.rooms.models import Room

from .base import BaseAPITestCase
from .factories import make_booking, make_guest, make_room, make_room_type, make_staff, make_user


class StaffBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.room_type = make_room_type("Standard", price="15000.00", max_guests=2)
        self.room_a = make_room(self.room_type, "101")
        self.room_b = make_room(self.room_type, "102")
        self.admin = make_staff("admin@staff.dev", role=User.Role.ADMIN)
        self.manager = make_staff("manager@staff.dev", role=User.Role.MANAGER)
        self.receptionist = make_staff("desk@staff.dev", role=User.Role.RECEPTIONIST)
        self.today = hotel_today()


class RoleGateTests(StaffBase):
    def test_dashboard_accessible_to_all_staff(self):
        for user in (self.admin, self.manager, self.receptionist):
            self.auth(user)
            response = self.client.get("/api/admin/dashboard/")
            self.assertEqual(response.status_code, 200, user.role)

    def test_dashboard_financials_hidden_from_receptionist(self):
        self.auth(self.receptionist)
        data = self.client.get("/api/admin/dashboard/").json()["data"]
        self.assertNotIn("revenue", data)
        self.assertIn("rooms", data)
        self.auth(self.manager)
        data = self.client.get("/api/admin/dashboard/").json()["data"]
        self.assertIn("revenue", data)

    def test_reports_require_manager_or_admin(self):
        params = {"start_date": self.today.isoformat(), "end_date": self.today.isoformat()}
        self.auth(self.receptionist)
        for url in ["/api/admin/reports/revenue/", "/api/admin/reports/occupancy/", "/api/admin/reports/bookings/"]:
            response = self.client.get(url, params)
            self.assertEqual(response.status_code, 403, url)
        self.auth(self.manager)
        for url in ["/api/admin/reports/revenue/", "/api/admin/reports/occupancy/", "/api/admin/reports/bookings/"]:
            response = self.client.get(url, params)
            self.assertEqual(response.status_code, 200, url)

    def test_users_admin_requires_admin(self):
        self.auth(self.receptionist)
        self.assertEqual(self.client.get("/api/admin/users/").status_code, 403)
        self.auth(self.manager)
        self.assertEqual(self.client.get("/api/admin/users/").status_code, 403)
        self.auth(self.admin)
        self.assertEqual(self.client.get("/api/admin/users/").status_code, 200)

    def test_settings_patch_requires_admin(self):
        self.auth(self.manager)
        response = self.client.patch("/api/admin/settings/", {"tax_rate_percent": "7.50"}, format="json")
        self.assertEqual(response.status_code, 403)
        self.auth(self.admin)
        response = self.client.patch("/api/admin/settings/", {"tax_rate_percent": "7.50"}, format="json")
        self.assertEqual(response.status_code, 200, response.json())

    def test_settings_change_writes_audit_log(self):
        self.auth(self.admin)
        self.client.patch("/api/admin/settings/", {"tax_rate_percent": "7.50"}, format="json")
        self.assertTrue(
            AuditLog.objects.filter(action="SETTINGS_CHANGED", actor=self.admin).exists()
        )

    def test_audit_logs_are_admin_only_and_readonly(self):
        self.auth(self.manager)
        self.assertEqual(self.client.get("/api/admin/audit-logs/").status_code, 403)
        self.auth(self.admin)
        self.assertEqual(self.client.get("/api/admin/audit-logs/").status_code, 200)
        self.assertEqual(self.client.post("/api/admin/audit-logs/", {}).status_code, 405)

    def test_admin_can_create_staff_and_role_changes_are_audited(self):
        self.auth(self.admin)
        response = self.client.post("/api/admin/users/", {
            "email": "newdesk@staff.dev", "first_name": "New", "last_name": "Desk",
            "role": "RECEPTIONIST", "password": "Str0ng!Pass",
        })
        self.assertEqual(response.status_code, 201, response.json())
        new_user = User.objects.get(email="newdesk@staff.dev")
        self.assertEqual(new_user.role, "RECEPTIONIST")
        self.assertTrue(AuditLog.objects.filter(action="USER_CREATED").exists())

        patch = self.client.patch(f"/api/admin/users/{new_user.pk}/", {"role": "MANAGER"}, format="json")
        self.assertEqual(patch.status_code, 200)
        new_user.refresh_from_db()
        self.assertEqual(new_user.role, "MANAGER")
        self.assertTrue(AuditLog.objects.filter(action="USER_ROLE_CHANGED").exists())

    def test_admin_cannot_demote_self(self):
        self.auth(self.admin)
        response = self.client.patch(f"/api/admin/users/{self.admin.pk}/", {"role": "GUEST"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.admin.refresh_from_db()
        self.assertEqual(self.admin.role, "ADMIN")

    def test_receptionist_cannot_write_room_inventory(self):
        self.auth(self.receptionist)
        self.assertEqual(self.client.get("/api/admin/rooms/").status_code, 200)
        response = self.client.post("/api/admin/rooms/", {
            "room_number": "999", "room_type": self.room_type.pk, "floor": 9,
        }, format="json")
        self.assertEqual(response.status_code, 403)
        self.auth(self.manager)
        response = self.client.post("/api/admin/rooms/", {
            "room_number": "999", "room_type": self.room_type.pk, "floor": 9,
        }, format="json")
        self.assertEqual(response.status_code, 201, response.json())
        self.assertTrue(AuditLog.objects.filter(action="ROOM_CREATED").exists())


class FrontDeskTests(StaffBase):
    def setUp(self):
        super().setUp()
        self.guest_user = make_user("stay@hotel.dev", password="Str0ng!Pass")
        self.guest = make_guest(user=self.guest_user)

    def _confirmed_booking(self, **kwargs):
        rooms = kwargs.pop("rooms", [self.room_a])
        return make_booking(
            self.guest, self.room_type, rooms=rooms,
            check_in=kwargs.pop("check_in", self.today),
            check_out=kwargs.pop("check_out", self.today + timedelta(days=2)),
            **kwargs,
        )

    def test_manual_booking_creation(self):
        self.auth(self.receptionist)
        response = self.client.post("/api/admin/bookings/", {
            "room_type": self.room_type.slug,
            "check_in": self.today.isoformat(),
            "check_out": (self.today + timedelta(days=2)).isoformat(),
            "rooms": 1, "adults": 1, "children": 0,
            "source": "WALK_IN",
            "guest": {"first_name": "Walk", "last_name": "In", "email": "walkin@hotel.dev", "phone": "08011112222"},
        }, format="json")
        self.assertEqual(response.status_code, 201, response.json())
        data = response.json()["data"]
        self.assertEqual(data["status"], "CONFIRMED")  # default for staff unless PENDING chosen
        self.assertEqual(data["source"], "WALK_IN")

    def test_check_in_marks_room_occupied(self):
        booking = self._confirmed_booking()
        self.auth(self.receptionist)
        response = self.client.post(f"/api/admin/bookings/{booking.booking_reference}/check-in/")
        self.assertEqual(response.status_code, 200, response.json())
        booking.refresh_from_db()
        self.assertEqual(booking.status, "CHECKED_IN")
        self.assertIsNotNone(booking.checked_in_at)
        self.room_a.refresh_from_db()
        self.assertEqual(self.room_a.status, Room.Status.OCCUPIED)
        self.assertTrue(AuditLog.objects.filter(action="CHECK_IN").exists())

    def test_check_in_is_idempotent(self):
        booking = self._confirmed_booking()
        self.auth(self.receptionist)
        url = f"/api/admin/bookings/{booking.booking_reference}/check-in/"
        self.assertEqual(self.client.post(url).status_code, 200)
        self.assertEqual(self.client.post(url).status_code, 200)  # retry: no error, no double state
        booking.refresh_from_db()
        self.assertEqual(booking.status, "CHECKED_IN")

    def test_check_in_rejects_pending_booking(self):
        booking = self._confirmed_booking(status=Booking.Status.PENDING)
        self.auth(self.receptionist)
        response = self.client.post(f"/api/admin/bookings/{booking.booking_reference}/check-in/")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "INVALID_BOOKING_STATE")

    def test_check_out_requires_settled_balance(self):
        booking = self._confirmed_booking(amount_paid="0.00")
        self.auth(self.receptionist)
        self.client.post(f"/api/admin/bookings/{booking.booking_reference}/check-in/")
        response = self.client.post(f"/api/admin/bookings/{booking.booking_reference}/check-out/")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "OUTSTANDING_BALANCE")
        # With an explicit override flag the front desk may still release the room.
        override = self.client.post(
            f"/api/admin/bookings/{booking.booking_reference}/check-out/",
            {"allow_balance_due": True}, format="json",
        )
        self.assertEqual(override.status_code, 200)
        booking.refresh_from_db()
        self.assertEqual(booking.status, "CHECKED_OUT")
        self.room_a.refresh_from_db()
        self.assertEqual(self.room_a.status, Room.Status.AVAILABLE)
        self.assertEqual(self.room_a.housekeeping_status, "DIRTY")

    def test_check_out_after_payment(self):
        booking = self._confirmed_booking(amount_paid="15000.00", total="15000.00")
        booking.total_amount = Decimal("15000.00")
        booking.payment_status = Booking.PaymentStatus.PAID
        booking.save()
        self.auth(self.receptionist)
        self.client.post(f"/api/admin/bookings/{booking.booking_reference}/check-in/")
        booking.refresh_from_db()
        booking.amount_paid = booking.total_amount
        booking.save()
        response = self.client.post(f"/api/admin/bookings/{booking.booking_reference}/check-out/")
        self.assertEqual(response.status_code, 200)

    def test_assign_room_validates_overlap(self):
        first = self._confirmed_booking(rooms=[self.room_a])
        second = self._confirmed_booking(rooms=[self.room_b])
        self.auth(self.receptionist)
        assignment = first.room_assignments.get()
        response = self.client.post(
            f"/api/admin/bookings/{first.booking_reference}/assign-room/",
            {"assignment_id": assignment.pk, "room_id": self.room_b.pk}, format="json",
        )
        self.assertEqual(response.status_code, 409)  # room_b is taken for these dates

        spare = make_room(self.room_type, "103")
        ok = self.client.post(
            f"/api/admin/bookings/{first.booking_reference}/assign-room/",
            {"assignment_id": assignment.pk, "room_id": spare.pk}, format="json",
        )
        self.assertEqual(ok.status_code, 200, ok.json())
        assignment.refresh_from_db()
        self.assertEqual(assignment.room_id, spare.pk)

    def test_no_show_flow(self):
        booking = self._confirmed_booking(
            check_in=self.today - timedelta(days=1),
            check_out=self.today + timedelta(days=1),
            rooms=[self.room_a],
        )
        self.auth(self.receptionist)
        response = self.client.post(f"/api/admin/bookings/{booking.booking_reference}/no-show/")
        self.assertEqual(response.status_code, 200)
        booking.refresh_from_db()
        self.assertEqual(booking.status, "NO_SHOW")

    def test_booking_list_filters_and_search(self):
        booking = self._confirmed_booking()
        self.auth(self.receptionist)
        listed = self.client.get("/api/admin/bookings/", {"status": "CONFIRMED"})
        self.assertGreaterEqual(listed.json()["pagination"]["count"], 1)
        found = self.client.get("/api/admin/bookings/", {"search": booking.booking_reference})
        self.assertEqual(found.json()["pagination"]["count"], 1)
        row = found.json()["data"][0]
        # Booking table contract (§126): the row carries everything the table needs.
        for field in ("booking_reference", "guest_name", "guest_phone", "room_type_name",
                      "room_numbers", "check_in", "check_out", "nights", "total_amount",
                      "payment_status", "status"):
            self.assertIn(field, row)


    def test_checkin_search_includes_checked_in_and_phone(self):
        booking = self._confirmed_booking()
        self.auth(self.receptionist)
        self.assertEqual(self.client.post(f"/api/admin/bookings/{booking.booking_reference}/check-in/").status_code, 200)
        for query in (booking.booking_reference, self.guest.phone):
            response = self.client.get("/api/admin/bookings/", {
                "status": "CONFIRMED,CHECKED_IN", "search": query,
            })
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["pagination"]["count"], 1)
            self.assertEqual(response.json()["data"][0]["status"], Booking.Status.CHECKED_IN)

    def test_dashboard_uses_operational_arrivals_and_in_house(self):
        booking = self._confirmed_booking()
        self.auth(self.receptionist)
        before = self.client.get("/api/admin/dashboard/").json()["data"]
        self.assertIn(booking.booking_reference, [row["booking_reference"] for row in before["upcoming_arrivals"]])
        self.client.post(f"/api/admin/bookings/{booking.booking_reference}/check-in/")
        after = self.client.get("/api/admin/dashboard/").json()["data"]
        self.assertIn(booking.booking_reference, [row["booking_reference"] for row in after["in_house_guests"]])
        self.assertEqual(after["today"]["in_house"], 1)

class DashboardDataTests(StaffBase):
    def _confirmed_booking(self):
        self.guest = make_guest(phone="08022223333")
        return make_booking(
            self.guest, self.room_type, rooms=[self.room_a],
            check_in=self.today, check_out=self.today + timedelta(days=2),
        )

    def test_dashboard_counts_and_recent_lists(self):
        guest = make_guest()
        booking = make_booking(guest, self.room_type, rooms=[self.room_a],
                               check_in=self.today, check_out=self.today + timedelta(days=2))
        PaymentModel.objects.create(
            booking=booking, reference="J1P-DASH-1", amount=Decimal("15000.00"), currency="NGN",
            status=PaymentModel.Status.SUCCESS, paid_at=__import__("django.utils.timezone", fromlist=["now"]).now(),
        )
        self.auth(self.manager)
        data = self.client.get("/api/admin/dashboard/").json()["data"]
        self.assertEqual(data["today"]["arrivals"], 1)
        self.assertEqual(data["rooms"]["total"], 2)
        self.assertGreaterEqual(len(data["recent_bookings"]), 1)
        self.assertIn("revenue", data)
        self.assertEqual(data["revenue"]["today"], "15000.00")

    def test_health_endpoint(self):
        response = self.client.get("/api/health/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_checkin_search_includes_checked_in_and_phone(self):
        booking = self._confirmed_booking()
        self.auth(self.receptionist)
        self.assertEqual(self.client.post(f"/api/admin/bookings/{booking.booking_reference}/check-in/").status_code, 200)
        for query in (booking.booking_reference, self.guest.phone):
            response = self.client.get("/api/admin/bookings/", {
                "status": "CONFIRMED,CHECKED_IN", "search": query,
            })
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["pagination"]["count"], 1)
            self.assertEqual(response.json()["data"][0]["status"], Booking.Status.CHECKED_IN)

    def test_dashboard_uses_operational_arrivals_and_in_house(self):
        booking = self._confirmed_booking()
        self.auth(self.receptionist)
        before = self.client.get("/api/admin/dashboard/").json()["data"]
        self.assertIn(booking.booking_reference, [row["booking_reference"] for row in before["upcoming_arrivals"]])
        self.client.post(f"/api/admin/bookings/{booking.booking_reference}/check-in/")
        after = self.client.get("/api/admin/dashboard/").json()["data"]
        self.assertIn(booking.booking_reference, [row["booking_reference"] for row in after["in_house_guests"]])
        self.assertEqual(after["today"]["in_house"], 1)

class RoomImageTests(StaffBase):
    def test_room_serialization_includes_physical_images_field(self):
        self.auth(self.receptionist)
        response = self.client.get("/api/admin/rooms/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("images", response.json()["data"][0])

    def test_receptionist_cannot_upload_physical_room_image(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        image = SimpleUploadedFile("room.jpg", b"not-an-image", content_type="image/jpeg")
        self.auth(self.receptionist)
        response = self.client.post(f"/api/admin/room-images/room/{self.room_a.pk}/", {"image": image})
        self.assertEqual(response.status_code, 403)

    def test_manager_can_upload_physical_room_image(self):
        from io import BytesIO
        from PIL import Image
        from django.core.files.uploadedfile import SimpleUploadedFile
        buffer = BytesIO()
        Image.new("RGB", (2, 2), "white").save(buffer, format="JPEG")
        image = SimpleUploadedFile("room.jpg", buffer.getvalue(), content_type="image/jpeg")
        self.auth(self.manager)
        response = self.client.post(f"/api/admin/room-images/room/{self.room_a.pk}/", {"image": image})
        self.assertEqual(response.status_code, 201, response.json())
        self.assertTrue(response.json()["data"]["image"].split("/")[-1].endswith(".jpg"))

