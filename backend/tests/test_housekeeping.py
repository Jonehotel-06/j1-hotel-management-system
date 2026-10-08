"""Controlled room-readiness task workflow coverage."""
from datetime import timedelta

from apps.accounts.models import User
from apps.core.utils import hotel_today
from apps.housekeeping.models import HousekeepingTask, HousekeepingTaskEvent
from apps.rooms.models import Room

from .base import BaseAPITestCase
from .factories import make_room, make_room_type, make_staff


class HousekeepingWorkflowTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.manager = make_staff("housekeeping-manager@staff.dev", role=User.Role.MANAGER)
        self.housekeeper = make_staff("housekeeping-worker@staff.dev", role=User.Role.HOUSEKEEPING)
        self.room = make_room(make_room_type("Housekeeping suite", price="8000.00"), "HK-01")

    def test_worker_claims_and_progresses_task_manager_inspects_room_clean(self):
        self.auth(self.manager)
        created = self.client.post("/api/admin/housekeeping/", {
            "room_id": self.room.pk,
            "type": "CHECKOUT_CLEAN",
            "priority": "HIGH",
            "summary": "Checkout clean Room HK-01",
            "idempotency_key": "housekeeping-create-1",
        }, format="json")
        self.assertEqual(created.status_code, 201, created.content)
        reference = created.json()["data"]["reference"]
        duplicate = self.client.post("/api/admin/housekeeping/", {
            "room_id": self.room.pk, "type": "CHECKOUT_CLEAN", "priority": "HIGH",
            "summary": "Checkout clean Room HK-01", "idempotency_key": "housekeeping-create-1",
        }, format="json")
        self.assertEqual(duplicate.status_code, 200, duplicate.content)
        self.assertEqual(duplicate.json()["data"]["reference"], reference)

        self.auth(self.housekeeper)
        queue = self.client.get("/api/admin/housekeeping/")
        self.assertEqual(queue.status_code, 200, queue.content)
        self.assertEqual(queue.json()["data"][0]["reference"], reference)
        self.assertEqual(self.client.post(f"/api/admin/housekeeping/{reference}/claim/", {}, format="json").status_code, 200)
        self.assertEqual(self.client.post(f"/api/admin/housekeeping/{reference}/status/", {"status": "ACCEPTED"}, format="json").status_code, 200)
        started = self.client.post(
            f"/api/admin/housekeeping/{reference}/status/", {"status": "IN_PROGRESS"}, format="json"
        )
        self.assertEqual(started.status_code, 200, started.content)
        self.room.refresh_from_db()
        self.assertEqual(self.room.housekeeping_status, Room.HousekeepingStatus.CLEANING)
        self.assertEqual(self.client.post(
            f"/api/admin/housekeeping/{reference}/status/", {"status": "READY_FOR_INSPECTION"}, format="json"
        ).status_code, 200)
        forbidden_complete = self.client.post(
            f"/api/admin/housekeeping/{reference}/status/", {"status": "COMPLETED"}, format="json"
        )
        self.assertEqual(forbidden_complete.status_code, 403, forbidden_complete.content)

        self.auth(self.manager)
        completed = self.client.post(
            f"/api/admin/housekeeping/{reference}/status/", {"status": "COMPLETED", "note": "Inspection passed"}, format="json"
        )
        self.assertEqual(completed.status_code, 200, completed.content)
        task = HousekeepingTask.objects.get(reference=reference)
        self.room.refresh_from_db()
        self.assertEqual(task.status, HousekeepingTask.Status.COMPLETED)
        self.assertEqual(task.inspected_by_id, self.manager.pk)
        self.assertEqual(self.room.housekeeping_status, Room.HousekeepingStatus.CLEAN)
        self.assertTrue(HousekeepingTaskEvent.objects.filter(task=task, type=HousekeepingTaskEvent.Type.INSPECTED).exists())
