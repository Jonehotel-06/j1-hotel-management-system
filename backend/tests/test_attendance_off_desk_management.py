"""Attendance off the Receptionist Desktop: who may still record it.

The desk rule exists to stop a personal phone recording attendance for other
people. It was refusing the people who SUPERVISE attendance as well, which
turned a control into a lockout: a manager whose shift began at the desk could
not record anything from anywhere else, and an administrator could not open the
attendance terminal at all while the front desk PC was unenrolled, re-imaged or
moved.

The rule now has exactly one exemption, and it is not a new hole: the accounts
that may already authorise a supervised fallback for somebody else (see
``attendance_fallback_service.manual_fallback_authority``) may also operate the
terminal away from the desk. Everything else is unchanged — an operational
account still reaches the terminal only from the desk it signed in at, and every
off-desk use is audited with the account and its authority.
"""
from django.core.cache import cache

from apps.accounts.desktop_policy import issue_desktop_key
from apps.accounts.models import Capability, User, UserCapabilityOverride, Workstation
from apps.audit.models import AuditLog
from apps.staff_operations.biometric_models import (
    TemporaryWorkstationCredential,
)
from apps.staff_operations.models import AttendanceRecord
from apps.staff_operations.services import fingerprint_service

from .base import BaseAPITestCase
from .factories import hotel_settings, make_staff

TERMINAL_URL = "/api/admin/staff-operations/biometrics/reception-terminal/"
CLOCK_URL = "/api/admin/staff-operations/attendance/clock/"
DESK_IP = "102.89.34.7"
PHONE_IP = "41.58.19.204"
PINNED_IP = "198.51.100.25"
REASON = "Front desk scanner is unserviceable today."

MINUTIAE = [
    {"x": 100 + i * 5, "y": 150 + i * 4, "angle": (30 + i * 15) % 360, "type": "ending", "quality": 90}
    for i in range(15)
]
MATCHING_PROBE = [
    {"x": 100 + i * 5 + 1.0, "y": 150 + i * 4 + 1.0, "angle": (30 + i * 15) % 360, "type": "ending", "quality": 85}
    for i in range(15)
]


class OffDeskAttendanceTestCase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.worker = make_staff("offdesk.worker@staff.dev", role=User.Role.HOUSEKEEPING)
        self.supervisor = make_staff("offdesk.supervisor@staff.dev", role=User.Role.FRONT_DESK_SUPERVISOR)
        self.manager = make_staff("offdesk.manager@staff.dev", role=User.Role.MANAGER)
        self.admin = make_staff("offdesk.admin@staff.dev", role=User.Role.ADMIN)
        self.desk = Workstation.objects.create(
            name="Front Desk Terminal", department=Workstation.Department.FRONT_DESK
        )
        self.desk_key = issue_desktop_key(self.desk)
        self.desk.save()
        self.desk_headers = {"HTTP_X_JONE_DESKTOP_KEY": self.desk_key}
        template = fingerprint_service.request_fingerprint_enrollment(
            staff=self.worker, actor=self.worker, minutiae=MINUTIAE
        )
        fingerprint_service.approve_fingerprint_enrollment(template=template, approver=self.manager)

    def terminal(self, payload, *, user=None, headers=None, ip=PHONE_IP):
        """Call the attendance terminal from `ip`, with every header explicit."""
        if user is not None:
            self.auth(user)
        else:
            self.unauth()
        return self.client.post(
            TERMINAL_URL, payload, format="json", REMOTE_ADDR=ip, **(headers or {}),
        )

    def fallback(self, actor, *, ip=PHONE_IP, headers=None):
        return self.terminal(
            {"action": "MANUAL_FALLBACK", "email": self.worker.email, "manual_override_reason": REASON},
            user=actor, headers=headers, ip=ip,
        )


