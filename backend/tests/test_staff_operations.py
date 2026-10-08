"""Workforce shift, attendance, and leave maker-checker regression coverage."""
from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from apps.accounts.models import User
from apps.staff_operations.models import AttendanceEvent, AttendanceRecord, LeaveRequest, ShiftAssignment
from apps.staff_operations.services.staff_operations_service import (
    clock_attendance, create_shift_assignment, ensure_staff_profile, request_leave, review_leave,
)

from .base import BaseAPITestCase
from .factories import make_staff


class StaffOperationsTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.manager = make_staff("staff-ops-manager@staff.dev", role=User.Role.MANAGER)
        self.worker = make_staff("staff-ops-housekeeper@staff.dev", role=User.Role.HOUSEKEEPING)

    def test_shift_prevents_overlap_and_attendance_is_source_keyed_immutable_evidence(self):
        start = timezone.now() + timedelta(hours=1)
        shift, created = create_shift_assignment(
            staff=self.worker, actor=self.manager, planned_start_at=start, planned_end_at=start + timedelta(hours=8),
            idempotency_key="staff-shift-one",
        )
        self.assertTrue(created)
        with self.assertRaisesMessage(ValidationError, "overlapping"):
            create_shift_assignment(
                staff=self.worker, actor=self.manager, planned_start_at=start + timedelta(hours=2), planned_end_at=start + timedelta(hours=6),
                idempotency_key="staff-shift-overlap",
            )

        record, clocked_in = clock_attendance(
            staff=self.worker, actor=self.worker, action=AttendanceEvent.Type.CLOCK_IN,
            idempotency_key="clock-in-one", shift_reference=shift.reference,
        )
        self.assertTrue(clocked_in)
        self.assertEqual(record.status, AttendanceRecord.Status.ON_DUTY)
        clock_attendance(staff=self.worker, actor=self.worker, action=AttendanceEvent.Type.BREAK_STARTED, idempotency_key="break-start-one")
        record, _ = clock_attendance(staff=self.worker, actor=self.worker, action=AttendanceEvent.Type.BREAK_ENDED, idempotency_key="break-end-one")
        record, clocked_out = clock_attendance(staff=self.worker, actor=self.worker, action=AttendanceEvent.Type.CLOCK_OUT, idempotency_key="clock-out-one")
        self.assertTrue(clocked_out)
        self.assertEqual(record.status, AttendanceRecord.Status.COMPLETED)
        shift.refresh_from_db()
        self.assertEqual(shift.status, ShiftAssignment.Status.COMPLETED)
        self.assertEqual(AttendanceEvent.objects.filter(record=record).count(), 4)
        repeated, created = clock_attendance(
            staff=self.worker, actor=self.worker, action=AttendanceEvent.Type.CLOCK_OUT, idempotency_key="clock-out-one",
        )
        self.assertFalse(created)
        self.assertEqual(repeated.pk, record.pk)

    def test_service_layer_does_not_allow_manager_impersonated_clocking_or_worker_shift_management(self):
        start = timezone.now() + timedelta(hours=1)
        with self.assertRaises(PermissionDenied):
            create_shift_assignment(
                staff=self.worker, actor=self.worker, planned_start_at=start, planned_end_at=start + timedelta(hours=8),
                idempotency_key="worker-cannot-schedule",
            )
        with self.assertRaises(PermissionDenied):
            clock_attendance(
                staff=self.worker, actor=self.manager, action=AttendanceEvent.Type.CLOCK_IN,
                idempotency_key="manager-cannot-impersonate-clock",
            )

    def test_leave_is_independently_approved_and_blocks_overlapping_shift(self):
        profile, created = ensure_staff_profile(staff=self.worker)
        self.assertTrue(created)
        self.assertEqual(profile.user_id, self.worker.pk)
        tomorrow = timezone.localdate() + timedelta(days=1)
        leave_request, created = request_leave(
            staff=self.worker, leave_type=LeaveRequest.Type.ANNUAL, start_date=tomorrow,
            end_date=tomorrow + timedelta(days=1), reason="Family event", idempotency_key="leave-one", actor=self.worker,
        )
        self.assertTrue(created)
        approved = review_leave(leave_request=leave_request, reviewer=self.manager, approved=True, review_note="Approved")
        self.assertEqual(approved.status, LeaveRequest.Status.APPROVED)
        start = timezone.make_aware(timezone.datetime.combine(tomorrow, timezone.datetime.min.time())) + timedelta(hours=8)
        with self.assertRaisesMessage(ValidationError, "approved leave"):
            create_shift_assignment(
                staff=self.worker, actor=self.manager, planned_start_at=start, planned_end_at=start + timedelta(hours=8),
                idempotency_key="shift-during-leave",
            )

        manager_leave, _ = request_leave(
            staff=self.manager, leave_type=LeaveRequest.Type.PERSONAL, start_date=tomorrow + timedelta(days=3),
            end_date=tomorrow + timedelta(days=3), reason="Personal", idempotency_key="manager-leave", actor=self.manager,
        )
        with self.assertRaises(PermissionDenied):
            review_leave(leave_request=manager_leave, reviewer=self.manager, approved=True)

    def test_leave_approval_requires_existing_scheduled_shift_to_be_resolved_first(self):
        tomorrow = timezone.localdate() + timedelta(days=1)
        start = timezone.make_aware(timezone.datetime.combine(tomorrow, timezone.datetime.min.time())) + timedelta(hours=8)
        create_shift_assignment(
            staff=self.worker, actor=self.manager, planned_start_at=start, planned_end_at=start + timedelta(hours=8),
            idempotency_key="shift-before-leave-review",
        )
        leave_request, _ = request_leave(
            staff=self.worker, leave_type=LeaveRequest.Type.ANNUAL, start_date=tomorrow, end_date=tomorrow,
            reason="Conflict test", idempotency_key="leave-after-shift", actor=self.worker,
        )
        with self.assertRaisesMessage(ValidationError, "Scheduled shifts overlap"):
            review_leave(leave_request=leave_request, reviewer=self.manager, approved=True)
        leave_request.refresh_from_db()
        self.assertEqual(leave_request.status, LeaveRequest.Status.PENDING)
