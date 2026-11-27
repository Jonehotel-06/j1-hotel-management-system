"""Salary adjustments: proration, unpaid absence, disciplinary, other — calculation,
approval control, payroll/ledger integration, duplicate prevention and privacy.

Scenario numbers in the test names follow the acceptance list for the feature
(1 full month … 11 reopen/refresh).
"""
from datetime import date, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError

from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.core.utils import hotel_today
from apps.finance.models import FinancialLine, FinancialTransaction
from apps.finance.services import accounting
from apps.staff_operations.models import (
    LeaveRequest, PayrollLine, PayrollPeriod, PayrollSalaryAdjustment, PayrollSalaryAdjustmentEvent,
    PayrollSalaryPayment, StaffCompensation,
)
from apps.staff_operations.services.payroll_adjustment_service import calculate_adjustment, month_bounds
from apps.staff_operations.services.staff_operations_service import ensure_staff_profile
from tests.base import BaseAPITestCase
from tests.factories import make_staff

BASE = "/api/admin/staff-operations/payroll"
ADJ = f"{BASE}/salary-adjustments/"


class SalaryAdjustmentApiTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.recorder = make_staff("adj.accountant@staff.dev", role=User.Role.ACCOUNTANT)      # record, no approve
        self.hr = make_staff("adj.hr@staff.dev", role=User.Role.HR_MANAGER)                   # prepares payroll runs
        self.approver = make_staff("adj.accounts.manager@staff.dev", role=User.Role.ACCOUNTS_MANAGER)
        self.employee = self._employee("adj.employee@staff.dev", "60000.00")
        self.today = hotel_today()
        self.year, self.month = self.today.year, self.today.month
        self.counter = 0

    # ------------------------------------------------------------------ helpers
    def _employee(self, email, basic, housing="0.00", transport="0.00", department="Housekeeping"):
        staff = make_staff(email, role=User.Role.HOUSEKEEPER)
        profile, _ = ensure_staff_profile(staff=staff)
        profile.department = department
        profile.save(update_fields=["department", "updated_at"])
        self.auth(self.hr)
        response = self.client.post(f"{BASE}/compensation/", {
            "staff_id": staff.pk, "effective_from": hotel_today().isoformat(), "currency": "NGN",
            "basic_salary": basic, "housing_allowance": housing, "transport_allowance": transport, "overtime_rate": "0.00",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        return staff

    def _key(self, prefix="adj"):
        self.counter += 1
        return f"{prefix}-{self.counter:04d}"

    def _record(self, payload, *, actor=None, expect=201):
        self.auth(actor or self.recorder)
        body = {"staff_id": self.employee.pk, "payroll_year": self.year, "payroll_month": self.month,
                "calculation_basis": "FIXED_30", "idempotency_key": self._key()}
        body.update(payload)
        response = self.client.post(ADJ, body, format="json")
        self.assertEqual(response.status_code, expect, response.content)
        return response.json()

    def _approve(self, reference, approved=True, actor=None, expect=200):
        self.auth(actor or self.approver)
        response = self.client.post(f"{ADJ}{reference}/decision/", {
            "approved": approved, "decision_note": "Independent review of the salary adjustment.",
        }, format="json")
        self.assertEqual(response.status_code, expect, response.content)
        return response.json()

    def _run(self, *, staff=None, key=None, expect=201, adjustments=None, department="Housekeeping"):
        self.auth(self.hr)
        payload = {
            "starts_on": self.today.isoformat(), "ends_on": self.today.isoformat(), "department": department,
            "idempotency_key": key or self._key("run"), "adjustments": adjustments or [],
        }
        response = self.client.post(f"{BASE}/periods/", payload, format="json")
        self.assertEqual(response.status_code, expect, response.content)
        return response.json()

    def _approve_run(self, reference):
        self.auth(self.hr)
        submitted = self.client.post(f"{BASE}/periods/{reference}/submit/", {}, format="json")
        self.assertEqual(submitted.status_code, 200, submitted.content)
        self.auth(self.approver)
        reviewed = self.client.post(f"{BASE}/periods/{reference}/review/", {
            "approved": True, "review_note": "Payroll approved for the adjustment test.",
        }, format="json")
        self.assertEqual(reviewed.status_code, 200, reviewed.content)
        return reviewed.json()["data"]

    def _line_for(self, run_data, staff):
        return next(line for line in run_data["lines"] if line["staff_id"] == staff.pk)

    def _pay_line(self, reference, line_id, amount, key):
        self.auth(self.hr)
        response = self.client.post(f"{BASE}/periods/{reference}/lines/{line_id}/payments/", {
            "amount": amount, "method": "BANK_TRANSFER", "payment_date": self.today.isoformat(),
            "external_reference": f"TRF-{key}", "idempotency_key": key,
        }, format="json")
        self.assertIn(response.status_code, (200, 201), response.content)  # 200 = idempotent replay of the same key
        return response.json()["data"]

    # ------------------------------------------------------------- scenario 1
    def test_01_full_month_without_adjustment_pays_normal_salary(self):
        run = self._run()["data"]
        line = self._line_for(run, self.employee)
        self.assertEqual(Decimal(line["basic_pay"]), Decimal("60000.00"))
        self.assertEqual(Decimal(line["net_pay"]), Decimal("60000.00"))
        self.assertEqual(PayrollSalaryAdjustment.objects.count(), 0)

    def test_01b_full_month_prorated_record_equals_normal_salary(self):
        created = self._record({"adjustment_type": "PRORATED", "days_worked": 30, "reason": "Full month check"})["data"]
        self.assertEqual(Decimal(created["monthly_salary"]), Decimal("60000.00"))
        self.assertEqual(Decimal(created["prorated_salary"]), Decimal("60000.00"))
        self.assertEqual(Decimal(created["final_payable"]), Decimal("60000.00"))
        self.assertEqual(created["status"], "PENDING_APPROVAL")
        self._approve(created["reference"])
        line = self._line_for(self._run()["data"], self.employee)
        self.assertEqual(Decimal(line["net_pay"]), Decimal("60000.00"))

    # ------------------------------------------------------------- scenario 2
    def test_02_half_month_prorated_salary_is_30000(self):
        created = self._record({"adjustment_type": "PRORATED", "days_worked": 15, "reason": "Joined mid-month"})["data"]
        self.assertEqual(Decimal(created["prorated_salary"]), Decimal("30000.00"))
        self.assertEqual(Decimal(created["daily_rate"]), Decimal("2000.00"))
        self.assertEqual(created["applicable_days"], 30)
        self.assertEqual(Decimal(created["final_payable"]), Decimal("30000.00"))
        self.assertEqual(created["month_summary"]["prorated_salary"], "30000.00")
        self._approve(created["reference"])
        run = self._run()["data"]
        line = self._line_for(run, self.employee)
        self.assertEqual(Decimal(line["basic_pay"]), Decimal("30000.00"))
        self.assertEqual(Decimal(line["gross_pay"]), Decimal("30000.00"))
        self.assertEqual(Decimal(line["net_pay"]), Decimal("30000.00"))
        self.assertEqual(Decimal(run["total_net"]), Decimal("30000.00"))
        # The original monthly salary terms are untouched.
        self.assertEqual(StaffCompensation.objects.get(staff=self.employee).basic_salary, Decimal("60000.00"))
        record = PayrollSalaryAdjustment.objects.get(reference=created["reference"])
        self.assertEqual(record.applied_payroll_line_id, line["id"])
        self.assertEqual(record.monthly_salary, Decimal("60000.00"))

    def test_02b_calendar_basis_rounds_half_up_and_components_add_up(self):
        staff = self._employee("adj.calendar@staff.dev", "50000.00", housing="7000.00", transport="3000.00")
        compensation = StaffCompensation.objects.get(staff=staff)
        calc = calculate_adjustment(
            compensation=compensation, adjustment_type="PRORATED", calculation_basis="CALENDAR_DAYS",
            payroll_year=2026, payroll_month=3, days_worked=15,
        )
        self.assertEqual(calc.calendar_days, 31)
        self.assertEqual(calc.prorated_salary, Decimal("29032.26"))  # 60,000 × 15 ÷ 31 = 29,032.258…
        self.assertEqual(calc.prorated_basic + calc.prorated_housing + calc.prorated_transport, calc.prorated_salary)
        self.assertEqual(calc.final_payable, Decimal("29032.26"))
        fixed = calculate_adjustment(
            compensation=compensation, adjustment_type="PRORATED", calculation_basis="FIXED_30",
            payroll_year=2026, payroll_month=3, days_worked=15,
        )
        self.assertEqual(fixed.prorated_salary, Decimal("30000.00"))
        scheduled = calculate_adjustment(
            compensation=compensation, adjustment_type="PRORATED", calculation_basis="SCHEDULED_DAYS",
            payroll_year=2026, payroll_month=3, days_worked=11, applicable_days=22,
        )
        self.assertEqual(scheduled.prorated_salary, Decimal("30000.00"))
        with self.assertRaises(ValidationError):
            calculate_adjustment(compensation=compensation, adjustment_type="PRORATED", calculation_basis="SCHEDULED_DAYS",
                                 payroll_year=2026, payroll_month=3, days_worked=11)
        with self.assertRaises(ValidationError):
            calculate_adjustment(compensation=compensation, adjustment_type="PRORATED", calculation_basis="FIXED_30",
                                 payroll_year=2026, payroll_month=3, days_worked=31)

    # ------------------------------------------------------------- scenario 3
    def test_03_three_unpaid_absence_days_on_90000_pays_81000(self):
        staff = self._employee("adj.absent@staff.dev", "90000.00")
        created = self._record({
            "staff_id": staff.pk, "adjustment_type": "ABSENCE", "absence_days": 3, "reason": "Three days absent without leave",
        })["data"]
        self.assertEqual(Decimal(created["absence_deduction"]), Decimal("9000.00"))
        self.assertEqual(Decimal(created["final_payable"]), Decimal("81000.00"))
        self._approve(created["reference"])
        run = self._run()["data"]
        line = self._line_for(run, staff)
        self.assertEqual(Decimal(line["basic_pay"]), Decimal("90000.00"))
        self.assertEqual(Decimal(line["deductions_total"]), Decimal("9000.00"))
        self.assertEqual(Decimal(line["net_pay"]), Decimal("81000.00"))
        self.assertTrue(any("Unpaid absence" in row["label"] and created["reference"] in row["label"] for row in line["deductions"]))
        # The other employee in the department is unaffected.
        self.assertEqual(Decimal(self._line_for(run, self.employee)["net_pay"]), Decimal("60000.00"))

    def test_03b_approved_leave_is_never_counted_as_unpaid_absence(self):
        first, last = month_bounds(self.year, self.month)
        LeaveRequest.objects.create(
            reference="LEAVE-ADJ-1", staff=self.employee, type=LeaveRequest.Type.ANNUAL,
            status=LeaveRequest.Status.APPROVED, start_date=first, end_date=first + timedelta(days=4),
        )
        self.auth(self.recorder)
        context = self.client.get(ADJ, {"options": "context", "staff_id": self.employee.pk, "payroll_year": self.year, "payroll_month": self.month}).json()["data"]
        self.assertEqual(context["approved_paid_leave_days"], 5)
        self.assertEqual(context["compensation"]["monthly_salary"], "60000.00")
        days_in_month = (last - first).days + 1
        blocked = self._record({"adjustment_type": "ABSENCE", "absence_days": days_in_month - 4, "reason": "Absent most of month",
                                "calculation_basis": "CALENDAR_DAYS"}, expect=400)
        self.assertIn("approved leave", str(blocked).lower())
        # No deduction is ever created automatically from leave, and a plain run pays the full salary.
        self.assertEqual(PayrollSalaryAdjustment.objects.count(), 0)
        line = self._line_for(self._run()["data"], self.employee)
        self.assertEqual(Decimal(line["net_pay"]), Decimal("60000.00"))

    # ------------------------------------------------------------- scenario 4
    def test_04_disciplinary_deduction_of_5000_pays_55000(self):
        incomplete = self._record({"adjustment_type": "DISCIPLINARY", "amount": "5000.00", "reason": "Left shift early"}, expect=400)
        self.assertIn("violation_category", incomplete.get("errors", incomplete))
        created = self._record({
            "adjustment_type": "DISCIPLINARY", "amount": "5000.00", "violation_category": "MISCONDUCT",
            "incident_date": (self.today - timedelta(days=2)).isoformat(), "reason": "Left shift early without handover",
            "evidence_reference": "Supervisor report SR-14",
        })["data"]
        self.assertEqual(Decimal(created["disciplinary_deduction"]), Decimal("5000.00"))
        self.assertEqual(Decimal(created["final_payable"]), Decimal("55000.00"))
        self._approve(created["reference"])
        line = self._line_for(self._run()["data"], self.employee)
        self.assertEqual(Decimal(line["net_pay"]), Decimal("55000.00"))
        self.assertTrue(any("Disciplinary deduction" in row["label"] for row in line["deductions"]))

    # ------------------------------------------------------------- scenario 5
    def test_05_combined_prorated_disciplinary_and_additional_payment_pays_57000(self):
        prorated = self._record({"adjustment_type": "PRORATED", "days_worked": 30, "reason": "Full month"})["data"]
        disciplinary = self._record({
            "adjustment_type": "DISCIPLINARY", "amount": "5000.00", "violation_category": "NEGLIGENCE",
            "incident_date": self.today.isoformat(), "reason": "Damaged guest-room equipment", "notes": "Maintenance report attached",
        })["data"]
        addition = self._record({
            "adjustment_type": "OTHER", "amount": "2000.00", "other_direction": "ADDITION", "reason": "Approved weekend cover allowance",
        })["data"]
        self.assertEqual(addition["month_summary"]["final_payable_salary"], "57000.00")
        for ref in (prorated["reference"], disciplinary["reference"], addition["reference"]):
            self._approve(ref)
        self.auth(self.recorder)
        context = self.client.get(ADJ, {"options": "context", "staff_id": self.employee.pk, "payroll_year": self.year, "payroll_month": self.month}).json()["data"]
        summary = context["summary"]
        self.assertEqual(summary["normal_monthly_salary"], "60000.00")
        self.assertEqual(summary["prorated_salary"], "60000.00")
        self.assertEqual(summary["disciplinary_deduction"], "5000.00")
        self.assertEqual(summary["additional_payment"], "2000.00")
        self.assertEqual(summary["final_payable_salary"], "57000.00")
        run = self._run()["data"]
        line = self._line_for(run, self.employee)
        self.assertEqual(Decimal(line["gross_pay"]), Decimal("62000.00"))
        self.assertEqual(Decimal(line["deductions_total"]), Decimal("5000.00"))
        self.assertEqual(Decimal(line["net_pay"]), Decimal("57000.00"))
        self.assertEqual(PayrollSalaryAdjustment.objects.filter(applied_payroll_line_id=line["id"]).count(), 3)

    def test_05b_deductions_above_eligible_salary_are_refused(self):
        self._record({"adjustment_type": "PRORATED", "days_worked": 3, "reason": "Left early in month"})
        blocked = self._record({
            "adjustment_type": "DISCIPLINARY", "amount": "7000.00", "violation_category": "MISCONDUCT",
            "incident_date": self.today.isoformat(), "reason": "Serious misconduct", "notes": "Report attached",
        }, expect=400)
        self.assertIn("exceed", str(blocked).lower())
        self.assertEqual(PayrollSalaryAdjustment.objects.filter(adjustment_type="DISCIPLINARY").count(), 0)
        preview = self.client.post(f"{ADJ}preview/", {
            "staff_id": self.employee.pk, "payroll_year": self.year, "payroll_month": self.month, "calculation_basis": "FIXED_30",
            "adjustment_type": "DISCIPLINARY", "amount": "6000.00", "violation_category": "MISCONDUCT",
        }, format="json")
        self.assertEqual(preview.status_code, 200, preview.content)
        self.assertEqual(preview.json()["data"]["summary"]["final_payable_salary"], "0.00")
        self.assertEqual(PayrollSalaryAdjustment.objects.count(), 1)  # a preview never saves anything

    # ------------------------------------------------------------- scenario 6
    def test_06_duplicate_prevention(self):
        first = self._record({"adjustment_type": "PRORATED", "days_worked": 15, "reason": "Joined mid-month", "idempotency_key": "adj-dup-1"})
        replay = self._record({"adjustment_type": "PRORATED", "days_worked": 15, "reason": "Joined mid-month", "idempotency_key": "adj-dup-1"}, expect=200)
        self.assertEqual(replay["data"]["reference"], first["data"]["reference"])
        duplicate = self._record({"adjustment_type": "PRORATED", "days_worked": 10, "reason": "Second attempt"}, expect=400)
        self.assertIn("already exists", str(duplicate))
        self.assertEqual(PayrollSalaryAdjustment.objects.filter(adjustment_type="PRORATED").count(), 1)
        # Two distinct disciplinary incidents are allowed; the same incident twice is not.
        base = {"adjustment_type": "DISCIPLINARY", "amount": "1000.00", "violation_category": "LATENESS",
                "reason": "Persistent lateness this week", "notes": "Attendance printout"}
        self._record({**base, "incident_date": (self.today - timedelta(days=1)).isoformat()})
        self._record({**base, "incident_date": self.today.isoformat()})
        self._record({**base, "incident_date": self.today.isoformat()}, expect=400)
        self.assertEqual(PayrollSalaryAdjustment.objects.filter(adjustment_type="DISCIPLINARY").count(), 2)
        # A rejected record stops blocking the month.
        self._approve(first["data"]["reference"], approved=False)
        again = self._record({"adjustment_type": "PRORATED", "days_worked": 10, "reason": "Corrected days"})
        self.assertEqual(Decimal(again["data"]["prorated_salary"]), Decimal("20000.00"))

    # ------------------------------------------------------------- scenario 7
    def test_07_unauthorized_access_is_denied_by_the_backend(self):
        created = self._record({"adjustment_type": "PRORATED", "days_worked": 15, "reason": "Joined mid-month"})["data"]
        reference = created["reference"]
        self.unauth()
        self.assertEqual(self.client.get(ADJ).status_code, 401)
        staff_only = make_staff("adj.ordinary@staff.dev", role=User.Role.HOUSEKEEPER)
        self.auth(staff_only)
        self.assertEqual(self.client.get(ADJ).status_code, 403)
        self.assertEqual(self.client.get(f"{ADJ}{reference}/").status_code, 403)
        self.assertEqual(self.client.get(ADJ, {"options": "context", "staff_id": self.employee.pk, "payroll_year": self.year, "payroll_month": self.month}).status_code, 403)
        self.assertEqual(self.client.post(ADJ, {"staff_id": self.employee.pk, "payroll_year": self.year, "payroll_month": self.month,
                                                "adjustment_type": "OTHER", "amount": "1.00", "other_direction": "ADDITION", "reason": "Trying it"}, format="json").status_code, 403)
        self.assertEqual(self.client.post(f"{ADJ}{reference}/decision/", {"approved": True, "decision_note": "Not allowed"}, format="json").status_code, 403)
        self.assertEqual(self.client.post(f"{ADJ}preview/", {"staff_id": self.employee.pk, "payroll_year": self.year, "payroll_month": self.month,
                                                            "adjustment_type": "PRORATED", "days_worked": 1}, format="json").status_code, 403)
        # The accountant can record and view but cannot approve; the recorder cannot approve their own record.
        self._approve(reference, actor=self.recorder, expect=403)
        hr_record = self._record({"adjustment_type": "OTHER", "amount": "500.00", "other_direction": "DEDUCTION", "reason": "Canteen debt"}, actor=self.hr)["data"]
        self._approve(hr_record["reference"], actor=self.hr, expect=403)
        self.assertEqual(PayrollSalaryAdjustment.objects.get(reference=reference).status, "PENDING_APPROVAL")
        # A procurement officer holds no payroll capability at all.
        self.auth(make_staff("adj.procurement@staff.dev", role=User.Role.PROCUREMENT_OFFICER))
        self.assertEqual(self.client.get(ADJ).status_code, 403)

    # ------------------------------------------------------------- scenario 8
    def test_08_historical_records_are_preserved_and_never_recalculated(self):
        created = self._record({"adjustment_type": "PRORATED", "days_worked": 15, "reason": "Joined mid-month"})["data"]
        reference = created["reference"]
        self._approve(reference)
        run = self._run()["data"]
        record = PayrollSalaryAdjustment.objects.get(reference=reference)
        # Approved records cannot be edited or deleted — only reversed through the audited path.
        record.days_worked = 20
        record.prorated_salary = Decimal("40000.00")
        with self.assertRaises(ValidationError):
            record.save()
        with self.assertRaises(ValidationError):
            record.delete()
        with self.assertRaises(ValidationError):
            PayrollSalaryAdjustmentEvent.objects.filter(adjustment=record).first().delete()
        self.auth(self.hr)
        self.assertEqual(self.client.patch(f"{ADJ}{reference}/", {"days_worked": 20}, format="json").status_code, 405)
        self.assertEqual(self.client.delete(f"{ADJ}{reference}/").status_code, 405)
        # Once frozen in a payroll run the approved record cannot be reversed; payroll is the correction path.
        self.auth(self.approver)
        blocked = self.client.post(f"{ADJ}{reference}/reverse/", {"reason": "Changed my mind"}, format="json")
        self.assertEqual(blocked.status_code, 400, blocked.content)
        # A later salary change does not rewrite the snapshot or the frozen payroll line.
        self.auth(self.hr)
        change = self.client.post(f"{BASE}/compensation/", {
            "staff_id": self.employee.pk, "effective_from": (self.today + timedelta(days=1)).isoformat(), "currency": "NGN",
            "basic_salary": "75000.00", "overtime_rate": "0.00", "notes": "Raise",
        }, format="json")
        self.assertEqual(change.status_code, 201, change.content)
        fresh = PayrollSalaryAdjustment.objects.get(reference=reference)
        self.assertEqual(fresh.monthly_salary, Decimal("60000.00"))
        self.assertEqual(fresh.prorated_salary, Decimal("30000.00"))
        self.assertEqual(PayrollLine.objects.get(pk=self._line_for(run, self.employee)["id"]).net_pay, Decimal("30000.00"))
        # New adjustments for an already-processed month are refused instead of recalculating history.
        late = self._record({"adjustment_type": "OTHER", "amount": "100.00", "other_direction": "DEDUCTION", "reason": "Late correction"}, expect=400)
        self.assertIn("already includes", str(late))
        detail = self.client.get(f"{ADJ}{reference}/").json()["data"]
        self.assertEqual([e["type"] for e in detail["events"]], ["CREATED", "SUBMITTED", "APPROVED", "APPLIED"])
        self.assertTrue(AuditLog.objects.filter(action="PAYROLL_SALARY_ADJUSTMENT_APPROVED", object_id=str(record.pk)).exists())

    # ------------------------------------------------------------- scenario 9
    def test_09_unapproved_adjustment_can_never_be_paid(self):
        pending = self._record({"adjustment_type": "PRORATED", "days_worked": 15, "reason": "Joined mid-month"})["data"]
        blocked = self._run(expect=400)
        self.assertIn("awaiting approval", str(blocked))
        self.assertEqual(PayrollPeriod.objects.count(), 0)
        # A draft is an intention only: it never changes pay and cannot be approved once the month is run.
        self._approve(pending["reference"], approved=False)
        draft = self._record({"adjustment_type": "PRORATED", "days_worked": 10, "reason": "Draft only", "submit": False})["data"]
        self.assertEqual(draft["status"], "DRAFT")
        run = self._run()["data"]
        self.assertEqual(Decimal(self._line_for(run, self.employee)["net_pay"]), Decimal("60000.00"))
        self.auth(self.recorder)
        late_submit = self.client.post(f"{ADJ}{draft['reference']}/submit/", {}, format="json")
        self.assertEqual(late_submit.status_code, 400, late_submit.content)
        self.assertIsNone(PayrollSalaryAdjustment.objects.get(reference=draft["reference"]).applied_payroll_line_id)
        # Approval alone pays nothing: no ledger entry and no payment row exist for an approved adjustment.
        self.assertEqual(FinancialTransaction.objects.count(), 0)
        self.assertEqual(PayrollSalaryPayment.objects.count(), 0)

    # ------------------------------------------------------------ scenario 10
    def test_10_exactly_one_salary_expense_and_one_payment_entry(self):
        prorated = self._record({"adjustment_type": "PRORATED", "days_worked": 30, "reason": "Full month"})["data"]
        disciplinary = self._record({
            "adjustment_type": "DISCIPLINARY", "amount": "5000.00", "violation_category": "NEGLIGENCE",
            "incident_date": self.today.isoformat(), "reason": "Damaged equipment", "notes": "Report attached",
        })["data"]
        addition = self._record({"adjustment_type": "OTHER", "amount": "2000.00", "other_direction": "ADDITION", "reason": "Weekend cover"})["data"]
        for ref in (prorated["reference"], disciplinary["reference"], addition["reference"]):
            self._approve(ref)
        reference = self._run()["data"]["reference"]
        approved = self._approve_run(reference)
        line = self._line_for(approved, self.employee)
        period = PayrollPeriod.objects.get(reference=reference)
        accruals = FinancialTransaction.objects.filter(type=FinancialTransaction.Type.PAYROLL_ACCRUAL)
        self.assertEqual(accruals.count(), 1)
        expense_lines = FinancialLine.objects.filter(transaction=accruals.first(), account_code=accounting.PAYROLL_EXPENSE, direction=FinancialLine.Direction.DEBIT)
        self.assertEqual(expense_lines.count(), 1)
        self.assertEqual(expense_lines.first().amount, Decimal("62000.00"))     # approved gross (60,000 + 2,000 addition)
        self.assertEqual(period.total_net, Decimal("57000.00"))                  # payable after the 5,000 deduction
        payable = FinancialLine.objects.get(transaction=accruals.first(), account_code=accounting.PAYROLL_PAYABLE)
        self.assertEqual(payable.amount, Decimal("57000.00"))
        self.assertFalse(FinancialLine.objects.filter(transaction=accruals.first(), account_code__startswith="REVENUE").exists())
        # Approved but unpaid: still APPROVED, never PAID.
        self.assertEqual(set(PayrollSalaryAdjustment.objects.values_list("status", flat=True)), {"APPROVED"})
        payment = self._pay_line(reference, line["id"], "57000.00", "sal-adj-001")
        replay = self._pay_line(reference, line["id"], "57000.00", "sal-adj-001")
        self.assertEqual(replay["reference"], payment["reference"])
        payments = FinancialTransaction.objects.filter(type=FinancialTransaction.Type.PAYROLL_PAYMENT)
        self.assertEqual(payments.count(), 1)
        self.assertEqual(PayrollSalaryPayment.objects.count(), 1)
        self.assertEqual(FinancialLine.objects.filter(account_code=accounting.PAYROLL_EXPENSE).count(), 1)
        self.assertEqual(set(PayrollSalaryAdjustment.objects.values_list("status", flat=True)), {"PAID"})
        self.assertEqual(PayrollPeriod.objects.get(reference=reference).status, "PAID")
        # An audited payment reversal reopens the adjustment; nothing is deleted or duplicated.
        self.auth(self.approver)
        reversed_payment = self.client.post(f"{BASE}/periods/{reference}/lines/{line['id']}/payments/{payment['reference']}/reverse/", {
            "correction_reason": "Transfer bounced", "payment_date": self.today.isoformat(), "idempotency_key": "sal-adj-rev-001",
        }, format="json")
        self.assertEqual(reversed_payment.status_code, 201, reversed_payment.content)
        self.assertEqual(set(PayrollSalaryAdjustment.objects.values_list("status", flat=True)), {"APPROVED"})
        self.assertEqual(FinancialLine.objects.filter(account_code=accounting.PAYROLL_EXPENSE).count(), 1)
        self.assertEqual(PayrollSalaryAdjustment.objects.count(), 3)

    def test_10b_full_run_settlement_marks_adjustments_paid_once(self):
        created = self._record({"adjustment_type": "ABSENCE", "absence_days": 2, "reason": "Two days absent"})["data"]
        self._approve(created["reference"])
        reference = self._run()["data"]["reference"]
        self._approve_run(reference)
        self.auth(self.hr)
        paid = self.client.post(f"{BASE}/periods/{reference}/pay/", {"method": "BANK_TRANSFER", "external_reference": "BULK-1"}, format="json")
        self.assertEqual(paid.status_code, 200, paid.content)
        again = self.client.post(f"{BASE}/periods/{reference}/pay/", {"method": "BANK_TRANSFER", "external_reference": "BULK-1"}, format="json")
        self.assertEqual(again.status_code, 200, again.content)
        self.assertEqual(FinancialTransaction.objects.filter(type=FinancialTransaction.Type.PAYROLL_PAYMENT).count(), 1)
        record = PayrollSalaryAdjustment.objects.get(reference=created["reference"])
        self.assertEqual(record.status, "PAID")
        self.assertEqual(record.events.filter(type="PAID").count(), 1)

    # ------------------------------------------------------------ scenario 11
    def test_11_reopen_and_refresh_create_no_duplicates(self):
        created = self._record({"adjustment_type": "PRORATED", "days_worked": 15, "reason": "Joined mid-month"})["data"]
        reference = created["reference"]
        self._approve(reference)
        run = self._run(key="run-fixed")["data"]
        # Refreshing: repeated reads and an idempotent re-POST of the same run change nothing.
        self.auth(self.hr)
        for _ in range(3):
            self.assertEqual(self.client.get(f"{ADJ}{reference}/").status_code, 200)
            self.assertEqual(self.client.get(ADJ, {"staff_id": self.employee.pk}).status_code, 200)
            self.assertEqual(self.client.get(f"{BASE}/periods/{run['reference']}/").status_code, 200)
        replay = self._run(key="run-fixed", expect=200)
        self.assertEqual(replay["data"]["reference"], run["reference"])
        self.assertEqual(PayrollSalaryAdjustment.objects.count(), 1)
        self.assertEqual(PayrollSalaryAdjustment.objects.get(reference=reference).events.filter(type="APPLIED").count(), 1)
        # Reopening: a rejected run releases the adjustment; the replacement run applies it exactly once more.
        self.client.post(f"{BASE}/periods/{run['reference']}/submit/", {}, format="json")
        self.auth(self.approver)
        rejected = self.client.post(f"{BASE}/periods/{run['reference']}/review/", {"approved": False, "review_note": "Wrong department filter"}, format="json")
        self.assertEqual(rejected.status_code, 200, rejected.content)
        record = PayrollSalaryAdjustment.objects.get(reference=reference)
        self.assertIsNone(record.applied_payroll_line_id)
        self.assertEqual(record.status, "APPROVED")
        self.auth(self.hr)
        replacement = self.client.post(f"{BASE}/periods/", {
            "starts_on": self.today.isoformat(), "ends_on": self.today.isoformat(), "department": "Housekeeping",
            "idempotency_key": "run-replacement", "replaces_reference": run["reference"], "adjustments": [],
        }, format="json")
        self.assertEqual(replacement.status_code, 201, replacement.content)
        line = self._line_for(replacement.json()["data"], self.employee)
        self.assertEqual(Decimal(line["net_pay"]), Decimal("30000.00"))
        record = PayrollSalaryAdjustment.objects.get(reference=reference)
        self.assertEqual(record.applied_payroll_line_id, line["id"])
        self.assertEqual([e["type"] for e in record.events.values("type")], ["CREATED", "SUBMITTED", "APPROVED", "APPLIED", "RELEASED", "APPLIED"])
        self._approve_run(replacement.json()["data"]["reference"])
        self.assertEqual(FinancialTransaction.objects.filter(type=FinancialTransaction.Type.PAYROLL_ACCRUAL).count(), 1)
        self.assertEqual(PayrollSalaryAdjustment.objects.count(), 1)

    # ------------------------------------------------------- history & inbox
    def test_history_filters_and_approvals_inbox(self):
        other = self._employee("adj.kitchen@staff.dev", "40000.00", department="Kitchen")
        a = self._record({"adjustment_type": "PRORATED", "days_worked": 15, "reason": "Joined mid-month"})["data"]
        b = self._record({"staff_id": other.pk, "adjustment_type": "ABSENCE", "absence_days": 1, "reason": "One day absent"})["data"]
        self._approve(a["reference"])
        self.auth(self.approver)
        inbox = self.client.get("/api/admin/reports/approvals/").json()["data"]
        category = next(c for c in inbox["categories"] if c["key"] == "salary_adjustments")
        self.assertEqual(category["count"], 1)
        self.assertEqual(category["items"][0]["reference"], b["reference"])
        self.assertEqual(category["href"], "payroll.html")
        self.auth(self.recorder)
        def refs(**params):
            return [row["reference"] for row in self.client.get(ADJ, params).json()["data"]]
        self.assertEqual(set(refs()), {a["reference"], b["reference"]})
        self.assertEqual(refs(department="Kitchen"), [b["reference"]])
        self.assertEqual(refs(staff_id=self.employee.pk), [a["reference"]])
        self.assertEqual(refs(adjustment_type="ABSENCE"), [b["reference"]])
        self.assertEqual(refs(status="APPROVED"), [a["reference"]])
        self.assertEqual(set(refs(payroll_year=self.year, payroll_month=self.month)), {a["reference"], b["reference"]})
        self.assertEqual(refs(payroll_year=self.year - 1), [])
        self.assertEqual(refs(search="kitchen"), [b["reference"]])
        row = self.client.get(ADJ, {"search": "adj.kitchen"}).json()["data"][0]
        self.assertEqual(row["department"], "Kitchen")
        self.assertEqual(row["status_label"], "Pending approval")
        # Recording does not touch the existing payroll endpoints' shape.
        periods = self.client.get(f"{BASE}/periods/")
        self.assertEqual(periods.status_code, 200)
        self.assertIn("pagination", periods.json())

    def test_reversal_of_an_unapplied_approved_adjustment_frees_the_month(self):
        created = self._record({"adjustment_type": "PRORATED", "days_worked": 15, "reason": "Joined mid-month"})["data"]
        self._approve(created["reference"])
        self.auth(self.recorder)
        refused = self.client.post(f"{ADJ}{created['reference']}/reverse/", {"reason": "Entered wrong days"}, format="json")
        self.assertEqual(refused.status_code, 403, refused.content)   # approved records need an approver
        self.auth(self.approver)
        reversed_response = self.client.post(f"{ADJ}{created['reference']}/reverse/", {"reason": "Entered wrong days"}, format="json")
        self.assertEqual(reversed_response.status_code, 200, reversed_response.content)
        self.assertEqual(reversed_response.json()["data"]["status"], "REVERSED")
        corrected = self._record({"adjustment_type": "PRORATED", "days_worked": 20, "reason": "Corrected days"})["data"]
        self._approve(corrected["reference"])
        line = self._line_for(self._run()["data"], self.employee)
        self.assertEqual(Decimal(line["net_pay"]), Decimal("40000.00"))
        # The reversed record stays in history, unchanged.
        old = PayrollSalaryAdjustment.objects.get(reference=created["reference"])
        self.assertEqual(old.prorated_salary, Decimal("30000.00"))
        self.assertIsNone(old.applied_payroll_line_id)
        # A pending record can be withdrawn by the payroll officer who maintains it.
        pending = self._record({"adjustment_type": "OTHER", "amount": "300.00", "other_direction": "DEDUCTION", "reason": "Mistaken entry"},
                               expect=400)  # month already run → refused outright
        self.assertIn("already includes", str(pending))
