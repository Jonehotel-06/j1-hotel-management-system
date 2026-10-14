"""End-to-end payroll privacy, maker-checker, correction and ledger tests."""
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError

from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.core.utils import hotel_today
from apps.finance.models import CashMovement, CashSession, FinancialLine, FinancialTransaction
from apps.finance.services import accounting
from apps.finance.services.cash_session_service import open_cash_session
from apps.inventory.models import StockCount, StockCountLine, StockItem, StockLocation
from apps.staff_operations.models import (
    Department, PayrollPeriod, PayrollSalaryPayment, PayrollStockShortageAllocation,
    PayrollStockShortageCase, StaffCompensation,
)
from apps.staff_operations.services.staff_operations_service import ensure_staff_profile
from tests.base import BaseAPITestCase
from tests.factories import make_staff


class PayrollApiTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.creator = make_staff("payroll.hr@staff.dev", role=User.Role.HR_MANAGER)
        self.approver = make_staff("payroll.accounts@staff.dev", role=User.Role.ACCOUNTS_MANAGER)
        self.employee = make_staff("payroll.employee@staff.dev", role=User.Role.HOUSEKEEPER)
        self.profile, _ = ensure_staff_profile(staff=self.employee)
        self.profile.department = "Housekeeping"
        self.profile.save(update_fields=["department", "updated_at"])
        self.today = hotel_today()
        self.compensation = self._create_compensation()

    def _create_compensation(self):
        self.auth(self.creator)
        response = self.client.post("/api/admin/staff-operations/payroll/compensation/", {
            "staff_id": self.employee.pk,
            "effective_from": self.today.isoformat(),
            "currency": "NGN",
            "basic_salary": "60000.00",
            "overtime_rate": "1000.00",
            "notes": "Monthly base compensation",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        return StaffCompensation.objects.get(pk=response.json()["data"]["id"])

    def _payload(self, key="payroll-run-001"):
        return {
            "starts_on": self.today.isoformat(),
            "ends_on": self.today.isoformat(),
            "department": "Housekeeping",
            "idempotency_key": key,
            "adjustments": [{
                "staff_id": self.employee.pk,
                "overtime_hours": "2.50",
                "allowances": [{"label": "Transport", "amount": "5000.00"}],
                "bonus": "0.00",
                "deductions": [{"label": "Uniform replacement", "amount": "1000.00"}],
            }],
        }

    def _create_tax_employee(self, *, department="Housekeeping", salary="1000000.00", housing="100000.00", transport="50000.00", register_tax_id=True):
        staff = make_staff(f"payroll.extra.{department.replace(' ', '').lower()}.{salary}@staff.dev", role=User.Role.HOUSEKEEPER)
        profile, _ = ensure_staff_profile(staff=staff)
        profile.department = department
        profile.employment_start = self.today
        profile.save(update_fields=["department", "employment_start", "updated_at"])
        self.auth(self.creator)
        response = self.client.post("/api/admin/staff-operations/payroll/compensation/", {
            "staff_id": staff.pk, "effective_from": self.today.isoformat(), "currency": "NGN",
            "basic_salary": salary, "housing_allowance": housing, "transport_allowance": transport,
            "overtime_rate": "0.00",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        return staff, StaffCompensation.objects.get(pk=response.json()["data"]["id"])

    def _create_period(self, payload=None):
        self.auth(self.creator)
        response = self.client.post("/api/admin/staff-operations/payroll/periods/", payload or self._payload(), format="json")
        self.assertIn(response.status_code, (200, 201), response.content)
        return response

    def _create_approved_run(self, payload=None):
        created = self._create_period(payload)
        data = created.json()["data"]
        reference = data["reference"]
        self.auth(self.creator)
        submitted = self.client.post(
            f"/api/admin/staff-operations/payroll/periods/{reference}/submit/", {}, format="json"
        )
        self.assertEqual(submitted.status_code, 200, submitted.content)
        self.auth(self.approver)
        approved = self.client.post(
            f"/api/admin/staff-operations/payroll/periods/{reference}/review/",
            {"approved": True, "review_note": "Independent approval for payment test."}, format="json",
        )
        self.assertEqual(approved.status_code, 200, approved.content)
        return reference, approved.json()["data"]["lines"][0]

    def test_payroll_uses_private_salary_snapshots_maker_checker_and_immutable_ledger(self):
        created = self._create_period()
        self.assertEqual(created.status_code, 201)
        data = created.json()["data"]
        reference = data["reference"]
        self.assertEqual(data["line_count"], 1)
        self.assertEqual(Decimal(data["total_gross"]), Decimal("67500.00"))
        self.assertEqual(Decimal(data["total_deductions"]), Decimal("1000.00"))
        self.assertEqual(Decimal(data["total_net"]), Decimal("66500.00"))
        self.assertEqual(Decimal(data["lines"][0]["overtime_pay"]), Decimal("2500.00"))
        self.assertNotIn("employee_pension", data["lines"][0])
        self.assertNotIn("paye_tax", data["lines"][0])
        self.assertNotIn("tax_snapshot", data["lines"][0])
        self.assertNotIn("statutory_rules_reference", data)
        self.assertNotIn("tax_identity_reference", data["lines"][0])

        duplicate = self._create_period(self._payload())
        self.assertEqual(duplicate.status_code, 200)
        self.assertEqual(duplicate.json()["data"]["reference"], reference)
        self.assertEqual(PayrollPeriod.objects.count(), 1)
        mismatch = self._payload()
        mismatch["adjustments"][0]["bonus"] = "10.00"
        self.auth(self.creator)
        conflict = self.client.post("/api/admin/staff-operations/payroll/periods/", mismatch, format="json")
        self.assertEqual(conflict.status_code, 400, conflict.content)

        # Existing user profile APIs remain salary-blind; the dedicated salary
        # endpoint rejects an ordinary department account.
        self.auth(self.employee)
        profile = self.client.get("/api/auth/profile/")
        self.assertEqual(profile.status_code, 200)
        self.assertNotIn("basic_salary", profile.json()["data"])
        self.assertEqual(self.client.get("/api/admin/staff-operations/payroll/compensation/").status_code, 403)
        self.assertEqual(self.client.get(f"/api/admin/staff-operations/payroll/periods/{reference}/").status_code, 403)

        self.auth(self.creator)
        submitted = self.client.post(f"/api/admin/staff-operations/payroll/periods/{reference}/submit/", {}, format="json")
        self.assertEqual(submitted.status_code, 200, submitted.content)
        # A preparer who also has approval capability cannot self-approve.
        self.assertEqual(
            self.client.post(f"/api/admin/staff-operations/payroll/periods/{reference}/review/", {"approved": True}, format="json").status_code,
            403,
        )

        self.auth(self.approver)
        approved = self.client.post(
            f"/api/admin/staff-operations/payroll/periods/{reference}/review/",
            {"approved": True, "review_note": "Independent review complete."}, format="json",
        )
        self.assertEqual(approved.status_code, 200, approved.content)
        period = PayrollPeriod.objects.get(reference=reference)
        self.assertEqual(period.status, PayrollPeriod.Status.APPROVED)
        accrual = period.accrual_transaction
        self.assertEqual(accrual.type, FinancialTransaction.Type.PAYROLL_ACCRUAL)
        accrual_lines = list(accrual.lines.order_by("pk"))
        self.assertEqual(sum(x.amount for x in accrual_lines if x.direction == FinancialLine.Direction.DEBIT), Decimal("67500.00"))
        self.assertEqual(sum(x.amount for x in accrual_lines if x.direction == FinancialLine.Direction.CREDIT), Decimal("67500.00"))
        self.assertTrue(any(x.account_code == accounting.PAYROLL_EXPENSE for x in accrual_lines))
        self.assertFalse(any(x.account_code == accounting.PAYROLL_PENSION_EXPENSE for x in accrual_lines))
        self.assertFalse(any(x.account_code == accounting.PAYROLL_PENSION_PAYABLE for x in accrual_lines))
        self.assertTrue(any(x.account_code == accounting.PAYROLL_DEDUCTIONS_PAYABLE for x in accrual_lines))

        paid = self.client.post(
            f"/api/admin/staff-operations/payroll/periods/{reference}/pay/",
            {"method": "BANK_TRANSFER", "external_reference": "BANK-REF-2026-10"}, format="json",
        )
        self.assertEqual(paid.status_code, 200, paid.content)
        period.refresh_from_db()
        self.assertEqual(period.status, PayrollPeriod.Status.PAID)
        self.assertEqual(period.payment_transaction.type, FinancialTransaction.Type.PAYROLL_PAYMENT)
        self.assertEqual(period.payment_transaction.lines.get(direction=FinancialLine.Direction.DEBIT).amount, Decimal("66500.00"))
        self.assertEqual(period.payment_transaction.lines.get(direction=FinancialLine.Direction.CREDIT).account_code, accounting.BANK_CLEARING)
        self.assertEqual(PayrollSalaryPayment.objects.filter(line__period=period).count(), 1)
        full_run_record = PayrollSalaryPayment.objects.get(line__period=period)
        self.assertFalse(full_run_record.is_legacy_import)
        self.assertTrue(full_run_record.reference.startswith("SAL-FULLRUN-"))
        self.assertEqual(paid.json()["data"]["salary_amount_paid"], "66500.00")
        self.assertEqual(paid.json()["data"]["salary_balance"], "0.00")
        self.assertEqual(paid.json()["data"]["lines"][0]["salary_balance"], "0.00")

        # Approved/paid employees can fetch only their own payslip; it is not
        # publicly cacheable, and payroll viewers can audit a requested line.
        self.auth(self.employee)
        slip = self.client.get(f"/api/admin/staff-operations/payroll/payslips/{reference}/")
        self.assertEqual(slip.status_code, 200)
        self.assertEqual(slip["Content-Type"], "application/pdf")
        self.assertIn(b"%PDF", slip.content[:10])
        self.assertIn("no-store", slip["Cache-Control"])
        other = make_staff("payroll.other@staff.dev", role=User.Role.HOUSEKEEPER)
        self.auth(other)
        self.assertEqual(self.client.get(f"/api/admin/staff-operations/payroll/payslips/{reference}/").status_code, 404)

        self.compensation.refresh_from_db()
        self.compensation.basic_salary = Decimal("99999.00")
        with self.assertRaises(ValidationError):
            self.compensation.save()

    def test_employee_salary_payments_support_partial_full_history_and_balances(self):
        reference, line = self._create_approved_run()
        url = f"/api/admin/staff-operations/payroll/periods/{reference}/lines/{line['id']}/payments/"
        payload = {
            "amount": "20000.00",
            "method": "BANK_TRANSFER",
            "payment_date": self.today.isoformat(),
            "external_reference": "BANK-SALARY-001",
            "evidence_reference": "PAYROLL-ADVICE-001",
            "notes": "First manual instalment",
            "idempotency_key": "salary-payment-partial-001",
        }

        self.auth(self.employee)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(url, payload, format="json").status_code, 403)

        self.auth(self.creator)
        first = self.client.post(url, payload, format="json")
        self.assertEqual(first.status_code, 201, first.content)
        self.assertIn("no-store", first["Cache-Control"])
        payment_reference = first.json()["data"]["reference"]
        payment = PayrollSalaryPayment.objects.get(reference=payment_reference)
        self.assertEqual(payment.amount, Decimal("20000.00"))
        self.assertEqual(payment.financial_transaction.type, FinancialTransaction.Type.PAYROLL_PAYMENT)
        self.assertEqual(payment.financial_transaction.lines.get(direction=FinancialLine.Direction.DEBIT).account_code, accounting.PAYROLL_PAYABLE)
        self.assertEqual(payment.financial_transaction.lines.get(direction=FinancialLine.Direction.CREDIT).account_code, accounting.BANK_CLEARING)
        self.assertFalse(payment.is_legacy_import)

        duplicate = self.client.post(url, payload, format="json")
        self.assertEqual(duplicate.status_code, 200, duplicate.content)
        self.assertEqual(duplicate.json()["data"]["reference"], payment_reference)
        self.assertEqual(PayrollSalaryPayment.objects.filter(line_id=line["id"]).count(), 1)

        mismatch = {**payload, "amount": "21000.00"}
        self.assertEqual(self.client.post(url, mismatch, format="json").status_code, 400)
        overpayment = {
            **payload, "amount": "47000.00", "external_reference": "BANK-SALARY-OVER",
            "idempotency_key": "salary-payment-over-001",
        }
        self.assertEqual(self.client.post(url, overpayment, format="json").status_code, 400)
        missing_bank_reference = {
            **payload, "external_reference": "", "idempotency_key": "salary-payment-ref-001",
        }
        self.assertEqual(self.client.post(url, missing_bank_reference, format="json").status_code, 400)

        period_detail = self.client.get(f"/api/admin/staff-operations/payroll/periods/{reference}/").json()["data"]
        self.assertEqual(period_detail["status"], PayrollPeriod.Status.PARTIALLY_PAID)
        self.assertEqual(period_detail["salary_amount_paid"], "20000.00")
        self.assertEqual(period_detail["salary_balance"], "46500.00")
        self.assertEqual(period_detail["lines"][0]["salary_amount_paid"], "20000.00")
        self.assertEqual(period_detail["lines"][0]["salary_balance"], "46500.00")
        listing = self.client.get("/api/admin/staff-operations/payroll/periods/", {"status": "PARTIALLY_PAID"})
        self.assertEqual(listing.status_code, 200, listing.content)
        self.assertEqual(listing.json()["data"][0]["salary_balance"], "46500.00")

        history = self.client.get(url)
        self.assertEqual(history.status_code, 200, history.content)
        self.assertEqual(history.json()["data"][0]["reference"], payment_reference)
        self.assertEqual(history.json()["data"][0]["status"], "RECORDED")
        self.assertEqual(history.json()["data"][0]["financial_reference"], payment.financial_transaction.reference)

        second = {
            "amount": "46500.00", "method": "BANK_TRANSFER", "payment_date": self.today.isoformat(),
            "external_reference": "BANK-SALARY-002", "idempotency_key": "salary-payment-final-001",
        }
        completed = self.client.post(url, second, format="json")
        self.assertEqual(completed.status_code, 201, completed.content)
        final = self.client.get(f"/api/admin/staff-operations/payroll/periods/{reference}/").json()["data"]
        self.assertEqual(final["status"], PayrollPeriod.Status.PAID)
        self.assertEqual(final["salary_amount_paid"], "66500.00")
        self.assertEqual(final["salary_balance"], "0.00")
        self.assertEqual(PayrollSalaryPayment.objects.filter(line_id=line["id"]).count(), 2)
        self.assertEqual(PayrollPeriod.objects.get(reference=reference).events.filter(type="PAYMENT_RECORDED").count(), 2)

    def test_salary_payment_reversal_is_separate_authorized_and_auditable(self):
        reference, line = self._create_approved_run()
        url = f"/api/admin/staff-operations/payroll/periods/{reference}/lines/{line['id']}/payments/"
        original_payload = {
            "amount": "20000.00", "method": "BANK_TRANSFER", "payment_date": self.today.isoformat(),
            "external_reference": "BANK-SALARY-REVERSAL-TEST", "idempotency_key": "salary-payment-reversible-001",
        }
        self.auth(self.creator)
        recorded = self.client.post(url, original_payload, format="json")
        self.assertEqual(recorded.status_code, 201, recorded.content)
        original = PayrollSalaryPayment.objects.get(reference=recorded.json()["data"]["reference"])
        reversal_url = url + original.reference + "/reverse/"
        reversal_payload = {
            "correction_reason": "Bank transfer was returned to the hotel account.",
            "payment_date": self.today.isoformat(),
            "external_reference": "BANK-RETURN-001",
            "evidence_reference": "BANK-STATEMENT-001",
            "idempotency_key": "salary-reversal-audit-001",
        }
        # The payment recorder cannot authorize their own correction.
        self.assertEqual(self.client.post(reversal_url, reversal_payload, format="json").status_code, 403)
        self.auth(self.approver)
        reversed_response = self.client.post(reversal_url, reversal_payload, format="json")
        self.assertEqual(reversed_response.status_code, 201, reversed_response.content)
        reversal = PayrollSalaryPayment.objects.get(reference=reversed_response.json()["data"]["reference"])
        self.assertEqual(reversal.reversal_of_id, original.pk)
        self.assertEqual(reversal.amount, original.amount)
        self.assertEqual(reversal.financial_transaction.reversal_of_id, original.financial_transaction_id)
        self.assertEqual(reversal.financial_transaction.lines.get(direction=FinancialLine.Direction.DEBIT).account_code, accounting.BANK_CLEARING)
        self.assertEqual(reversal.financial_transaction.lines.get(direction=FinancialLine.Direction.CREDIT).account_code, accounting.PAYROLL_PAYABLE)
        self.assertEqual(reversed_response.json()["data"]["status"], "REVERSAL")

        duplicate = self.client.post(reversal_url, reversal_payload, format="json")
        self.assertEqual(duplicate.status_code, 200, duplicate.content)
        self.assertEqual(duplicate.json()["data"]["reference"], reversal.reference)
        self.assertEqual(self.client.post(
            reversal_url,
            {**reversal_payload, "correction_reason": "Different request, same key."},
            format="json",
        ).status_code, 400)
        self.assertEqual(self.client.post(
            reversal_url,
            {**reversal_payload, "idempotency_key": "salary-reversal-duplicate-002"},
            format="json",
        ).status_code, 400)

        original.refresh_from_db()
        period = PayrollPeriod.objects.get(reference=reference)
        self.assertEqual(period.status, PayrollPeriod.Status.APPROVED)
        self.assertEqual(period.events.filter(type="PAYMENT_REVERSED").count(), 1)
        self.assertEqual(period.lines.get(pk=line["id"]).salary_payments.filter(reversal_of__isnull=True).count(), 1)
        history = self.client.get(url).json()["data"]
        original_record = next(row for row in history if row["reference"] == original.reference)
        self.assertEqual(original_record["status"], "REVERSED")
        self.assertEqual(original_record["reversed_by_reference"], reversal.reference)
        self.assertTrue(AuditLog.objects.filter(
            action="PAYROLL_SALARY_PAYMENT_REVERSED", object_id=str(reversal.pk), actor=self.approver,
        ).exists())

    def test_full_run_employee_reversal_preserves_other_employees_paid_records(self):
        second_employee, _ = self._create_tax_employee(
            department="Housekeeping", salary="1000000.00", housing="100000.00", transport="50000.00",
        )
        profile = second_employee.staff_profile
        profile.department = "Housekeeping"
        profile.save(update_fields=["department", "updated_at"])

        reference, line = self._create_approved_run()
        period_url = f"/api/admin/staff-operations/payroll/periods/{reference}/"
        self.auth(self.creator)
        paid = self.client.post(
            f"/api/admin/staff-operations/payroll/periods/{reference}/pay/",
            {"method": "BANK_TRANSFER", "external_reference": "BANK-FULLRUN-MULTI-001"}, format="json",
        )
        self.assertEqual(paid.status_code, 200, paid.content)
        records = list(PayrollSalaryPayment.objects.filter(line__period__reference=reference, reversal_of__isnull=True))
        self.assertEqual(len(records), 2)
        target = next(record for record in records if record.line_id == line["id"])
        other = next(record for record in records if record.line.staff_id == second_employee.pk)
        self.assertEqual(target.financial_transaction_id, other.financial_transaction_id)
        self.assertFalse(target.is_legacy_import)

        self.auth(self.approver)
        reversal = self.client.post(
            f"/api/admin/staff-operations/payroll/periods/{reference}/lines/{line['id']}/payments/{target.reference}/reverse/",
            {
                "correction_reason": "The individual salary transfer was returned after full-run recording.",
                "payment_date": self.today.isoformat(),
                "external_reference": "BANK-FULLRUN-RETURN-001",
                "idempotency_key": "full-run-employee-reversal-001",
            },
            format="json",
        )
        self.assertEqual(reversal.status_code, 201, reversal.content)
        target.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(target.reversed_by.amount, target.amount)
        self.assertFalse(PayrollSalaryPayment.objects.filter(reversal_of=other).exists())
        self.assertEqual(other.financial_transaction_id, target.financial_transaction_id)
        self.assertEqual(PayrollPeriod.objects.get(reference=reference).status, PayrollPeriod.Status.PARTIALLY_PAID)
        details = self.client.get(period_url).json()["data"]
        self.assertEqual(details["salary_balance"], str(target.amount))

    def test_cash_salary_entries_reconcile_open_drawers_and_cash_reversals(self):
        reference, line = self._create_approved_run()
        cashier = self.approver
        cashier_session = open_cash_session(cashier=cashier, actor=cashier, opening_float="80000.00")
        foreign_session = open_cash_session(cashier=self.creator, actor=self.creator, opening_float="50000.00")
        url = f"/api/admin/staff-operations/payroll/periods/{reference}/lines/{line['id']}/payments/"
        payload = {
            "amount": "20000.00", "method": "CASH", "payment_date": self.today.isoformat(),
            "cash_session_reference": foreign_session.reference, "idempotency_key": "cash-salary-owner-check-001",
        }
        self.auth(cashier)
        self.assertEqual(self.client.post(url, payload, format="json").status_code, 403)
        payload["cash_session_reference"] = cashier_session.reference
        payload["idempotency_key"] = "cash-salary-payment-valid-001"
        recorded = self.client.post(url, payload, format="json")
        self.assertEqual(recorded.status_code, 201, recorded.content)
        original = PayrollSalaryPayment.objects.get(reference=recorded.json()["data"]["reference"])
        cashier_session.refresh_from_db()
        self.assertEqual(cashier_session.expected_cash, Decimal("60000.00"))
        self.assertTrue(CashMovement.objects.filter(
            transaction=original.financial_transaction, type=CashMovement.Type.PAID_OUT,
        ).exists())

        corrector = make_staff("payroll.cash-corrector@staff.dev", role=User.Role.MANAGER)
        return_session = open_cash_session(cashier=corrector, actor=corrector, opening_float="0.00")
        self.auth(corrector)
        reversal = self.client.post(url + original.reference + "/reverse/", {
            "correction_reason": "Cash returned to the controlled hotel drawer.",
            "payment_date": self.today.isoformat(),
            "cash_session_reference": return_session.reference,
            "idempotency_key": "cash-salary-reversal-001",
        }, format="json")
        self.assertEqual(reversal.status_code, 201, reversal.content)
        return_session.refresh_from_db()
        cashier_session.refresh_from_db()
        self.assertEqual(return_session.expected_cash, Decimal("20000.00"))
        self.assertEqual(cashier_session.expected_cash, Decimal("60000.00"))
        self.assertTrue(CashMovement.objects.filter(
            transaction__reversal_of=original.financial_transaction,
            cash_session=return_session,
            type=CashMovement.Type.CASH_IN,
        ).exists())
        self.assertEqual(PayrollPeriod.objects.get(reference=reference).status, PayrollPeriod.Status.APPROVED)

    def test_rejected_run_is_corrected_by_a_new_replacement_record(self):
        run = self._create_period()
        reference = run.json()["data"]["reference"]
        self.auth(self.creator)
        self.assertEqual(self.client.post(f"/api/admin/staff-operations/payroll/periods/{reference}/submit/", {}, format="json").status_code, 200)
        self.auth(self.approver)
        rejected = self.client.post(
            f"/api/admin/staff-operations/payroll/periods/{reference}/review/",
            {"approved": False, "review_note": "Correct the allowance evidence."}, format="json",
        )
        self.assertEqual(rejected.status_code, 200, rejected.content)
        self.assertEqual(PayrollPeriod.objects.get(reference=reference).status, PayrollPeriod.Status.REJECTED)

        replacement_payload = self._payload(key="payroll-run-correction-002")
        replacement_payload["replaces_reference"] = reference
        self.auth(self.creator)
        replacement = self.client.post("/api/admin/staff-operations/payroll/periods/", replacement_payload, format="json")
        self.assertEqual(replacement.status_code, 201, replacement.content)
        self.assertEqual(replacement.json()["data"]["replaces_reference"], reference)
        self.assertEqual(PayrollPeriod.objects.count(), 2)

    def test_cash_payroll_requires_assigned_open_drawer_and_posts_cash_movement(self):
        run = self._create_period()
        reference = run.json()["data"]["reference"]
        self.auth(self.creator)
        self.client.post(f"/api/admin/staff-operations/payroll/periods/{reference}/submit/", {}, format="json")
        self.auth(self.approver)
        self.client.post(f"/api/admin/staff-operations/payroll/periods/{reference}/review/", {"approved": True}, format="json")
        no_drawer = self.client.post(
            f"/api/admin/staff-operations/payroll/periods/{reference}/pay/", {"method": "CASH"}, format="json"
        )
        self.assertEqual(no_drawer.status_code, 400, no_drawer.content)

    def test_removed_statutory_endpoints_and_compensation_fields_are_not_exposed(self):
        self.auth(self.creator)
        self.assertEqual(self.client.get("/api/admin/staff-operations/payroll/statutory-rules/").status_code, 404)
        self.assertEqual(self.client.get("/api/admin/staff-operations/payroll/tax-identities/").status_code, 404)
        compensation = self.client.get("/api/admin/staff-operations/payroll/compensation/").json()["data"][0]
        self.assertNotIn("pension_applicable", compensation)
        self.assertNotIn("minimum_wage_applicable", compensation)

    def test_government_tax_and_pension_labels_cannot_be_used_as_internal_deductions(self):
        payload = self._payload("payroll-forbidden-statutory-label")
        payload["adjustments"][0]["deductions"] = [{"label": "PAYE tax", "amount": "1000.00"}]
        self.auth(self.creator)
        response = self.client.post("/api/admin/staff-operations/payroll/periods/", payload, format="json")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("Government tax and pension deductions are no longer calculated", str(response.json()))

    def test_payroll_includes_housing_and_transport_without_tax_or_a_tax_id(self):
        staff, _ = self._create_tax_employee(department="Simplified Payroll", salary="100000.00", housing="20000.00", transport="10000.00")
        payload = {
            "starts_on": self.today.isoformat(), "ends_on": self.today.isoformat(),
            "department": "Simplified Payroll", "idempotency_key": "payroll-simplified-001", "adjustments": [],
        }
        self.auth(self.creator)
        response = self.client.post("/api/admin/staff-operations/payroll/periods/", payload, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        line = response.json()["data"]["lines"][0]
        self.assertEqual(line["staff_id"], staff.pk)
        self.assertEqual(Decimal(line["gross_pay"]), Decimal("130000.00"))
        self.assertEqual(Decimal(line["deductions_total"]), Decimal("0.00"))
        self.assertEqual(Decimal(line["net_pay"]), Decimal("130000.00"))

    def _shortage_case(self, *, total="100.01", staff=()):
        department = Department.objects.create(code="HK-SHORTAGE", name="Housekeeping Shortage")
        for user in staff:
            profile, _ = ensure_staff_profile(staff=user)
            profile.department_ref = department
            profile.department = department.name
            profile.employment_status = profile.EmploymentStatus.ACTIVE
            profile.save(update_fields=["department_ref", "department", "employment_status", "updated_at"])
            if StaffCompensation.objects.filter(staff=user, effective_from=self.today).exists():
                continue
            self.auth(self.creator)
            response = self.client.post("/api/admin/staff-operations/payroll/compensation/", {
                "staff_id": user.pk, "effective_from": self.today.isoformat(), "currency": "NGN",
                "basic_salary": "60000.00", "housing_allowance": "0.00", "transport_allowance": "0.00",
                "overtime_rate": "0.00",
            }, format="json")
            self.assertEqual(response.status_code, 201, response.content)
        self.auth(self.creator)
        created = self.client.post("/api/admin/staff-operations/payroll/stock-shortages/", {
            "department_id": department.pk, "item_summary": "Five bottles of wine missing from the store",
            "total_amount": total, "incident_date": self.today.isoformat(),
            "effective_date": self.today.isoformat(), "evidence_reference": "STORE-COUNT-EVIDENCE-001",
            "notes": "Individual responsibility has not been established.",
        }, format="json")
        self.assertEqual(created.status_code, 201, created.content)
        return department, created.json()["data"]

    def test_stock_shortage_is_only_shared_after_admin_approval_and_applies_once(self):
        admin = make_staff("payroll.shortage.admin@staff.dev", role=User.Role.ADMIN)
        colleague = make_staff("payroll.shortage.colleague@staff.dev", role=User.Role.HOUSEKEEPER)
        department, case_data = self._shortage_case(staff=(self.employee, colleague))
        case = PayrollStockShortageCase.objects.get(reference=case_data["reference"])
        self.assertEqual(case.status, PayrollStockShortageCase.Status.PENDING_ADMIN_APPROVAL)
        self.assertFalse(case.allocations.exists())

        for actor in (self.creator, self.approver):
            self.auth(actor)
            denied = self.client.post(
                f"/api/admin/staff-operations/payroll/stock-shortages/{case.reference}/decision/",
                {"approved": True, "decision_note": "Manager attempted approval."}, format="json",
            )
            self.assertEqual(denied.status_code, 403, denied.content)
        case.refresh_from_db()
        self.assertEqual(case.status, PayrollStockShortageCase.Status.PENDING_ADMIN_APPROVAL)
        self.assertFalse(case.allocations.exists())

        self.auth(admin)
        approved = self.client.post(
            f"/api/admin/staff-operations/payroll/stock-shortages/{case.reference}/decision/",
            {"approved": True, "decision_note": "Admin accepted collective responsibility after store review."},
            format="json",
        )
        self.assertEqual(approved.status_code, 200, approved.content)
        allocations = list(PayrollStockShortageAllocation.objects.filter(case=case).order_by("staff__email"))
        self.assertEqual(len(allocations), 2)
        self.assertEqual(sum(item.amount for item in allocations), Decimal("100.01"))
        self.assertEqual(sorted(item.amount for item in allocations), [Decimal("50.00"), Decimal("50.01")])
        self.assertTrue(AuditLog.objects.filter(action="PAYROLL_STOCK_SHORTAGE_APPROVED", actor=admin).exists())

        payload = {
            "starts_on": self.today.isoformat(), "ends_on": self.today.isoformat(),
            "department": department.name, "idempotency_key": "payroll-shortage-run-001", "adjustments": [],
        }
        self.auth(self.creator)
        first = self.client.post("/api/admin/staff-operations/payroll/periods/", payload, format="json")
        self.assertEqual(first.status_code, 201, first.content)
        lines = {line["staff_id"]: line for line in first.json()["data"]["lines"]}
        for allocation in allocations:
            self.assertIn(f"Department stock shortage ({case.reference})", str(lines[allocation.staff_id]["deductions"]))
            self.assertEqual(Decimal(lines[allocation.staff_id]["deductions_total"]), allocation.amount)
            allocation.refresh_from_db()
            self.assertIsNotNone(allocation.applied_payroll_line_id)

        payload["idempotency_key"] = "payroll-shortage-run-002"
        second = self.client.post("/api/admin/staff-operations/payroll/periods/", payload, format="json")
        self.assertEqual(second.status_code, 201, second.content)
        for line in second.json()["data"]["lines"]:
            self.assertEqual(Decimal(line["deductions_total"]), Decimal("0.00"))

    def test_stock_count_shortage_total_is_derived_by_the_server(self):
        department = Department.objects.create(code="BAR-SHORTAGE", name="Bar Shortage")
        location = StockLocation.objects.create(code="BAR-STORE", name="Bar Store")
        item = StockItem.objects.create(
            sku="WINE-RED-001", name="Red wine", standard_cost=Decimal("1500.00"), currency="NGN",
        )
        count = StockCount.objects.create(
            reference="COUNT-SHORTAGE-001", location=location, status=StockCount.Status.POSTED,
            initiated_by=self.creator,
        )
        StockCountLine.objects.create(
            stock_count=count, item=item, expected_quantity=Decimal("10.0000"),
            counted_quantity=Decimal("8.0000"), variance=Decimal("-2.0000"),
        )
        self.auth(self.creator)
        created = self.client.post("/api/admin/staff-operations/payroll/stock-shortages/", {
            "department_id": department.pk, "stock_count_reference": count.reference,
            "incident_date": self.today.isoformat(), "effective_date": self.today.isoformat(),
            "notes": "Posted count variance requires Admin review.",
        }, format="json")
        self.assertEqual(created.status_code, 201, created.content)
        data = created.json()["data"]
        self.assertEqual(Decimal(data["total_amount"]), Decimal("3000.00"))
        self.assertEqual(data["shortage_lines"][0]["item_sku"], "WINE-RED-001")
        self.assertEqual(Decimal(data["shortage_lines"][0]["amount"]), Decimal("3000.00"))
        duplicate = self.client.post("/api/admin/staff-operations/payroll/stock-shortages/", {
            "department_id": department.pk, "stock_count_reference": count.reference,
            "incident_date": self.today.isoformat(), "effective_date": self.today.isoformat(),
        }, format="json")
        self.assertEqual(duplicate.status_code, 400, duplicate.content)

    def test_rejected_stock_shortage_creates_no_allocations_or_payroll_deduction(self):
        admin = make_staff("payroll.shortage.reject.admin@staff.dev", role=User.Role.ADMIN)
        _, case_data = self._shortage_case(staff=(self.employee,))
        self.auth(admin)
        rejected = self.client.post(
            f"/api/admin/staff-operations/payroll/stock-shortages/{case_data['reference']}/decision/",
            {"approved": False, "decision_note": "Evidence did not justify a department deduction."},
            format="json",
        )
        self.assertEqual(rejected.status_code, 200, rejected.content)
        case = PayrollStockShortageCase.objects.get(reference=case_data["reference"])
        self.assertEqual(case.status, PayrollStockShortageCase.Status.REJECTED)
        self.assertFalse(case.allocations.exists())

    def test_my_payslips_scope_to_the_caller_and_exclude_unapproved_runs(self):
        """PAY-004: the correct self-service route, correctly scoped.

        Two housekeepers share one approved payroll run. Each must see only
        their own line through `/payroll/my-payslips/`, a colleague's line must
        not leak, a run that has not been approved must not appear at all, and a
        staff member with no payroll line must get an empty list rather than the
        whole table.
        """
        colleague = make_staff("payroll.colleague@staff.dev", role=User.Role.HOUSEKEEPER)
        profile, _ = ensure_staff_profile(staff=colleague)
        profile.department = "Housekeeping"
        profile.employment_start = self.today
        profile.save(update_fields=["department", "employment_start", "updated_at"])
        self.auth(self.creator)
        compensation = self.client.post("/api/admin/staff-operations/payroll/compensation/", {
            "staff_id": colleague.pk, "effective_from": self.today.isoformat(),
            "currency": "NGN", "basic_salary": "90000.00", "overtime_rate": "0.00",
        }, format="json")
        self.assertEqual(compensation.status_code, 201, compensation.content)

        # A rejected run keeps no visible payslip; its documented replacement does.
        rejected = self._create_period(self._payload("payroll-scope-rejected"))
        rejected_reference = rejected.json()["data"]["reference"]
        self.auth(self.creator)
        self.assertEqual(
            self.client.post(
                f"/api/admin/staff-operations/payroll/periods/{rejected_reference}/submit/", {}, format="json"
            ).status_code,
            200,
        )
        self.auth(self.approver)
        refused = self.client.post(
            f"/api/admin/staff-operations/payroll/periods/{rejected_reference}/review/",
            {"approved": False, "review_note": "Rejected: hours did not reconcile."}, format="json",
        )
        self.assertEqual(refused.status_code, 200, refused.content)
        self.assertEqual(
            PayrollPeriod.objects.get(reference=rejected_reference).status, PayrollPeriod.Status.REJECTED
        )

        replacement_payload = self._payload("payroll-scope-approved")
        replacement_payload["replaces_reference"] = rejected_reference
        approved_reference, line = self._create_approved_run(replacement_payload)
        self.assertEqual(Decimal(line["gross_pay"]), Decimal("67500.00"))
        self.assertNotEqual(rejected_reference, approved_reference)

        def payslips_for(user):
            self.auth(user)
            response = self.client.get("/api/admin/staff-operations/payroll/my-payslips/")
            self.assertEqual(response.status_code, 200, response.content)
            self.assertIn("no-store", response["Cache-Control"])
            return response.json()["data"]

        own = payslips_for(self.employee)
        self.assertTrue(own, "the caller's own approved payslip is missing")
        self.assertEqual({row["period_reference"] for row in own}, {approved_reference})
        self.assertNotIn(
            rejected_reference, {row["period_reference"] for row in own},
            "a rejected payroll run leaked into self-service",
        )
        self.assertEqual({Decimal(row["gross_pay"]) for row in own}, {Decimal("67500.00")})

        colleague_rows = payslips_for(colleague)
        self.assertEqual(
            {Decimal(row["gross_pay"]) for row in colleague_rows}, {Decimal("90000.00")},
            "the colleague saw a line that is not theirs",
        )
        self.assertNotIn(Decimal("67500.00"), {Decimal(r["gross_pay"]) for r in colleague_rows})

        self.auth(self.approver)
        empty = self.client.get("/api/admin/staff-operations/payroll/my-payslips/")
        self.assertEqual(empty.status_code, 200, empty.content)
        self.assertEqual(empty.json()["data"], [])

    def test_employee_my_payslips_are_private(self):
        self.auth(self.employee)
        response = self.client.get("/api/admin/staff-operations/payroll/my-payslips/")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn("no-store", response["Cache-Control"])
        payload = response.json()
        rows = payload.get("data") or payload.get("results") or []
        self.assertTrue(isinstance(rows, list) or "results" in payload)
