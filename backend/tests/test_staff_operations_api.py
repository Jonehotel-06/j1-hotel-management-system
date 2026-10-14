"""Capability-scoped staff operations HTTP coverage."""
import json
from datetime import timedelta

from django.utils import timezone

from apps.accounts.desktop_policy import issue_desktop_key
from apps.accounts.models import User, Workstation
from apps.staff_operations.models import AttendanceEvent, AttendanceRecord, LeaveRequest

from .base import BaseAPITestCase
from .factories import make_staff


class StaffOperationsApiTests(BaseAPITestCase):
    """The worker's clocks are issued AT the Receptionist Desktop (audit N4):
    every clock request below carries that desk's key, as the desk browser does.
    """

    def setUp(self):
        super().setUp()
        self.manager = make_staff("staff-api-manager@staff.dev", role=User.Role.MANAGER)
        self.worker = make_staff("staff-api-worker@staff.dev", role=User.Role.HOUSEKEEPING)
        self.workstation = Workstation.objects.create(
            name="Staff API Desk", department=Workstation.Department.FRONT_DESK
        )
        self.desk_key = issue_desktop_key(self.workstation)
        self.workstation.save()

    def clock_post(self, payload, **extra):
        """POST to the attendance clock with the desk key the desk browser sends."""
        extra.setdefault("format", "json")
        return self.client.post(
            "/api/admin/staff-operations/attendance/clock/", payload,
            HTTP_X_JONE_DESKTOP_KEY=self.desk_key, **extra,
        )

    def test_worker_can_clock_and_request_own_leave_while_manager_reviews(self):
        self.auth(self.worker)
        clocked_in = self.clock_post({
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

    def test_attendance_clock_in_break_and_out_state_machine(self):
        """The full clock lifecycle, not just clock-in (audit HR-002).

        The audit could only record a single CLOCK_IN before its credential
        bundle was lost, so the break and clock-out transitions were never
        exercised. With biometric verification policies off (the test default)
        the state machine runs on its own and needs no hardware.

        Timings are made deterministic by winding the persisted projection back
        before the later transitions, so the arithmetic below is exact: 60
        minutes elapsed with a 15-minute break leaves 45 worked minutes.
        """
        self.auth(self.worker)
        clock = "/api/admin/staff-operations/attendance/clock/"

        def act(action, key, expected, phrase=None):
            """POST one transition. Accepted transitions answer 201; only an
            idempotent replay of the same source key answers 200."""
            response = self.clock_post({"action": action, "idempotency_key": key}, format="json")
            self.assertEqual(response.status_code, expected, f"{action}: {response.content}")
            if phrase:
                # The API keeps the human-readable reason in ``errors`` and a
                # generic ``message`` ("Validation failed.") beside it, so the
                # refusal is asserted against the whole payload.
                self.assertIn(phrase, json.dumps(response.json()), f"{action}: {response.content}")
            return response

        started = act("CLOCK_IN", "hr002-in", 201)
        record = AttendanceRecord.objects.get(staff=self.worker)
        self.assertEqual(started.json()["data"]["reference"], record.reference)
        self.assertEqual(record.status, AttendanceRecord.Status.ON_DUTY)

        # A second clock-in while on duty is refused.
        act("CLOCK_IN", "hr002-in-again", 400, "clocking in again")

        # A break cannot end when none is open.
        act("BREAK_ENDED", "hr002-break-end-early", 400, "no active break")

        # Wind the projection back so the arithmetic is deterministic.
        AttendanceRecord.objects.filter(pk=record.pk).update(
            clock_in_at=timezone.now() - timedelta(minutes=60),
        )
        act("BREAK_STARTED", "hr002-break-start", 201)
        record.refresh_from_db()
        self.assertEqual(record.status, AttendanceRecord.Status.ON_BREAK)
        self.assertIsNotNone(record.active_break_started_at)
        AttendanceRecord.objects.filter(pk=record.pk).update(
            active_break_started_at=timezone.now() - timedelta(minutes=15),
        )

        # Clocking out over an open break is refused.
        act("CLOCK_OUT", "hr002-out-during-break", 400, "End the active break")

        act("BREAK_ENDED", "hr002-break-end", 201)
        record.refresh_from_db()
        self.assertEqual(record.status, AttendanceRecord.Status.ON_DUTY)
        self.assertEqual(record.accumulated_break_minutes, 15)
        self.assertIsNone(record.active_break_started_at)

        act("CLOCK_OUT", "hr002-out", 201)
        record.refresh_from_db()
        self.assertEqual(record.status, AttendanceRecord.Status.COMPLETED)
        self.assertIsNotNone(record.clock_out_at)
        self.assertEqual(record.worked_minutes, 45)   # 60 elapsed − 15 on break

        # The record is finished, so there is no current attendance to act on.
        act("CLOCK_OUT", "hr002-out-again", 400, "Clock in before")

        # Every accepted transition left one immutable event, in order.
        events = list(AttendanceEvent.objects.filter(record=record).order_by("occurred_at", "pk"))
        self.assertEqual(
            [event.type for event in events],
            [
                AttendanceEvent.Type.CLOCK_IN,
                AttendanceEvent.Type.BREAK_STARTED,
                AttendanceEvent.Type.BREAK_ENDED,
                AttendanceEvent.Type.CLOCK_OUT,
            ],
        )

    def test_attendance_clock_is_idempotent_per_source_key(self):
        """The same source key returns the same record instead of double-booking.

        The audit's own note was that the attendance module was untested; this
        pins the retry behaviour a dropped connection would otherwise exploit.
        """
        self.auth(self.worker)
        clock = "/api/admin/staff-operations/attendance/clock/"
        first = self.clock_post({"action": "CLOCK_IN", "idempotency_key": "hr002-retry"}, format="json")
        self.assertEqual(first.status_code, 201, first.content)
        second = self.clock_post({"action": "CLOCK_IN", "idempotency_key": "hr002-retry"}, format="json")
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(first.json()["data"]["reference"], second.json()["data"]["reference"])
        self.assertEqual(AttendanceRecord.objects.filter(staff=self.worker).count(), 1)
        self.assertEqual(
            AttendanceEvent.objects.filter(record__staff=self.worker, type=AttendanceEvent.Type.CLOCK_IN).count(),
            1,
        )

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
