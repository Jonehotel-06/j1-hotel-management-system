"""Facial verification for staff attendance: enrollment, clock gate, outcomes, retention.

Scope: descriptor matching against an approved enrollment. No liveness claim is
made or tested here. Descriptors are synthetic numeric vectors, not face images.
"""
from datetime import timedelta
from unittest import mock

from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.staff_operations.face_models import FaceTemplate, FaceVerificationAttempt
from apps.staff_operations.models import AttendanceRecord
from apps.staff_operations.services import face_service

from .base import BaseAPITestCase
from .factories import make_staff

CLOCK = "/api/admin/staff-operations/attendance/clock/"
ENROLL = "/api/admin/staff-operations/face/enrollment/"
QUEUE = "/api/admin/staff-operations/face/enrollments/"

ENROLLED = [0.1] * 128
SAME_FACE = [0.1 + 0.001] * 128          # distance ~0.011
OTHER_FACE = [0.9] * 128                 # distance ~9.05


def _action(template_id, action):
    return f"/api/admin/staff-operations/face/enrollments/{template_id}/{action}/"


class FaceEnrollmentWorkflowTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.worker = make_staff("face-worker@staff.dev", role=User.Role.HOUSEKEEPING)
        self.manager = make_staff("face-manager@staff.dev", role=User.Role.MANAGER)
        self.admin = make_staff("face-admin@staff.dev", role=User.Role.ADMIN)

    def _enroll_and_approve(self, user=None, descriptor=ENROLLED, approver=None):
        user = user or self.worker
        self.auth(user)
        response = self.client.post(ENROLL, {"descriptor": descriptor}, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertNotIn("descriptor", response.content.decode())
        self.auth(approver or self.manager)
        approved = self.client.post(_action(response.data["data"]["id"], "approve"), {}, format="json")
        self.assertEqual(approved.status_code, 200, approved.content)
        return FaceTemplate.objects.get(pk=response.data["data"]["id"])

    def test_staff_request_is_pending_and_response_carries_no_biometric_values(self):
        self.auth(self.worker)
        response = self.client.post(ENROLL, {"descriptor": ENROLLED}, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["data"]["status"], "PENDING")
        self.assertNotIn("0.1", response.content.decode())
        status_response = self.client.get(ENROLL)
        self.assertFalse(status_response.data["data"]["enrolled"])

    def test_descriptor_validation_rejects_wrong_shape_and_types(self):
        self.auth(self.worker)
        bad_values = [
            [0.1] * 127, "not a list", [True] * 128, ["x"] * 128, [50.0] * 128,
        ]
        for value in bad_values:
            response = self.client.post(ENROLL, {"descriptor": value}, format="json")
            self.assertEqual(response.status_code, 400, value if not isinstance(value, list) else value[:2])
        self.assertFalse(FaceTemplate.objects.exists())

    def test_non_finite_descriptor_values_are_rejected_by_the_service(self):
        from django.core.exceptions import ValidationError as DjangoValidationError
        with self.assertRaises(DjangoValidationError):
            face_service.validate_descriptor([float("nan")] * 128)

    def test_staff_cannot_enrol_against_another_account_and_attempt_is_audited(self):
        with self.assertRaises(PermissionDenied):
            face_service.request_enrollment(staff=self.worker, actor=self.manager, descriptor=ENROLLED)
        self.assertFalse(FaceTemplate.objects.exists())
        self.assertTrue(AuditLog.objects.filter(action="FACE_ENROLLMENT_DENIED").exists())

    def test_self_approval_is_refused(self):
        self.auth(self.manager)
        response = self.client.post(ENROLL, {"descriptor": ENROLLED}, format="json")
        template_id = response.data["data"]["id"]
        refused = self.client.post(_action(template_id, "approve"), {}, format="json")
        self.assertEqual(refused.status_code, 403)
        self.assertEqual(FaceTemplate.objects.get(pk=template_id).status, "PENDING")

    def test_worker_without_manage_capability_cannot_approve(self):
        self.auth(self.worker)
        response = self.client.post(ENROLL, {"descriptor": ENROLLED}, format="json")
        self.auth(self.worker)
        refused = self.client.post(_action(response.data["data"]["id"], "approve"), {}, format="json")
        self.assertEqual(refused.status_code, 403)

    def test_approval_sets_retention_expiry_and_is_audited(self):
        template = self._enroll_and_approve()
        self.assertEqual(template.status, "ACTIVE")
        self.assertIsNotNone(template.expires_at)
        self.assertEqual(template.decided_by_id, self.manager.pk)
        self.assertTrue(AuditLog.objects.filter(action="FACE_ENROLLMENT_APPROVED").exists())

    def test_new_approval_revokes_previous_and_database_allows_only_one_active(self):
        first = self._enroll_and_approve()
        second = self._enroll_and_approve(descriptor=OTHER_FACE, approver=self.admin)
        first.refresh_from_db()
        self.assertEqual(first.status, "REVOKED")
        self.assertEqual(first.descriptor, [])
        self.assertEqual(second.status, "ACTIVE")
        with self.assertRaises(IntegrityError), transaction.atomic():
            FaceTemplate.objects.create(staff=self.worker, status="ACTIVE", descriptor=ENROLLED)

    def test_second_pending_request_supersedes_first_pending(self):
        self.auth(self.worker)
        first = self.client.post(ENROLL, {"descriptor": ENROLLED}, format="json").data["data"]["id"]
        second = self.client.post(ENROLL, {"descriptor": SAME_FACE}, format="json").data["data"]["id"]
        self.assertEqual(FaceTemplate.objects.get(pk=first).status, "REJECTED")
        self.assertEqual(FaceTemplate.objects.get(pk=first).descriptor, [])
        self.assertEqual(FaceTemplate.objects.get(pk=second).status, "PENDING")

    def test_reject_and_revoke_clear_the_descriptor(self):
        self.auth(self.worker)
        pending = self.client.post(ENROLL, {"descriptor": ENROLLED}, format="json").data["data"]["id"]
        self.auth(self.manager)
        self.assertEqual(self.client.post(_action(pending, "reject"), {"note": "blurred"}, format="json").status_code, 200)
        rejected = FaceTemplate.objects.get(pk=pending)
        self.assertEqual((rejected.status, rejected.descriptor), ("REJECTED", []))
        active = self._enroll_and_approve()
        self.assertEqual(self.client.post(_action(active.pk, "revoke"), {}, format="json").status_code, 200)
        active.refresh_from_db()
        self.assertEqual((active.status, active.descriptor), ("REVOKED", []))

    def test_review_queue_requires_manage_capability_and_hides_descriptors(self):
        self.auth(self.worker)
        self.client.post(ENROLL, {"descriptor": ENROLLED}, format="json")
        self.assertEqual(self.client.get(QUEUE).status_code, 403)
        self.auth(self.manager)
        response = self.client.get(QUEUE, {"status": "pending"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["data"]), 1)
        self.assertNotIn("descriptor", response.content.decode())


class FaceClockGateTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.worker = make_staff("face-clock@staff.dev", role=User.Role.HOUSEKEEPING)
        self.manager = make_staff("face-clock-mgr@staff.dev", role=User.Role.MANAGER)
        self.admin = make_staff("face-clock-admin@staff.dev", role=User.Role.ADMIN)
        self.template = self._active_template(ENROLLED)

    def _active_template(self, descriptor):
        t = FaceTemplate.objects.create(staff=self.worker, status="PENDING", descriptor=descriptor)
        face_service.approve_enrollment(template=t, approver=self.manager)
        return FaceTemplate.objects.get(pk=t.pk)

    def _clock(self, action="CLOCK_IN", key="k1", **extra):
        self.auth(self.worker)
        return self.client.post(CLOCK, {"action": action, "idempotency_key": key, **extra}, format="json")

    def test_matching_probe_allows_clock_in_and_records_verified_attempt(self):
        response = self._clock(face_probe=SAME_FACE, idempotency_key="match-1")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(AttendanceRecord.objects.filter(staff=self.worker).count(), 1)
        attempt = FaceVerificationAttempt.objects.get(staff=self.worker)
        self.assertEqual(attempt.outcome, "VERIFIED")
        self.assertIsNotNone(attempt.distance)

    def test_non_matching_probe_refuses_and_writes_no_attendance(self):
        response = self._clock(face_probe=OTHER_FACE, idempotency_key="nomatch-1")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.data["code"], "FACE_VERIFICATION_FAILED")
        self.assertIn("NO_MATCH", response.data["message"])
        self.assertFalse(AttendanceRecord.objects.filter(staff=self.worker).exists())
        self.assertEqual(FaceVerificationAttempt.objects.get(staff=self.worker).outcome, "NO_MATCH")
        self.assertNotIn("0.9", response.content.decode())

    def test_missing_enrollment_refuses_with_not_enrolled(self):
        self.template.delete()
        response = self._clock(face_probe=SAME_FACE, idempotency_key="none-1")
        self.assertEqual(response.status_code, 422)
        self.assertIn("NOT_ENROLLED", response.data["message"])
        self.assertFalse(AttendanceRecord.objects.exists())

    def test_expired_template_refuses_and_is_expired_by_the_check(self):
        FaceTemplate.objects.filter(pk=self.template.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        response = self._clock(face_probe=SAME_FACE, idempotency_key="exp-1")
        self.assertEqual(response.status_code, 422)
        self.assertIn("TEMPLATE_EXPIRED", response.data["message"])
        self.template.refresh_from_db()
        self.assertEqual((self.template.status, self.template.descriptor), ("EXPIRED", []))

    def test_malformed_probe_refuses_as_probe_invalid(self):
        response = self._clock(face_probe=[1, 2, 3], idempotency_key="bad-1")
        self.assertEqual(response.status_code, 422)
        self.assertIn("PROBE_INVALID", response.data["message"])
        self.assertFalse(AttendanceRecord.objects.exists())

    @override_settings(STAFF_FACE_VERIFICATION_REQUIRED=True)
    def test_required_mode_refuses_clock_without_probe(self):
        response = self._clock(idempotency_key="req-1")
        self.assertEqual(response.status_code, 422)
        self.assertIn("FACE_REQUIRED", response.data["message"])
        self.assertFalse(AttendanceRecord.objects.exists())

    def test_default_mode_keeps_existing_clock_behaviour_without_probe(self):
        response = self._clock(idempotency_key="legacy-1")
        self.assertEqual(response.status_code, 201)
        self.assertFalse(FaceVerificationAttempt.objects.exists())

    def test_repeated_request_with_same_key_returns_recorded_outcome_without_rematching(self):
        first = self._clock(face_probe=SAME_FACE, idempotency_key="idem-1")
        self.assertEqual(first.status_code, 201)
        with mock.patch.object(face_service, "distance", side_effect=AssertionError("must not re-match")):
            second = self._clock(face_probe=OTHER_FACE, idempotency_key="idem-1")
        self.assertEqual(second.status_code, 200)
        self.assertEqual(FaceVerificationAttempt.objects.count(), 1)
        self.assertEqual(AttendanceRecord.objects.count(), 1)

    def test_clock_out_is_verified_the_same_way(self):
        self.assertEqual(self._clock(face_probe=SAME_FACE, idempotency_key="in-1").status_code, 201)
        refused = self._clock(action="CLOCK_OUT", face_probe=OTHER_FACE, idempotency_key="out-1")
        self.assertEqual(refused.status_code, 422)
        self.assertIsNone(AttendanceRecord.objects.get(staff=self.worker).clock_out_at)
        allowed = self._clock(action="CLOCK_OUT", face_probe=SAME_FACE, idempotency_key="out-2")
        self.assertEqual(allowed.status_code, 201, allowed.content)

    def test_manual_fallback_requires_supervisor_own_shift_and_reason(self):
        self.auth(self.worker)
        denied = self.client.post(CLOCK, {"action": "CLOCK_IN", "idempotency_key": "man-w",
                                          "manual_override_reason": "Camera broken at desk"}, format="json")
        self.assertEqual(denied.status_code, 403)
        self.assertFalse(AttendanceRecord.objects.exists())

        self.auth(self.manager)
        short = self.client.post(CLOCK, {"action": "CLOCK_IN", "idempotency_key": "man-s",
                                         "manual_override_reason": "x"}, format="json")
        self.assertEqual(short.status_code, 400)

    def test_supervisor_manual_fallback_is_recorded_and_audited(self):
        self.auth(self.manager)
        response = self.client.post(CLOCK, {"action": "CLOCK_IN", "idempotency_key": "man-ok",
                                            "manual_override_reason": "Camera unavailable at the front desk"}, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(FaceVerificationAttempt.objects.get().outcome, "MANUAL_OVERRIDE")
        self.assertTrue(AuditLog.objects.filter(action="ATTENDANCE_MANUAL_OVERRIDE").exists())

    def test_refused_attempt_is_audited_without_biometric_content(self):
        self._clock(face_probe=OTHER_FACE, idempotency_key="audit-1")
        row = AuditLog.objects.filter(action="ATTENDANCE_FACE_REFUSED").first()
        self.assertIsNotNone(row)
        self.assertNotIn("0.9", str(row.metadata))


class FaceRetentionTests(BaseAPITestCase):
    def test_retention_command_expires_due_templates_and_deletes_old_attempts(self):
        worker = make_staff("face-retain@staff.dev", role=User.Role.HOUSEKEEPING)
        due = FaceTemplate.objects.create(staff=worker, status="ACTIVE", descriptor=[0.1] * 128,
                                          expires_at=timezone.now() - timedelta(days=1))
        old = FaceVerificationAttempt.objects.create(staff=worker, action="CLOCK_IN", outcome="NO_MATCH", source_key="old-1")
        FaceVerificationAttempt.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=400))
        recent = FaceVerificationAttempt.objects.create(staff=worker, action="CLOCK_IN", outcome="VERIFIED", source_key="new-1")
        call_command("purge_face_data", stdout=mock.Mock())
        due.refresh_from_db()
        self.assertEqual((due.status, due.descriptor), ("EXPIRED", []))
        self.assertFalse(FaceVerificationAttempt.objects.filter(pk=old.pk).exists())
        self.assertTrue(FaceVerificationAttempt.objects.filter(pk=recent.pk).exists())
