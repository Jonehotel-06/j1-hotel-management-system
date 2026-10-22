"""Regression tests for the reported access-code error and Workforce attendance visibility.

Two production reports are pinned here:

1. "Attendance clocking is limited to the active staff member." was returned when
   an administrator issued a 6-digit access code for a newly created staff
   member. The message came from ``clock_attendance``, which merged two distinct
   checks — "the actor is not the staff member" and "the staff member's role does
   not hold ``attendance.clock``" — behind one string. The terminal/credential
   flows clock the employee as themselves, so only the capability half could
   fire; the identity half was never the cause. ``ACCOUNTS_MANAGER`` is the one
   operational role the seeded matrix does not grant ``attendance.clock`` to, and
   every other staff role is verified below to still receive a code.

2. Staff who had clocked in did not all appear in Workforce → Attendance. The
   page opened on the viewer's own records ("My attendance") even for accounts
   the server authorises to read the team, and it always bound the list to
   today's business date, which hides a still-open record opened on an earlier
   business date. The API already supported both behaviours; no filter was
   weakened.

No permission is granted by these tests, and the self-service identity rule is
preserved (last test).
"""
from datetime import timedelta
from unittest import mock

from django.core.exceptions import PermissionDenied
from django.utils import timezone

from apps.accounts.desktop_policy import issue_desktop_key
from apps.accounts.models import User, Workstation
from apps.staff_operations.models import AttendanceRecord, StaffProfile
from apps.staff_operations.services import fingerprint_service
from apps.staff_operations.services.staff_operations_service import clock_attendance

from .base import BaseAPITestCase
from .factories import make_staff, make_user
from .test_attendance_workstation_policy import MATCHING_PROBE, MINUTIAE

TERMINAL_URL = "/api/admin/staff-operations/biometrics/reception-terminal/"
CREDENTIAL_URL = "/api/admin/staff-operations/biometrics/temporary-credential/"
ATTENDANCE_URL = "/api/admin/staff-operations/attendance/"


