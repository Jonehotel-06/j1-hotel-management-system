# Batch 02 — Payroll implementation report (continued)

## Status and provenance

The four follow-up gaps selected for completion are now implemented: multi-department responsibility, per-item recovery reservations, remote inventory search/pagination, and historical Super Admin employee presentation. This is a tested source update package, not a production deployment or unconditional production sign-off. Staging and the operational checks below remain necessary.

Source: https://github.com/Jonehotel-06/j1-hotel-management-system

Base commit: `decbcf7adc9ca7f3e7c50d784993910c330f6964`.

Work was performed in a fresh clone at `/home/user/payroll-work`, branch `ai/batch-02-payroll`. No original deployment or remote repository was modified or pushed. Test fixtures were confined to disposable Django test databases; no operational staff, stock or attendance records were fabricated.

## 1. Problems discovered

- Missing-stock selection exposed count references rather than inventory item names.
- Missing-stock valuation used mutable standard cost, with a manual amount fallback.
- Cases recorded only a date and lacked historical duty evidence.
- Department approval automatically divided losses across current active/on-leave members without verifying attendance.
- Lateness charged a daily-pay percentage rather than actual minutes at a configured rate.
- Payroll employee lists/generation did not exclude the hotel-owner/superuser account.
- The first update retained one department and one recovery case per whole count, limited the picker to the first 100 lines, and left historical excluded employee rows visible. The continuation removes these four limitations.

## 2. Root causes and inspected integrations

Inspected payroll models, serializers, API views, service, migrations, salary-payment/financial posting paths, attendance/shift models, inventory stock counts/movements, receipt handling, existing valuation service, payroll HTML, shared form/picker contracts and relevant tests.

Inventory already uses receipt-weighted purchase-cost projection by item/location. Its standard-cost fallback is not sufficient evidence for wage recovery, so payroll rejects missing actual receipt costs instead. Existing count and movement quantities are stored in base units.

The old approval service used present-day department membership. The old lateness calculation intentionally used a salary-derived daily percentage. Corrections stay within the existing payroll workflow; no new payroll architecture, inventory valuation engine or attendance system was introduced.

## 3–4. Files modified and original locations

`UPDATED_FILES_MANIFEST.md` lists every updated/new file. Each packaged application path is identical to its repository-relative destination. Application changes are limited to payroll models, serializers, views, services, two additive staff_operations migrations and the payroll HTML. Payroll tests are updated/extended.

No authentication, staff-management, attendance-posting, inventory-posting, salary-payment service or accounting implementation file is modified.

## 5. Calculations and validation

### Missing-stock selection and valuation

Select an existing negative-variance posted StockCountLine, presented using its inventory item name, count location and missing base quantity. Retain the count-line ID, underlying item FK ID and count reference; names do not replace references.

Purchase unit cost = sum(received base quantity × recorded receipt unit cost) / sum(received base quantity), for the same item/location, restricted to movements occurring at or before discovery. Different receipt batches/prices are quantity-weighted. Later receipts cannot reprice an immutable case. Missing, zero, invalid or currency-inconsistent purchase costs fail closed: no selling-price, estimated-price or standard-cost fallback.

Missing quantity = negative posted variance in base units. Total = missing base quantity × unrounded weighted purchase unit cost, rounded once to cents using Decimal/ROUND_HALF_UP. Thus 3 bottles at NGN 1,200/base bottle = NGN 3,600; 0.5 base unit at NGN 1,500 = NGN 750. There is no second pack multiplication because both inputs already use inventory base units.

The form displays quantity × unit cost = total, using the item's currency. Backend independently computes the value and rejects conflicting submitted totals. The case snapshots receipt movement IDs, individual quantities/costs, numerator/denominator, valuation method, cutoff, item ID/name, quantity, unit cost and total. Manual estimated cases are disallowed.

### Discovery and responsibility

