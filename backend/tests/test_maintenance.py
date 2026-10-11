"""Maintenance lifecycle, maker/worker boundaries, and outage availability controls."""
from apps.accounts.models import User
from apps.maintenance.models import MaintenanceEvent, MaintenanceWorkOrder, RoomOutage
from apps.rooms.models import Room

from .base import BaseAPITestCase
from .factories import make_room, make_room_type, make_staff


class MaintenanceAliasRoleTests(BaseAPITestCase):
    """A MAINTENANCE_TECHNICIAN account is a maintenance worker (audit D5/N8).

    The audit's role register holds both spellings as live accounts
    (MAINTENANCE and MAINTENANCE_TECHNICIAN, 4 capabilities each), and the
    workflow used to compare ``role == User.Role.MAINTENANCE`` in three places -
    so a technician could not be assigned an order, could not claim one, and
    received an empty queue. Each of those three paths is asserted here.
    """

    def setUp(self):
        super().setUp()
        self.manager = make_staff("alias-maintenance-manager@staff.dev", role=User.Role.MANAGER)
        self.technician = make_staff("alias-maintenance-tech@staff.dev", role=User.Role.MAINTENANCE_TECHNICIAN)
        self.room = make_room(make_room_type("Alias suite", price="8000.00"), "AL-01")

    def _open_order(self, key, summary):
        self.auth(self.manager)
        created = self.client.post("/api/admin/maintenance/", {
            "room_id": self.room.pk, "category": "PLUMBING", "priority": "NORMAL",
            "summary": summary, "idempotency_key": key,
        }, format="json")
        self.assertEqual(created.status_code, 201, created.content)
        reference = created.json()["data"]["reference"]
        self.assertEqual(self.client.post(
            f"/api/admin/maintenance/{reference}/status/", {"status": "TRIAGED"}, format="json"
        ).status_code, 200)
        return reference

    def test_technician_sees_and_claims_the_unassigned_queue(self):
        reference = self._open_order("alias-claim-1", "Leaking tap")
        self.auth(self.technician)
        queue = self.client.get("/api/admin/maintenance/")
        self.assertEqual(queue.status_code, 200, queue.content)
        self.assertIn(reference, {row["reference"] for row in queue.json()["data"]})
        claimed = self.client.post(f"/api/admin/maintenance/{reference}/claim/", {}, format="json")
        self.assertEqual(claimed.status_code, 200, claimed.content)
        order = MaintenanceWorkOrder.objects.get(reference=reference)
        self.assertEqual(order.assigned_to_id, self.technician.pk)

    def test_dispatcher_may_assign_a_work_order_to_a_technician(self):
        reference = self._open_order("alias-assign-1", "Faulty shower")
        self.auth(self.manager)
        assigned = self.client.post(
            f"/api/admin/maintenance/{reference}/assign/",
            {"assigned_to_id": self.technician.pk}, format="json",
        )
        self.assertEqual(assigned.status_code, 200, assigned.content)
        self.assertEqual(MaintenanceWorkOrder.objects.get(reference=reference).assigned_to_id, self.technician.pk)

    def test_technician_can_progress_an_assigned_order(self):
        reference = self._open_order("alias-progress-1", "Blocked drain")
        self.auth(self.manager)
        self.assertEqual(self.client.post(
            f"/api/admin/maintenance/{reference}/assign/",
            {"assigned_to_id": self.technician.pk}, format="json",
        ).status_code, 200)
        self.auth(self.technician)
        self.assertEqual(self.client.post(
            f"/api/admin/maintenance/{reference}/status/", {"status": "IN_PROGRESS"}, format="json"
        ).status_code, 200)
        self.assertEqual(self.client.post(
            f"/api/admin/maintenance/{reference}/status/", {"status": "READY_FOR_VERIFICATION"}, format="json"
        ).status_code, 200)
        self.assertEqual(
            MaintenanceWorkOrder.objects.get(reference=reference).status,
            MaintenanceWorkOrder.Status.READY_FOR_VERIFICATION,
        )

    def test_non_maintenance_staff_still_cannot_claim(self):
        reference = self._open_order("alias-refuse-1", "Air-conditioner service")
        storekeeper = make_staff("alias-storekeeper@staff.dev", role=User.Role.STOREKEEPER)
        self.auth(storekeeper)
        refused = self.client.post(f"/api/admin/maintenance/{reference}/claim/", {}, format="json")
        self.assertEqual(refused.status_code, 403, refused.content)
        self.assertIsNone(MaintenanceWorkOrder.objects.get(reference=reference).assigned_to_id)


class MaintenanceWorkflowTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.manager = make_staff("maintenance-manager@staff.dev", role=User.Role.MANAGER)
        self.technician = make_staff("maintenance-worker@staff.dev", role=User.Role.MAINTENANCE)
        self.room = make_room(make_room_type("Maintenance suite", price="9500.00"), "MT-01")

    def test_outage_is_explicit_and_must_clear_before_manager_verifies_completion(self):
        self.auth(self.manager)
        created = self.client.post("/api/admin/maintenance/", {
            "room_id": self.room.pk, "category": "ELECTRICAL", "priority": "HIGH",
            "summary": "Repair bedside socket", "idempotency_key": "maintenance-socket-1",
        }, format="json")
        self.assertEqual(created.status_code, 201, created.content)
        reference = created.json()["data"]["reference"]
        self.assertEqual(self.client.post(
            f"/api/admin/maintenance/{reference}/status/", {"status": "TRIAGED", "note": "Electrical work needed"}, format="json"
        ).status_code, 200)

        self.auth(self.technician)
        queue = self.client.get("/api/admin/maintenance/")
        self.assertEqual(queue.status_code, 200, queue.content)
        self.assertEqual(queue.json()["data"][0]["reference"], reference)
        self.assertEqual(self.client.post(f"/api/admin/maintenance/{reference}/claim/", {}, format="json").status_code, 200)
        self.assertEqual(self.client.post(
            f"/api/admin/maintenance/{reference}/status/", {"status": "IN_PROGRESS"}, format="json"
        ).status_code, 200)

        self.auth(self.manager)
        outage = self.client.post(
            f"/api/admin/maintenance/{reference}/outage/start/", {"outage_status": "MAINTENANCE", "reason": "Electrical isolation"}, format="json"
        )
        self.assertEqual(outage.status_code, 200, outage.content)
        self.room.refresh_from_db()
        self.assertEqual(self.room.status, Room.Status.MAINTENANCE)
        self.assertEqual(RoomOutage.objects.get(work_order__reference=reference).status, RoomOutage.Status.ACTIVE)

        self.auth(self.technician)
        self.assertEqual(self.client.post(
            f"/api/admin/maintenance/{reference}/status/", {"status": "READY_FOR_VERIFICATION"}, format="json"
        ).status_code, 200)
        self.auth(self.manager)
        blocked = self.client.post(
            f"/api/admin/maintenance/{reference}/status/", {"status": "COMPLETED"}, format="json"
        )
        self.assertEqual(blocked.status_code, 400, blocked.content)
        cleared = self.client.post(
            f"/api/admin/maintenance/{reference}/outage/clear/", {"note": "Socket tested"}, format="json"
        )
        self.assertEqual(cleared.status_code, 200, cleared.content)
        self.room.refresh_from_db()
        self.assertEqual(self.room.status, Room.Status.AVAILABLE)
        completed = self.client.post(
            f"/api/admin/maintenance/{reference}/status/", {"status": "COMPLETED", "note": "Verified"}, format="json"
        )
        self.assertEqual(completed.status_code, 200, completed.content)
        work_order = MaintenanceWorkOrder.objects.get(reference=reference)
        self.assertEqual(work_order.status, MaintenanceWorkOrder.Status.COMPLETED)
        self.assertEqual(work_order.completed_by_id, self.manager.pk)
        self.assertTrue(MaintenanceEvent.objects.filter(work_order=work_order, type=MaintenanceEvent.Type.OUTAGE_STARTED).exists())
        self.assertTrue(MaintenanceEvent.objects.filter(work_order=work_order, type=MaintenanceEvent.Type.OUTAGE_CLEARED).exists())