class AttendanceCodeRoleTests(BaseAPITestCase):
    """Part C: who may receive a code, and what the refusal actually says."""

    def setUp(self):
        super().setUp()
        self.admin = make_staff("codefix.admin@staff.dev", role=User.Role.ADMIN)
        self.manager = make_staff("codefix.manager@staff.dev", role=User.Role.MANAGER)
        self.workstation = Workstation.objects.create(
            name="Front Desk Terminal", department=Workstation.Department.FRONT_DESK
        )
        self.desk_key = issue_desktop_key(self.workstation)
        self.workstation.save(update_fields=["sign_in_key_hash", "sign_in_key_issued_at"])
        self.desk_headers = {"HTTP_X_JONE_DESKTOP_KEY": self.desk_key}

    def enroll(self, staff, code, department="Operations", status=StaffProfile.EmploymentStatus.ACTIVE):
        StaffProfile.objects.update_or_create(
            user=staff,
            defaults={"employee_code": code, "department": department, "employment_status": status},
        )
        template = fingerprint_service.request_fingerprint_enrollment(
            staff=staff, actor=staff, minutiae=MINUTIAE
        )
        fingerprint_service.approve_fingerprint_enrollment(template=template, approver=self.manager)
        return template

    def terminal(self, payload, user=None):
        if user is None:
            self.unauth()
        else:
            self.auth(user)
        return self.client.post(TERMINAL_URL, payload, format="json", **self.desk_headers)

    # -- the reported failure -------------------------------------------------

    def test_role_without_clock_capability_gets_an_accurate_reason(self):
        """A database that never seeded the grant must explain itself, not blame identity.

        Migration 0024 seeds ``attendance.clock`` for ACCOUNTS_MANAGER, so the
        row is removed here to reproduce the pre-fix production database. The
        refusal has to name the missing capability instead of telling a person
        standing at the terminal that they are not the active staff member.
        """
        from apps.accounts.models import RoleCapability

        RoleCapability.objects.filter(
            role=User.Role.ACCOUNTS_MANAGER, capability__code="attendance.clock"
        ).delete()
        staff = make_staff("codefix.accounts@staff.dev", role=User.Role.ACCOUNTS_MANAGER)
        self.enroll(staff, "STF-CODE-ACC")

        response = self.terminal(
            {"action": "ISSUE_CODE", "email": staff.email, "fingerprint_probe": MATCHING_PROBE},
            user=self.admin,
        )
        self.assertEqual(response.status_code, 403, response.content)
        message = response.json()["message"]
        self.assertIn("attendance.clock", message)
        self.assertIn("role", message.lower())
        self.assertNotIn("limited to the active staff member", message)
        # A refused code leaves nothing behind: no attendance opened, no code.
        self.assertFalse(AttendanceRecord.objects.filter(staff=staff).exists())
        self.assertFalse(
            __import__(
                "apps.staff_operations.biometric_models", fromlist=["TemporaryWorkstationCredential"]
            ).TemporaryWorkstationCredential.objects.filter(staff=staff).exists()
        )

    def test_every_other_operational_role_still_receives_a_code(self):
        """The fix must not narrow (or widen) any other role's access."""
        skipped = {User.Role.GUEST}
        for index, role in enumerate(role for role in User.Role.values if role not in skipped):
            with self.subTest(role=role):
                staff = make_staff(f"codefix.role{index}@staff.dev", role=role)
                self.enroll(staff, f"STF-CODE-{index:03d}")
                response = self.terminal(
                    {"action": "ISSUE_CODE", "email": staff.email, "fingerprint_probe": MATCHING_PROBE},
                    user=self.admin,
                )
                self.assertEqual(response.status_code, 200, response.content)
                body = response.json()["data"]
                self.assertEqual(len(body["access_code"]), 6)
                record = AttendanceRecord.objects.get(reference=body["attendance_reference"])
                self.assertEqual(record.staff_id, staff.pk)
                self.assertIsNone(record.clock_out_at)

    def test_repeat_issue_reuses_the_open_record_and_keeps_one_code(self):
        staff = make_staff("codefix.repeat@staff.dev", role=User.Role.HOUSEKEEPER)
        self.enroll(staff, "STF-CODE-REP")
        first = self.terminal(
            {"action": "ISSUE_CODE", "email": staff.email, "fingerprint_probe": MATCHING_PROBE},
            user=self.admin,
        ).json()["data"]
        second = self.terminal(
            {"action": "ISSUE_CODE", "email": staff.email, "fingerprint_probe": MATCHING_PROBE},
            user=self.admin,
        ).json()["data"]
        self.assertEqual(first["attendance_reference"], second["attendance_reference"])
        self.assertFalse(second["created"])
        self.assertEqual(AttendanceRecord.objects.filter(staff=staff).count(), 1)

    def test_inactive_employee_is_refused_and_writes_nothing(self):
        staff = make_staff("codefix.inactive@staff.dev", role=User.Role.HOUSEKEEPER)
        self.enroll(staff, "STF-CODE-INA")
        staff.is_active = False
        staff.save(update_fields=["is_active"])
        response = self.terminal(
            {"action": "ISSUE_CODE", "email": staff.email, "fingerprint_probe": MATCHING_PROBE},
            user=self.admin,
        )
        self.assertEqual(response.status_code, 403, response.content)
        self.assertFalse(AttendanceRecord.objects.filter(staff=staff).exists())

    def test_employee_away_from_the_desk_cannot_issue_a_code_for_anyone(self):
        """The documented boundary: no desk key, no terminal — whatever the role.

        The Reception terminal authenticates the DESK (its browser, by key or
        registered address), never the person standing at it, so a staff session
        is deliberately not sufficient. That is why an ordinary employee on a
        phone is refused; the physical desk browser is the boundary and its use
        is audited. See docs/ATTENDANCE_WORKSTATION_SETUP.md.
        """
        worker = make_staff("codefix.worker@staff.dev", role=User.Role.HOUSEKEEPER)
        target = make_staff("codefix.target@staff.dev", role=User.Role.CHEF)
        self.enroll(worker, "STF-CODE-WRK")
        self.enroll(target, "STF-CODE-TGT")
        payload = {"action": "ISSUE_CODE", "email": target.email, "fingerprint_probe": MATCHING_PROBE}

        self.unauth()
        unauthenticated = self.client.post(TERMINAL_URL, payload, format="json")
        self.assertEqual(unauthenticated.status_code, 403, unauthenticated.content)

        self.auth(worker)
        with_session_but_no_desk = self.client.post(TERMINAL_URL, payload, format="json")
        self.assertEqual(with_session_but_no_desk.status_code, 403, with_session_but_no_desk.content)
        self.assertFalse(AttendanceRecord.objects.filter(staff=target).exists())

    def test_self_service_identity_rule_is_unchanged(self):
        """A different actor still cannot clock somebody else in directly."""
        worker = make_staff("codefix.self@staff.dev", role=User.Role.HOUSEKEEPER)
        other = make_staff("codefix.other@staff.dev", role=User.Role.CHEF)
        with self.assertRaisesMessage(
            PermissionDenied, "Attendance clocking is limited to the active staff member."
        ):
            clock_attendance(
                staff=other, action="CLOCK_IN", actor=worker, idempotency_key="self-rule-1"
            )
        self.assertFalse(AttendanceRecord.objects.filter(staff=other).exists())


