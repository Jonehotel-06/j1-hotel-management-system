"""Attendance-code persistence for the lifetime of one attendance session.

Production report: a staff member who signed in with the 6-digit code issued at
the Reception Attendance Workstation could not sign in again after signing out.
The code was treated as a *single-use ticket* — the first successful sign-in
stamped ``is_used`` and every later attempt was refused with "has already been
used" — and it also carried a 15-minute TTL, so a shift longer than that lost
its code entirely. Attendance itself stayed open; only the credential died.

These tests pin the corrected lifecycle (see the docstring of
``fingerprint_service.authenticate_numeric_workstation_code``):

    eligible clock-in / approved issuance
        -> code bound to the staff member + open attendance record
        -> repeated application sign-ins with the SAME code (logout, browser
           refresh, token expiry included)
        -> official clock-out revokes active-session authentication
           (history and the code row are kept for reporting)

The application JWT session and the attendance session are deliberately
independent: signing out blacklists the refresh token, never the attendance
record, and an expired JWT is fixed by signing in again with the same code.
"""
import threading
import unittest
from datetime import timedelta

from django.core.cache import cache
from django.db import connections
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.desktop_policy import issue_desktop_key
from apps.accounts.models import User, Workstation
from apps.audit.models import AuditLog
from apps.staff_operations.biometric_models import TemporaryWorkstationCredential
from apps.staff_operations.models import AttendanceRecord, StaffProfile
from apps.staff_operations.services import fingerprint_service
from apps.staff_operations.services.staff_operations_service import ensure_open_attendance_record

from .base import BaseAPITestCase
from .factories import make_staff
from .test_attendance_workstation_policy import MATCHING_PROBE, MISMATCHING_PROBE, MINUTIAE

LOGIN_URL = "/api/auth/login/"
LOGOUT_URL = "/api/auth/logout/"
PROFILE_URL = "/api/auth/profile/"
TERMINAL_URL = "/api/admin/staff-operations/biometrics/reception-terminal/"
CREDENTIAL_URL = "/api/admin/staff-operations/biometrics/temporary-credential/"
ATTENDANCE_URL = "/api/admin/staff-operations/attendance/"
STAFF_PROFILES_URL = "/api/admin/staff-operations/profiles/"

EMPLOYEE_CODE = "STF-PERSIST01"


