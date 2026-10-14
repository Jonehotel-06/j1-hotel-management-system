"""Attendance policy: Receptionist Desktop enforcement, code lifecycle, fallback.

Covers items 4–7 of the production audit:

4. Receptionist Desktop is the sole attendance station — enforced server side
   (never from a user agent, an IP address or a screen size).
5. Clock-in issues a backend-minted, attendance-bound, single-use numeric code.
6. A manager fallback code needs a recorded capture/hardware failure; an
   administrator may authorise one without a prior attempt; ordinary employees
   can never issue one.
7. Clock-out invalidates the code while the attendance history is preserved.
"""
from datetime import timedelta

from django.utils import timezone

from apps.accounts.desktop_policy import issue_desktop_key
from apps.accounts.models import User, Workstation
from apps.audit.models import AuditLog
from apps.staff_operations.biometric_models import (
    FingerprintTemplate,
    FingerprintVerificationAttempt,
    TemporaryWorkstationCredential,
)
from apps.staff_operations.models import AttendanceRecord
from apps.staff_operations.services import fingerprint_service

from .base import BaseAPITestCase
from .factories import make_staff

CLOCK_URL = "/api/admin/staff-operations/attendance/clock/"
TERMINAL_URL = "/api/admin/staff-operations/biometrics/reception-terminal/"
CREDENTIAL_URL = "/api/admin/staff-operations/biometrics/temporary-credential/"
LOGIN_URL = "/api/auth/login/"

MINUTIAE = [
    {"x": 100 + i * 5, "y": 150 + i * 4, "angle": (30 + i * 15) % 360, "type": "ending", "quality": 90}
    for i in range(15)
]
MATCHING_PROBE = [
    {"x": 100 + i * 5 + 1.0, "y": 150 + i * 4 + 1.0, "angle": (30 + i * 15) % 360, "type": "ending", "quality": 85}
    for i in range(15)
]
MISMATCHING_PROBE = [
    {"x": 500 + i * 10, "y": 800 + i * 10, "angle": (180 + i * 20) % 360, "type": "bifurcation", "quality": 75}
    for i in range(15)
]


class WorkstationPolicyTestCase(BaseAPITestCase):
    """Shared fixtures: an enrolled worker, a desk key and the management roles."""

    def setUp(self):
        super().setUp()
        self.worker = make_staff("policy.worker@staff.dev", role=User.Role.HOUSEKEEPING)
        self.manager = make_staff("policy.manager@staff.dev", role=User.Role.MANAGER)
        self.admin = make_staff("policy.admin@staff.dev", role=User.Role.ADMIN)
        self.workstation = Workstation.objects.create(
            name="Front Desk Terminal", department=Workstation.Department.FRONT_DESK
        )
        self.desk_key = issue_desktop_key(self.workstation)
        self.workstation.save()
        self.desk_headers = {"HTTP_X_JONE_DESKTOP_KEY": self.desk_key}

        template = fingerprint_service.request_fingerprint_enrollment(
            staff=self.worker, actor=self.worker, minutiae=MINUTIAE
        )
        fingerprint_service.approve_fingerprint_enrollment(template=template, approver=self.manager)

    def terminal(self, payload, headers=None, user=None):
        if user is not None:
            self.auth(user)
        else:
            self.unauth()
        return self.client.post(TERMINAL_URL, payload, format="json", **(headers if headers is not None else self.desk_headers))

    def clock_in_at_desk(self, staff=None):
        staff = staff or self.worker
        email = staff.email
        response = self.terminal({"action": "CLOCK_IN", "email": email, "fingerprint_probe": MATCHING_PROBE})
        self.assertEqual(response.status_code, 200, response.content)
        return response.data["data"]


