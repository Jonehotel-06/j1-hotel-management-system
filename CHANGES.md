# Batch 07 — CHANGES

## Task 1 — Remove Staff Profile from sidebar

* `frontend/js/dashboard.js`
  * Removed the `{ label: "Staff Profiles", href: "staff-profile.html", … }`
    entry from the `NAV` "Administration" group (with an explanatory comment).
  * `PAGE_ACCESS["staff-profile.html"]` unchanged:
    `["admin", "general_manager", "procurement_officer"]` — the page, its route
    and its server authorization are preserved; it stays reachable from the
    staff directory ("10-Tab Profile", disciplinary "Manage") and direct links.

## Task 2 — Staffs in the General Manager sidebar

* `frontend/js/dashboard.js`
  * Renamed the admin directory entry `"Staff"` → `"Staffs"` (same
    `staff.html` target — no duplicate page created).
  * `PAGE_ACCESS["staff.html"]`: `[admin]` → `["admin", "general_manager"]`.
    The entry keeps its capability narrowing (`staff.profile.manage`,
    `staff.view`), which both roles hold.
* `frontend/dashboard/staff.html`
  * Boot gate: `boot("admin")` → `boot(["admin", "general_manager"])` with the
    standard `__staffAllowed` guard (matches the registry exactly, per the
    drift test).
  * Non-administrators get a read-only console: the "New staff member" button,
    row Edit/Activate/Deactivate buttons, the owner-account shortcut and the
    Permission Matrix tab are hidden; roster, profile cards, departments and
    disciplinary lists load normally. Every write is still re-checked
    server-side.
* `backend/apps/core/permissions.py`
  * New `IsAdminOrManagerRead`: ADMIN = all methods; MANAGER / GENERAL_MANAGER
    holding `account.view` = safe methods only; every other role refused.
    MANAGER is included because the frontend normalises a Manager account to
    the General Manager for every page gate — excluding it would promise a
    screen the API refuses.
* `backend/apps/accounts/views_admin.py`
  * `AdminUserQuerysetMixin.permission_classes`: `[IsAdminRole]` →
    `[IsAdminOrManagerRead]` (affects only the list/create + detail views).
    Account creation, role changes and activation stay administrator-only.
  * Module docstring updated to describe the read/write split.

## Task 3 — Manager approval permissions (five categories)

* `backend/apps/accounts/migrations/0032_manager_approval_permissions.py` (new)
  * Idempotently ensures `RoleCapability` grants for role `MANAGER` ×
    `leave.approve`, `inventory.adjust.approve`, `procurement.approve`,
    `payroll.approve`, `expense.approve`. Writes an audit marker; the reverse
    removes only grants this run created.
  * Grants nothing else, revokes nothing, touches no other role.
* No endpoint gate needed widening: all five decision endpoints were already
  capability-gated (`HasCapability`) and the Manager holds the five
  capabilities, so `MANAGER` accounts pass the backend checks. Verified by
  direct API tests for each category (approve + reject paths).
* No unrestricted administration was granted: the Manager holds neither
  `account.manage`, `settings.manage` nor `audit.view`; admin-only queues and
  the permission matrix stay closed.

## Task 4 — Approval audit trail

* `backend/apps/core/approval_audit.py` (new)
  * `ApprovalCategory` codes (`leave`, `stock_count`, `purchase_order`,
    `payroll`, `expense`), `actor_ref()` and `approval_decision_metadata()`,
    which build the uniform evidence block: `approval_category`, `reference`,
    `initiated_by {id,email}`, `decided_by {id,email}`, `previous_status`,
    `new_status`, `decided_at`, `note`.
* Decision views merge this block into the `log_action` metadata they already
  write (existing keys preserved; initiators read from already-selected
  relations — no new queries):
  * `backend/apps/staff_operations/views.py` — `LeaveReviewView`
    (`LEAVE_REVIEWED`)
  * `backend/apps/inventory/views.py` — `StockCountApproveView`
    (`STOCK_COUNT_APPROVED_AND_POSTED`), `PurchaseOrderApproveView`
    (`PURCHASE_ORDER_APPROVED`), `PurchaseOrderRejectView`
    (`PURCHASE_ORDER_REJECTED`)
  * `backend/apps/staff_operations/payroll_views.py` — `PayrollReviewView`
    (`PAYROLL_RUN_APPROVED` / `PAYROLL_RUN_REJECTED`)
  * `backend/apps/finance/views.py` — `ExpenseReviewView` (`EXPENSE_REVIEWED`)
* No workflow logic, status machine, validation, or history model was changed.
  Duplicate-approval idempotency, maker-checker separation and rejection
  workflows are preserved and covered by tests.

## Task 5 — Category-specific rules (verified, not redesigned)

* Leave: Manager approves/rejects via `leave.approve`; request data and shift
  validation preserved (test asserts type/dates untouched).
* Stock variances: approval posts the `ADJUSTMENT` movements; submission alone
  never adjusts (test asserts balance unchanged pre-approval).
* Purchase orders: Manager approves submitted orders; supplier lines and
  receive workflow untouched.
* Payroll: approval accrues to the ledger without changing totals (test asserts
  gross/deductions/net identical pre/post approval).
* Expenses: approval sets `APPROVED` only; payment/posting stays a separate
  step (test asserts no financial transaction on approval).

## Task 6 — Permission matrix

* `PERMISSION_MATRIX.md` (this package) documents the actual resulting matrix.

## Tests changed / added

* `backend/tests/test_batch07_staff_approvals.py` (new, 15 tests) — Manager
  approval of all five categories via API, uniform audit evidence, inbox
  queues, maker-checker refusal, duplicate-approval idempotency, rejections,
  GM/manager-tier staff-directory read-only access, admin privileges intact,
  department-staff 403s.
* `backend/tests/test_permission_alignment.py` — GM boundary updated:
  `GET /api/admin/users/` now 200; audit logs and account writes still 403.
* `backend/tests/test_staff.py` — manager-tier boundary updated: roster read
  200; account writes still 403.
* `frontend/tests-js/test-department-page-access.js` — GM expectations updated
  (`staff.html` allowed); staff-profile cross-link test updated (page routable,
  no longer a sidebar entry); new `batch 07` test (single "Staffs" entry,
  registries, GM visibility, other departments refused).