class AttendanceCodePersistenceTests(BaseAPITestCase):
    """Scenario A/B/C/D/E of the code-lifecycle report, through the real API."""

    def setUp(self):
        super().setUp()
        self.worker = make_staff("persist.worker@staff.dev", role=User.Role.CHEF)
        self.other = make_staff("persist.other@staff.dev", role=User.Role.HOUSEKEEPER)
        self.manager = make_staff("persist.manager@staff.dev", role=User.Role.MANAGER)
        self.admin = make_staff("persist.admin@staff.dev", role=User.Role.ADMIN)

        StaffProfile.objects.update_or_create(
            user=self.worker,
            defaults={
                "employee_code": EMPLOYEE_CODE,
                "department": "Kitchen",
                "job_title": "Head Chef",
                "employment_status": StaffProfile.EmploymentStatus.ACTIVE,
            },
        )
        StaffProfile.objects.update_or_create(
            user=self.other,
            defaults={
                "employee_code": "STF-PERSIST02",
                "department": "Housekeeping",
                "employment_status": StaffProfile.EmploymentStatus.ACTIVE,
            },
        )

        self.workstation = Workstation.objects.create(
            name="Front Desk Terminal", department=Workstation.Department.FRONT_DESK
        )
        self.desk_key = issue_desktop_key(self.workstation)
        self.workstation.save(update_fields=["sign_in_key_hash", "sign_in_key_issued_at"])
        self.desk_headers = {"HTTP_X_JONE_DESKTOP_KEY": self.desk_key}

        for staff in (self.worker, self.other):
            template = fingerprint_service.request_fingerprint_enrollment(
                staff=staff, actor=staff, minutiae=MINUTIAE
            )
            fingerprint_service.approve_fingerprint_enrollment(
                template=template, approver=self.manager
            )

    # ------------------------------------------------------------------ helpers

    def terminal(self, payload, user=None):
        if user is not None:
            self.auth(user)
        else:
            self.unauth()
        return self.client.post(
            TERMINAL_URL, payload, format="json", **self.desk_headers
        )

    def clock_in(self, staff=None, action="CLOCK_IN"):
        staff = staff or self.worker
        response = self.terminal(
            {
                "action": action,
                "email": staff.email,
                "fingerprint_probe": MATCHING_PROBE,
            }
        )
        self.assertEqual(response.status_code, 200, response.content)
        return response.data["data"]

    def clock_out(self, staff=None):
        staff = staff or self.worker
        response = self.terminal(
            {
                "action": "CLOCK_OUT",
                "email": staff.email,
                "fingerprint_probe": MATCHING_PROBE,
            }
        )
        self.assertEqual(response.status_code, 200, response.content)
        return response.data["data"]

    def login_with_code(self, code, *, email=None, employee_code=None, department=None):
        """POST the real sign-in endpoint.

        The login scope is throttled to 5/min per client; each test's repeated
        sign-ins are *not* an attack, so the counter is reset between them (the
        dedicated throttle test below deliberately does the opposite).
        """
        cache.clear()
        payload = {"access_code": str(code)}
        if email:
            payload["email"] = email
        if employee_code:
            payload["employee_code"] = employee_code
        if department:
            payload["department"] = department
        return APIClient().post(LOGIN_URL, payload, format="json")

    def login_ok(self, code, **kwargs):
        response = self.login_with_code(code, **kwargs)
        self.assertEqual(response.status_code, 200, response.content)
        return response.data["data"]["tokens"]

    def logout(self, tokens):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
        response = client.post(LOGOUT_URL, {"refresh": tokens["refresh"]}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        return response

    def profile_with(self, tokens):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
        return client.get(PROFILE_URL)

    # ------------------------------------------------------- Scenario A: clock in

    def test_clock_in_assigns_a_code_bound_to_the_open_attendance_session(self):
        data = self.clock_in()
        record = AttendanceRecord.objects.get(staff=self.worker)
        credential = TemporaryWorkstationCredential.objects.get(staff=self.worker)

        self.assertTrue(data["access_code"].isdigit() and len(data["access_code"]) == 6)
        self.assertEqual(data["attendance_reference"], record.reference)
        self.assertEqual(credential.attendance_record_id, record.pk)
        self.assertIsNone(record.clock_out_at)
        self.assertEqual(record.status, AttendanceRecord.Status.ON_DUTY)
        self.assertTrue(credential.session_active)
        self.assertIsNone(credential.revoked_at)
        # The plaintext code is never stored or audited.
        self.assertNotIn(data["access_code"], credential.numeric_code_hash)
        issued = AuditLog.objects.filter(action="TEMPORARY_WORKSTATION_CREDENTIAL_ISSUED").first()
        self.assertIsNotNone(issued)
        self.assertNotIn(data["access_code"], str(issued.metadata))

    def test_the_assigned_code_signs_the_staff_member_in(self):
        code = self.clock_in()["access_code"]
        tokens = self.login_ok(code, email=self.worker.email)
        response = self.profile_with(tokens)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data["data"]["email"], self.worker.email)

    # ------------------------------------- Scenario B/E: logout and sign back in

    def test_sign_out_leaves_attendance_and_the_code_active(self):
        code = self.clock_in()["access_code"]
        self.logout(self.login_ok(code, email=self.worker.email))

        record = AttendanceRecord.objects.get(staff=self.worker)
        credential = TemporaryWorkstationCredential.objects.get(staff=self.worker)
        self.assertIsNone(record.clock_out_at, "application logout must never clock the staff member out")
        self.assertEqual(record.status, AttendanceRecord.Status.ON_DUTY)
        self.assertTrue(credential.session_active)
        self.assertIsNone(credential.revoked_at)
        self.assertTrue(credential.is_used, "first use is still recorded for the audit trail")
        self.assertEqual(credential.used_at is not None, True)

    def test_the_same_code_signs_in_again_after_logout(self):
        code = self.clock_in()["access_code"]
        self.logout(self.login_ok(code, email=self.worker.email))

        second = self.login_ok(code, email=self.worker.email)
        self.assertEqual(self.profile_with(second).status_code, 200)

    def test_repeated_sign_out_and_sign_in_never_rotates_the_code(self):
        code = self.clock_in()["access_code"]
        for _ in range(4):
            tokens = self.login_ok(code, email=self.worker.email)
            self.assertEqual(self.profile_with(tokens).status_code, 200)
            self.logout(tokens)

        credential = TemporaryWorkstationCredential.objects.get(staff=self.worker)
        self.assertEqual(
            TemporaryWorkstationCredential.objects.filter(staff=self.worker).count(), 1,
            "repeat sign-ins must never mint a replacement credential",
        )
        self.assertTrue(credential.session_active)
        self.assertEqual(AttendanceRecord.objects.filter(staff=self.worker).count(), 1)
        self.assertEqual(
            AttendanceRecord.objects.filter(staff=self.worker, clock_out_at__isnull=True).count(), 1,
        )

    def test_browser_refresh_and_cleared_tokens_do_not_invalidate_the_code(self):
        """"Sign in from the page again" = a fresh client with no local session."""
        code = self.clock_in()["access_code"]
        self.login_ok(code, email=self.worker.email)

        # A new browser context holds no token at all; only the code is kept.
        refreshed = APIClient()
        response = refreshed.post(
            LOGIN_URL, {"access_code": code, "email": self.worker.email}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.content)

    def test_an_expired_application_token_does_not_invalidate_the_code(self):
        code = self.clock_in()["access_code"]
        tokens = self.login_ok(code, email=self.worker.email)

        expired = APIClient()
        expired.credentials(HTTP_AUTHORIZATION="Bearer this-token-is-not-valid")
        self.assertEqual(expired.get(PROFILE_URL).status_code, 401)

        # The application session is gone; the attendance session is not.
        renewed = self.login_ok(code, email=self.worker.email)
        self.assertEqual(self.profile_with(renewed).status_code, 200)

    def test_a_code_older_than_the_legacy_ttl_still_signs_in_while_on_duty(self):
        """Scenario E: the staff member returns much later, still clocked in."""
        code = self.clock_in()["access_code"]
        TemporaryWorkstationCredential.objects.filter(staff=self.worker).update(
            expires_at=timezone.now() - timedelta(hours=6)
        )

        response = self.login_with_code(code, email=self.worker.email)
        self.assertEqual(response.status_code, 200, response.content)

    def test_an_unbound_legacy_credential_keeps_its_short_ttl_and_single_use(self):
        """Security boundary retained for credentials with no attendance binding."""
        open_record, _ = ensure_open_attendance_record(
            staff=self.worker, idempotency_key="legacy-unbound-1"
        )
        details = fingerprint_service.mint_temporary_workstation_credential(
            staff=self.worker,
            workstation_reference=self.workstation.reference,
            attendance_record=open_record,
        )
        TemporaryWorkstationCredential.objects.filter(pk=details["credential_id"]).update(
            attendance_record=None, expires_at=timezone.now() - timedelta(minutes=1)
        )

        expired = self.login_with_code(details["access_code"], email=self.worker.email)
        self.assertIn(expired.status_code, (401, 403), expired.content)
        self.assertIn("expired", expired.data["message"])

        # Inside its window such a legacy credential is still single-use.
        TemporaryWorkstationCredential.objects.filter(pk=details["credential_id"]).update(
            expires_at=timezone.now() + timedelta(minutes=5)
        )
        self.login_ok(details["access_code"], email=self.worker.email)
        replay = self.login_with_code(details["access_code"], email=self.worker.email)
        self.assertIn(replay.status_code, (401, 403), replay.content)
        self.assertIn("already been used", replay.data["message"])

    # -------------------------------------------- Scenario C: administrator issue

    def test_administrator_issued_code_works_and_survives_logout(self):
        self.unauth()
        issued = self.client.post(
            CREDENTIAL_URL,
            {"email": self.worker.email, "fingerprint_probe": MATCHING_PROBE},
            format="json",
            **self.desk_headers,
        )
        self.assertEqual(issued.status_code, 200, issued.content)
        data = issued.data["data"]
        self.assertTrue(data["attendance_created"])
        self.assertTrue(data["access_code"].isdigit())
        credential = TemporaryWorkstationCredential.objects.get(staff=self.worker)
        self.assertEqual(credential.attendance_record.reference, data["attendance_reference"])

        tokens = self.login_ok(data["access_code"], email=self.worker.email)
        self.logout(tokens)
        self.login_ok(data["access_code"], email=self.worker.email)

        self.assertEqual(AttendanceRecord.objects.filter(staff=self.worker).count(), 1)
        self.assertEqual(TemporaryWorkstationCredential.objects.filter(staff=self.worker).count(), 1)

    def test_supervised_fallback_code_survives_logout(self):
        self.unauth()
        self.auth(self.admin)
        response = self.client.post(
            TERMINAL_URL,
            {
                "action": "MANUAL_FALLBACK",
                "email": self.worker.email,
                "manual_override_reason": "Reader unavailable; administrator exception.",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        code = response.data["data"]["access_code"]
        self.assertEqual(response.data["data"]["authority"], "ADMINISTRATOR")

        self.logout(self.login_ok(code, email=self.worker.email))
        self.login_ok(code, email=self.worker.email)

    # ----------------------------------------------- Scenario D: official clock-out

    def test_official_clock_out_revokes_the_code_but_keeps_history(self):
        code = self.clock_in()["access_code"]
        self.login_ok(code, email=self.worker.email)
        self.clock_out()

        refused = self.login_with_code(code, email=self.worker.email)
        self.assertIn(refused.status_code, (401, 403), refused.content)

        record = AttendanceRecord.objects.get(staff=self.worker)
        credential = TemporaryWorkstationCredential.objects.get(staff=self.worker)
        self.assertIsNotNone(record.clock_out_at)
        self.assertEqual(record.status, AttendanceRecord.Status.COMPLETED)
        self.assertFalse(credential.session_active)
        self.assertIsNotNone(credential.revoked_at)
        self.assertEqual(credential.revoked_reason, "CLOCKED_OUT")
        self.assertTrue(record.events.filter(type="CLOCK_IN").exists())
        self.assertTrue(record.events.filter(type="CLOCK_OUT").exists())

    def test_a_completed_sessions_code_stays_dead_after_the_next_clock_in(self):
        old_code = self.clock_in()["access_code"]
        self.clock_out()
        new_code = self.clock_in()["access_code"]
        self.assertNotEqual(old_code, new_code, "a new session gets a fresh code")

        stale = self.login_with_code(old_code, email=self.worker.email)
        self.assertIn(stale.status_code, (401, 403), stale.content)
        self.login_ok(new_code, email=self.worker.email)

    def test_completed_attendance_history_remains_queryable(self):
        code = self.clock_in()["access_code"]
        self.clock_out()

        self.auth(self.manager)
        response = self.client.get(ATTENDANCE_URL, {"staff_id": self.worker.pk})
        self.assertEqual(response.status_code, 200, response.content)
        rows = response.data["data"]
        self.assertTrue(any(row["reference"] for row in rows), response.content)
        self.assertTrue(all(row["clock_out_at"] for row in rows), response.content)

    # ---------------------------------------------------------- security boundaries

    def test_an_incorrect_code_is_rejected(self):
        self.clock_in()
        response = self.login_with_code("000000", email=self.worker.email)
        self.assertIn(response.status_code, (401, 403), response.content)
        self.assertIn("Invalid workstation access code", response.data["message"])

    def test_another_employees_code_is_rejected_for_this_employee(self):
        worker_code = self.clock_in()["access_code"]
        other_code = self.clock_in(self.other)["access_code"]
        self.assertNotEqual(worker_code, other_code)

        # Worker's code presented as the other employee.
        stolen = self.login_with_code(worker_code, email=self.other.email)
        self.assertIn(stolen.status_code, (401, 403), stolen.content)
        self.assertIn("different employee", stolen.data["message"])
        self.assertTrue(AuditLog.objects.filter(action="WORKSTATION_CODE_SHARING_BLOCKED").exists())

        # The other employee's code presented as the worker, by email and by
        # employee code — both identify a different owner than the credential.
        for identity in ({"email": self.worker.email}, {"employee_code": EMPLOYEE_CODE}):
            response = self.login_with_code(other_code, **identity)
            self.assertIn(response.status_code, (401, 403), response.content)
            self.assertIn("different employee", response.data["message"])

    def test_a_code_does_not_authenticate_into_another_department_dashboard(self):
        code = self.clock_in()["access_code"]
        refused = self.login_with_code(code, email=self.worker.email, department="accountant")
        self.assertIn(refused.status_code, (401, 403), refused.content)
        allowed = self.login_with_code(code, email=self.worker.email, department="kitchen")
        self.assertEqual(allowed.status_code, 200, allowed.content)

    def test_disabled_account_is_refused_even_with_a_live_code(self):
        code = self.clock_in()["access_code"]
        User.objects.filter(pk=self.worker.pk).update(is_active=False)

        response = self.login_with_code(code, email=self.worker.email)
        self.assertIn(response.status_code, (401, 403), response.content)

    def test_suspended_employment_status_is_refused_even_with_a_live_code(self):
        code = self.clock_in()["access_code"]
        StaffProfile.objects.filter(user=self.worker).update(
            employment_status=StaffProfile.EmploymentStatus.SUSPENDED
        )

        response = self.login_with_code(code, email=self.worker.email)
        self.assertIn(response.status_code, (401, 403), response.content)
        self.assertIn("suspended", response.data["message"].lower())

    def test_revoked_credential_can_no_longer_sign_in(self):
        code = self.clock_in()["access_code"]
        fingerprint_service.revoke_workstation_credentials_on_clock_out(
            staff=self.worker, reason="BIOMETRIC_REVOKED"
        )
        response = self.login_with_code(code, email=self.worker.email)
        self.assertIn(response.status_code, (401, 403), response.content)

    def test_role_and_permission_checks_still_apply_after_code_sign_in(self):
        code = self.clock_in()["access_code"]
        tokens = self.login_ok(code, email=self.worker.email)

        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
        forbidden = client.get(STAFF_PROFILES_URL)
        self.assertEqual(forbidden.status_code, 403, forbidden.content)

    def test_a_mismatched_fingerprint_never_issues_a_code_or_opens_attendance(self):
        self.unauth()
        response = self.client.post(
            CREDENTIAL_URL,
            {"email": self.worker.email, "fingerprint_probe": MISMATCHING_PROBE},
            format="json",
            **self.desk_headers,
        )
        self.assertEqual(response.status_code, 403, response.content)
        self.assertFalse(AttendanceRecord.objects.filter(staff=self.worker).exists())
        self.assertFalse(TemporaryWorkstationCredential.objects.exists())

    def test_repeated_login_requests_create_no_duplicate_attendance_or_credentials(self):
        code = self.clock_in()["access_code"]
        for _ in range(5):
            self.login_ok(code, email=self.worker.email)

        self.assertEqual(AttendanceRecord.objects.filter(staff=self.worker).count(), 1)
        self.assertEqual(
            TemporaryWorkstationCredential.objects.filter(staff=self.worker, session_active=True).count(), 1
        )
        self.assertEqual(
            TemporaryWorkstationCredential.objects.filter(staff=self.worker).count(), 1
        )

    def test_login_throttle_still_guards_repeated_wrong_codes(self):
        self.clock_in()
        cache.clear()
        statuses = []
        for _ in range(6):
            response = APIClient().post(
                LOGIN_URL, {"access_code": "000000", "email": self.worker.email}, format="json"
            )
            statuses.append(response.status_code)
        self.assertEqual(statuses[-1], 429, statuses)


def _in_memory_sqlite_test_database() -> bool:
    """True when the test database is SQLite's shared-cache in-memory file.

    That harness serialises every connection with a table-level lock and answers
    immediately with ``database table is locked`` instead of waiting, so it
    cannot host genuinely parallel database requests. Production runs MySQL,
    and a file-backed SQLite database (or MySQL) hosts these tests unchanged.
    """
    name = str(connections["default"].settings_dict.get("NAME", ""))
    return connections["default"].vendor == "sqlite" and name.startswith("file:memorydb")


class ConcurrentSignInTests(TransactionTestCase):
    """Simultaneous sign-in requests must not consume or invalidate the code.

    ``TransactionTestCase`` is required so each request thread sees committed
    fixture data. The credential row is locked with ``select_for_update`` inside
    one transaction by the service itself; these tests exercise that both at the
    authentication endpoint and at the service boundary.
    """

    def setUp(self):
        if _in_memory_sqlite_test_database():
            # Evaluated here, after the test database exists, rather than at
            # import time when the configured (not the test) database is visible.
            raise unittest.SkipTest(
                "The SQLite shared-cache in-memory test database cannot serve "
                "parallel connections (SQLITE_LOCKED). Run against MySQL or a "
                "file-backed SQLite test database to execute these assertions."
            )
        super().setUp()
        from apps.accounts.models import User as UserModel

        self.worker = UserModel.objects.create_user(
            email="concurrent.chef@staff.dev", password="Passw0rd!234", role=User.Role.CHEF
        )
        StaffProfile.objects.create(
            user=self.worker,
            employee_code="STF-CONCUR01",
            department="Kitchen",
            employment_status=StaffProfile.EmploymentStatus.ACTIVE,
        )
        self.manager = UserModel.objects.create_user(
            email="concurrent.manager@staff.dev", password="Passw0rd!234", role=User.Role.MANAGER
        )
        self.workstation = Workstation.objects.create(
            name="Front Desk Terminal", department=Workstation.Department.FRONT_DESK
        )
        self.desk_key = issue_desktop_key(self.workstation)
        self.workstation.save(update_fields=["sign_in_key_hash", "sign_in_key_issued_at"])
        self.desk_headers = {"HTTP_X_JONE_DESKTOP_KEY": self.desk_key}
        template = fingerprint_service.request_fingerprint_enrollment(
            staff=self.worker, actor=self.worker, minutiae=MINUTIAE
        )
        fingerprint_service.approve_fingerprint_enrollment(template=template, approver=self.manager)

        response = APIClient().post(
            TERMINAL_URL,
            {"action": "CLOCK_IN", "email": self.worker.email, "fingerprint_probe": MATCHING_PROBE},
            format="json",
            **self.desk_headers,
        )
        assert response.status_code == 200, response.content
        self.code = response.data["data"]["access_code"]

        # One first sign-in establishes the credential's first-use record; the
        # concurrent burst below is the "several tabs/devices at once" case.
        cache.clear()
        first = APIClient().post(
            LOGIN_URL, {"access_code": self.code, "email": self.worker.email}, format="json"
        )
        assert first.status_code == 200, first.content

    def tearDown(self):
        for connection in connections.all():
            connection.close()
        super().tearDown()

    def run_threads(self, target, count=3):
        results = []
        lock = threading.Lock()
        barrier = threading.Barrier(count)

        def wrapped(index):
            try:
                barrier.wait(timeout=10)
                outcome = target(index)
            except Exception as exc:  # pragma: no cover - surfaced in the assertion
                outcome = repr(exc)
            finally:
                for connection in connections.all():
                    connection.close()
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=wrapped, args=(i,)) for i in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        return results

    def test_parallel_sign_in_requests_keep_the_code_valid_and_attendance_unique(self):
        from unittest import mock

        # The per-sign-in audit insert is silenced only for this burst: it is
        # covered by the remaining tests, and it is not what this test is about.
        with mock.patch("apps.accounts.views.log_action"):
            results = self.run_threads(
                lambda _index: APIClient().post(
                    LOGIN_URL,
                    {"access_code": self.code, "email": self.worker.email},
                    format="json",
                ).status_code
            )

        self.assertEqual(results, [200] * 3, results)
        self.assertEqual(AttendanceRecord.objects.filter(staff=self.worker).count(), 1)
        credential = TemporaryWorkstationCredential.objects.get(staff=self.worker)
        self.assertTrue(credential.session_active)
        self.assertIsNone(credential.revoked_at)
        self.assertTrue(credential.is_used)

    def test_parallel_service_validation_never_consumes_the_code(self):
        from apps.staff_operations.services.fingerprint_service import (
            authenticate_numeric_workstation_code,
        )

        def authenticate(_index):
            credential = authenticate_numeric_workstation_code(
                access_code=self.code, email=self.worker.email
            )
            return credential.pk

        results = self.run_threads(authenticate)
        self.assertEqual(len(results), 3, results)
        self.assertEqual(len({str(item) for item in results}), 1, results)

        credential = TemporaryWorkstationCredential.objects.get(staff=self.worker)
        self.assertTrue(credential.session_active)
        self.assertEqual(AttendanceRecord.objects.filter(staff=self.worker).count(), 1)
        self.assertEqual(
            AttendanceRecord.objects.filter(staff=self.worker, clock_out_at__isnull=True).count(), 1
        )
