# tests/test_room_mark_clean.py
"""Receptionists can release a ready room from the housekeeping page (D1).

Before the repair the only path to ``Room.housekeeping_status = CLEAN`` ran
through housekeeping-task completion or a manager-only room edit, so a
receptionist who had just inspected a clean room could not release it: room
status stayed DIRTY/NOT READY until a manager logged in.

The repair adds one backend-authoritative action —
``POST /api/admin/housekeeping/rooms/<room_number>/mark-clean/`` — gated to
``housekeeping.task.assign`` (the dispatcher capability receptionists and
housekeeping staff already hold), which:

* moves ONLY the clean/dirty projection; occupancy and maintenance states
  (AVAILABLE/OCCUPIED/RESERVED/MAINTENANCE/OUT_OF_SERVICE) are preserved;
* is idempotent (a repeat call reports ``changed: false`` and changes nothing);
* is audited with who, when and the before/after state on every call;
* is refused (403) to roles without the capability and (404) to unknown rooms.

``GET …/rooms/not-ready/`` backs the "Rooms not ready" board with the same
capability gate.
"""
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.rooms.models import Room

from .base import BaseAPITestCase
from .factories import make_room, make_room_type, make_staff

NOT_READY_URL = "/api/admin/housekeeping/rooms/not-ready/"


def _mark_clean_url(room_number):
    return f"/api/admin/housekeeping/rooms/{room_number}/mark-clean/"


class RoomMarkCleanTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.receptionist = make_staff("markclean-desk@staff.dev", role=User.Role.RECEPTIONIST)
        self.housekeeper = make_staff("markclean-hk@staff.dev", role=User.Role.HOUSEKEEPING)
        self.cashier = make_staff("markclean-cashier@staff.dev", role=User.Role.CASHIER)
        room_type = make_room_type("Mark clean suite", price="50000.00")
        self.room = make_room(room_type, "401")
        self.room.housekeeping_status = Room.HousekeepingStatus.DIRTY
        self.room.status = Room.Status.OCCUPIED
        self.room.save(update_fields=["housekeeping_status", "status", "updated_at"])

    def test_receptionist_can_mark_a_dirty_room_clean_and_occupancy_is_preserved(self):
        self.auth(self.receptionist)
        response = self.client.post(_mark_clean_url(self.room.room_number), {}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()["data"]
        self.assertEqual(payload["housekeeping_status"], Room.HousekeepingStatus.CLEAN)
        self.assertTrue(payload["changed"])

        self.room.refresh_from_db()
        self.assertEqual(self.room.housekeeping_status, Room.HousekeepingStatus.CLEAN)
        # The room is still OCCUPIED: marking clean touches only the
        # clean/dirty projection.
        self.assertEqual(self.room.status, Room.Status.OCCUPIED)

        # The audit trail names who released it, when, and the before state.
        entry = AuditLog.objects.filter(action="HOUSEKEEPING_ROOM_MARKED_CLEAN").get()
        self.assertEqual(entry.actor_id, self.receptionist.pk)
        self.assertEqual(entry.metadata.get("housekeeping_status_before"), Room.HousekeepingStatus.DIRTY)
        self.assertEqual(entry.metadata.get("room_number"), self.room.room_number)

    def test_mark_clean_is_idempotent(self):
        self.auth(self.receptionist)
        first = self.client.post(_mark_clean_url(self.room.room_number), {}, format="json")
        self.assertTrue(first.json()["data"]["changed"])
        second = self.client.post(_mark_clean_url(self.room.room_number), {}, format="json")
        self.assertEqual(second.status_code, 200, second.content)
        self.assertFalse(second.json()["data"]["changed"])
        self.assertEqual(
            AuditLog.objects.filter(action="HOUSEKEEPING_ROOM_MARKED_CLEAN").count(), 2
        )

    def test_maintenance_states_are_never_touched(self):
        for status in (Room.Status.MAINTENANCE, Room.Status.OUT_OF_SERVICE):
            self.room.status = status
            self.room.housekeeping_status = Room.HousekeepingStatus.CLEANING
            self.room.save(update_fields=["housekeeping_status", "status", "updated_at"])
            self.auth(self.receptionist)
            response = self.client.post(_mark_clean_url(self.room.room_number), {}, format="json")
            self.assertEqual(response.status_code, 200, response.content)
            self.room.refresh_from_db()
            self.assertEqual(self.room.housekeeping_status, Room.HousekeepingStatus.CLEAN)
            self.assertEqual(self.room.status, status, "occupancy/maintenance state must survive")

    def test_roles_without_the_capability_are_refused_and_unknown_rooms_404(self):
        self.auth(self.cashier)
        refused = self.client.post(_mark_clean_url(self.room.room_number), {}, format="json")
        self.assertEqual(refused.status_code, 403, refused.content)
        self.room.refresh_from_db()
        self.assertEqual(self.room.housekeeping_status, Room.HousekeepingStatus.DIRTY)

        self.auth(self.receptionist)
        missing = self.client.post(_mark_clean_url("9999"), {}, format="json")
        self.assertEqual(missing.status_code, 404, missing.content)

    def test_the_not_ready_board_lists_only_unclean_rooms(self):
        room_type = self.room.room_type
        cleaning = make_room(room_type, "402")
        cleaning.housekeeping_status = Room.HousekeepingStatus.CLEANING
        cleaning.save(update_fields=["housekeeping_status", "updated_at"])
        ready = make_room(room_type, "403")
        ready.housekeeping_status = Room.HousekeepingStatus.CLEAN
        ready.save(update_fields=["housekeeping_status", "updated_at"])

        self.auth(self.receptionist)
        response = self.client.get(NOT_READY_URL)
        self.assertEqual(response.status_code, 200, response.content)
        rows = response.json()["data"]
        numbers = sorted(row["room_number"] for row in rows)
        self.assertEqual(numbers, sorted([self.room.room_number, cleaning.room_number]))
        for row in rows:
            self.assertNotEqual(row["housekeeping_status"], Room.HousekeepingStatus.CLEAN)
            self.assertIn("status", row)

        self.auth(self.cashier)
        self.assertEqual(self.client.get(NOT_READY_URL).status_code, 403)
