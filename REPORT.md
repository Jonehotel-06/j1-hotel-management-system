# Admin Manager / Procurement dashboard update

Base inspected: `Jonehotel-06/j1-hotel-management-system`, commit `974be99e1e3de4ade428de54ba67d0084f4a99f7`. Work was performed on an isolated clone; nothing was pushed.

## Changed-file manifest

Repository-relative paths in this folder are complete replacement files. `changes.patch` is the equivalent unified patch; do not apply both ways.

- `backend/apps/accounts/capabilities.py`
- `backend/apps/accounts/migrations/0031_procurement_inventory_access.py` (new)
- `backend/apps/staff_operations/payroll_views.py`
- `backend/tests/test_procurement_agent_workflow.py`
- `backend/tests/test_procurement_inventory_dashboard_access.py` (new)
- `frontend/dashboard/inventory.html`
- `frontend/dashboard/payroll.html`
- `frontend/js/dashboard.js`
- `frontend/tests-js/test-department-page-access.js`
- `frontend/tests-js/test-payroll-ui.js`
- `frontend/tests-js/test-procurement-dashboard-inventory.js` (new)

Also in this delivery: `REPORT.md` and `changes.patch`; these are delivery artifacts, not repository changes.

## Verified cause and design

The approved Admin Manager/Procurement role is `PROCUREMENT_OFFICER`, whose landing page is `dashboard/procurement.html`. The shared sidebar and page registry explicitly admitted this role to Payroll and excluded it from Inventory; each page repeated that gate. Migration 0030 also assigned payroll view/manage to this role, while its persisted role did not have `inventory.manage`. Backend permissions are resolved from database role grants and per-user overrides, not solely the Python defaults. Thus changing navigation alone would both leave Payroll APIs available and fail to authorize Inventory.

The shared sidebar now shows Inventory at the former Payroll position for this role, without moving the existing Inventory entry for other roles. There was no separate Payroll shortcut in the procurement page or bottom bar in this revision. The registry and page boot guards agree on the new audiences. The static page guards are client-side UX, not the security boundary; backend capability checks remain authoritative.

## Permission effects

Per your selection, migration 0031 grants the existing **full `inventory.manage`** role capability, not merely read-only access. It allows the inventory operations already covered by that capability, including permitted stock changes, and may affect other existing capability-gated APIs. It does not grant `inventory.adjust.approve` or `procurement.approve`. Existing individual inventory revocations continue to apply. No inventory or procurement business logic was altered.

For this role only, the capability resolver removes `payroll.*` from the effective result after applying both persisted role grants and individual overrides. Stored grants are retained for audit and reversibility, but cannot authorize Payroll while the code is active. The two stock-shortage Payroll endpoints also reject this role explicitly: unlike other Payroll endpoints, they otherwise accept `inventory.manage`. Other roles' shortage access is unchanged. Self-service payslip access remains available for the employee's own finalized payslips, as it is for all staff; it is not access to payroll administration or other employees' payslips. Do not interpret denial of payroll administration as denial of the employee's own salary records.

A pre-existing sidebar snapshot can briefly display the previous menu before the new JS renders on a page load. For a clean deployment, ask affected staff to close existing dashboard tabs and sign in again, or clear the `jone.dash.nav` session-storage key in each tab. This is a display-cache issue, not API authorization. Ensure frontend and backend deploy together and refresh old frontend/service-worker caches.

## Tests actually run

- Node: `node --test tests-js/test-procurement-dashboard-inventory.js tests-js/test-department-page-access.js tests-js/test-payroll-ui.js tests-js/test-inventory-ui.js` — **34 passed**. Checks role page gates, role-filtered menu and order, page registry agreement, and existing Payroll/Inventory UI contracts; it is not a browser end-to-end test.
- Django: `python manage.py test tests.test_procurement_inventory_dashboard_access tests.test_procurement_agent_workflow --noinput` — **45 passed** on SQLite. Includes Inventory and purchase-order GETs for procurement (200), payroll periods/compensation/shortage GET and periods POST denial (403), explicit payroll override denial, accountant Payroll (200), storekeeper shortage access (200), and unauthenticated API denial. Existing procurement workflow tests passed. An old test expecting procurement Inventory directories to return 403 was updated to the newly authorized behavior.
- After adjusting the migration's audit dependency, the two new Django tests were rerun — **2 passed**. Fresh test-database migration also succeeded.
- Isolated SQLite migration: apply 0031, reverse to 0030, reapply 0031 — **passed** after correcting the audit dependency. The initial rollback attempt exposed a historical audit-model dependency mismatch and failed; the final migration fixes it.
- `git diff --check`, `node --check frontend/js/dashboard.js`, and `git apply --check changes.patch` against a clean clone of the base — **passed**.

