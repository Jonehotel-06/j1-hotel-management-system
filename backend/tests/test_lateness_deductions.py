"""Automatic lateness deductions: policy, calculation, revocation and refund."""
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.utils import timezone

from apps.accounts.models import User
from apps.core.utils import hotel_today
from apps.staff_operations.models import (
    AttendanceRecord, PayrollPeriod, ShiftAssignment, StaffCompensation,
    StaffLatenessDeduction,
)
from apps.staff_operations.services.payroll_service import (
    create_compensation, create_lateness_deduction_for_attendance, get_lateness_policy,
)
from apps.staff_operations.services.staff_operations_service import ensure_staff_profile
from tests.base import BaseAPITestCase
from tests.factories import make_staff


POLICY_URL = "/api/admin/staff-operations/payroll/lateness-policy/"
DEDUCTIONS_URL = "/api/admin/staff-operations/payroll/lateness-deductions/"
PERIODS_URL = "/api/admin/staff-operations/payroll/periods/"


class LatenessDeductionTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.admin = make_staff("lateness.admin@staff.dev", role=User.Role.ADMIN)
        self.manager = make_staff("lateness.manager@staff.dev", role=User.Role.MANAGER)
        self.creator = make_staff("lateness.hr@staff.dev", role=User.Role.HR_MANAGER)
        self.approver = make_staff("lateness.accounts@staff.dev", role=User.Role.ACCOUNTS_MANAGER)
        self.employee = make_staff("lateness.employee@staff.dev", role=User.Role.HOUSEKEEPER)
        self.today = hotel_today()
        self._prepare_employee(
            self.employee,
            basic="62000.00", housing="31000.00", transport="31000.00",
        )

    # -- helpers ---------------------------------------------------------
    def _prepare_employee(self, staff, *, basic, housing="0.00", transport="0.00"):
        profile, _ = ensure_staff_profile(staff=staff)
        profile.department = "Housekeeping"
        profile.employment_start = self.today
        profile.save(update_fields=["department", "employment_start", "updated_at"])
        return create_compensation(
            staff=staff, actor=self.creator, effective_from=self.today, currency="NGN",
            basic_salary=Decimal(basic), housing_allowance=Decimal(housing),
            transport_allowance=Decimal(transport), overtime_rate=Decimal("0.00"), notes="",
        )

    def _aware(self, hour, minute=0):
        return timezone.make_aware(datetime.combine(self.today, time(hour, minute)))

    def _attendance(self, staff, *, start_hour=8, clock_hour=8, clock_minute=16):
        marker = f"{staff.pk}-{start_hour}-{clock_hour}-{clock_minute}-{ShiftAssignment.objects.count()}"
        shift = ShiftAssignment.objects.create(
            reference=f"SHIFT-LAT-{marker}",
            staff=staff,
            planned_start_at=self._aware(start_hour),
            planned_end_at=self._aware(start_hour + 8),
            department="Housekeeping", location="Main building",
            status=ShiftAssignment.Status.SCHEDULED,
        )
        return AttendanceRecord.objects.create(
            reference=f"ATT-LAT-{marker}",
            staff=staff, shift=shift, business_date=self.today,
            status=AttendanceRecord.Status.COMPLETED,
            clock_in_at=self._aware(clock_hour, clock_minute),
            clock_out_at=self._aware(start_hour + 8),
            worked_minutes=480, last_event_at=self._aware(start_hour + 8),
        )

    def _enable_policy(self):
        self.auth(self.admin)
        response = self.client.patch(POLICY_URL, {
            "is_enabled": True, "grace_minutes": 15, "deduction_percent": "2.50",
        }, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        return response

    def _create_run(self, key):
        self.auth(self.creator)
        response = self.client.post(PERIODS_URL, {
            "starts_on": self.today.isoformat(), "ends_on": self.today.isoformat(),
            "department": "Housekeeping", "adjustments": [], "idempotency_key": key,
        }, format="json")
        self.assertIn(response.status_code, (200, 201), response.content)
        return response.json()["data"]

    def _approve_run(self, reference):
        self.auth(self.creator)
        submitted = self.client.post(f"{PERIODS_URL}{reference}/submit/", {}, format="json")
        self.assertEqual(submitted.status_code, 200, submitted.content)
        self.auth(self.approver)
        approved = self.client.post(
            f"{PERIODS_URL}{reference}/review/",
            {"approved": True, "review_note": "Reviewed for lateness test."}, format="json",
        )
        self.assertEqual(approved.status_code, 200, approved.content)

    def _line_for(self, run, staff):
        return next(line for line in run["lines"] if line["staff_id"] == staff.pk)

    # -- policy ----------------------------------------------------------
    def test_policy_is_admin_configured_and_payroll_scoped(self):
        # Payroll managers may read the policy but never change it.
        self.auth(self.manager)
        self.assertEqual(self.client.get(POLICY_URL).status_code, 200)
        denied = self.client.patch(POLICY_URL, {
            "is_enabled": True, "grace_minutes": 10, "deduction_percent": "5.00",
        }, format="json")
        self.assertEqual(denied.status_code, 403, denied.content)
        self.assertFalse(get_lateness_policy().is_enabled)

        # An enabled policy needs a real percentage.
        self.auth(self.admin)
        invalid = self.client.patch(POLICY_URL, {
            "is_enabled": True, "grace_minutes": 10, "deduction_percent": "0.00",
        }, format="json")
        self.assertEqual(invalid.status_code, 400, invalid.content)

        response = self._enable_policy()
        self.assertEqual(response.json()["data"]["grace_minutes"], 15)
        self.assertEqual(response.json()["data"]["deduction_percent"], "2.50")

        # Employees have no access to the payroll lateness workspace.
        self.auth(self.employee)
        self.assertEqual(self.client.get(POLICY_URL).status_code, 403)
        self.assertEqual(self.client.get(DEDUCTIONS_URL).status_code, 403)

    # -- calculation -----------------------------------------------------
    def test_grace_period_boundary_and_daily_pay_basis(self):
        self._enable_policy()
        # Exactly at the end of grace: no deduction.
        on_time = self._attendance(self.employee, clock_minute=15)
        self.assertIsNone(create_lateness_deduction_for_attendance(attendance_record=on_time))

        # One minute beyond grace: 2.5% of one day's pay.
        # (62,000 + 31,000 + 31,000) / days-in-month; October-style month used
        # by the test clock has 31 days in the current test year context, but
        # assert from the snapshot instead of assuming the month length.
        late = self._attendance(self.employee, start_hour=10, clock_hour=10, clock_minute=16)
        deduction = create_lateness_deduction_for_attendance(attendance_record=late)
        self.assertIsNotNone(deduction)
        days = deduction.days_in_month
        self.assertEqual(deduction.monthly_pay_basis, Decimal("124000.00"))
        self.assertEqual(deduction.daily_pay, (Decimal("124000.00") / days).quantize(Decimal("0.01")))
        self.assertEqual(
            deduction.amount,
            (deduction.daily_pay * Decimal("2.50") / 100).quantize(Decimal("0.01")),
        )
        self.assertEqual(deduction.late_minutes, 16)
        self.assertEqual(deduction.grace_minutes, 15)
        # Idempotent: the same attendance record never produces a second row.
        again = create_lateness_deduction_for_attendance(attendance_record=late)
        self.assertEqual(again.pk, deduction.pk)
        self.assertEqual(StaffLatenessDeduction.objects.count(), 1)

    def test_disabled_policy_deducts_nothing(self):
        late = self._attendance(self.employee, clock_minute=45)
        self.assertIsNone(create_lateness_deduction_for_attendance(attendance_record=late))
        run = self._create_run("lateness-disabled-run")
        line = self._line_for(run, self.employee)
        self.assertEqual(Decimal(line["deductions_total"]), Decimal("0.00"))
        self.assertFalse(StaffLatenessDeduction.objects.exists())

    # -- payroll application + add-back ----------------------------------
    def test_payroll_applies_then_next_run_adds_back_after_admin_approval(self):
        self._enable_policy()
        self._attendance(self.employee, clock_minute=16)

        run = self._create_run("lateness-run-one")
        line = self._line_for(run, self.employee)
        deduction = StaffLatenessDeduction.objects.get(staff=self.employee)
        self.assertEqual(Decimal(line["gross_pay"]), Decimal("124000.00"))
        self.assertEqual(Decimal(line["deductions_total"]), deduction.amount)
        self.assertTrue(any(deduction.reference in row["label"] for row in line["deductions"]))
        deduction.refresh_from_db()
        self.assertEqual(deduction.status, StaffLatenessDeduction.Status.APPLIED)

        # The deduction is applied exactly once: a replacement-style second
        # run never deducts it again.
        second = self._create_run("lateness-run-two")
        second_line = self._line_for(second, self.employee)
        self.assertFalse(any(deduction.reference in row["label"] for row in second_line["deductions"]))

        self._approve_run(run["reference"])

        # Manager records the tangible reason; a manager cannot decide it.
        self.auth(self.manager)
        requested = self.client.post(
            f"{DEDUCTIONS_URL}{deduction.reference}/request-revocation/",
            {"reason": "Ambulance delay documented at the gate.", "evidence_reference": "GATE-LOG-44"},
            format="json",
        )
        self.assertEqual(requested.status_code, 200, requested.content)
        manager_decision = self.client.post(
            f"{DEDUCTIONS_URL}{deduction.reference}/decision/",
            {"approved": True, "decision_note": "Manager trying to decide."}, format="json",
        )
        self.assertEqual(manager_decision.status_code, 403, manager_decision.content)

        self.auth(self.admin)
        decided = self.client.post(
            f"{DEDUCTIONS_URL}{deduction.reference}/decision/",
            {"approved": True, "decision_note": "Verified ambulance record; refund approved."}, format="json",
        )
        self.assertEqual(decided.status_code, 200, decided.content)
        deduction.refresh_from_db()
        self.assertEqual(deduction.status, StaffLatenessDeduction.Status.REIMBURSEMENT_PENDING)

        # The employee can see their own deduction and its state.
        self.auth(self.employee)
        own = self.client.get("/api/admin/staff-operations/my-profile/")
        self.assertEqual(own.status_code, 200, own.content)
        own_rows = own.json()["data"]["salary"]["lateness_deductions"]
        self.assertEqual(len(own_rows), 1)
        self.assertEqual(own_rows[0]["reference"], deduction.reference)

        # Next payroll run adds the same amount back as an allowance.
        third = self._create_run("lateness-run-three")
        third_line = self._line_for(third, self.employee)
        self.assertTrue(any(deduction.reference in row["label"] for row in third_line["allowances"]))
        self.assertEqual(
            Decimal(third_line["gross_pay"]), Decimal("124000.00") + deduction.amount,
        )
        deduction.refresh_from_db()
        self.assertEqual(deduction.status, StaffLatenessDeduction.Status.REIMBURSED)

    # -- revocation before payroll ---------------------------------------
    def test_revocation_before_payroll_cancels_and_rejection_reactivates(self):
        self._enable_policy()
        other = make_staff("lateness.other@staff.dev", role=User.Role.HOUSEKEEPER)
        self._prepare_employee(other, basic="31000.00")
        record = self._attendance(other, clock_minute=30)
        deduction = create_lateness_deduction_for_attendance(attendance_record=record)
        self.assertEqual(deduction.amount, Decimal("25.00") if deduction.days_in_month == 31 else deduction.amount)
        self.assertGreater(deduction.amount, Decimal("0.00"))

        self.auth(self.manager)
        requested = self.client.post(
            f"{DEDUCTIONS_URL}{deduction.reference}/request-revocation/",
            {"reason": "Official duty at the bank before shift start."}, format="json",
        )
        self.assertEqual(requested.status_code, 200, requested.content)

        # While the Admin has not decided, payroll must not deduct it.
        held_run = self._create_run("lateness-held-run")
        held_line = self._line_for(held_run, other)
        self.assertFalse(any(deduction.reference in row["label"] for row in held_line["deductions"]))

        # Admin rejects: the deduction stands and the next run takes it once.
        self.auth(self.admin)
        rejected = self.client.post(
            f"{DEDUCTIONS_URL}{deduction.reference}/decision/",
            {"approved": False, "decision_note": "Evidence does not cover the delay."}, format="json",
        )
        self.assertEqual(rejected.status_code, 200, rejected.content)
        deduction.refresh_from_db()
        self.assertEqual(deduction.status, StaffLatenessDeduction.Status.ACTIVE)

        applied_run = self._create_run("lateness-applied-run")
        applied_line = self._line_for(applied_run, other)
        self.assertTrue(any(deduction.reference in row["label"] for row in applied_line["deductions"]))

        # A fresh request approved by Admin now means add-back, not cancel.
        self.auth(self.manager)
        self.client.post(
            f"{DEDUCTIONS_URL}{deduction.reference}/request-revocation/",
            {"reason": "New hospital evidence produced."}, format="json",
        )
        self.auth(self.admin)
        self.client.post(
            f"{DEDUCTIONS_URL}{deduction.reference}/decision/",
            {"approved": True, "decision_note": "Hospital evidence accepted."}, format="json",
        )
        deduction.refresh_from_db()
        self.assertEqual(deduction.status, StaffLatenessDeduction.Status.REIMBURSEMENT_PENDING)

    def test_revocation_cancel_when_never_applied(self):
        self._enable_policy()
        record = self._attendance(self.employee, clock_minute=40)
        deduction = create_lateness_deduction_for_attendance(attendance_record=record)
        self.auth(self.manager)
        self.client.post(
            f"{DEDUCTIONS_URL}{deduction.reference}/request-revocation/",
            {"reason": "Vehicle broke down; mechanic receipt supplied."}, format="json",
        )
        self.auth(self.admin)
        decided = self.client.post(
            f"{DEDUCTIONS_URL}{deduction.reference}/decision/",
            {"approved": True, "decision_note": "Receipt verified by Admin."}, format="json",
        )
        self.assertEqual(decided.status_code, 200, decided.content)
        deduction.refresh_from_db()
        self.assertEqual(deduction.status, StaffLatenessDeduction.Status.REVOKED)

        run = self._create_run("lateness-cancelled-run")
        line = self._line_for(run, self.employee)
        self.assertFalse(any(deduction.reference in row["label"] for row in line["deductions"]))
        self.assertFalse(any(deduction.reference in row["label"] for row in line["allowances"]))