class ManagementMayRecordOffDeskTests(OffDeskAttendanceTestCase):
    """The requested exemption, and its limits."""

    def test_an_administrator_issues_a_fallback_code_from_their_own_phone(self):
        response = self.fallback(self.admin)
        self.assertEqual(response.status_code, 200, response.content)
        data = response.data["data"]
        self.assertEqual(data["authority"], "ADMINISTRATOR")
        self.assertEqual(data["evidence"], "ADMINISTRATOR_EXCEPTION")
        self.assertTrue(data["access_code"], "the employee still gets a usable 6-digit code")
        self.assertTrue(
            TemporaryWorkstationCredential.objects.filter(staff=self.worker, session_active=True).exists()
        )
        self.assertTrue(AttendanceRecord.objects.filter(staff=self.worker, clock_out_at__isnull=True).exists())

    def test_a_manager_issues_one_after_recording_the_failure_from_off_site(self):
        # The manager's EVIDENCE requirement is untouched by the station
        # exemption: they still have to record the capture failure first, and
        # the whole chain now works from a phone.
        recorded = self.terminal(
            {"action": "RECORD_FAILURE", "email": self.worker.email,
             "failure_classification": "DEVICE_UNAVAILABLE"},
            user=self.manager,
        )
        self.assertEqual(recorded.status_code, 200, recorded.content)
        response = self.fallback(self.manager)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data["data"]["authority"], "MANAGER")
        self.assertEqual(response.data["data"]["evidence"], "VERIFICATION_ATTEMPT")

    def test_an_off_desk_fallback_is_recorded_with_who_did_it_and_why(self):
        self.fallback(self.admin)
        entry = AuditLog.objects.filter(action="ATTENDANCE_OFF_DESK_TERMINAL").first()
        self.assertIsNotNone(entry, "operating the terminal away from the desk must leave a trace")
        self.assertEqual(entry.actor, self.admin)
        self.assertEqual(entry.metadata["authority"], "ADMINISTRATOR")
        self.assertEqual(entry.metadata["requested_action"], "MANUAL_FALLBACK")
        self.assertFalse(entry.metadata["receptionist_desktop_used"])
        override = AuditLog.objects.filter(action="ATTENDANCE_MANUAL_OVERRIDE").first()
        self.assertEqual(override.metadata["authority"], "ADMINISTRATOR")
        self.assertIn(REASON[:20], override.metadata["reason"])

    def test_the_administrator_can_clock_their_own_shift_off_desk(self):
        self.auth(self.admin)
        response = self.client.post(
            CLOCK_URL, {"action": "CLOCK_IN", "idempotency_key": "offdesk-admin-1"},
            format="json", REMOTE_ADDR=PHONE_IP,
        )
        self.assertEqual(response.status_code, 201, response.content)

    def test_a_manager_without_attendance_management_is_not_exempt(self):
        Capability.objects.get_or_create(code="attendance.manage", defaults={"name": "Manage attendance"})
        UserCapabilityOverride.objects.create(
            user=self.manager,
            capability=Capability.objects.get(code="attendance.manage"),
            effect=UserCapabilityOverride.Effect.REVOKE,
            reason="test: supervisor without attendance management",
        )
        self.manager._jone_capability_codes = None
        response = self.fallback(self.manager)
        self.assertEqual(response.status_code, 403, response.content)
        self.assertIn("Receptionist Desktop", response.data["message"])

    def test_an_operational_supervisor_is_not_exempt_by_holding_the_capability(self):
        # FRONT_DESK_SUPERVISOR holds attendance.manage but is NOT management:
        # their sign-in is bound to the desk, so a phone gets them nowhere. The
        # exemption is management authority, not the capability alone.
        from apps.accounts.capabilities import has_capability

        self.assertTrue(has_capability(self.supervisor, "attendance.manage"))
        response = self.fallback(self.supervisor)
        self.assertEqual(response.status_code, 403, response.content)
        self.assertIn("Receptionist Desktop", response.data["message"])
        self.assertFalse(TemporaryWorkstationCredential.objects.exists())

    def test_an_ordinary_employee_can_never_use_the_terminal_off_desk(self):
        response = self.terminal(
            {"action": "VERIFY_STATUS", "email": self.worker.email, "fingerprint_probe": MATCHING_PROBE},
            user=self.worker,
        )
        self.assertEqual(response.status_code, 403, response.content)
        self.assertFalse(AuditLog.objects.filter(action="ATTENDANCE_OFF_DESK_TERMINAL").exists())

    def test_an_ordinary_employee_still_cannot_clock_themselves_off_desk(self):
        self.auth(self.worker)
        response = self.client.post(
            CLOCK_URL, {"action": "CLOCK_IN", "idempotency_key": "offdesk-worker-1"},
            format="json", REMOTE_ADDR=PHONE_IP,
        )
        self.assertEqual(response.status_code, 403, response.content)
        self.assertFalse(AttendanceRecord.objects.exists())

    def test_the_terminal_still_never_invents_a_workstation(self):
        Workstation.objects.all().delete()
        response = self.fallback(self.admin)
        self.assertEqual(response.status_code, 403, response.content)
        self.assertIn("No active Receptionist Desktop workstation", response.data["message"])