class ReceptionistDesktopEnforcementTests(WorkstationPolicyTestCase):
    """Item 4: the desk key — not a device fingerprint — is the gate."""

    def test_staff_cannot_clock_from_a_personal_device(self):
        self.auth(self.worker)
        response = self.client.post(CLOCK_URL, {
            "action": "CLOCK_IN", "idempotency_key": "personal-device-1",
        }, format="json")
        self.assertEqual(response.status_code, 403, response.content)
        self.assertIn("Receptionist Desktop", response.data["message"])
        self.assertFalse(AttendanceRecord.objects.filter(staff=self.worker).exists())

    def test_staff_can_clock_when_the_request_carries_the_desk_key(self):
        self.auth(self.worker)
        response = self.client.post(CLOCK_URL, {
            "action": "CLOCK_IN", "idempotency_key": "desk-device-1",
        }, format="json", **self.desk_headers)
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.data["data"]["status"], "ON_DUTY")

    def test_management_keeps_its_existing_device_freedom(self):
        self.auth(self.manager)
        response = self.client.post(CLOCK_URL, {
            "action": "CLOCK_IN", "idempotency_key": "manager-device-1",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.content)

    def test_staff_session_cannot_drive_the_reception_terminal(self):
        response = self.terminal({
            "action": "VERIFY_STATUS", "email": self.worker.email, "fingerprint_probe": MATCHING_PROBE,
        }, headers={}, user=self.worker)
        self.assertEqual(response.status_code, 403, response.content)
        self.assertIn("Receptionist Desktop", response.data["message"])

    def test_terminal_never_invents_a_workstation_reference(self):
        Workstation.objects.all().delete()
        response = self.terminal({"action": "VERIFY_STATUS", "email": self.worker.email}, headers={}, user=self.manager)
        self.assertEqual(response.status_code, 403, response.content)
        self.assertIn("No active Receptionist Desktop workstation", response.data["message"])

    def test_revoked_desk_key_stops_working(self):
        self.workstation.sign_in_key_hash = ""
        self.workstation.save(update_fields=["sign_in_key_hash"])
        response = self.terminal({"action": "VERIFY_STATUS", "email": self.worker.email})
        self.assertEqual(response.status_code, 403)


class AccessCodeLifecycleTests(WorkstationPolicyTestCase):
    """Items 5 and 7: one code per attendance session, dead on clock-out."""

    def test_code_is_not_issued_before_the_employee_has_clocked_in(self):
        self.unauth()
        response = self.client.post(CREDENTIAL_URL, {
            "email": self.worker.email, "fingerprint_probe": MATCHING_PROBE,
        }, format="json", **self.desk_headers)
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("Clock in", str(response.data))
        self.assertFalse(TemporaryWorkstationCredential.objects.filter(staff=self.worker).exists())

    def test_clock_in_issues_a_bound_single_use_code(self):
        data = self.clock_in_at_desk()
        code = data["access_code"]
        self.assertTrue(code and code.isdigit() and len(code) == 6)
        credential = TemporaryWorkstationCredential.objects.get(staff=self.worker)
        self.assertEqual(credential.attendance_record.reference, data["attendance_reference"])
        self.assertTrue(credential.session_active)
        self.assertNotIn(code, str(credential.numeric_code_hash))
        self.assertNotIn(code, AuditLog.objects.filter(action="TEMPORARY_WORKSTATION_CREDENTIAL_ISSUED").first().metadata)

        # The code signs the employee in exactly once...
        first = self.client.post(LOGIN_URL, {"email": self.worker.email, "access_code": code}, format="json")
        self.assertEqual(first.status_code, 200, first.content)
        credential.refresh_from_db()
        self.assertTrue(credential.is_used)
        # ...and never twice.
        second = self.client.post(LOGIN_URL, {"email": self.worker.email, "access_code": code}, format="json")
        self.assertIn(second.status_code, (401, 403), second.content)
        self.assertIn("already been used", second.data["message"])

    def test_code_cannot_be_used_by_another_employee(self):
        data = self.clock_in_at_desk()
        other = make_staff("policy.other@staff.dev", role=User.Role.CHEF)
        response = self.client.post(LOGIN_URL, {"email": other.email, "access_code": data["access_code"]}, format="json")
        self.assertIn(response.status_code, (401, 403), response.content)
        self.assertIn("different employee", response.data["message"])
        self.assertTrue(AuditLog.objects.filter(action="WORKSTATION_CODE_SHARING_BLOCKED").exists())

    def test_clock_out_invalidates_the_code_and_keeps_the_history(self):
        data = self.clock_in_at_desk()
        code = data["access_code"]
        out = self.terminal({"action": "CLOCK_OUT", "email": self.worker.email, "fingerprint_probe": MATCHING_PROBE})
        self.assertEqual(out.status_code, 200, out.content)
        self.assertEqual(out.data["data"]["status"], "CLOCKED_OUT")

        credential = TemporaryWorkstationCredential.objects.get(staff=self.worker)
        self.assertFalse(credential.session_active)
        self.assertIsNotNone(credential.revoked_at)
        self.assertEqual(credential.revoked_reason, "CLOCKED_OUT")

        record = AttendanceRecord.objects.get(staff=self.worker)
        self.assertIsNotNone(record.clock_out_at)
        self.assertTrue(record.events.filter(type="CLOCK_OUT").exists(), "attendance history is preserved")

        refused = self.client.post(LOGIN_URL, {"email": self.worker.email, "access_code": code}, format="json")
        self.assertIn(refused.status_code, (401, 403), refused.content)

    def test_expired_code_is_refused(self):
        data = self.clock_in_at_desk()
        credential = TemporaryWorkstationCredential.objects.get(staff=self.worker)
        credential.expires_at = timezone.now() - timedelta(minutes=1)
        credential.save(update_fields=["expires_at"])
        response = self.client.post(LOGIN_URL, {"email": self.worker.email, "access_code": data["access_code"]}, format="json")
        self.assertIn(response.status_code, (401, 403), response.content)
        self.assertIn("expired", response.data["message"])


class SupervisedFallbackPolicyTests(WorkstationPolicyTestCase):
    """Item 6: who may fall back, on what evidence, with what trail."""

    def fallback(self, user, reason="Scanner at the front desk could not read the finger.", staff=None, headers=None):
        staff = staff or self.worker
        payload = {"action": "MANUAL_FALLBACK", "email": staff.email, "manual_override_reason": reason}
        return self.terminal(payload, headers=headers if headers is not None else {}, user=user)

    def record_capture_failure(self, classification="DEVICE_UNAVAILABLE"):
        """The terminal reports that the reader could not produce a probe."""
        response = self.terminal({
            "action": "RECORD_FAILURE",
            "email": self.worker.email,
            "failure_classification": classification,
        })
        self.assertEqual(response.status_code, 200, response.content)
        return response

    def test_ordinary_employee_can_never_issue_a_fallback(self):
        self.record_capture_failure()
        response = self.fallback(self.worker)
        self.assertEqual(response.status_code, 403, response.content)
        self.assertFalse(TemporaryWorkstationCredential.objects.exists())
        self.assertFalse(AttendanceRecord.objects.exists())

    def test_manager_fallback_without_recorded_failure_is_refused(self):
        response = self.fallback(self.manager)
        self.assertEqual(response.status_code, 403, response.content)
        self.assertIn("recorded capture failure", response.data["message"])
        self.assertTrue(AuditLog.objects.filter(action="ATTENDANCE_MANUAL_FALLBACK_REFUSED").exists())
        self.assertFalse(TemporaryWorkstationCredential.objects.exists())
        self.assertFalse(AttendanceRecord.objects.exists())

    def test_a_mismatch_never_qualifies_a_manager_fallback(self):
        """A wrong finger is not a hardware failure (audit N7)."""
        bad_scan = self.terminal({
            "action": "VERIFY_STATUS", "email": self.worker.email, "fingerprint_probe": MISMATCHING_PROBE,
        })
        self.assertEqual(bad_scan.status_code, 403, bad_scan.content)
        self.assertTrue(
            FingerprintVerificationAttempt.objects.filter(staff=self.worker, outcome="NO_MATCH").exists()
        )
        response = self.fallback(self.manager)
        self.assertEqual(response.status_code, 403, response.content)
        self.assertFalse(TemporaryWorkstationCredential.objects.exists())

    def test_short_reason_is_refused(self):
        self.record_capture_failure()
        response = self.fallback(self.manager, reason="nope")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("manual_override_reason", response.data.get("errors", {}))
        self.assertFalse(TemporaryWorkstationCredential.objects.exists())

    def test_manager_fallback_with_recorded_failure_issues_a_code(self):
        self.record_capture_failure("DEVICE_UNAVAILABLE")
        response = self.fallback(self.manager)
        self.assertEqual(response.status_code, 200, response.content)
        data = response.data["data"]
        self.assertTrue(data["access_code"].isdigit())
        self.assertEqual(data["authority"], "MANAGER")
        self.assertEqual(data["evidence"], "VERIFICATION_ATTEMPT")
        self.assertEqual(data["status"], "CODE_ISSUED")

        record = AttendanceRecord.objects.get(staff=self.worker)
        self.assertEqual(record.reference, data["attendance_reference"])
        credential = TemporaryWorkstationCredential.objects.get(staff=self.worker)
        self.assertEqual(credential.attendance_record_id, record.pk)
        self.assertTrue(credential.session_active)

        override = AuditLog.objects.filter(action="ATTENDANCE_MANUAL_OVERRIDE").first()
        self.assertIsNotNone(override)
        self.assertEqual(override.actor, self.manager)
        self.assertEqual(override.metadata["authority"], "MANAGER")
        self.assertEqual(override.metadata["staff_id"], self.worker.pk)
        self.assertNotIn(data["access_code"], str(override.metadata))
        self.assertTrue(
            FingerprintVerificationAttempt.objects.filter(staff=self.worker, outcome="MANUAL_OVERRIDE").exists()
        )

        # The fallback code is a real workstation code: it signs the employee in.
        signed_in = self.client.post(LOGIN_URL, {"email": self.worker.email, "access_code": data["access_code"]}, format="json")
        self.assertEqual(signed_in.status_code, 200, signed_in.content)

    def test_administrator_may_fall_back_without_a_prior_attempt(self):
        response = self.fallback(self.admin, reason="Administrator exception: reader physically absent.")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data["data"]["authority"], "ADMINISTRATOR")
        self.assertEqual(response.data["data"]["evidence"], "ADMINISTRATOR_EXCEPTION")
        override = AuditLog.objects.filter(action="ATTENDANCE_MANUAL_OVERRIDE").first()
        self.assertEqual(override.metadata["authority"], "ADMINISTRATOR")

    def test_repeated_fallback_does_not_duplicate_attendance_or_codes(self):
        self.record_capture_failure()
        first = self.fallback(self.manager)
        self.assertEqual(first.status_code, 200, first.content)
        second = self.fallback(self.manager)
        self.assertEqual(second.status_code, 200, second.content)

        self.assertEqual(AttendanceRecord.objects.filter(staff=self.worker).count(), 1)
        self.assertEqual(TemporaryWorkstationCredential.objects.filter(staff=self.worker, session_active=True).count(), 1)
        first_credential = TemporaryWorkstationCredential.objects.order_by("created_at").first()
        self.assertFalse(first_credential.session_active, "the previous code is superseded, not shared")
        self.assertEqual(first_credential.revoked_reason, "SUPERSEDED")

    def test_failure_classification_must_be_from_the_allowlist(self):
        response = self.terminal({
            "action": "RECORD_FAILURE", "email": self.worker.email, "failure_classification": "FINGER_TOO_DRY",
        })
        self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(FingerprintVerificationAttempt.objects.filter(action="ATTEMPT_FAILED").exists())

    def test_recorded_failure_is_staff_specific(self):
        """Another employee's failure must not open a fallback for this one."""
        other = make_staff("policy.other-fallback@staff.dev", role=User.Role.CHEF)
        self.terminal({
            "action": "RECORD_FAILURE", "email": other.email, "failure_classification": "DEVICE_UNAVAILABLE",
        })
        response = self.fallback(self.manager)
        self.assertEqual(response.status_code, 403, response.content)

    def test_expired_evidence_does_not_qualify(self):
        response = self.record_capture_failure()
        attempt = FingerprintVerificationAttempt.objects.get(staff=self.worker, action="ATTEMPT_FAILED")
        FingerprintVerificationAttempt.objects.filter(pk=attempt.pk).update(
            created_at=timezone.now() - timedelta(hours=6)
        )
        refused = self.fallback(self.manager)
        self.assertEqual(refused.status_code, 403, refused.content)


class FallbackPermissionMatrixTests(WorkstationPolicyTestCase):
    """The workstation gate and the authority gate are separate layers."""

    def test_staff_session_without_desk_key_cannot_even_reach_the_authority_check(self):
        self.terminal({
            "action": "RECORD_FAILURE", "email": self.worker.email,
            "failure_classification": "DEVICE_UNAVAILABLE",
        })
        response = self.terminal({
            "action": "MANUAL_FALLBACK", "email": self.worker.email,
            "manual_override_reason": "Front desk scanner is unserviceable today.",
        }, headers={}, user=self.worker)
        self.assertEqual(response.status_code, 403, response.content)
        self.assertIn("Receptionist Desktop", response.data["message"])
        self.assertFalse(TemporaryWorkstationCredential.objects.exists())
