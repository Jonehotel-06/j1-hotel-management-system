# tests/test_terminal_staff_identifier.py
"""Staff identification at the Reception Biometric Terminal.

Production report: an administrator issuing a supervised fallback code
in Workforce -> Reception Biometric Terminal got

    "Identify the staff member (email, employee code or id)
     for a supervised fallback."

for staff whose email / id the admin typed correctly.

Two identifier shapes the system itself presents could never match:

* the numeric staff id — the workforce/terminals pages classify every
  non-"@" identifier as ``employee_code``, so "12" was looked up as an
  employee code and found nothing, even though the backend message
  (and the ``staff_id`` request field) claim ids are accepted;
* the displayed ``EMP-0012`` code — terminal/fingerprint responses
  synthesise ``EMP-{pk:04d}`` for staff without an operational profile,
  but no such value exists in ``staff_profile.employee_code`` (real
  codes are ``STF-...``), so typing the code shown on screen failed.

The terminal lookup must therefore resolve every identifier the system
advertises or displays, whichever request field the (possibly cached)
frontend bucketed it into — while an identifier that matches nobody
still creates nothing and says so clearly to the signed-in supervisor.
"""
from apps.accounts.models import User
from apps.staff_operations.biometric_models import TemporaryWorkstationCredential
from apps.staff_operations.models import AttendanceRecord, StaffProfile
from apps.staff_operations.services.staff_operations_service import ensure_staff_profile

from .test_attendance_workstation_policy import WorkstationPolicyTestCase

REASON = "Scanner at the front desk could not read the finger."


class SupervisedFallbackIdentifierTests(WorkstationPolicyTestCase):
    """ADMIN actor: no recorded failure needed (administrator exception)."""

    def fallback(self, payload):
        payload.setdefault("action", "MANUAL_FALLBACK")
        payload.setdefault("manual_override_reason", REASON)
        return self.terminal(payload, user=self.admin)

    def assert_code_issued_for_worker(self, response):
        self.assertEqual(response.status_code, 200, response.content)
        data = response.data["data"]
        self.assertEqual(data["staff_id"], self.worker.pk)
        self.assertTrue(data.get("access_code"))
        self.assertTrue(
            TemporaryWorkstationCredential.objects.filter(staff=self.worker).exists()
        )

    def test_email_control(self):
        self.assert_code_issued_for_worker(self.fallback({"email": self.worker.email}))

    def test_email_case_and_whitespace_insensitive(self):
        self.assert_code_issued_for_worker(
            self.fallback({"email": f"  {self.worker.email.upper()}  "})
        )

    def test_numeric_staff_id_field(self):
        self.assert_code_issued_for_worker(self.fallback({"staff_id": self.worker.pk}))

    def test_numeric_id_as_the_frontend_sends_it_today(self):
        """workforce.html sends non-'@' identifiers as employee_code."""
        self.assert_code_issued_for_worker(
            self.fallback({"employee_code": str(self.worker.pk)})
        )

    def test_displayed_emp_code_for_profile_less_staff(self):
        """The terminal displays EMP-{pk:04d} when no profile exists."""
        self.assertFalse(hasattr(self.worker, "staff_profile") and self.worker.staff_profile)
        self.assert_code_issued_for_worker(
            self.fallback({"employee_code": f"EMP-{self.worker.pk:04d}"})
        )

    def test_real_stf_employee_code(self):
        profile, _ = ensure_staff_profile(staff=self.worker)
        self.assert_code_issued_for_worker(
            self.fallback({"employee_code": profile.employee_code.lower()})
        )

    def test_email_value_sent_in_employee_code_field(self):
        """Older/cached clients bucketing identifiers differently still work."""
        self.assert_code_issued_for_worker(
            self.fallback({"employee_code": self.worker.email})
        )

    def test_unknown_identifier_creates_nothing_and_says_no_match(self):
        response = self.fallback({"email": "nobody@staff.dev"})
        self.assertEqual(response.status_code, 400, response.content)
        errors = response.data.get("errors") or {}
        self.assertIn("staff", errors)
        self.assertIn("No staff account matches", str(errors["staff"]))
        self.assertFalse(TemporaryWorkstationCredential.objects.exists())
        self.assertFalse(AttendanceRecord.objects.exists())

    def test_unknown_emp_code_creates_nothing(self):
        response = self.fallback({"employee_code": "EMP-999999"})
        self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(TemporaryWorkstationCredential.objects.exists())
        self.assertFalse(AttendanceRecord.objects.exists())

    def test_missing_identifier_still_asks_to_identify(self):
        response = self.fallback({})
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("Identify the staff member", str(response.data))

    def test_record_failure_accepts_numeric_id_as_sent_by_frontend(self):
        response = self.terminal(
            {
                "action": "RECORD_FAILURE",
                "employee_code": str(self.worker.pk),
                "failure_classification": "DEVICE_UNAVAILABLE",
            },
            user=self.admin,
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data["data"]["staff_id"], self.worker.pk)

    def test_identifier_resolves_exactly_the_named_person(self):
        other = self.manager
        response = self.fallback({"employee_code": f"EMP-{other.pk:04d}"})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data["data"]["staff_id"], other.pk)
        self.assertFalse(
            TemporaryWorkstationCredential.objects.filter(staff=self.worker).exists()
        )

    def test_generic_identifier_aliases_are_accepted(self):
        self.assert_code_issued_for_worker(self.fallback({"identifier": self.worker.email}))

    def test_user_id_alias_is_accepted(self):
        self.assert_code_issued_for_worker(self.fallback({"user_id": self.worker.pk}))

    def test_explicit_staff_profile_id_is_unambiguous(self):
        profile, _ = ensure_staff_profile(staff=self.worker)
        self.assert_code_issued_for_worker(
            self.fallback({"staff_profile_id": profile.pk})
        )

    def test_bare_staff_profile_id_resolves_when_it_is_not_a_user_id(self):
        profile = StaffProfile.objects.create(
            id=987654,
            user=self.worker,
            employee_code="STF-PROFILE-ID-TEST",
        )
        self.assert_code_issued_for_worker(
            self.fallback({"employee_code": str(profile.pk)})
        )

    def test_copied_email_with_internal_spaces_and_zero_width_character(self):
        copied = self.worker.email.replace("@", "\u200b @ ")
        self.assert_code_issued_for_worker(self.fallback({"email": copied}))

    def test_stored_email_with_outer_whitespace_still_resolves(self):
        clean_email = self.worker.email
        User.objects.filter(pk=self.worker.pk).update(email=f"  {clean_email}  ")
        self.assert_code_issued_for_worker(self.fallback({"email": clean_email}))

    def test_employee_code_with_unicode_dashes_and_spaces(self):
        profile, _ = ensure_staff_profile(staff=self.worker)
        copied = profile.employee_code.replace("-", " – ")
        self.assert_code_issued_for_worker(self.fallback({"employee_code": copied}))

    def test_stored_employee_code_with_outer_whitespace_still_resolves(self):
        profile, _ = ensure_staff_profile(staff=self.worker)
        clean_code = profile.employee_code
        StaffProfile.objects.filter(pk=profile.pk).update(
            employee_code=f"  {clean_code}  "
        )
        self.assert_code_issued_for_worker(self.fallback({"employee_code": clean_code}))

    def test_displayed_emp_code_without_separator_or_with_space(self):
        for copied in (f"EMP{self.worker.pk:04d}", f"EMP {self.worker.pk:04d}"):
            with self.subTest(copied=copied):
                self.assert_code_issued_for_worker(
                    self.fallback({"employee_code": copied})
                )
