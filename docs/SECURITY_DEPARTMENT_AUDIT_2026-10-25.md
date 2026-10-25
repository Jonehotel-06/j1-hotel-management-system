# Department Security & Single-Role Audit — 2026-10-25

## Scope and source checked

Inspected the current GitHub source at commit `d3b24c5d66de1de9bc36860f8c394c278b4d6a4f` before editing. The requested source files and all five named suites existed in that checkout: `test_department_roles`, `test_role_permissions_matrix`, `test_permission_alignment`, `test_role_alias_guards`, and `test_dashboard_role_access`.

This is a focused role/profile/account-boundary update, not a fresh line-by-line certification of all 321 endpoint rows in the existing matrix snapshot.

## Findings

**Already present in the source:** `DEPARTMENT_ROLES` had 14 unique operational department keys and 14 distinct canonical role values. It already mapped Security to `SECURITY`, the combined Administration & Procurement department to one `PROCUREMENT_OFFICER` role, and Kitchen to `CHEF`. `StaffProfile.job_title` already supports a non-role position such as Kitchen Assistant. The existing capability registry already limited Security to `guest_request.manage` and `attendance.clock`, and gave Procurement `procurement.manage`, `attendance.clock`, and `leave.request`—not `procurement.approve`.

**Verified gaps corrected:** secondary staff-profile creation and patching and the account lifecycle department-transfer action did not consistently refuse retired roles or mismatched department/role pairs; the staff-profile department could override a conflicting role-derived department; the permission-matrix write path still accepted retired aliases; and the staff create/edit role picker offered retired titles, including `KITCHEN_ASSISTANT`, as assignable roles. The Django admin profile editor also lacked department/role validation.

A repository search found `is_cross_department()` had no production callers (only its definition and tests). It is not represented here as an enforcement control. Department identity validation is now connected to the existing capability resolver and the common core DRF permission predicate instead.

## Canonical mapping

| Department | Standard role | Notes |
| --- | --- | --- |
| Management | `MANAGER` | Existing departmental standard role. `ADMIN` and `GENERAL_MANAGER` remain global oversight roles. |
| Accounts | `ACCOUNTANT` | Cashier remains a separate department. |
| Admin Management / Procurement | `PROCUREMENT_OFFICER` | One `PROCUREMENT` department entry and one standard role; no separate Admin role. |
| Kitchen | `CHEF` | Kitchen Assistant is `StaffProfile.job_title`, not a role. |
| Restaurant | `WAITER` | One role for waiters and waitresses. |
| Porter | `PORTER` | — |
| Front Desk | `RECEPTIONIST` | — |
| Housekeeping | `HOUSEKEEPING` | — |
| Inventory / Stores | `STOREKEEPER` | — |
| Security | `SECURITY` | Narrow default grants listed above. |
| Cashier | `CASHIER` | Preserved as distinct. |
| Bar | `BARTENDER` | Preserved as distinct. |
| Maintenance | `MAINTENANCE` | Preserved as distinct. |
| Human Resources | `HR_OFFICER` | Existing operational department retained. |

## Implementation

- Added shared department normalization and role/department validation with compatibility for stored display labels and legacy aliases. Conflicting operational role/profile data now yields no department context.
- Applied fail-closed identity validation to existing capability resolution and the common DRF `_active()` permission predicate. Explicit global-role policy remains unchanged.
- Enforced retired-role refusal and pair validation in staff account create/update, staff-profile create/patch, lifecycle transfer, Django admin profile editing, and role-capability matrix writes. Role changes synchronize an existing staff profile.
- Kept role-specific capability grants, explicit user overrides, approval/maker-checker policy, and operational workflow endpoints unchanged. No automatic approval privilege was added.
- Updated staff creation/editing and transfer forms to offer canonical roles/departments. Retired role codes remain available for historical filtering/compatibility, not new assignments. Kitchen Assistant is entered as a position under `CHEF`.
- No database migration was added. Existing `StaffProfile.job_title`, department, user role, and capability schema support the change. Existing migration `0027_normalize_department_roles.py` was inspected, not modified or run against production.

