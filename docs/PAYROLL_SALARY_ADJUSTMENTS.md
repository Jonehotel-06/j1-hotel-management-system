# Payroll — Approved Salary Adjustments (partial month, unpaid absence, disciplinary, other)

This document describes the **salary adjustment** feature added to the payroll module:
how an adjusted salary is recorded, calculated, approved, applied by the payroll run and paid,
and the accounting decision behind it.

Dashboard: **Payroll → "Record Adjusted Salary"** (button on the Payroll runs panel and on the
new **Salary adjustments** tab).

---

## 1. What problem it solves

Before this update, payroll always paid the full monthly salary terms (basic + housing + transport
+ overtime + allowances + bonus) for every eligible staff member in the period. There was no
controlled way to pay a **reduced salary for a specific month** when a staff member:

* joined, resigned or returned part-way through the month (**partial month / prorated**),
* was absent **without approved leave** (**unpaid absence deduction**),
* breached a hotel rule and management decided on a **disciplinary deduction**,
* needs any **other** one-off deduction or an **additional payment** for that month.

The new feature records such an adjustment **per staff member and payroll month**, calculates
everything on the server from the staff member's current salary terms, requires an **independent
approval**, and is then **applied by the normal payroll run** for that month. Nothing in the
feature pays anyone and nothing changes the staff member's monthly salary terms (StaffCompensation
history is untouched).

---

## 2. Calculation rules (server-side only)

* `monthly_salary = basic_salary + housing_allowance + transport_allowance` from the StaffCompensation
  record effective on the **first day of the payroll month**.
* `daily_rate = monthly_salary ÷ applicable_days`, where the **calculation basis** is one of:
  * `CALENDAR_DAYS` – actual days in the month (28/29/30/31),
  * `FIXED_30` – a fixed 30-day month,
  * `SCHEDULED_DAYS` – the staff member's scheduled working days (`applicable_days` must be supplied;
    the UI pre-fills it from the roster when shifts exist).
* **PRORATED**: `prorated_salary = monthly_salary × days_worked ÷ applicable_days`
  (the three components are prorated individually and the last one absorbs the rounding so the
  parts add up exactly; `days_worked` cannot exceed `applicable_days`).
  Example: ₦60,000 × 15 ÷ 30 = **₦30,000.00**; ₦60,000 × 15 ÷ 31 = **₦29,032.26** (ROUND_HALF_UP).
* **ABSENCE**: `absence_deduction = monthly_salary × absence_days ÷ applicable_days`.
  The number of absence days is capped by `calendar_days − approved paid leave days`;
  **approved leave is never treated as unpaid absence and is never deducted automatically.**
* **DISCIPLINARY**: a fixed `amount`, with a mandatory `violation_category`, `incident_date`
  (not in the future), `reason` (≥ 5 characters) and `evidence_reference` or `notes`.
* **OTHER**: a fixed `amount` with an explicit `other_direction` (`DEDUCTION` or `ADDITION`).
* Optional `additional_payment` (with its reason) can be added to PRORATED / ABSENCE / DISCIPLINARY
  records.
* `final_payable = prorated_salary − (absence + disciplinary + other deductions) + additional_payment`
  and is never negative; combined deductions that exceed the eligible salary for the month are
  refused.
* One **active** record per staff / month / type (DISCIPLINARY: also per category + incident date).
  Rejected and reversed records free the month again.
* The month's **calculation summary** (`Normal Monthly Salary`, `Prorated Salary`,
  `Absence Deduction`, `Disciplinary Deduction`, `Other Deduction`, `Additional Payment`,
  `Final Payable Salary`) always combines every effective record for that staff member and month.

All values are `Decimal` quantised to 2 dp; the browser only displays what the server returns
(`?options=context` and `POST …/preview/`).

---

## 3. Workflow and controls

| Step | Who | What happens |
|------|-----|--------------|
| Record | `payroll.manage` (Accountant, HR Officer, HR Manager, Accounts Manager, Manager, GM, Admin) | `POST /payroll/salary-adjustments/` validates, calculates and stores a snapshot. With `submit=true` (default) the record goes straight to `PENDING_APPROVAL`; otherwise `DRAFT` → `…/submit/`. Idempotent via `idempotency_key`. |
| Approve / reject | `payroll.approve` (HR Manager, Accounts Manager, Manager, GM, Admin) – **never the recorder** | `POST …/<reference>/decision/ {approved, decision_note}`. Self-approval is refused (403). |
| Apply | the payroll run | `create_payroll_period` consumes every **APPROVED** adjustment of the run's month for staff in the run and freezes it onto the PayrollLine (`applied_payroll_line`). A **pending** adjustment blocks the run ("awaiting approval"); a processed run for the month blocks new adjustments for that month ("already includes"). |
| Release | payroll run rejected | A rejected run releases its adjustments back to `APPROVED` (event `RELEASED`); the replacement run (`replaces_reference`) applies them again exactly once. |
| Pay | normal payroll payment endpoints | When the employee line (or the whole period) is fully paid the adjustment becomes `PAID`; reversing the payment puts it back to `APPROVED`. |
| Reverse | `payroll.approve` for approved records, `payroll.manage` for drafts/pending | `POST …/<reference>/reverse/ {reason}` keeps the record as history (`REVERSED`). Refused once the record is frozen in a live payroll run. |

