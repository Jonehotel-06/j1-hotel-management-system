# tests/test_booking_access_capabilities.py
"""Who may work the staff reservation desk (audit N10).

The capability matrix and the dashboard sidebar both give GENERAL_MANAGER and
FRONT_DESK_SUPERVISOR the reservations workspace and ``booking.read`` /
``booking.manage``, but the API answered 403 because the endpoints were still
gated by the older three-role allowlist (ADMIN/MANAGER/RECEPTIONIST). These
tests pin the intended split: front-of-house roles may work reservations, while
operations roles without a booking grant (and guest accounts) stay refused.
"""
from datetime import timedelta

from apps.accounts.models import User
from apps.bookings.models import Booking
from apps.core.utils import hotel_today

from .base import BaseAPITestCase
from .factories import make_booking, make_guest, make_room, make_room_type, make_staff, make_user

READ_ROLES = (
    User.Role.GENERAL_MANAGER,
    User.Role.FRONT_DESK_SUPERVISOR,
    # The legacy allowlist must keep working exactly as before.
    User.Role.ADMIN,
    User.Role.MANAGER,
    User.Role.RECEPTIONIST,
)
REFUSED_ROLES = (
    User.Role.CASHIER,
    User.Role.HOUSEKEEPING,
    User.Role.MAINTENANCE,
    User.Role.STOREKEEPER,
    User.Role.SECURITY,
)


class BookingAccessCapabilityTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.room_type = make_room_type("Access suite", price="12000.00")
        self.room = make_room(self.room_type, "401")
        self.booking = make_booking(
            make_guest("access-guest@example.com"), self.room_type, [self.room],
            check_in=hotel_today() + timedelta(days=5),
            check_out=hotel_today() + timedelta(days=7), total="24000.00",
        )

    def _list(self):
        return self.client.get("/api/admin/bookings/")

    def _detail(self):
        return self.client.get(f"/api/admin/bookings/{self.booking.booking_reference}/")

    def test_front_of_house_roles_may_read_the_reservation_desk(self):
        for index, role in enumerate(READ_ROLES):
            with self.subTest(role=role):
                self.auth(make_staff(f"n10.read{index}@staff.dev", role=role))
                listing = self._list()
                self.assertEqual(listing.status_code, 200, listing.content)
                detail = self._detail()
                self.assertEqual(detail.status_code, 200, detail.content)
                self.assertEqual(
                    detail.json()["data"]["booking_reference"], self.booking.booking_reference
                )
                guests = self.client.get("/api/admin/guests/")
                self.assertEqual(guests.status_code, 200, guests.content)

    def test_roles_without_a_booking_grant_are_still_refused(self):
        for index, role in enumerate(REFUSED_ROLES):
            with self.subTest(role=role):
                self.auth(make_staff(f"n10.deny{index}@staff.dev", role=role))
                self.assertEqual(self._list().status_code, 403)
                self.assertEqual(self._detail().status_code, 403)

    def test_a_guest_account_cannot_reach_the_staff_reservation_surface(self):
        self.auth(make_user("n10.guest@example.com", role=User.Role.GUEST))
        self.assertEqual(self._list().status_code, 403)
        self.assertEqual(self._detail().status_code, 403)

    def test_front_desk_supervisor_can_confirm_and_the_change_persists(self):
        pending = make_booking(
            make_guest("n10.pending@example.com"), self.room_type, [self.room],
            check_in=hotel_today() + timedelta(days=9),
            check_out=hotel_today() + timedelta(days=10),
            status=Booking.Status.PENDING, total="12000.00",
            expires_at=hotel_today() + timedelta(days=1),
        )
        self.assertIsNotNone(pending.expires_at)

        self.auth(make_staff("n10.supervisor@staff.dev", role=User.Role.FRONT_DESK_SUPERVISOR))
        response = self.client.post(f"/api/admin/bookings/{pending.booking_reference}/confirm/")
        self.assertEqual(response.status_code, 200, response.content)

        pending.refresh_from_db()
        self.assertEqual(pending.status, Booking.Status.CONFIRMED, "state change not persisted")
        self.assertIsNone(pending.expires_at)

    def test_general_manager_can_manage_reservations_end_to_end(self):
        self.auth(make_staff("n10.gm@staff.dev", role=User.Role.GENERAL_MANAGER))
        response = self.client.patch(
            f"/api/admin/bookings/{self.booking.booking_reference}/",
            {"special_requests": "Late arrival, please hold the room."},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)

        self.booking.refresh_from_db()
        self.assertEqual(self.booking.special_requests, "Late arrival, please hold the room.")