Date and time are collected separately and combined into a proper datetime. DRF interprets timezone-less hotel wall time in the configured Django timezone; aware inputs are treated as instants. Backend rejects future/invalid values and requires the incident date to equal the hotel-local discovery date. Historical unknown times remain null, never invented.

Eligibility requires actual clock-in at/before discovery, no clock-out at/before discovery, an uncancelled linked shift covering discovery, no attendance EXCEPTION, and matching **historical shift department**, not today's profile department. Open attendance is bounded by scheduled shift end. Owner ADMIN/superuser employees are excluded.

Cases accept one or multiple existing active department IDs through `department_ids`. The original `department_id` remains supported as a single-department compatibility input and primary display reference. Selected departments have database M2M references and immutable JSON name/code snapshots. Attendance evidence retains department, attendance and shift IDs/references and relevant timestamps.

STAFF responsibility requires distinct explicitly selected eligible staff. DEPARTMENT responsibility stores relevant duty evidence but does not automatically charge members. Approval requires explicit positive per-person allocations, summing exactly to the case total, plus an authorization/rationale note. Approval rechecks duty eligibility and restricts recipients to the recorded responsibility evidence. Allocation department snapshots identify each recipient's actual duty department. No automatic equal division runs.

### Lateness

Whole late minutes = floor((clock-in instant − scheduled start instant) / 60 seconds).

Chargeable minutes = max(0, whole late minutes − configured grace minutes).

Amount = chargeable minutes × administrator-configured `rate_per_minute`, rounded to cents with Decimal/ROUND_HALF_UP. At zero grace: 5×1=5, 10×1=10, 20×1=20 and 30×2=60. With 15 free grace minutes, 16 minutes late at NGN 2.50/minute costs NGN 2.50. Grace is explicitly free time, not a threshold that subsequently charges those minutes.

Datetime subtraction handles overnight shifts, including clock-in after midnight. Missing/cancelled schedules, attendance exceptions and owner/superuser accounts do not generate new deductions. The existing attendance OneToOne link prevents duplicate generation, and business_date filtering respects payroll periods. Each deduction snapshots its rate. Historical percentage snapshots remain identifiable and unchanged.

## 6. Database changes and historical preservation

### Migration 0016

Adds nullable case discovery datetime, responsibility type/evidence JSON, and Decimal per-minute rates to policy and deduction snapshots. Historical amounts and unknown times are not rewritten.

The data operation disables the old lateness policy; an administrator must explicitly configure a NGN/minute rate and re-enable it. Percentages are never silently interpreted as money/minute.

### Migration 0017

Adds responsible-departments M2M references and `PayrollStockShortageClaim`, a live reservation with a database-unique OneToOne link to StockCountLine. This uses a portable unique constraint rather than relying on partial indexes unsupported by some MySQL deployments.

Creation locks the parent StockCount, verifies availability and creates the claim in the same transaction as the case. The unique constraint is an additional backstop. Different missing lines in the same count can have separate cases, but a line cannot have multiple active recoveries. Rejection releases only its reservation; the rejected case and evidence remain intact. Approved claims remain reserved even after payroll consumption, preventing later duplicate recovery.

The migration attaches the original department and backfills active reservations from existing count-line evidence. Old whole-count cases without line IDs reserve every negative-variance line. Conflicting/invalid historical evidence causes migration failure for investigation rather than silent duplicate recovery. Legacy cases without discovery-time evidence still cannot receive new approvals.

## 7. Payroll integration, UI and access

Retains case allocation records, unique case/staff constraint, applied-payroll-line links, reference-bearing deduction reasons, existing decision audit logs and transactional one-time payroll consumption. Regenerated payroll does not reapply consumed allocations. Ordinary staff cannot decide cases or edit approved deductions. Existing salary-payment and accounting services remain unchanged.

Inventory selection now performs server-side item-name/SKU search and uses previous/next pages. It is no longer limited to searching the first 100 lines. Inputs are bounded/validated, ordering is stable, stale asynchronous results are ignored, and stale item IDs cannot be submitted after a search change/failure. Preview and backend valuation use the same discovery cutoff and rounding.

