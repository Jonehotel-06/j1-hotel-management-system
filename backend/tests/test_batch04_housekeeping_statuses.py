"""Batch 04: Claim, Assigned and Cancelled are retired from housekeeping workflow.

History is preserved: legacy rows keep their status, event timelines keep their
CLAIMED/ASSIGNED entries, and CANCELLED rows stay readable.
"""
from apps.accounts.models import User
from apps.housekeeping.models import HousekeepingTask, HousekeepingTaskEvent
from apps.housekeeping.services.task_service import (
    assign_housekeeping_task, create_housekeeping_task, transition_housekeeping_task,
)
from apps.rooms.models import Room

from .base import BaseAPITestCase
from .factories import make_room, make_room_type, make_staff

Status = HousekeepingTask.Status


class HousekeepingRetiredStatusTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.manager = make_staff("b04-hk-manager@staff.dev", role=User.Role.MANAGER)
        self.housekeeper = make_staff("b04-hk-worker@staff.dev", role=User.Role.HOUSEKEEPING)
        self.other_worker = make_staff("b04-hk-other@staff.dev", role=User.Role.HOUSEKEEPING)
        self.room = make_room(make_room_type("Batch 04 housekeeping suite", price="8000.00"), "B04-01")

    def new_task(self, assigned_to=None):
        task, _ = create_housekeeping_task(
            room=self.room, task_type=HousekeepingTask.Type.CHECKOUT_CLEAN,
            summary="Checkout clean Room B04-01", priority=HousekeepingTask.Priority.HIGH,
            assigned_to=assigned_to, actor=self.manager,
        )
        return task

    def test_new_task_is_open_even_when_an_assignee_is_given_and_keeps_assignment_history(self):
        task = self.new_task(assigned_to=self.housekeeper)
        task.refresh_from_db()
        self.assertEqual(task.status, Status.OPEN)
        self.assertEqual(task.assigned_to_id, self.housekeeper.pk)
        self.assertTrue(HousekeepingTaskEvent.objects.filter(
            task=task, type=HousekeepingTaskEvent.Type.ASSIGNED).exists())

    def test_dispatch_does_not_promote_open_to_assigned_and_unassigning_keeps_open(self):
        task = self.new_task()
        assign_housekeeping_task(task=task, actor=self.manager, assignee=self.housekeeper)
        task.refresh_from_db()
        self.assertEqual(task.status, Status.OPEN)
        assign_housekeeping_task(task=task, actor=self.manager, assignee=None)
        task.refresh_from_db()
        self.assertEqual((task.status, task.assigned_to_id), (Status.OPEN, None))

    def test_legacy_assigned_rows_can_still_be_accepted_and_unassigned_back_to_open(self):
        accepted = self.new_task(assigned_to=self.housekeeper)
        HousekeepingTask.objects.filter(pk=accepted.pk).update(status=Status.ASSIGNED)
        transition_housekeeping_task(task=accepted, target_status=Status.ACCEPTED, actor=self.housekeeper)
        accepted.refresh_from_db()
        self.assertEqual(accepted.status, Status.ACCEPTED)

        legacy = self.new_task(assigned_to=self.housekeeper)
        HousekeepingTask.objects.filter(pk=legacy.pk).update(status=Status.ASSIGNED)
        assign_housekeeping_task(task=legacy, actor=self.manager, assignee=None)
        legacy.refresh_from_db()
        self.assertEqual(legacy.status, Status.OPEN)

    def test_retired_statuses_are_refused_by_the_status_endpoint(self):
        task = self.new_task(assigned_to=self.housekeeper)
        self.auth(self.housekeeper)
        for target in ("ASSIGNED", "CANCELLED"):
            response = self.client.post(
                f"/api/admin/housekeeping/{task.reference}/status/", {"status": target}, format="json",
            )
            self.assertEqual(response.status_code, 400, (target, response.content))
        task.refresh_from_db()
        self.assertEqual(task.status, Status.OPEN)

    def test_open_task_moves_to_accepted_and_can_no_longer_be_cancelled_or_skip_work(self):
        task = self.new_task(assigned_to=self.housekeeper)
        self.auth(self.housekeeper)
        skipped = self.client.post(
            f"/api/admin/housekeeping/{task.reference}/status/", {"status": "COMPLETED"}, format="json",
        )
        self.assertEqual(skipped.status_code, 400, skipped.content)
        accepted = self.client.post(
            f"/api/admin/housekeeping/{task.reference}/status/", {"status": "ACCEPTED"}, format="json",
        )
        self.assertEqual(accepted.status_code, 200, accepted.content)

    def test_claim_route_is_gone_and_changes_nothing(self):
        task = self.new_task()
        self.auth(self.housekeeper)
        response = self.client.post(f"/api/admin/housekeeping/{task.reference}/claim/", {}, format="json")
        self.assertEqual(response.status_code, 410, response.content)
        self.assertIn("HOUSEKEEPING_CLAIM_RETIRED", response.content.decode())
        task.refresh_from_db()
        self.assertEqual((task.status, task.assigned_to_id), (Status.OPEN, None))
        self.assertFalse(HousekeepingTaskEvent.objects.filter(
            task=task, type=HousekeepingTaskEvent.Type.CLAIMED).exists())

    def test_unassigned_task_is_visible_but_cannot_be_acted_on_by_a_housekeeper(self):
        task = self.new_task()
        self.auth(self.housekeeper)
        self.assertEqual(self.client.get(f"/api/admin/housekeeping/{task.reference}/").status_code, 200)
        response = self.client.post(
            f"/api/admin/housekeeping/{task.reference}/status/", {"status": "ACCEPTED"}, format="json",
        )
        self.assertEqual(response.status_code, 403, response.content)

    def test_historical_cancelled_and_assigned_rows_remain_readable(self):
        cancelled = self.new_task(assigned_to=self.housekeeper)
        HousekeepingTask.objects.filter(pk=cancelled.pk).update(status=Status.CANCELLED)
        legacy = self.new_task(assigned_to=self.other_worker)
        HousekeepingTask.objects.filter(pk=legacy.pk).update(status=Status.ASSIGNED)
        self.auth(self.manager)
        detail = self.client.get(f"/api/admin/housekeeping/{cancelled.reference}/")
        self.assertEqual(detail.status_code, 200, detail.content)
        self.assertEqual(detail.json()["data"]["status"], "CANCELLED")
        listing = self.client.get("/api/admin/housekeeping/?page_size=100")
        self.assertEqual(listing.status_code, 200)
        statuses = {row["reference"]: row["status"] for row in listing.json()["data"]}
        self.assertEqual(statuses[cancelled.reference], "CANCELLED")
        self.assertEqual(statuses[legacy.reference], "ASSIGNED")
        self.assertTrue(Room.objects.filter(pk=self.room.pk).exists())
