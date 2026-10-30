# Batch 07 — TEST REPORT

Every result below was executed; nothing is claimed without a run.
Environment: backend Django `manage.py test` with
`DJANGO_SETTINGS_MODULE=config.settings.development` (SQLite test DB);
frontend `node --test` (Node v20).

## 1. New Batch 07 backend tests — 15/15 PASS

`python manage.py test tests.test_batch07_staff_approvals` → `Ran 15 tests … OK`

| Test | Covers |
|---|---|
| `test_manager_holds_exactly_the_five_defined_approval_capabilities` | Persisted MANAGER grants + resolution for all five; GM/Admin keep them; waiter holds none |
| `test_manager_approves_leave_request` | Leave approve via API; leave data preserved; uniform audit evidence |
| `test_manager_approves_stock_count_variance_without_silent_adjustment` | Variance approve via API; balance unchanged pre-approval, adjusted post; evidence |
| `test_manager_approves_purchase_order` | PO approve via API; supplier workflow intact; evidence |
| `test_manager_approves_payroll_run_without_changing_amounts` | Payroll approve via API; totals identical pre/post; evidence |
| `test_manager_approves_expense_without_paying_it` | Expense approve via API; status APPROVED, no payment transaction; evidence |
| `test_manager_sees_all_five_queues_in_the_approvals_inbox` | Inbox lists all five queues; admin-only queues excluded |
| `test_manager_cannot_approve_own_request` | Maker-checker: self-approval → 403, status stays PENDING |
| `test_duplicate_approvals_do_not_reapply_effects` | Re-approval posts no second stock adjustment |
| `test_rejection_and_return_for_correction_workflows_preserved` | Leave reject; PO reject → DRAFT with reason + evidence |
| `test_unauthorized_staff_are_refused_on_all_five_approval_endpoints` | Waiter + receptionist → 403 on all five decision endpoints |
| `test_general_manager_reads_but_cannot_write_staff_accounts` | GM: roster/detail 200; create/update 403 |
| `test_manager_tier_reads_but_cannot_write_staff_accounts` | Manager: roster 200; update 403 |
| `test_admin_staff_management_privileges_unchanged` | Admin create/update/read all work |
| `test_department_staff_cannot_open_the_staff_account_collection` | Waiter/receptionist/housekeeper/accountant/clerk → 403 on list + detail |

## 2. Backend regression suites — all PASS

* Approvals/permissions core — 92/92 PASS:
  `test_approvals_inbox test_capabilities test_role_permissions_matrix
  test_permission_alignment test_separate_approver_control
  test_admin_approval_override test_staff`
* Touched-workflow suites — 57/57 PASS:
  `test_staff_operations test_staff_operations_api test_payroll_api
  test_expense_api test_expenses test_inventory test_inventory_api
  test_audit_actions test_purchase_order_payments
  test_procurement_inventory_dashboard_access`
* Post-refinement re-run — 20/20 PASS:
  `test_batch07_staff_approvals test_expense_api test_expenses`

## 3. Full backend suite — 2115/2118 (3 pre-existing failures)

`python manage.py test tests` in three serial chunks:
chunk 00: 666 tests, 2 failures · chunk 01: 689 tests, OK ·
chunk 02: 763 tests, 1 failure.
(`--parallel` could not be used: the runner itself crashed with
`TypeError: cannot pickle 'traceback' object` — a test-runner
infrastructure issue, not a test result.)

Each of the 3 failures was re-run standalone on the **pristine baseline**
(isolated clone without Batch 07 changes) and fails identically there:

| Failure | Baseline result | Relation to Batch 07 |
|---|---|---|
| `test_department_roles…test_security_and_procurement_role_grants_stay_narrow` | FAILS on baseline | None: asserts PROCUREMENT_OFFICER resolution incl. `payroll.*`, which `capability_codes_for_user` strips, and omits `inventory.manage` from migration 0031 |
| `test_deployment_readiness…test_production_settings_boot_cleanly_with_northflank_mysqls_and_rediss_urls` | FAILS on baseline | None: requires production MySQL/Redis infrastructure |
| `test_portal_stay_lifecycle…test_menu_shows_only_active_items…` | FAILS on baseline | None: guest portal catalogue, untouched by this batch |

## 4. Migration checks — PASS

* `manage.py check` → no issues. `manage.py makemigrations --check --dry-run`
  → "No changes detected" (data-only migration; no schema change).
* `accounts.0032_manager_approval_permissions` on a scratch database:
  * forward ensures all five MANAGER grants + writes audit marker;
  * reverse restores the exact pre-migration grant set + writes reverse marker;
  * re-apply is idempotent;
  * a deliberately deleted `payroll.approve` MANAGER grant is recreated by 0032.
  (`MIGRATION-0032-OK`, `GRANT-REPAIR-OK`.)

## 5. Frontend tests

* `node --test tests-js/test-department-page-access.js` → **24/24 PASS**
  (23 baseline tests updated where Batch 07 intentionally changed behavior +
  1 new `batch 07` sidebar/registry test).
* Full `node --test tests-js/` → **701 pass / 10 fail of 711**; the pristine
  baseline gives **700 pass / 10 fail of 710** — the same 10 failures
  (accounting UI, manifest scope, deploy config, export options, table-wrap),
  all unrelated to staff/approvals. No regressions; the +1 test is the new
  Batch 07 test, passing.
* `python3 frontend/validate.py` → **ALL OK** (73 HTML files incl. modified
  `staff.html`, 33 standalone JS files incl. modified `dashboard.js` with
  syntax checks).

## 6. Requirement coverage (§TESTING)

| Requirement | Evidence |
|---|---|
| Sidebar visibility | 24/24 dept-access tests incl. new Batch 07 sidebar test |
| General Manager staff access | `test_general_manager_reads_but_cannot_write_staff_accounts` + GM sidebar test |
| Manager access to all five approval categories | 5 API approval tests + capability test + inbox test |
| Unauthorized staff access | 403 tests (approvals + staff collection) |
| Direct API permission checks | All approval/staff tests exercise the HTTP endpoints |
| Approval and rejection | Approve + reject tests incl. PO return-to-draft |
| Duplicate approval prevention | Re-approval idempotency test |
| Audit history | Uniform-evidence assertions on all six decision log actions |
| Existing administrator privileges | Admin create/update/read test; admin-only queues/matrix/logs unchanged |
| Existing General Manager privileges | GM keeps all five approvals (capability test); GM inbox queues resolved by capability |

## 7. Known limitations

* The 3 backend + 10 frontend pre-existing failures (listed above) exist on the
  pristine tree and were left untouched as out-of-batch scope.
* Stock-count variances remain approve-and-post only (no reject transition) —
  the workflow's own pre-existing design, preserved deliberately.