Approved records refuse `save()`/`delete()` of financial fields at the model level; events are
append-only. Every action is written to the audit log
(`PAYROLL_SALARY_ADJUSTMENT_{CREATED,SUBMITTED,APPROVED,REJECTED,REVERSED}`) and pending
adjustments appear in the approvals inbox (`/api/admin/reports/approvals/`, category
`salary_adjustments`, link `payroll.html`).

---

## 4. Accounting decision

**Approved adjustments are consumed into the PayrollLine *before* the run is approved, so the
feature adds no new ledger postings and no second salary expense.**

* Proration lowers the line's basic / housing / transport components.
* Absence, disciplinary and other deductions go to the line's `deductions`
  (labelled e.g. `Unpaid absence 3 day(s) ADJ-2026-000012`, `Disciplinary deduction ADJ-…`).
* Additional payments go to the line's `allowances`.
* The existing accrual (`PAYROLL_EXPENSE` debit = gross, `PAYROLL_PAYABLE` credit = net) therefore
  already reflects the adjusted amounts; salary payments continue to move only `PAYROLL_PAYABLE`.
* Exactly **one** salary expense and **one** payment entry per run — verified by
  `tests.test_payroll_salary_adjustments` scenario 10.

Deductions are *not* posted as income; they simply reduce the salary expense, which matches how
the payroll module already treats disciplinary, shortage and lateness deductions.

---

## 5. API summary

Base: `/api/admin/staff-operations/payroll/salary-adjustments/`

| Method & path | Purpose |
|---------------|---------|
| `GET ?staff_id&department&payroll_year&payroll_month&adjustment_type&status&search&page&page_size` | Paginated history (`payroll.view`). |
| `GET ?options=context&staff_id&payroll_year&payroll_month` | Staff salary terms, calendar/scheduled days, approved leave, processed run, existing adjustments, month summary. |
| `GET ?options=categories` | Violation categories for disciplinary deductions. |
| `POST` | Record (`payroll.manage`). Body: `staff_id, payroll_year, payroll_month, adjustment_type, calculation_basis, applicable_days?, days_worked?, absence_days?, amount?, other_direction?, additional_payment?, additional_payment_reason?, reason, violation_category?, incident_date?, evidence_reference?, notes?, submit=true, idempotency_key?`. `201` created, `200` replay. |
| `POST preview/` | Same body, nothing saved → `{calculation, summary}` or `{error}`. |
| `GET <reference>/` | Detail incl. `events` and `month_summary`. |
| `POST <reference>/submit/` | Draft → pending approval. |
| `POST <reference>/decision/` | `{approved, decision_note}` (`payroll.approve`, independent). |
| `POST <reference>/reverse/` | `{reason}`. |

Unauthenticated → 401; missing capability → 403; validation problems → 400 with the standard
error envelope.

---

## 6. Database

Migration `staff_operations.0020_payroll_salary_adjustment` adds
`staff_operations_payrollsalaryadjustment` and `staff_operations_payrollsalaryadjustmentevent`
(MySQL-safe: explicit `max_length`s, `DecimalField(14,2)`, FK to `PayrollLine` with
`on_delete=PROTECT`, unique `active_key`). Apply with `python manage.py migrate` as described in
`docs/DATABASE_MIGRATIONS_AND_MYSQL.md`.

---

## 7. Tests

* Backend: `python manage.py test tests.test_payroll_salary_adjustments` (18 scenarios: proration
  on 30/31-day months, absence vs approved leave, disciplinary evidence, duplicate and over-deduction
  refusal, independent approval, run blocking/consumption, single ledger posting, payment status
  flips, rejected-run release and re-application, history filters, approvals inbox, reversal).
* Frontend: `node --test tests-js/test-payroll-salary-adjustments.js` (endpoint centralisation,
  API helper URLs, capability gating, server-side calculation only, independent approval wording,
  inline script parsing).