## Files changed

- `backend/apps/accounts/departments.py` — trusted context, aliases, fail-closed conflict resolution, assignment validation.
- `backend/apps/accounts/capabilities.py` — reject capability resolution for conflicting departmental identity.
- `backend/apps/core/permissions.py` — apply the same identity check to existing core permission classes.
- `backend/apps/accounts/serializers.py` — validate account creation and synchronize profile department on role update.
- `backend/apps/accounts/views_admin.py` — validate lifecycle transfers and prevent permission-matrix writes against retired aliases.
- `backend/apps/accounts/admin.py` — synchronize a profile when the Django admin changes a user's role.
- `backend/apps/staff_operations/serializers.py` — validate profile creation and department edits.
- `backend/apps/staff_operations/views.py` — secure the secondary profile create/patch paths and lifecycle role assignment.
- `backend/apps/staff_operations/admin.py` — validate department/role combinations in Django admin.
- `backend/tests/test_department_roles.py` — canonical mapping, narrow Security/Procurement grants, spoofing, profile conflict, admin, and writer-path tests.
- `frontend/dashboard/staff.html`, `frontend/dashboard/staff-profile.html`, `frontend/tests-js/test-workforce-ui.js` — canonical assignment UI, combined department labels, and Kitchen Assistant designation coverage.
- `docs/ROLE_PERMISSION_MATRIX.md`, `docs/DEPARTMENT_ROLES_AND_POS_MENU_REPORT.md` — current policy mapping, retirement distinction, and scope clarification. The older generated matrix rows remain a clearly labeled 2026-10-22 compatibility snapshot, not an account-creation catalog.
- This audit report.

## Verification evidence

- `manage.py check`: **passed**, no issues.
- `manage.py makemigrations --check --dry-run`: **passed**, no changes detected.
- `tests.test_department_roles`: **27 passed** on the final focused run.
- A role/security batch covering `test_department_roles`, `test_role_permissions_matrix`, `test_permission_alignment`, `test_role_alias_guards`, and `test_dashboard_role_access`: **66 passed**.
- Account/staff/procurement/capability batch (`test_staff`, `test_staff_operations_api`, `test_procurement_receipts_and_editing`, and related role suites): **117 passed** in its run.
- Payment/POS/inventory/housekeeping/maintenance/guest-services/finance/payroll regression batch: **38 passed**.
- Frontend full suite: **590 passed, 3 failed of 593**. The three failures are `test-cloudflare-northflank-deployment.js` (asset version mismatch), `test-deploy-config.js` (obsolete worker artifact), and `test-pwa-dashboard-only.js` (root manifest). Re-running those three test files from a clean archive of the original commit reproduced **the same 3 failures** (11 passed, 3 failed); they are unrelated to this change and their files were not modified.
- Targeted frontend department/workforce/landing tests: **30 passed**. `frontend/validate.py`: **72 HTML pages and 31 standalone JS files checked; ALL OK**.
- Python compile checks and `git diff --check`: **passed**.

The full backend suite was not run. No production database, migrations, or deployment settings were changed.

## Security scope and remaining limitations

The tests verify representative assignment and authorization boundaries, including Security, Admin Management / Procurement, profile conflict denial, and selected staff, procurement, finance, POS, inventory, housekeeping, maintenance, guest-service, and payroll workflows. They do **not** prove object-level isolation for every list/detail/action/export endpoint or every background task. This update adds no universal query filter and does not claim complete cross-department security certification. The existing capability/object-ownership checks remain the endpoint-level authority; any endpoint outside those common permission/capability paths still merits separate review. The generated permission JSON/matrix is an older snapshot and was not regenerated because its generator contains an outdated hard-coded department map; the current canonical table is stated explicitly in the updated documentation.

Changes are present in the local workspace clone; they were not committed or pushed to GitHub.
