"""Attendance policy: Receptionist Desktop enforcement, code lifecycle, fallback.

Covers items 4–7 of the production audit:

4. Receptionist Desktop is the sole attendance station — enforced server side
   (never from a user agent, an IP address or a screen size).
5. Clock-in issues a backend-minted, attendance-bound numeric code that stays
   valid for repeated sign-ins until clock-out (logout never consumes it).
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
ATTENDANCE_URL = "/api/admin/staff-operations/attendance/"
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

    def test_issuing_the_first_code_opens_the_attendance_record_once(self):
        """Item F: identifying the employee IS the clock-in — no prior clock-in needed."""
        self.unauth()
        response = self.client.post(CREDENTIAL_URL, {
            "email": self.worker.email, "fingerprint_probe": MATCHING_PROBE,
        }, format="json", **self.desk_headers)
        self.assertEqual(response.status_code, 200, response.content)
        data = response.data["data"]
        self.assertTrue(data["attendance_created"], response.content)
        record = AttendanceRecord.objects.get(staff=self.worker)
        self.assertIsNone(record.clock_out_at)
        self.assertEqual(record.status, AttendanceRecord.Status.ON_DUTY)
        self.assertEqual(record.reference, data["attendance_reference"])
        self.assertEqual(TemporaryWorkstationCredential.objects.get(staff=self.worker).attendance_record_id, record.pk)

        # Issuing again reuses that open record and never opens a second one.
        again = self.client.post(CREDENTIAL_URL, {
            "email": self.worker.email, "fingerprint_probe": MATCHING_PROBE,
        }, format="json", **self.desk_headers)
        self.assertEqual(again.status_code, 200, again.content)
        self.assertFalse(again.data["data"]["attendance_created"], again.content)
        self.assertEqual(AttendanceRecord.objects.filter(staff=self.worker).count(), 1)
        self.assertEqual(
            AttendanceRecord.objects.filter(staff=self.worker, clock_out_at__isnull=True).count(), 1
        )

    def test_a_failed_probe_never_creates_attendance(self):
        """Item F: the record is opened only AFTER the finger matched."""
        self.unauth()
        response = self.client.post(CREDENTIAL_URL, {
            "email": self.worker.email, "fingerprint_probe": MISMATCHING_PROBE,
        }, format="json", **self.desk_headers)
        self.assertEqual(response.status_code, 403, response.content)
        self.assertFalse(AttendanceRecord.objects.filter(staff=self.worker).exists())
        self.assertFalse(TemporaryWorkstationCredential.objects.exists())

    def test_clock_in_issues_a_bound_code_that_signs_in_again_until_clock_out(self):
        data = self.clock_in_at_desk()
        code = data["access_code"]
        self.assertTrue(code and code.isdigit() and len(code) == 6)
        credential = TemporaryWorkstationCredential.objects.get(staff=self.worker)
        self.assertEqual(credential.attendance_record.reference, data["attendance_reference"])
        self.assertTrue(credential.session_active)
        self.assertNotIn(code, str(credential.numeric_code_hash))
        self.assertNotIn(code, AuditLog.objects.filter(action="TEMPORARY_WORKSTATION_CREDENTIAL_ISSUED").first().metadata)

        # The code signs the employee in...
        first = self.client.post(LOGIN_URL, {"email": self.worker.email, "access_code": code}, format="json")
        self.assertEqual(first.status_code, 200, first.content)
        credential.refresh_from_db()
        self.assertTrue(credential.is_used, "first use is kept as audit evidence")
        # ...and keeps working for the rest of the attendance session. Logging
        # out of the application never consumes the attendance credential; only
        # clock-out does (test_clock_out_invalidates_the_code_and_keeps_the_history).
        second = self.client.post(LOGIN_URL, {"email": self.worker.email, "access_code": code}, format="json")
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(AttendanceRecord.objects.filter(staff=self.worker).count(), 1)
        self.assertEqual(TemporaryWorkstationCredential.objects.filter(staff=self.worker).count(), 1)

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

    def test_expired_unbound_code_is_refused(self):
        """Compatibility rule: a credential with NO attendance binding keeps its
        original short TTL. Attendance-bound codes live until clock-out (see
        tests/test_attendance_code_persistence), but an old unbound row must not
        gain a longer life from that change."""
        data = self.clock_in_at_desk()
        credential = TemporaryWorkstationCredential.objects.get(staff=self.worker)
        TemporaryWorkstationCredential.objects.filter(pk=credential.pk).update(
            attendance_record=None, expires_at=timezone.now() - timedelta(minutes=1)
        )
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


class AutoClockInOnIdentificationTests(WorkstationPolicyTestCase):
    """Item F: identifying a staff member IS their clock-in; one open record only.

    Every terminal action that names a staff member (biometric CLOCK_IN, code
    issuance, supervised fallback) must leave exactly one open attendance
    record — the existing one reused, a new one opened only when there is none —
    and the code stays bound to that record so clock-out expires it. The
    personal Clock in control is then refused (and disabled in the UI) until the
    staff member clocks out. No probe means no clock-in, ever.
    """

    def test_repeated_terminal_clock_in_keeps_one_open_record(self):
        first = self.clock_in_at_desk()
        self.assertTrue(first["created"], first)
        second = self.terminal({
            "action": "CLOCK_IN", "email": self.worker.email, "fingerprint_probe": MATCHING_PROBE,
        })
        self.assertEqual(second.status_code, 200, second.content)
        self.assertFalse(second.data["data"]["created"], second.content)
        self.assertEqual(second.data["data"]["attendance_reference"], first["attendance_reference"])
        self.assertEqual(AttendanceRecord.objects.filter(staff=self.worker).count(), 1)
        self.assertEqual(AttendanceRecord.objects.filter(staff=self.worker, clock_out_at__isnull=True).count(), 1)

    def test_code_issuance_opens_the_record_and_binds_the_code_to_it(self):
        response = self.terminal({
            "action": "ISSUE_CODE", "email": self.worker.email, "fingerprint_probe": MATCHING_PROBE,
        })
        self.assertEqual(response.status_code, 200, response.content)
        data = response.data["data"]
        self.assertTrue(data["created"], response.content)
        record = AttendanceRecord.objects.get(staff=self.worker)
        self.assertIsNone(record.clock_out_at)
        self.assertEqual(record.reference, data["attendance_reference"])
        self.assertEqual(TemporaryWorkstationCredential.objects.get(staff=self.worker).attendance_record_id, record.pk)

        again = self.terminal({
            "action": "ISSUE_CODE", "email": self.worker.email, "fingerprint_probe": MATCHING_PROBE,
        })
        self.assertEqual(again.status_code, 200, again.content)
        self.assertFalse(again.data["data"]["created"], again.content)
        self.assertEqual(AttendanceRecord.objects.filter(staff=self.worker).count(), 1)

    def test_code_is_refused_while_the_employee_is_on_break(self):
        """An issued code signs a workstation in; the on-break policy is unchanged."""
        self.clock_in_at_desk()
        started = self.terminal({
            "action": "BREAK_START", "email": self.worker.email, "fingerprint_probe": MATCHING_PROBE,
        })
        self.assertEqual(started.status_code, 200, started.content)
        refused = self.terminal({
            "action": "ISSUE_CODE", "email": self.worker.email, "fingerprint_probe": MATCHING_PROBE,
        })
        self.assertEqual(refused.status_code, 400, refused.content)
        self.assertIn("on active duty", str(refused.data))
        record = AttendanceRecord.objects.get(staff=self.worker)
        self.assertEqual(record.status, AttendanceRecord.Status.ON_BREAK)
        self.assertEqual(
            TemporaryWorkstationCredential.objects.filter(staff=self.worker).count(), 1,
            "a refused issuance mints nothing",
        )

    def test_fallback_opens_the_record_once_and_reports_it(self):
        failure = self.terminal({
            "action": "RECORD_FAILURE", "email": self.worker.email,
            "failure_classification": "DEVICE_UNAVAILABLE",
        })
        self.assertEqual(failure.status_code, 200, failure.content)
        first = self.terminal({
            "action": "MANUAL_FALLBACK", "email": self.worker.email,
            "manual_override_reason": "Scanner at the front desk could not read the finger.",
        }, headers={}, user=self.admin)
        self.assertEqual(first.status_code, 200, first.content)
        self.assertTrue(first.data["data"]["attendance_created"], first.content)
        self.assertEqual(AttendanceRecord.objects.filter(staff=self.worker, clock_out_at__isnull=True).count(), 1)

        second = self.terminal({
            "action": "MANUAL_FALLBACK", "email": self.worker.email,
            "manual_override_reason": "Scanner at the front desk still could not read the finger.",
        }, headers={}, user=self.admin)
        self.assertEqual(second.status_code, 200, second.content)
        self.assertFalse(second.data["data"]["attendance_created"], second.content)
        self.assertEqual(AttendanceRecord.objects.filter(staff=self.worker).count(), 1)

    def test_personal_clock_in_is_refused_until_the_staff_member_clocks_out(self):
        self.clock_in_at_desk()
        self.auth(self.worker)
        refused = self.client.post(CLOCK_URL, {
            "action": "CLOCK_IN", "idempotency_key": "after-terminal-1",
        }, format="json", **self.desk_headers)
        self.assertEqual(refused.status_code, 400, refused.content)
        self.assertIn("Clock out", str(refused.data))
        self.assertEqual(AttendanceRecord.objects.filter(staff=self.worker).count(), 1)

        out = self.terminal({
            "action": "CLOCK_OUT", "email": self.worker.email, "fingerprint_probe": MATCHING_PROBE,
        })
        self.assertEqual(out.status_code, 200, out.content)
        self.auth(self.worker)
        allowed = self.client.post(CLOCK_URL, {
            "action": "CLOCK_IN", "idempotency_key": "after-clock-out-1",
        }, format="json", **self.desk_headers)
        self.assertEqual(allowed.status_code, 201, allowed.content)
        self.assertEqual(AttendanceRecord.objects.filter(staff=self.worker).count(), 2)
        self.assertEqual(AttendanceRecord.objects.filter(staff=self.worker, clock_out_at__isnull=True).count(), 1)

    def test_open_filter_reports_exactly_the_record_the_button_must_respect(self):
        self.clock_in_at_desk()
        self.auth(self.worker)
        open_response = self.client.get(ATTENDANCE_URL, {"open": "true"})
        self.assertEqual(open_response.status_code, 200, open_response.content)
        self.assertEqual(len(open_response.data["data"]), 1)
        self.assertEqual(open_response.data["data"][0]["status"], "ON_DUTY")
        self.assertIsNone(open_response.data["data"][0]["clock_out_at"])

        self.terminal({"action": "CLOCK_OUT", "email": self.worker.email, "fingerprint_probe": MATCHING_PROBE})
        self.auth(self.worker)
        closed = self.client.get(ATTENDANCE_URL, {"open": "true"})
        self.assertEqual(closed.status_code, 200, closed.content)
        self.assertEqual(closed.data["data"], [], closed.content)

    def test_a_manager_reads_a_staff_members_open_record_through_the_same_filter(self):
        other = make_staff("policy.open-filter@staff.dev", role=User.Role.CHEF)
        self.auth(self.manager)
        response = self.client.get(ATTENDANCE_URL, {"open": "true", "staff_id": other.pk})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data["data"], [], response.content)

        self.auth(self.worker)
        self.client.post(CLOCK_URL, {
            "action": "CLOCK_IN", "idempotency_key": "open-filter-1",
        }, format="json", **self.desk_headers)
        self.auth(self.manager)
        mine = self.client.get(ATTENDANCE_URL, {"open": "true", "staff_id": self.worker.pk})
        self.assertEqual(mine.status_code, 200, mine.content)
        self.assertEqual(len(mine.data["data"]), 1, mine.content)
        self.assertEqual(mine.data["data"][0]["staff"], self.worker.pk)