class WorkforceAttendanceVisibilityTests(BaseAPITestCase):
    """Part D: the Workforce Attendance tab must show what the viewer may read."""

    def setUp(self):
        super().setUp()
        self.manager = make_staff("vis.manager@staff.dev", role=User.Role.MANAGER)
        self.admin = make_staff("vis.admin@staff.dev", role=User.Role.ADMIN)
        self.workstation = Workstation.objects.create(
            name="Front Desk", department=Workstation.Department.FRONT_DESK
        )
        self.desk_key = issue_desktop_key(self.workstation)
        self.workstation.save(update_fields=["sign_in_key_hash", "sign_in_key_issued_at"])
        self.desk_headers = {"HTTP_X_JONE_DESKTOP_KEY": self.desk_key}
        self.clocked_in = []
        for index, (role, dept) in enumerate(
            (
                (User.Role.HOUSEKEEPER, "Housekeeping"),
                (User.Role.CHEF, "Kitchen"),
                (User.Role.WAITER, "Restaurant"),
                (User.Role.SECURITY, "Security"),
            )
        ):
            staff = make_staff(f"vis.staff{index}@staff.dev", role=role)
            StaffProfile.objects.create(
                user=staff, employee_code=f"STF-VIS-{index}", department=dept,
                employment_status=StaffProfile.EmploymentStatus.ACTIVE,
            )
            template = fingerprint_service.request_fingerprint_enrollment(
                staff=staff, actor=staff, minutiae=MINUTIAE
            )
            fingerprint_service.approve_fingerprint_enrollment(template=template, approver=self.manager)
            AttendanceRecord.objects.create(
                reference=f"ATT-VIS-{index}", staff=staff,
                business_date=timezone.localdate(), status=AttendanceRecord.Status.ON_DUTY,
                clock_in_at=timezone.now() - timedelta(hours=2),
                last_event_at=timezone.now() - timedelta(hours=2),
            )
            self.clocked_in.append(staff)

    def test_manager_team_view_lists_every_department(self):
        self.auth(self.manager)
        response = self.client.get(ATTENDANCE_URL, {"open": "true", "page_size": 100})
        self.assertEqual(response.status_code, 200, response.content)
        rows = response.json()["data"]
        staff_ids = {row["staff"] for row in rows}
        self.assertEqual(staff_ids, {s.pk for s in self.clocked_in})

    def test_worker_without_manage_reads_only_their_own_records(self):
        worker = self.clocked_in[0]
        self.auth(worker)
        response = self.client.get(ATTENDANCE_URL, {"open": "true", "page_size": 100})
        self.assertEqual(response.status_code, 200, response.content)
        rows = response.json()["data"]
        self.assertEqual({row["staff"] for row in rows}, {worker.pk})
        # A staff_id for somebody else must not widen the queryset either.
        response = self.client.get(
            ATTENDANCE_URL, {"open": "true", "staff_id": self.clocked_in[1].pk}
        )
        self.assertEqual({row["staff"] for row in response.json()["data"]}, {worker.pk})

    def test_front_desk_supervisor_can_read_team_attendance(self):
        """attendance.manage — not the admin/manager role list — is the gate."""
        supervisor = make_staff("vis.supervisor@staff.dev", role=User.Role.FRONT_DESK_SUPERVISOR)
        self.auth(supervisor)
        response = self.client.get(ATTENDANCE_URL, {"open": "true", "page_size": 100})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(response.json()["data"]), len(self.clocked_in))

    def test_open_filter_includes_a_record_opened_on_an_earlier_business_date(self):
        """A night shift opened yesterday is still clocked in today."""
        night = self.clocked_in[0]
        record = AttendanceRecord.objects.get(staff=night)
        record.business_date = timezone.localdate() - timedelta(days=1)
        record.clock_in_at = timezone.now() - timedelta(hours=20)
        record.last_event_at = record.clock_in_at
        record.save(update_fields=["business_date", "clock_in_at", "last_event_at"])
        self.auth(self.manager)

        today_filtered = self.client.get(
            ATTENDANCE_URL, {"business_date": timezone.localdate().isoformat(), "page_size": 100}
        ).json()
        self.assertNotIn(night.pk, {row["staff"] for row in today_filtered["data"]})

        still_open = self.client.get(ATTENDANCE_URL, {"open": "true", "page_size": 100}).json()
        self.assertIn(night.pk, {row["staff"] for row in still_open["data"]})

    def test_pagination_does_not_drop_matching_records(self):
        extra = []
        for index in range(10):
            staff = make_staff(f"vis.extra{index}@staff.dev", role=User.Role.PORTER)
            AttendanceRecord.objects.create(
                reference=f"ATT-VIS-X{index}", staff=staff,
                business_date=timezone.localdate(), status=AttendanceRecord.Status.ON_DUTY,
                clock_in_at=timezone.now() - timedelta(minutes=30 + index),
                last_event_at=timezone.now(),
            )
            extra.append(staff)
        self.auth(self.manager)
        first = self.client.get(ATTENDANCE_URL, {"open": "true", "page_size": 10}).json()
        second = self.client.get(ATTENDANCE_URL, {"open": "true", "page_size": 10, "page": 2}).json()
        total = len(self.clocked_in) + len(extra)
        self.assertEqual(first["pagination"]["count"], total)
        seen = {row["staff"] for row in first["data"]} | {row["staff"] for row in second["data"]}
        self.assertEqual(len(seen), total)

    def test_clock_out_removes_the_row_from_the_open_view(self):
        worker = self.clocked_in[1]
        record = AttendanceRecord.objects.get(staff=worker)
        now = timezone.now()
        record.status = AttendanceRecord.Status.COMPLETED
        record.clock_out_at = now
        record.last_event_at = now
        record.save(update_fields=["status", "clock_out_at", "last_event_at"])
        self.auth(self.manager)
        open_rows = self.client.get(ATTENDANCE_URL, {"open": "true", "page_size": 100}).json()["data"]
        self.assertNotIn(worker.pk, {row["staff"] for row in open_rows})
        # ... but the historical row is still listed for the business date.
        history = self.client.get(
            ATTENDANCE_URL,
            {"business_date": timezone.localdate().isoformat(), "page_size": 100},
        ).json()["data"]
        self.assertIn(worker.pk, {row["staff"] for row in history})

    def test_attendance_list_requires_a_clocking_or_managing_capability(self):
        # ACCOUNTS_MANAGER holds attendance.clock as of migration 0024 and is
        # covered positively elsewhere; a guest holds neither capability.
        outsider = make_user("vis.outsider@example.com", password="Str0ng!Pass")
        self.auth(outsider)
        response = self.client.get(ATTENDANCE_URL)
        self.assertEqual(response.status_code, 403, response.content)