class DeskRoutesStillWorkTests(OffDeskAttendanceTestCase):
    """The desk's own two enrolment routes, and what each still refuses."""

    def test_a_desk_enrolled_by_address_can_drive_the_terminal_with_nothing_signed_in(self):
        # The second enrolment route from sign-in: the desk IS the address, so
        # the attendance terminal works there without a key too.
        Workstation.objects.filter(pk=self.desk.pk).update(ip_address=DESK_IP)
        response = self.terminal(
            {"action": "VERIFY_STATUS", "email": self.worker.email, "fingerprint_probe": MATCHING_PROBE},
            headers={}, ip=DESK_IP,
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data["data"]["staff_email"], self.worker.email)

    def test_the_same_request_from_another_address_is_refused(self):
        Workstation.objects.filter(pk=self.desk.pk).update(ip_address=DESK_IP)
        response = self.terminal(
            {"action": "VERIFY_STATUS", "email": self.worker.email, "fingerprint_probe": MATCHING_PROBE},
            headers={}, ip=PHONE_IP,
        )
        self.assertEqual(response.status_code, 403, response.content)
        self.assertIn("Receptionist Desktop", response.data["message"])

    def test_a_desk_outside_the_pinned_network_is_told_exactly_that(self):
        hotel_settings(front_desk_desktop_ip=PINNED_IP)
        response = self.terminal(
            {"action": "VERIFY_STATUS", "email": self.worker.email, "fingerprint_probe": MATCHING_PROBE},
            headers=self.desk_headers, ip=DESK_IP,
        )
        self.assertEqual(response.status_code, 403, response.content)
        self.assertIn("configured Front Desk desktop IP address", response.data["message"])

    def test_management_may_still_record_when_the_desk_is_pinned_elsewhere(self):
        # The pin binds the desk for operational staff (its sign-in is refused
        # there too). Management keeps its device freedom, and this use is
        # audited rather than silent.
        hotel_settings(front_desk_desktop_ip=PINNED_IP)
        response = self.fallback(self.admin, ip=PHONE_IP)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(AuditLog.objects.filter(action="ATTENDANCE_OFF_DESK_TERMINAL").exists())

    def test_a_desk_browser_that_holds_the_key_is_unaffected(self):
        response = self.terminal(
            {"action": "VERIFY_STATUS", "email": self.worker.email, "fingerprint_probe": MATCHING_PROBE},
            headers=self.desk_headers, ip=DESK_IP,
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(
            AuditLog.objects.filter(action="ATTENDANCE_OFF_DESK_TERMINAL").exists(),
            "the desk's own route must not be reported as off-desk attendance",
        )

    def test_an_expired_or_unknown_key_is_refused_for_anonymous_callers(self):
        response = self.terminal(
            {"action": "VERIFY_STATUS", "email": self.worker.email},
            headers={"HTTP_X_JONE_DESKTOP_KEY": "not-a-real-key"}, ip=PHONE_IP,
        )
        self.assertEqual(response.status_code, 403, response.content)
        self.assertIn("desktop key", response.data["message"].lower())
