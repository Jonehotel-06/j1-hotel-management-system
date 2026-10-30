# Batch 07 — MANIFEST

Batch: 07 — Staff management, sidebar navigation & approval permissions.
Source repository: `https://github.com/Jonehotel-06/j1-hotel-management-system`
(source commit `6c47455`; the original repository was never modified — all work
was done in an isolated clone).
Output directory: `ai_updates/batch_07_staff_approvals/`

## Package contents

### Documentation (package root)

| File | Purpose |
|---|---|
| `MANIFEST.md` | This file: contents, file map, integration notes |
| `CHANGES.md` | Per-task change log |
| `TEST_REPORT.md` | Tests executed, results, baselines |
| `PERMISSION_MATRIX.md` | Actual resulting permission matrix |
| `DEPENDENCIES.md` | Dependencies: new, reused, cross-batch notes |

### Mirrored source files (repository-relative paths preserved)

| Package path | Status | Batch tasks |
|---|---|---|
| `backend/apps/core/approval_audit.py` | NEW | Task 4 |
| `backend/apps/core/permissions.py` | MODIFIED (added `IsAdminOrManagerRead`) | Task 2 |
| `backend/apps/accounts/views_admin.py` | MODIFIED (mixin permission + docstring) | Task 2 |
| `backend/apps/accounts/migrations/0032_manager_approval_permissions.py` | NEW | Task 3 |
| `backend/apps/staff_operations/views.py` | MODIFIED (leave audit metadata) | Task 4 |
| `backend/apps/staff_operations/payroll_views.py` | MODIFIED (payroll audit metadata) | Task 4 |
| `backend/apps/inventory/views.py` | MODIFIED (stock-count + PO audit metadata) | Task 4 |
| `backend/apps/finance/views.py` | MODIFIED (expense audit metadata) | Task 4 |
| `backend/tests/test_batch07_staff_approvals.py` | NEW (15 tests) | Tasks 2–5 |
| `backend/tests/test_permission_alignment.py` | MODIFIED (GM boundary) | Task 2 |
| `backend/tests/test_staff.py` | MODIFIED (manager-tier boundary) | Task 2 |
| `frontend/js/dashboard.js` | MODIFIED (sidebar + registry) | Tasks 1–2 |
| `frontend/dashboard/staff.html` | MODIFIED (gate + read-only GM UI) | Task 2 |
| `frontend/tests-js/test-department-page-access.js` | MODIFIED + 1 new test | Tasks 1–2 |

Total: 14 files (4 new, 10 modified). No other repository files were touched.

## Safe integration instructions

1. Copy each mirrored file over the same repository-relative path in a clean
   checkout of the source repository at (or after) commit `6c47455`.
2. Backend: run `python manage.py migrate` (applies data-only migration
   `accounts.0032_manager_approval_permissions`; no schema change, reversible).
3. Backend: run `python manage.py check` and the test suites listed in
   `TEST_REPORT.md`.
4. Frontend: no build step — static files. Run
   `node --test tests-js/test-department-page-access.js` (and the full
   `tests-js/` suite to confirm only the 10 pre-existing, unrelated failures
   remain — see `TEST_REPORT.md`).
5. Deploy backend + frontend together: the GM sidebar links `staff.html`, whose
   data requires the new `IsAdminOrManagerRead` gate.

## Rollback

* Reverse the migration: `python manage.py migrate accounts 0031`
  (removes only grants created by `0032`; pre-existing grants are restored
  byte-for-byte — verified in `TEST_REPORT.md`).
* Restore the 10 modified files from version control and delete the 4 new
  files. Frontend rollback is static-file only; note the sidebar snapshot cache
  (`jone.dash.nav` in sessionStorage) refreshes itself on next load.
