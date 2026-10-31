# Deployment and rollback

## Before deployment

1. Review IMPLEMENTATION_REPORT.md limitations. Obtain payroll-owner authorization and staging acceptance; this package is not unconditional production sign-off.
2. Back up the database and the exact deployed source/frontend versions. Save the current lateness policy and outstanding case/deduction reports.
3. Compare the target repository with base commit `decbcf7adc9ca7f3e7c50d784993910c330f6964`. Resolve migration numbering conflicts rather than overwriting another migration. This package now contains both 0016 and 0017. If 0016 is already applied from the earlier package, retain its applied history and apply 0017.
4. Copy only manifest-listed source files from this package to their repository-relative destinations. Do not copy the package directory over the repository root.
5. Install the target project's requirements, run `python backend/manage.py migrate --plan`, then `python backend/manage.py migrate`. Use the deployment's normal settings, secrets and database configuration.
6. Deploy backend and frontend together and restart normal application services. Clear the deployed payroll page cache through the existing release process.
7. Review legacy unpaid/unapplied percentage deductions and unresolved cases. The migration disables the old lateness policy. Admin must set a new NGN/minute rate and appropriate grace before explicitly enabling it.
8. Re-run packaged backend/frontend tests and stage a real authorized receipt/count/shift investigation and payroll review. Do not manufacture operational evidence for acceptance.

## Preferred rollback after any new payroll activity

1. Stop new payroll generation, case approvals and policy changes through operational maintenance controls.
2. Export all new case references, selected department links, attendance evidence, active per-line recovery claims, allocations, applied payroll links, payments and audit entries since deployment.
3. **Do not delete or reset applied deductions or salary payments.** Use existing rejection, authorized revocation/reimbursement and payment reversal workflows where appropriate. Approved missing-stock allocations have no newly introduced reversal endpoint; obtain a reviewed payroll correction rather than manipulating database links.
4. Prefer a forward corrective release while retaining additive columns and migration history. Reverting the source to the old version would restore unsafe department-wide equal splitting and daily-pay percentage behavior. Keep deductions disabled if the old source must be restored.
5. Restore prior application/frontend files from the saved release; remove the new helper only when no deployed code imports it. Leaving additive database columns in place avoids destroying evidence.

## Database reversal only before new activity (or full coordinated restoration)

- With the application stopped and a verified backup, `python backend/manage.py migrate staff_operations 0015` reverses 0017 and 0016, removing department associations, live per-line recovery reservations and the new fields. **This destroys discovery/evidence/rate/reservation data** and is not acceptable after new payroll activity without a complete audited recovery plan.
- The data migration's reverse is intentionally a no-op: it does not guess the previous enabled policy state. Review the saved policy before any manual re-enablement.
- Restore all modified files from the prior release and remove the new migrations/helper as part of a coordinated rollback. Restart and run the prior release's tests.
- If restoring a database backup after live transactions, restore the matching full system state under the hotel's recovery procedure and reconcile all intervening payments, receipts, stock and attendance. Never restore payroll tables alone and leave linked financial transactions inconsistent.

## Migration 0017 operational note

0017 preserves live recovery reservations from existing cases. Legacy whole-count cases reserve every missing line; line-specific cases reserve only their selected line. Conflicting evidence stops migration for investigation. Do not bypass uniqueness, delete cases, or discard claims to force migration success. Reject invalid pending cases through the authorized review workflow where appropriate, or use a separately reviewed data correction.

Reverting to a pre-0017 application after approving multiple per-line cases would restore whole-count assumptions. Keep new case entry/approvals disabled during rollback and preserve the full case/claim export. Prefer a forward fix rather than schema reversal after payroll activity.
