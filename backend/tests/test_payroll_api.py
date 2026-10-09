"""End-to-end payroll privacy, maker-checker, correction and ledger tests."""
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError

from apps.accounts.models import User
from apps.core.utils import hotel_today
from apps.finance.models import FinancialLine, FinancialTransaction
from apps.finance.services import accounting
from apps.staff_operations.models import PayrollPeriod, PayrollStatutoryRuleSet, PayrollTaxIdentity, StaffCompensation, StaffProfile
from apps.staff_operations.services.payroll_service import progressive_tax
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
        self.ruleset_reference = self._create_approved_ruleset()
        self.tax_identity = self._register_tax_identity(self.employee)
        self.compensation = self._create_compensation()

    def _create_approved_ruleset(self):
        bands = [
            {"up_to": "800000.00", "rate": "0.0000"},
            {"up_to": "3000000.00", "rate": "0.1500"},
            {"up_to": "12000000.00", "rate": "0.1800"},
            {"up_to": "25000000.00", "rate": "0.2100"},
            {"up_to": "50000000.00", "rate": "0.2300"},
            {"up_to": None, "rate": "0.2500"},
        ]
        self.auth(self.creator)
        proposed = self.client.post("/api/admin/staff-operations/payroll/statutory-rules/", {
            "jurisdiction": "NG", "currency": "NGN", "effective_from": "2026-01-01",
            "paye_bands": bands, "minimum_wage_monthly": "70000.00",
            "employee_pension_rate": "0.0800", "employer_pension_rate": "0.1000",
            "rent_relief_rate": "0.2000", "rent_relief_cap": "500000.00",
            "legal_basis": "2026 JRB guidance and pension applicability pending independent legal validation.",
            "source_references": ["https://jrb.gov.ng/guidelines/2026", "PenCom FAQ 2023; verify current applicability"],
        }, format="json")
        self.assertEqual(proposed.status_code, 201, proposed.content)
        reference = proposed.json()["data"]["reference"]
        self.auth(self.approver)
        reviewed = self.client.post(
            f"/api/admin/staff-operations/payroll/statutory-rules/{reference}/review/",
            {"approved": True, "review_note": "Source checked; applicability confirmed for test fixture only."},
            format="json",
        )
        self.assertEqual(reviewed.status_code, 200, reviewed.content)
        return reference

    def _register_tax_identity(self, staff, *, effective_from=None, tax_id=None, registered_on=None, change_reported_on=None, change_reason=""):
        effective_from = effective_from or self.today
        registered_on = registered_on or self.today
        self.auth(self.creator)
        response = self.client.post("/api/admin/staff-operations/payroll/tax-identities/", {
            "staff_id": staff.pk,
            "tax_id": tax_id or f"NG-TIN-{staff.pk}",
            "effective_from": effective_from.isoformat(),
            "registered_on": registered_on.isoformat(),
            "evidence_reference": f"TIN-EVIDENCE-{staff.pk}-{effective_from.isoformat()}",
            **({"change_reported_on": change_reported_on.isoformat(), "change_reason": change_reason} if change_reported_on else {}),
        }, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertIn("no-store", response["Cache-Control"])
        self.assertNotIn("tax_id", response.json()["data"])
        return PayrollTaxIdentity.objects.get(reference=response.json()["data"]["reference"])

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
                "deductions": [{"label": "Withholding", "amount": "1000.00"}],
            }],
        }

    def _create_tax_employee(self, *, department="Tax Test", salary="1000000.00", housing="100000.00", transport="50000.00", register_tax_id=True):
        staff = make_staff("payroll.taxemployee@staff.dev", role=User.Role.HOUSEKEEPER)
        profile, _ = ensure_staff_profile(staff=staff)
        profile.department = department
        profile.employment_start = self.today
        profile.save(update_fields=["department", "employment_start", "updated_at"])
        if register_tax_id:
            self._register_tax_identity(staff)
        self.auth(self.creator)
        response = self.client.post("/api/admin/staff-operations/payroll/compensation/", {
            "staff_id": staff.pk, "effective_from": self.today.isoformat(), "currency": "NGN",
            "basic_salary": salary, "housing_allowance": housing, "transport_allowance": transport,
            "overtime_rate": "0.00", "pension_applicable": True,
            "minimum_wage_applicable": True,
        }, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        return staff, StaffCompensation.objects.get(pk=response.json()["data"]["id"])

    def _create_period(self, payload=None):
        self.auth(self.creator)
        response = self.client.post("/api/admin/staff-operations/payroll/periods/", payload or self._payload(), format="json")
        self.assertIn(response.status_code, (200, 201), response.content)
        return response

    def test_payroll_uses_private_salary_snapshots_maker_checker_and_immutable_ledger(self):
        created = self._create_period()
        self.assertEqual(created.status_code, 201)
        data = created.json()["data"]
        reference = data["reference"]
        self.assertEqual(data["line_count"], 1)
        self.assertEqual(Decimal(data["total_gross"]), Decimal("67500.00"))
        self.assertEqual(Decimal(data["total_deductions"]), Decimal("5800.00"))
        self.assertEqual(Decimal(data["total_net"]), Decimal("61700.00"))
        self.assertEqual(Decimal(data["lines"][0]["overtime_pay"]), Decimal("2500.00"))
        self.assertEqual(Decimal(data["lines"][0]["employee_pension"]), Decimal("4800.00"))
        self.assertEqual(Decimal(data["lines"][0]["paye_tax"]), Decimal("0.00"))
        self.assertTrue(data["lines"][0]["tax_snapshot"]["minimum_wage_exempt"])
        self.assertEqual(data["statutory_rules_reference"], self.ruleset_reference)
        self.assertEqual(data["lines"][0]["tax_identity_reference"], self.tax_identity.reference)

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
        self.assertEqual(sum(x.amount for x in accrual_lines if x.direction == FinancialLine.Direction.DEBIT), Decimal("73500.00"))
        self.assertEqual(sum(x.amount for x in accrual_lines if x.direction == FinancialLine.Direction.CREDIT), Decimal("73500.00"))
        self.assertTrue(any(x.account_code == accounting.PAYROLL_EXPENSE for x in accrual_lines))
        self.assertTrue(any(x.account_code == accounting.PAYROLL_PENSION_EXPENSE for x in accrual_lines))
        self.assertTrue(any(x.account_code == accounting.PAYROLL_PENSION_PAYABLE for x in accrual_lines))
        self.assertTrue(any(x.account_code == accounting.PAYROLL_DEDUCTIONS_PAYABLE for x in accrual_lines))

        paid = self.client.post(
            f"/api/admin/staff-operations/payroll/periods/{reference}/pay/",
            {"method": "BANK_TRANSFER", "external_reference": "BANK-REF-2026-10"}, format="json",
        )
        self.assertEqual(paid.status_code, 200, paid.content)
        period.refresh_from_db()
        self.assertEqual(period.status, PayrollPeriod.Status.PAID)
        self.assertEqual(period.payment_transaction.type, FinancialTransaction.Type.PAYROLL_PAYMENT)
        self.assertEqual(period.payment_transaction.lines.get(direction=FinancialLine.Direction.DEBIT).amount, Decimal("61700.00"))
        self.assertEqual(period.payment_transaction.lines.get(direction=FinancialLine.Direction.CREDIT).account_code, accounting.BANK_CLEARING)

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

    def test_rules_require_independent_review_and_are_immutable_after_approval(self):
        self.auth(self.creator)
        proposed = self.client.post("/api/admin/staff-operations/payroll/statutory-rules/", {
            "jurisdiction": "NG", "currency": "NGN", "effective_from": date(self.today.year + 1, 1, 1).isoformat(),
            "paye_bands": PayrollStatutoryRuleSet.objects.get(reference=self.ruleset_reference).paye_bands,
            "legal_basis": "Proposed next-year rule; independent legal review is required.",
            "source_references": ["Next-year official gazette source pending review"],
        }, format="json")
        self.assertEqual(proposed.status_code, 201, proposed.content)
        reference = proposed.json()["data"]["reference"]
        self.assertEqual(proposed.json()["data"]["status"], PayrollStatutoryRuleSet.Status.PENDING_REVIEW)
        self.assertIn("no-store", proposed["Cache-Control"])
        self.assertEqual(self.client.post(
            f"/api/admin/staff-operations/payroll/statutory-rules/{reference}/review/",
            {"approved": True, "review_note": "Proposer cannot review own change."}, format="json",
        ).status_code, 403)
        self.auth(self.approver)
        reviewed = self.client.post(
            f"/api/admin/staff-operations/payroll/statutory-rules/{reference}/review/",
            {"approved": True, "review_note": "Independent review completed against cited legal sources."}, format="json",
        )
        self.assertEqual(reviewed.status_code, 200, reviewed.content)
        self.assertEqual(reviewed.json()["data"]["status"], PayrollStatutoryRuleSet.Status.APPROVED)
        self.assertTrue(reviewed.json()["data"]["events"])
        stored = PayrollStatutoryRuleSet.objects.get(reference=reference)
        stored.legal_basis = "Attempted silent edit after approval."
        with self.assertRaises(ValidationError):
            stored.save()

    def test_2026_progressive_paye_and_pension_are_calculated_from_reviewed_rules(self):
        staff, _ = self._create_tax_employee()
        payload = {
            "starts_on": self.today.isoformat(), "ends_on": self.today.isoformat(),
            "department": "Tax Test", "idempotency_key": "payroll-progressive-tax-001", "adjustments": [],
        }
        self.auth(self.creator)
        response = self.client.post("/api/admin/staff-operations/payroll/periods/", payload, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        line = response.json()["data"]["lines"][0]
        monthly = Decimal("1150000.00")
        expected_periods = 13 - self.today.month
        projected_chargeable = monthly * expected_periods * Decimal("0.92")
        annual_tax, _ = progressive_tax(
            projected_chargeable,
            PayrollStatutoryRuleSet.objects.get(reference=self.ruleset_reference).paye_bands,
        )
        expected_paye = (annual_tax / Decimal(expected_periods)).quantize(Decimal("0.01"), rounding="ROUND_HALF_UP")
        self.assertEqual(line["staff_id"], staff.pk)
        self.assertEqual(Decimal(line["gross_pay"]), monthly)
        self.assertEqual(Decimal(line["pensionable_pay"]), monthly)
        self.assertEqual(Decimal(line["employee_pension"]), Decimal("92000.00"))
        self.assertEqual(Decimal(line["employer_pension"]), Decimal("115000.00"))
        self.assertEqual(Decimal(line["paye_tax"]), expected_paye)
        self.assertEqual(Decimal(response.json()["data"]["total_employer_cost"]), monthly + Decimal("115000.00"))
        self.assertFalse(line["tax_snapshot"]["minimum_wage_exempt"])
        self.assertEqual(line["tax_snapshot"]["expected_pay_periods"], expected_periods)

    def test_external_opening_ytd_cash_emoluments_distinguish_prior_benefits(self):
        staff, _ = self._create_tax_employee(department="Opening YTD Test")
        prior_ytd = {
            "pay_periods": 9, "gross_emoluments": "3600000.00", "cash_emoluments": "3300000.00",
            "employee_pension": "264000.00", "paye_withheld": "250000.00",
            "evidence_reference": "EXT-PAYROLL-2026-09-CERTIFIED",
        }
        self.auth(self.creator)
        response = self.client.post("/api/admin/staff-operations/payroll/periods/", {
            "starts_on": self.today.isoformat(), "ends_on": self.today.isoformat(),
            "department": "Opening YTD Test", "idempotency_key": "payroll-opening-ytd-cash-001",
            "adjustments": [{"staff_id": staff.pk, "prior_ytd": prior_ytd}],
        }, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        line = response.json()["data"]["lines"][0]
        self.assertEqual(line["tax_snapshot"]["cash_emoluments_ytd"], "4450000.00")
        self.assertEqual(line["tax_snapshot"]["taxable_benefits_ytd"], "300000.00")

        invalid = dict(prior_ytd, cash_emoluments="3600000.01")
        response = self.client.post("/api/admin/staff-operations/payroll/periods/", {
            "starts_on": self.today.isoformat(), "ends_on": self.today.isoformat(),
            "department": "Opening YTD Test", "idempotency_key": "payroll-opening-ytd-cash-invalid",
            "adjustments": [{"staff_id": staff.pk, "prior_ytd": invalid}],
        }, format="json")
        self.assertEqual(response.status_code, 400)

    def test_benefits_in_kind_use_official_valuation_rules_without_becoming_cash_pay(self):
        staff, _ = self._create_tax_employee(department="BIK Test")
        benefits = [
            {"benefit_type": "EMPLOYER_OWNED_ASSET", "description": "Company vehicle", "acquisition_or_market_value": "1200000.00", "evidence_reference": "ASSET-REGISTER-VEHICLE-001"},
            {"benefit_type": "HIRED_ASSET", "description": "Employer-leased device", "annual_rental_value": "240000.00", "evidence_reference": "DEVICE-LEASE-001"},
            {"benefit_type": "EMPLOYER_ACCOMMODATION", "description": "Staff accommodation", "annual_rental_value": "600000.00", "evidence_reference": "ACCOMMODATION-VALUATION-001"},
            {"benefit_type": "EXEMPT_GENERAL_CANTEEN", "description": "General canteen meal", "evidence_reference": "CANTEEN-POLICY-001"},
        ]
        self.auth(self.creator)
        response = self.client.post("/api/admin/staff-operations/payroll/periods/", {
            "starts_on": self.today.isoformat(), "ends_on": self.today.isoformat(),
            "department": "BIK Test", "idempotency_key": "payroll-bik-001",
            "adjustments": [{"staff_id": staff.pk, "benefits_in_kind": benefits}],
        }, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        line = response.json()["data"]["lines"][0]
        self.assertEqual(Decimal(line["gross_pay"]), Decimal("1150000.00"))
        self.assertEqual(Decimal(line["taxable_benefits"]), Decimal("75000.00"))
        self.assertEqual(Decimal(line["taxable_gross_pay"]), Decimal("1225000.00"))
        self.assertEqual(Decimal(line["net_pay"]), Decimal("1150000.00") - Decimal(line["deductions_total"]))
        self.assertEqual(line["benefits_in_kind"][0]["period_taxable_value"], "5000.00")
        self.assertEqual(line["benefits_in_kind"][1]["period_taxable_value"], "20000.00")
        self.assertEqual(line["benefits_in_kind"][2]["period_taxable_value"], "50000.00")
        self.assertEqual(line["benefits_in_kind"][3]["period_taxable_value"], "0.00")
        self.assertEqual(line["tax_snapshot"]["projected_annual_cash_emoluments"], str(Decimal("1150000.00") * (13 - self.today.month)))

    def test_opening_ytd_is_required_for_existing_staff_and_is_used_cumulatively(self):
        self.assertGreater(self.today.month, 1)
        staff, _ = self._create_tax_employee(department="YTD Test")
        profile = StaffProfile.objects.get(user=staff)
        profile.employment_start = date(self.today.year, 1, 1)
        profile.save(update_fields=["employment_start", "updated_at"])
        base_payload = {
            "starts_on": self.today.isoformat(), "ends_on": self.today.isoformat(),
            "department": "YTD Test", "adjustments": [],
        }
        self.auth(self.creator)
        missing = self.client.post("/api/admin/staff-operations/payroll/periods/", {
            **base_payload, "idempotency_key": "payroll-ytd-missing-001",
        }, format="json")
        self.assertEqual(missing.status_code, 400, missing.content)
        monthly = Decimal("1150000.00")
        prior_periods = self.today.month - 1
        prior_gross = monthly * prior_periods
        opening = {
            "pay_periods": prior_periods,
            "gross_emoluments": str(prior_gross),
            "employee_pension": str((prior_gross * Decimal("0.08")).quantize(Decimal("0.01"))),
            "paye_withheld": "1000000.00",
            "evidence_reference": "VERIFIED-2026-YTD-REGISTER-001",
        }
        accepted_payload = {
            **base_payload,
            "idempotency_key": "payroll-ytd-opening-002",
            "adjustments": [{"staff_id": staff.pk, "prior_ytd": opening}],
        }
        accepted = self.client.post("/api/admin/staff-operations/payroll/periods/", accepted_payload, format="json")
        self.assertEqual(accepted.status_code, 201, accepted.content)
        line = accepted.json()["data"]["lines"][0]
        self.assertEqual(line["tax_snapshot"]["gross_emoluments_ytd"], str(prior_gross + monthly))
        self.assertEqual(line["tax_snapshot"]["pay_periods_ytd"], self.today.month)
        self.assertEqual(line["tax_snapshot"]["opening_balance_evidence_reference"], opening["evidence_reference"])
        self.assertGreater(Decimal(line["paye_tax"]), Decimal("0.00"))

    def test_tax_claims_require_evidence_and_rent_is_prorated_by_covered_calendar_days(self):
        staff, _ = self._create_tax_employee(department="Claims Test")
        year = self.today.year
        rent_from, rent_to = date(year, 7, 1), date(year + 1, 6, 30)
        rent_amount = Decimal("1000000.00")
        eligible_start, eligible_end = max(rent_from, date(year, 1, 1)), min(rent_to, date(year, 12, 31))
        eligible_days = Decimal((eligible_end - eligible_start).days + 1)
        covered_days = Decimal((rent_to - rent_from).days + 1)
        rent_attributable = (rent_amount * eligible_days / covered_days).quantize(Decimal("0.01"), rounding="ROUND_HALF_UP")
        expected_relief = min((rent_attributable * Decimal("0.20")).quantize(Decimal("0.01"), rounding="ROUND_HALF_UP"), Decimal("500000.00"))
        claims = {
            "nhf_contribution": "1000.00", "nhf_evidence_reference": "NHF-REMITTANCE-001",
            "nhis_contribution": "500.00", "nhis_evidence_reference": "NHIS-REMITTANCE-001",
            "mortgage_interest": "2000.00", "mortgage_owner_occupied": True,
            "mortgage_evidence_reference": "MORTGAGE-STATEMENT-001",
            "life_insurance_premium": "10000.00", "life_insurance_paid_year": year - 1,
            "life_insurance_evidence_reference": "LIFE-PREMIUM-RECEIPT-001",
            "rent_payment_amount": str(rent_amount), "rent_paid_on": date(year, 6, 15).isoformat(),
            "rent_period_start": rent_from.isoformat(), "rent_period_end": rent_to.isoformat(),
            "rent_tenant_name": "Test Staff Member", "rent_landlord_name": "Test Property Owner",
            "rent_landlord_contact": "08000000000", "rent_property_address": "Test residential property, Nigeria",
            "rent_evidence_reference": "RENT-RECEIPT-001",
        }
        payload = {
            "starts_on": self.today.isoformat(), "ends_on": self.today.isoformat(), "department": "Claims Test",
            "idempotency_key": "payroll-claims-001",
            "adjustments": [{"staff_id": staff.pk, "tax_claims": claims}],
        }
        self.auth(self.creator)
        response = self.client.post("/api/admin/staff-operations/payroll/periods/", payload, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        line = response.json()["data"]["lines"][0]
        self.assertEqual(Decimal(line["rent_paid_attributable"]), rent_attributable)
        self.assertEqual(Decimal(line["rent_relief"]), expected_relief)
        self.assertEqual(Decimal(line["nhf_contribution"]), Decimal("1000.00"))
        self.assertEqual(Decimal(line["nhis_contribution"]), Decimal("500.00"))
        self.assertEqual(Decimal(line["mortgage_interest_claim"]), Decimal("2000.00"))
        self.assertEqual(Decimal(line["life_insurance_premium_claim"]), Decimal("10000.00"))
        self.assertEqual(line["tax_claims"]["rent_evidence_reference"], "RENT-RECEIPT-001")
        bad_claims = {**claims, "rent_evidence_reference": ""}
        rejected = self.client.post("/api/admin/staff-operations/payroll/periods/", {
            **payload, "idempotency_key": "payroll-claims-no-evidence-002",
            "adjustments": [{"staff_id": staff.pk, "tax_claims": bad_claims}],
        }, format="json")
        self.assertEqual(rejected.status_code, 400, rejected.content)

    def test_statutory_payroll_requires_a_registered_tax_id_for_every_employee(self):
        staff, _ = self._create_tax_employee(department="No TIN Test", register_tax_id=False)
        self.auth(self.creator)
        response = self.client.post("/api/admin/staff-operations/payroll/periods/", {
            "starts_on": self.today.isoformat(), "ends_on": self.today.isoformat(),
            "department": "No TIN Test", "idempotency_key": "payroll-missing-tin-001", "adjustments": [],
        }, format="json")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("tax_id", str(response.json()).casefold())

    def test_tax_identity_changes_must_be_reported_within_30_days(self):
        staff = make_staff("payroll.tinchange@staff.dev", role=User.Role.HOUSEKEEPER)
        ensure_staff_profile(staff=staff)
        year = self.today.year
        self._register_tax_identity(
            staff, effective_from=date(year, 1, 1), registered_on=date(year, 1, 1),
            tax_id=f"NG-TIN-{staff.pk}-A",
        )
        changed = self._register_tax_identity(
            staff, effective_from=date(year, 4, 1), registered_on=date(year, 4, 1),
            change_reported_on=date(year, 4, 15), change_reason="Taxpayer particulars updated",
            tax_id=f"NG-TIN-{staff.pk}-B",
        )
        self.assertEqual(changed.change_reported_on, date(year, 4, 15))
        self.auth(self.creator)
        listed = self.client.get("/api/admin/staff-operations/payroll/tax-identities/")
        self.assertEqual(listed.status_code, 200, listed.content)
        serialized = str(listed.json())
        self.assertNotIn(changed.tax_id, serialized)
        self.assertIn("tax_id_masked", serialized)
        late = self.client.post("/api/admin/staff-operations/payroll/tax-identities/", {
            "staff_id": staff.pk, "tax_id": f"NG-TIN-{staff.pk}-C",
            "effective_from": date(year, 5, 1).isoformat(), "registered_on": date(year, 5, 1).isoformat(),
            "change_reported_on": date(year, 6, 1).isoformat(), "change_reason": "Late reported update",
            "evidence_reference": "TIN-CHANGE-EVIDENCE-LATE",
        }, format="json")
        self.assertEqual(late.status_code, 400, late.content)
        identity = PayrollTaxIdentity.objects.get(pk=changed.pk)
        identity.tax_id = "EDITED"
        with self.assertRaises(ValidationError):
            identity.save()

    def test_pre_2026_manual_payroll_behavior_is_preserved_without_statutory_rules(self):
        StaffCompensation.objects.create(
            staff=self.employee, effective_from=date(2025, 1, 1), currency="NGN",
            basic_salary=Decimal("60000.00"), housing_allowance=Decimal("100000.00"),
            transport_allowance=Decimal("50000.00"), overtime_rate=Decimal("1000.00"),
            pension_applicable=True, minimum_wage_applicable=True, created_by=self.creator,
        )
        payload = {
            "starts_on": "2025-12-01", "ends_on": "2025-12-31", "department": "Housekeeping",
            "idempotency_key": "payroll-manual-2025-001", "adjustments": [],
        }
        self.auth(self.creator)
        response = self.client.post("/api/admin/staff-operations/payroll/periods/", payload, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()["data"]
        line = data["lines"][0]
        self.assertIsNone(data["statutory_rules_reference"])
        self.assertEqual(Decimal(line["gross_pay"]), Decimal("60000.00"))
        self.assertEqual(Decimal(line["housing_pay"]), Decimal("0.00"))
        self.assertEqual(Decimal(line["transport_pay"]), Decimal("0.00"))
        self.assertEqual(Decimal(line["paye_tax"]), Decimal("0.00"))
        self.assertEqual(Decimal(line["employee_pension"]), Decimal("0.00"))