Payroll employee eligibility excludes both `is_superuser=True` and the repository's hotel-owner `ADMIN` role. This is distinct from the Admin Manager/Procurement role. General Manager, Accountant and Admin Manager capabilities are unchanged. Owner ADMIN approval/policy permissions and global permissions remain intact.

Historical excluded employee rows are filtered from payroll detail employee lists, employee counts and payslip presentation; compensation/lateness lists also exclude them. Underlying payroll lines, salary payment history, audit actors and financial totals are not erased or recalculated. A UI reconciliation note explains that historical excluded employee rows are hidden while financial totals retain original ledger values. Existing payment-history/correction services remain available under their original authorization so historical money can still be reconciled lawfully.

## 8. Tests executed and final results

Final backend command:

`python backend/manage.py test tests.test_payroll_api tests.test_lateness_deductions tests.test_attendance_code_and_visibility tests.test_attendance_code_persistence tests.test_attendance_off_desk_management tests.test_attendance_workstation_policy --noinput`

**115 run: 113 passed, 2 skipped by the existing suite; no failures.** This includes 31 payroll/lateness tests and 84 attendance regression tests.

Final frontend command:

`node --test frontend/tests-js/test-payroll-ui.js`

**10 passed.** Includes inline JavaScript syntax checks and an executable mocked-API wizard test for multi-department submission, remote page navigation, searching an item beyond the first 100, preserving selected IDs and rejecting stale selections. This is a behavior/unit test, not a production browser end-to-end test.

Additional checks:

- `python backend/manage.py makemigrations --check --dry-run` — no changes detected.
- `git diff --check` — clean.

Final logs: `test_results/backend.log`, `test_results/frontend.log`.

Coverage includes stock ID/value ownership; receipt batches and historical cutoff; fractional base-unit value; missing cost failure; staff and department selection; absent/duplicate staff; invalid IDs/time/future date; multi-department uneven allocations; approval restrictions; no implicit allocations; independent line recoveries; database uniqueness; rejection/retry; legacy reservation backfill; remote search/pagination; payroll regeneration; per-minute examples/grace/midnight crossing; revocation/add-back; owner/superuser exclusion without permission loss; historical total preservation; existing salary payment/reversal and ledger behavior; unchanged attendance regressions.

## 9. Remaining operational limitations / staging gates

- Missing quantity is the verified posted variance in base units. There is no editable manual pack/quantity workflow. Upstream receipt/count base-unit correctness must be confirmed; ambiguous purchase units are not guessed.
- Duty eligibility is conservative and requires a linked shift. Legacy attendance without shifts cannot establish liability. Break-event timelines and historical department aliases are not reconstructed. Duty evidence alone is not proof of legal liability: independent investigation and lawful wage-deduction authorization remain mandatory.
- Authorized schedule changes before generation use the current scheduled start. Changes after an immutable deduction snapshot require the existing revocation/reimbursement workflow, not silent historical repricing.
- Existing legacy percentage deductions, including outstanding unapplied ones, are not recalculated. Review/revoke inappropriate outstanding legacy deductions before enabling the new policy.
- Receipt weighting follows the existing inventory projection, not FIFO or a perpetual remaining-stock cost-layer engine. Transfer-only/opening stock without purchase receipts at the count location fails closed.
- Ledger totals and historical audit identities intentionally remain visible; hiding employee rows does not rewrite accounting or obscure who approved historical activity.
- Tests use SQLite; production MySQL locking/concurrent requests were not load-tested. The database unique constraint is tested, but that does not substitute for production-engine concurrency testing. No full repository suite, real browser end-to-end deployment or production/staging database was exercised.

## 10. Deployment and rollback

See `ROLLBACK.md`. Deploy frontend/backend together and apply both migrations after backup and staging review. Old clients submitting only deduction_percent or manually estimated shortage values are intentionally incompatible with the new safer write contract. Do not enable deductions without payroll-owner acceptance and review of legacy outstanding deductions.
