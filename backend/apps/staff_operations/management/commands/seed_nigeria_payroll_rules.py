# apps/staff_operations/management/commands/seed_nigeria_payroll_rules.py
"""Seed the researched Nigerian payroll statutory rule set (Nigeria Tax Act 2025).

The rule set is created PENDING_REVIEW and is versioned/effective-dated exactly
like any ruleset proposed through the application.  It must be APPROVED through
the normal review workflow (``review_statutory_rule_set``) by a reviewer who is
NOT the proposer before any payroll run can rely on it — this command never
bypasses that control.

Research baseline (checked 2026-10-11 against public legal/tax guidance; each
run must be re-verified by the owner's accountant before real payroll):

* PAYE bands — Nigeria Tax Act 2025 (in force 2026-01-01), annual chargeable
  income: first ₦800,000 at 0%; ₦800,001–₦3m at 15%; ₦3m–₦12m at 18%;
  ₦12m–₦25m at 21%; ₦25m–₦50m at 23%; above ₦50m at 25%.  The old
  Consolidated Relief Allowance was abolished and replaced by the 0% band plus
  targeted reliefs.
* National minimum wage ₦70,000/month (₦840,000/year): earners at or below the
  minimum wage are exempt from personal income tax.
* Rent relief: 20% of documented annual rent paid, capped at ₦500,000.
* Pension — Pension Reform Act 2014: employee 8% / employer 10% of pensionable
  emolument (basic + housing + transport).  NHF (2.5% of monthly basic) and
  NHIS remain deductible inputs where applicable and are recorded separately
  from PAYE.  NSITF/Employee Compensation (1% of payroll) is an employer cost,
  never an employee deduction.
* PAYE remittance is due by the 10th of the following month; the employer
  annual PAYE return by 31 January.

Idempotent: re-running finds the existing versioned rule set by (jurisdiction,
currency, effective_from) and reports it instead of creating a duplicate.
"""
from datetime import date

from django.core.management.base import BaseCommand

from apps.staff_operations.models import PayrollStatutoryRuleEvent, PayrollStatutoryRuleSet


EFFECTIVE_FROM = date(2026, 1, 1)

LEGAL_BASIS = (
    "Nigeria Tax Act 2025 (signed June 2025; core provisions in force 1 January "
    "2026): progressive personal income tax bands on annual chargeable income — "
    "0% on the first ₦800,000; 15% ₦800,001–₦3,000,000; 18% ₦3,000,001–₦12,000,000; "
    "21% ₦12,000,001–₦25,000,000; 23% ₦25,000,001–₦50,000,000; 25% above ₦50,000,000. "
    "The Consolidated Relief Allowance is abolished; reliefs are the 0% band, "
    "documented rent relief (20% of annual rent paid, capped at ₦500,000), "
    "pension contributions, NHF and NHIS. Employees earning at or below the "
    "national minimum wage (₦70,000/month) are exempt. "
    "Pension Reform Act 2014: mandatory 8% employee / 10% employer contribution "
    "on pensionable emolument (basic, housing, transport). NSITF Employee "
    "Compensation Act 2010: 1% of total monthly payroll is an EMPLOYER cost. "
    "PAYE is remitted by the 10th of the month following deduction (state "
    "internal revenue service of the employee's residence); employer annual "
    "PAYE return is due 31 January. "
    "Owner/professional confirmation still required for: each employee's tax "
    "residency state, tax identification numbers, pension commission status, "
    "NHF/NHIS applicability, and any state-level levies."
)

SOURCE_REFERENCES = [
    "Nigeria Tax Act 2025 (assented June 2025; effective 1 January 2026)",
    "Personal Income Tax (rate etc.) Order — 2026 chargeable-income bands (0/15/18/21/23/25%)",
    "Pension Reform Act 2014 (8% employee / 10% employer minimum)",
    "National Minimum Wage Act 2019 as amended 2024 (₦70,000/month)",
    "Nigeria Tax Administration Act 2025 (PAYE remittance: 10th of following month; employer annual return: 31 January)",
    "Employee Compensation Act 2010 (NSITF 1% employer contribution)",
]


class Command(BaseCommand):
    help = (
        "Seed the researched Nigerian payroll statutory rule set (Nigeria Tax Act "
        "2025) as a versioned PENDING_REVIEW record. Approval stays a human "
        "two-person workflow; this command never approves its own rules."
    )

    def handle(self, *args, **options):
        existing = PayrollStatutoryRuleSet.objects.filter(
            jurisdiction="NG", currency="NGN", effective_from=EFFECTIVE_FROM,
        ).first()
        if existing:
            self.stdout.write(self.style.WARNING(
                f"Rule set already present: {existing.reference} "
                f"(status={existing.status}, effective_from={existing.effective_from}). "
                "Nothing created — statutory rules are versioned; use a later "
                "effective_from for a new version."
            ))
            return

        rule_set = PayrollStatutoryRuleSet(
            jurisdiction="NG",
            currency="NGN",
            effective_from=EFFECTIVE_FROM,
            legal_basis=LEGAL_BASIS,
            source_references=SOURCE_REFERENCES,
            status=PayrollStatutoryRuleSet.Status.PENDING_REVIEW,
        )
        rule_set.full_clean(exclude=["created_by", "reviewed_by"])
        rule_set.save()
        PayrollStatutoryRuleEvent.objects.create(
            rule_set=rule_set,
            type=PayrollStatutoryRuleEvent.Type.CREATED,
            actor=None,
            details={
                "seeded_by": "seed_nigeria_payroll_rules",
                "effective_from": EFFECTIVE_FROM.isoformat(),
                "research_date": "2026-10-11",
                "paye_bands": rule_set.paye_bands,
                "minimum_wage_monthly": str(rule_set.minimum_wage_monthly),
                "employee_pension_rate": str(rule_set.employee_pension_rate),
                "employer_pension_rate": str(rule_set.employer_pension_rate),
                "rent_relief_rate": str(rule_set.rent_relief_rate),
                "rent_relief_cap": str(rule_set.rent_relief_cap),
                "source_references": SOURCE_REFERENCES,
                "note": (
                    "Seeded from published statutory research. Requires independent "
                    "legal/compliance approval (reviewer must not be the proposer) "
                    "before any payroll run uses it."
                ),
            },
        )

        self.stdout.write(self.style.SUCCESS(
            f"Created PayrollStatutoryRuleSet {rule_set.reference} "
            f"(NG/NGN, effective {rule_set.effective_from}, PENDING_REVIEW)."
        ))
        self.stdout.write(
            "NEXT ACTION REQUIRED: an authorised reviewer with payroll.rules.review "
            "must approve this rule set through the payroll rules review workflow "
            "(reviewer must differ from the proposer). Real payroll runs must not "
            "finalize before that approval and the owner's accountant confirms the "
            "assumptions listed in the rule set's legal_basis."
        )
        self.stdout.write(self.style.WARNING(
            "Status is PENDING_REVIEW by design: payroll finalization stays "
            "unavailable until independent review."
        ))
