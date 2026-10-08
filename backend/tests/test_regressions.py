# tests/test_regressions.py
"""Regression tests for the staff-dashboard audit fixes.

Covers: admin-only settings (GET and PATCH), settings cache invalidation of
the public hotel payload, public timezone field, reservation-aware
effective_status + dashboard KPI, modify_booking date-conflict re-validation,
and the room / room-type image routes (including the legacy alias).
"""
import io
from datetime import timedelta

from django.core.cache import cache
from PIL import Image

from apps.accounts.models import User
from apps.bookings.models import Booking
from apps.core.utils import hotel_today
from apps.rooms.models import Room, RoomImage, RoomTypeImage

from .base import BaseAPITestCase
from .factories import (
    hotel_settings,
    make_booking,
    make_guest,
    make_room,
    make_room_type,
    make_staff,
)


def png_upload(name="test.png"):
    from django.core.files.uploadedfile import SimpleUploadedFile

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 30, 30)).save(buf, format="PNG")
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/png")


class SettingsAuthTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        hotel_settings()
        self.admin = make_staff("admin@auth.dev", role=User.Role.ADMIN)
        self.manager = make_staff("manager@auth.dev", role=User.Role.MANAGER)
        self.receptionist = make_staff("desk@auth.dev", role=User.Role.RECEPTIONIST)

    def test_settings_get_admin_only(self):
        for user in (self.manager, self.receptionist):
            self.auth(user)
            self.assertEqual(self.client.get("/api/admin/settings/").status_code, 403, user.role)
        self.auth(self.admin)
        self.assertEqual(self.client.get("/api/admin/settings/").status_code, 200)

    def test_settings_patch_admin_only(self):
        self.auth(self.receptionist)
        r = self.client.patch("/api/admin/settings/", {"tagline": "x"}, format="json")
        self.assertEqual(r.status_code, 403)
        self.auth(self.admin)
        r = self.client.patch("/api/admin/settings/", {"tagline": "New tagline"}, format="json")
        self.assertEqual(r.status_code, 200)

    def test_settings_patch_invalidates_public_hotel_cache(self):
        # Prime the public cache.
        first = self.client.get("/api/hotel/").json()["data"]
        self.assertNotEqual(first.get("hotel_name"), "Renamed Hotel")
        self.auth(self.admin)
        r = self.client.patch("/api/admin/settings/", {"hotel_name": "Renamed Hotel"}, format="json")
        self.assertEqual(r.status_code, 200)
        self.unauth()
        second = self.client.get("/api/hotel/").json()["data"]
        self.assertEqual(second["hotel_name"], "Renamed Hotel")


class PublicHotelPayloadTests(BaseAPITestCase):
    def test_public_payload_includes_hotel_timezone(self):
        hotel_settings()
        data = self.client.get("/api/hotel/").json()["data"]
        self.assertEqual(data.get("timezone"), "Africa/Lagos")


class EffectiveStatusTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        hotel_settings()
        self.admin = make_staff("admin@eff.dev", role=User.Role.ADMIN)
        self.room_type = make_room_type("Deluxe", price="20000.00", max_guests=2)
        self.room = make_room(self.room_type, "201")
        self.guest = make_guest("eff@test.dev")
        self.today = hotel_today()

    def room_row(self):
        self.auth(self.admin)
        data = self.client.get("/api/admin/rooms/").json()["data"]
        rows = data["results"] if isinstance(data, dict) and "results" in data else data
        return next(r for r in rows if r["room_number"] == "201")

    def test_confirmed_booking_tonight_shows_reserved(self):
        make_booking(
            self.guest, self.room_type, rooms=[self.room],
            check_in=self.today, check_out=self.today + timedelta(days=2),
            status=Booking.Status.CONFIRMED,
        )
        self.assertEqual(self.room_row()["effective_status"], "RESERVED")

    def test_checked_in_booking_shows_occupied(self):
        make_booking(
            self.guest, self.room_type, rooms=[self.room],
            check_in=self.today - timedelta(days=1), check_out=self.today + timedelta(days=1),
            status=Booking.Status.CHECKED_IN,
        )
        self.assertEqual(self.room_row()["effective_status"], "OCCUPIED")

    def test_cancelled_booking_frees_the_room(self):
        make_booking(
            self.guest, self.room_type, rooms=[self.room],
            check_in=self.today, check_out=self.today + timedelta(days=2),
            status=Booking.Status.CANCELLED,
        )
        self.assertEqual(self.room_row()["effective_status"], "AVAILABLE")

    def test_maintenance_wins_over_reservation(self):
        self.room.status = Room.Status.MAINTENANCE
        self.room.save(update_fields=["status"])
        make_booking(
            self.guest, self.room_type, rooms=[self.room],
            check_in=self.today, check_out=self.today + timedelta(days=2),
            status=Booking.Status.CONFIRMED,
        )
        self.assertEqual(self.room_row()["effective_status"], "MAINTENANCE")

    def test_dashboard_kpi_counts_reserved_room_as_unavailable(self):
        make_room(self.room_type, "202")  # a second, free room
        make_booking(
            self.guest, self.room_type, rooms=[self.room],
            check_in=self.today, check_out=self.today + timedelta(days=2),
            status=Booking.Status.CONFIRMED,
        )
        self.auth(self.admin)
        rooms = self.client.get("/api/admin/dashboard/").json()["data"]["rooms"]
        self.assertEqual(rooms["total"], 2)
        self.assertEqual(rooms["available"], 1)


class ModifyBookingDateConflictTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        hotel_settings()
        self.admin = make_staff("admin@mod.dev", role=User.Role.ADMIN)
        self.room_type = make_room_type("Suite", price="30000.00", max_guests=3)
        self.room = make_room(self.room_type, "301")
        self.guest = make_guest("mod@test.dev")
        self.today = hotel_today()

    def test_date_change_onto_conflicting_stay_is_rejected(self):
        # Booking A occupies room 301 for days +10..+13.
        make_booking(
            self.guest, self.room_type, rooms=[self.room],
            check_in=self.today + timedelta(days=10),
            check_out=self.today + timedelta(days=13),
            status=Booking.Status.CONFIRMED,
        )
        # Booking B holds the same room for days +20..+23.
        booking_b = make_booking(
            self.guest, self.room_type, rooms=[self.room],
            check_in=self.today + timedelta(days=20),
            check_out=self.today + timedelta(days=23),
            status=Booking.Status.CONFIRMED,
        )
        self.auth(self.admin)
        r = self.client.patch(
            f"/api/admin/bookings/{booking_b.booking_reference}/",
            {
                "check_in": (self.today + timedelta(days=11)).isoformat(),
                "check_out": (self.today + timedelta(days=14)).isoformat(),
            },
            format="json",
        )
        self.assertEqual(r.status_code, 409)
        booking_b.refresh_from_db()
        self.assertEqual(booking_b.check_in, self.today + timedelta(days=20))

    def test_date_change_to_free_window_succeeds(self):
        booking = make_booking(
            self.guest, self.room_type, rooms=[self.room],
            check_in=self.today + timedelta(days=20),
            check_out=self.today + timedelta(days=23),
            status=Booking.Status.CONFIRMED,
        )
        self.auth(self.admin)
        r = self.client.patch(
            f"/api/admin/bookings/{booking.booking_reference}/",
            {
                "check_in": (self.today + timedelta(days=30)).isoformat(),
                "check_out": (self.today + timedelta(days=33)).isoformat(),
            },
            format="json",
        )
        self.assertEqual(r.status_code, 200)
        booking.refresh_from_db()
        self.assertEqual(booking.check_in, self.today + timedelta(days=30))


class RoomImageRouteTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        hotel_settings()
        self.admin = make_staff("admin@img.dev", role=User.Role.ADMIN)
        self.receptionist = make_staff("desk@img.dev", role=User.Role.RECEPTIONIST)
        self.room_type = make_room_type("Classic", price="12000.00", max_guests=2)
        self.room = make_room(self.room_type, "401")

    def test_room_image_upload_patch_delete(self):
        self.auth(self.admin)
        r = self.client.post(
            f"/api/admin/rooms/{self.room.pk}/images/", {"image": png_upload()}, format="multipart"
        )
        self.assertEqual(r.status_code, 201)
        image_id = r.json()["data"]["id"]
        self.assertTrue(RoomImage.objects.filter(pk=image_id, room=self.room).exists())

        r = self.client.patch(
            f"/api/admin/rooms/images/{image_id}/", {"is_primary": True}, format="multipart"
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(RoomImage.objects.get(pk=image_id).is_primary)

        r = self.client.delete(f"/api/admin/rooms/images/{image_id}/")
        self.assertIn(r.status_code, (200, 204))
        self.assertFalse(RoomImage.objects.filter(pk=image_id).exists())

    def test_room_type_image_upload_and_legacy_detail_route(self):
        self.auth(self.admin)
        r = self.client.post(
            f"/api/admin/room-types/{self.room_type.pk}/images/",
            {"image": png_upload()},
            format="multipart",
        )
        self.assertEqual(r.status_code, 201)
        image_id = r.json()["data"]["id"]
        self.assertTrue(RoomTypeImage.objects.filter(pk=image_id).exists())

        # Legacy alias used by the shipped dashboard: /api/admin/room-images/{id}/
        r = self.client.patch(
            f"/api/admin/room-images/{image_id}/", {"is_primary": True}, format="multipart"
        )
        self.assertEqual(r.status_code, 200)
        r = self.client.delete(f"/api/admin/room-images/{image_id}/")
        self.assertIn(r.status_code, (200, 204))
        self.assertFalse(RoomTypeImage.objects.filter(pk=image_id).exists())

    def test_receptionist_cannot_upload_room_images(self):
        self.auth(self.receptionist)
        r = self.client.post(
            f"/api/admin/rooms/{self.room.pk}/images/", {"image": png_upload()}, format="multipart"
        )
        self.assertEqual(r.status_code, 403)
