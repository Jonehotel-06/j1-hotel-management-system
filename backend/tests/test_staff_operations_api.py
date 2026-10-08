"""Capability-scoped staff operations HTTP coverage."""
from datetime import timedelta

from django.utils import timezone

from apps.accounts.models import User
from apps.staff_operations.models import LeaveRequest

from .base import BaseAPITestCase
from .factories import make_staff


class StaffOperationsApiTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.manager = make_staff("staff-api-manager@staff.dev", role=User.Role.MANAGER)
        self.worker = make_staff("staff-api-worker@staff.dev", role=User.Role.HOUSEKEEPING)

    def test_worker_can_clock_and_request_own_leave_while_manager_reviews(self):
        self.auth(self.worker)
        clocked_in = self.client.post("/api/admin/staff-operations/attendance/clock/", {
            "action": "CLOCK_IN", "idempotency_key": "api-clock-in",
        }, format="json")
        self.assertEqual(clocked_in.status_code, 201, clocked_in.content)
        self.assertEqual(clocked_in.json()["data"]["status"], "ON_DUTY")
        self.assertEqual(self.client.get("/api/admin/staff-operations/attendance/").status_code, 200)
        self.assertEqual(self.client.get("/api/admin/staff-operations/profiles/").status_code, 403)

        tomorrow = timezone.localdate() + timedelta(days=1)
        leave = self.client.post("/api/admin/staff-operations/leave-requests/", {
            "type": LeaveRequest.Type.ANNUAL, "start_date": tomorrow.isoformat(), "end_date": tomorrow.isoformat(),
            "reason": "Leave", "idempotency_key": "api-leave-one",
        }, format="json")
        self.assertEqual(leave.status_code, 201, leave.content)
        reference = leave.json()["data"]["reference"]
        self.auth(self.manager)
        reviewed = self.client.post(f"/api/admin/staff-operations/leave-requests/{reference}/review/", {
            "approved": True, "review_note": "Covered.",
        }, format="json")
        self.assertEqual(reviewed.status_code, 200, reviewed.content)
        self.assertEqual(reviewed.json()["data"]["status"], "APPROVED")

    def test_manager_schedules_shift_and_worker_cannot_manage_shift_templates(self):
        self.auth(self.manager)
        template = self.client.post("/api/admin/staff-operations/shift-templates/", {
            "code": "DAY", "name": "Day shift", "start_time": "08:00:00", "end_time": "16:00:00",
        }, format="json")
        self.assertEqual(template.status_code, 201, template.content)
        template_search = self.client.get("/api/admin/staff-operations/shift-templates/", {"search": "DAY", "active": "true"})
        self.assertEqual(template_search.status_code, 200, template_search.content)
        self.assertEqual(template_search.json()["data"][0]["id"], template.json()["data"]["id"])
        start = timezone.now() + timedelta(days=2)
        shift = self.client.post("/api/admin/staff-operations/shifts/", {
            "staff_id": self.worker.pk, "template_id": template.json()["data"]["id"],
            "planned_start_at": start.isoformat(), "planned_end_at": (start + timedelta(hours=8)).isoformat(),
            "idempotency_key": "api-shift-one",
        }, format="json")
        self.assertEqual(shift.status_code, 201, shift.content)
        self.auth(self.worker)
        self.assertEqual(self.client.post("/api/admin/staff-operations/shift-templates/", {
            "code": "NO", "name": "No", "start_time": "08:00:00", "end_time": "10:00:00",
        }, format="json").status_code, 403)