No interactive browser, real deployed authentication, production database, all endpoint permutations, or full repository regression suite was tested. Direct URL behavior was tested via the actual JS role/page gates, not by navigating in a browser. Static frontend files themselves may still be fetched; sensitive data and actions must be protected by the API. Verify deployment-specific overrides and exercise representative workflows in staging.

## Manual edit locations

Line numbers below refer to the **delivered files** at the base revision; they can shift if the target differs. Use the patch rather than transcribing these edits where possible.

- `frontend/js/dashboard.js`: lines 38, 46–47, 143, 158, 209–210: role-specific Inventory placement, page audiences, and placement filter.
- `frontend/dashboard/inventory.html`: line 55, add procurement to the boot role list.
- `frontend/dashboard/payroll.html`: line 75, remove procurement from the boot role list.
- `backend/apps/accounts/capabilities.py`: lines 220–229 and 337–340, update documented defaults and enforce the role-specific payroll capability denial.
- `backend/apps/accounts/migrations/0031_procurement_inventory_access.py`: lines 1–28, new migration and reversible role grant.
- `backend/apps/staff_operations/payroll_views.py`: import at line 13; permission at lines 204–208; attach it at lines 213 and 263.
- Tests: `backend/tests/test_procurement_agent_workflow.py` lines 167–170; `frontend/tests-js/test-department-page-access.js` lines 323–324; `frontend/tests-js/test-payroll-ui.js` line 55; the two new test files in full.

## Safe application

1. Back up the database and current deployment; schedule a maintenance window or coordinated deployment. Confirm the target revision and migration graph; inspect `changes.patch`, local changes, per-user capability overrides, and any migration numbered 0031. Do not overwrite divergent files or resolve a migration conflict blindly.
2. Copy `changes.patch` outside the checkout, or use its absolute path. From the repository root run `git status --short`, then `git apply --check /path/to/changes.patch`; stop on any conflict. Apply with `git apply /path/to/changes.patch`. Do not copy the replacement files as well.
3. Install the project's normal dependencies and run the tests above in staging. Run `cd backend && python manage.py migrate --plan`, inspect the plan, then `python manage.py migrate` using your established release procedure. Check that 0031 ran, check effective capabilities for affected users (especially explicit revocations), and verify UI and API behavior. Restart application workers so the new authorization code is loaded.
4. Deploy the corresponding frontend, refresh old browser/service-worker caches, and clear stale sidebar snapshots as noted above. Monitor authorization failures and audit logs.

## Rollback

First assess security implications: restoring the old code **reactivates any retained role or individual Payroll grants for procurement**, and the old frontend again exposes Payroll. If that is not acceptable, maintain an independent deny or revoke those grants before rollback. Do not restore an entire production database over new business activity.

In a maintenance window, verify no later migration depends on 0031 using `python manage.py migrate --plan accounts 0030`; do not proceed if unrelated migrations would be reversed. With the updated migration file still present, run `python manage.py migrate accounts 0030`. The reverse removes the Inventory role-grant row only if this migration recorded creating it; it preserves a pre-existing row and retains append-only audit markers. Review any grant changes made since deployment before reversal. Then from the repository root run `git apply --reverse --check /path/to/changes.patch` and, only if clean, `git apply --reverse /path/to/changes.patch`; redeploy the previous frontend/backend and restart workers. Clear stale frontend caches and verify permissions. Restore a backup only under your normal disaster-recovery procedure if needed.
