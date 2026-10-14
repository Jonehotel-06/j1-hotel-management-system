"""Staff self-service profile: owner scoping, privacy, and empty states."""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from apps.accounts.models import User
from apps.core.utils import hotel_today
from apps.staff_operations.models import (
    AttendanceRecord, LeaveRequest, ShiftAssignment, StaffCompensation,
    StaffDisciplinaryRecord, StaffDocument, StaffPerformanceReview, StaffProfile,
)

from .base import BaseAPITestCase
from .factories import make_staff, make_user

URL = "/api/admin/staff-operations/my-profile/"


class MyStaffProfileTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.alice = make_staff("alice@staff.dev", role=User.Role.BARTENDER)
        self.alice.first_name, self.alice.last_name = "Alice", "Ade"
        self.alice.save(update_fields=["first_name", "last_name"])
        self.bob = make_staff("bob@staff.dev", role=User.Role.WAITER)
        self.hr = make_staff("hr-self@staff.dev", role=User.Role.HR_MANAGER)
        for user, code in ((self.alice, "EMP-ALICE"), (self.bob, "EMP-BOB")):
            StaffProfile.objects.create(
                user=user, employee_code=code, department="Bar", job_title="Bartender",
                employment_start=hotel_today() - timedelta(days=90),
            )
        StaffCompensation.objects.create(
            staff=self.alice, effective_from=hotel_today() - timedelta(days=30),
            basic_salary=Decimal("200000.00"), housing_allowance=Decimal("50000.00"), created_by=self.hr,
        )
        StaffCompensation.objects.create(
            staff=self.bob, effective_from=hotel_today() - timedelta(days=30),
            basic_salary=Decimal("999999.00"), created_by=self.hr,
        )
        now = timezone.now()
        ShiftAssignment.objects.create(
            reference="SHIFT-ALICE-1", staff=self.alice, status=ShiftAssignment.Status.SCHEDULED,
            planned_start_at=now + timedelta(days=1), planned_end_at=now + timedelta(days=1, hours=8), department="Bar",
        )
        ShiftAssignment.objects.create(
            reference="SHIFT-BOB-1", staff=self.bob, status=ShiftAssignment.Status.SCHEDULED,
            planned_start_at=now + timedelta(days=1), planned_end_at=now + timedelta(days=1, hours=8), department="Restaurant",
        )
        AttendanceRecord.objects.create(
            reference="ATT-ALICE-1", staff=self.alice, business_date=hotel_today(),
            status=AttendanceRecord.Status.COMPLETED, clock_in_at=now - timedelta(hours=8),
            clock_out_at=now, last_event_at=now, worked_minutes=480,
        )
        LeaveRequest.objects.create(
            reference="LEAVE-ALICE-1", staff=self.alice, type=LeaveRequest.Type.ANNUAL,
            status=LeaveRequest.Status.APPROVED, start_date=hotel_today() + timedelta(days=10),
            end_date=hotel_today() + timedelta(days=12),
        )
        self.alice_case = StaffDisciplinaryRecord.objects.create(
            reference="DSC-ALICE-1", staff=self.alice, category=StaffDisciplinaryRecord.Category.ATTENDANCE,
            allegation_summary="Late arrival allegation", status=StaffDisciplinaryRecord.Status.OPEN,
            reported_by=self.hr,
        )
        StaffDisciplinaryRecord.objects.create(
            reference="DSC-BOB-1", staff=self.bob, category=StaffDisciplinaryRecord.Category.MISCONDUCT,
            allegation_summary="Bob secret case", status=StaffDisciplinaryRecord.Status.OPEN, reported_by=self.hr,
        )
        StaffPerformanceReview.objects.create(
            reference="PERF-ALICE-FINAL", staff=self.alice, reviewer=self.hr, rating=4,
            strengths="Reliable service", status=StaffPerformanceReview.Status.FINALIZED,
        )
        StaffPerformanceReview.objects.create(
            reference="PERF-ALICE-DRAFT", staff=self.alice, reviewer=self.hr, rating=2,
            strengths="Draft only", status=StaffPerformanceReview.Status.DRAFT,
        )
        StaffDocument.objects.create(reference="DOC-ALICE-1", staff=self.alice, category=StaffDocument.Category.TRAINING, title="Bar safety")
        StaffDocument.objects.create(
            reference="DOC-ALICE-SECRET", staff=self.alice, category=StaffDocument.Category.OTHER,
            title="Confidential HR note", is_confidential=True,
        )

    def test_employee_sees_only_own_records(self):
        self.auth(self.alice)
        response = self.client.get(URL)
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertEqual(data["user"]["email"], "alice@staff.dev")
        self.assertEqual(data["profile"]["employee_code"], "EMP-ALICE")
        self.assertEqual(data["salary"]["current_compensation"]["basic_salary"], "200000.00")
        self.assertEqual([row["reference"] for row in data["shifts"]], ["SHIFT-ALICE-1"])
        self.assertEqual([row["reference"] for row in data["attendance"]], ["ATT-ALICE-1"])
        self.assertEqual([row["reference"] for row in data["leave"]["requests"]], ["LEAVE-ALICE-1"])
        self.assertEqual(data["leave"]["requests"][0]["days"], 3)
        self.assertIsNone(data["leave"]["balance"])
        disciplinary = data["disciplinary"]
        self.assertEqual([row["reference"] for row in disciplinary], ["DSC-ALICE-1"])
        self.assertEqual(disciplinary[0]["finding"], "PENDING")
        self.assertIsNone(disciplinary[0]["approved_deduction_amount"])
        # Only finalized reviews are employee-visible.
        self.assertEqual([row["reference"] for row in data["performance_reviews"]], ["PERF-ALICE-FINAL"])
        self.assertEqual([row["reference"] for row in data["documents"]], ["DOC-ALICE-1"])
        self.assertEqual(data["guest_reviews"]["items"], [])
        self.assertFalse(data["guest_reviews"]["attribution_available"])
        body = str(response.content)
        self.assertNotIn("999999", body)
        self.assertNotIn("Bob secret case", body)
        self.assertNotIn("Confidential HR note", body)
        # No credential/biometric material: `must_change_password` is only a
        # boolean flag in the existing safe user serializer, so assert on
        # actual secret material instead of the substring "password".
        for secret in ("pbkdf2", "fingerprint_template", "biometric_template", "access_token", "refresh_token", "minutiae"):
            self.assertNotIn(secret, body.lower())

    def test_changing_id_parameters_cannot_widen_scope(self):
        self.auth(self.alice)
        for params in ({"staff_id": self.bob.pk}, {"user_id": self.bob.pk}, {"id": self.bob.pk}):
            response = self.client.get(URL, params)
            self.assertEqual(response.status_code, 200, response.content)
            self.assertEqual(response.json()["data"]["user"]["email"], "alice@staff.dev")
        # The manager comprehensive endpoint keeps its own capability gate.
        forbidden = self.client.get(f"/api/admin/staff-operations/staff/{self.bob.pk}/full-profile/")
        self.assertEqual(forbidden.status_code, 403, forbidden.content)

    def test_unauthenticated_and_guest_rejected(self):
        self.unauth()
        response = self.client.get(URL)
        self.assertIn(response.status_code, (401, 403))
        guest = make_user("guest-self@example.com", role=User.Role.GUEST)
        self.auth(guest)
        self.assertEqual(self.client.get(URL).status_code, 403)

    def test_empty_states_for_employee_without_records(self):
        carol = make_staff("carol@staff.dev", role=User.Role.HOUSEKEEPING)
        self.auth(carol)
        response = self.client.get(URL)
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertFalse(data["salary"]["configured"])
        self.assertIn("No payroll compensation has been configured", data["salary"]["message"])
        self.assertEqual(data["salary"]["payslips"], [])
        self.assertEqual(data["attendance"], [])
        self.assertEqual(data["shifts"], [])
        self.assertEqual(data["leave"]["requests"], [])
        self.assertEqual(data["disciplinary"], [])
        self.assertEqual(data["performance_reviews"], [])
        self.assertEqual(data["documents"], [])

    def test_attendance_date_filter(self):
        self.auth(self.alice)
        response = self.client.get(URL, {"attendance_from": "2000-01-01", "attendance_to": "2000-01-31"})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["data"]["attendance"], [])
        bad = self.client.get(URL, {"attendance_from": "not-a-date"})
        self.assertEqual(bad.status_code, 400, bad.content)

    def test_profile_is_read_only(self):
        self.auth(self.alice)
        self.assertEqual(self.client.post(URL, {}, format="json").status_code, 405)
        self.assertEqual(self.client.patch(URL, {"job_title": "Manager"}, format="json").status_code, 405)

    def test_manager_can_record_and_finalize_performance_review(self):
        self.auth(self.hr)
        created = self.client.post("/api/admin/staff-operations/performance-reviews/", {
            "staff": self.alice.pk, "review_date": str(hotel_today()), "rating": 5,
            "strengths": "Excellent", "status": "FINALIZED",
        }, format="json")
        self.assertEqual(created.status_code, 201, created.content)
        reference = created.json()["data"]["reference"]
        self.auth(self.alice)
        refs = [row["reference"] for row in self.client.get(URL).json()["data"]["performance_reviews"]]
        self.assertIn(reference, refs)
        # An ordinary employee cannot write reviews (endpoint is capability gated).
        forbidden = self.client.post("/api/admin/staff-operations/performance-reviews/", {
            "staff": self.alice.pk, "review_date": str(hotel_today()), "rating": 5, "status": "FINALIZED",
        }, format="json")
        self.assertEqual(forbidden.status_code, 403, forbidden.content)
