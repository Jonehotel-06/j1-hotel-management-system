# Batch 07 — Permission Matrix (actual, resulting)

Source repository: `https://github.com/Jonehotel-06/j1-hotel-management-system`
Batch: 07 — Staff management, sidebar navigation & approval permissions.

This matrix documents the **actual resulting** authorization state after Batch 07.
Authority rule of the codebase (unchanged): the persisted `RoleCapability` rows
are the authorization source; `ROLE_CAPABILITY_CODES` in
`backend/apps/accounts/capabilities.py` is the compatibility matrix they were
seeded from; the frontend never authorizes anything.

> "Manager" in this batch is the existing `MANAGER` role code
> (`User.Role.MANAGER`, "Manager"). No new role was created and the 11-department
> structure is untouched. The frontend normalises a `MANAGER` account to
> `general_manager` at the session boundary (`frontend/js/auth.js`
> `RETIRED_ROLE_ALIASES`), so a Manager sees the General Manager screens while
> every decision is still authorized server-side against the Manager's own
> persisted capability grants.

## 1. The five Batch 07 approval capabilities

| Capability | Approves | ADMIN | GENERAL_MANAGER | MANAGER | All other staff roles |
|---|---|---|---|---|---|
| `leave.approve` | Leave requests | ✅ all | ✅ | ✅ (ensured by migration `0032`) | ❌ none hold it |
| `inventory.adjust.approve` | Stock-count variances | ✅ all | ✅ | ✅ (ensured by migration `0032`) | ❌ none hold it |
| `procurement.approve` | Purchase orders | ✅ all | ✅ | ✅ (ensured by migration `0032`) | ❌ none hold it |
| `payroll.approve` | Payroll runs | ✅ all | ✅ | ✅ (ensured by migration `0032`) | ❌ none hold it |
| `expense.approve` | Expenses | ✅ all | ✅ | ✅ (ensured by migration `0032`) | ❌ none hold it |

Enforcement points (all server-side, unchanged in logic, Batch 07 only added
audit evidence): `LeaveReviewView`, `StockCountApproveView`,
`PurchaseOrderApproveView` / `PurchaseOrderRejectView`, `PayrollReviewView`,
`ExpenseReviewView`, plus the matching service-layer `has_capability` checks and
the central maker-checker policy (`apps/core/approvals.py`).
Unauthorized API requests receive `403` with the existing permission messages.

## 2. Staff management surface

| Capability / endpoint | ADMIN | GENERAL_MANAGER | MANAGER | PROCUREMENT_OFFICER | Other staff |
|---|---|---|---|---|---|
| `account.view` (view staff accounts) | ✅ | ✅ | ✅ | ❌ | ❌ |
| `account.manage` (create/change roles) | ✅ (full catalogue) | ❌ | ❌ | ❌ | ❌ |
| `staff.view` / `staff.profile.manage` / `staff.manage` | ✅ | ✅ | ✅ | ✅ | ❌ |
| `GET /api/admin/users/` (roster) | ✅ | ✅ read-only (Batch 07) | ✅ read-only (Batch 07) | ❌ 403 | ❌ 403 |
| `GET /api/admin/users/<id>/` (account) | ✅ | ✅ read-only (Batch 07) | ✅ read-only (Batch 07) | ❌ 403 | ❌ 403 |
| `POST /api/admin/users/` (create) | ✅ | ❌ 403 | ❌ 403 | ❌ 403 | ❌ 403 |
| `PATCH /api/admin/users/<id>/` (edit/role/active) | ✅ | ❌ 403 | ❌ 403 | ❌ 403 | ❌ 403 |
| `GET /api/admin/users/staff/<id>/` (profile card) | ✅ | ✅ (`staff.view`, pre-existing) | ✅ (`staff.view`, pre-existing) | ✅ own+`staff.view` | own card only |
| Permission matrix API | ✅ | ❌ 403 (unchanged) | ❌ 403 (unchanged) | ❌ 403 | ❌ 403 |
| Audit logs API | ✅ | ❌ 403 (unchanged) | ❌ 403 (unchanged) | ❌ 403 | ❌ 403 |

Read access for the manager tier is granted by the new
`IsAdminOrManagerRead` permission (`backend/apps/core/permissions.py`): ADMIN =
all methods; MANAGER / GENERAL_MANAGER holding `account.view` = safe methods
only; everyone else refused. Writes remain administrator-only.

## 3. Sidebar & page visibility (UX only; backend decisive)

| Page / entry | ADMIN | GENERAL_MANAGER | MANAGER | PROCUREMENT_OFFICER | Other staff |
|---|---|---|---|---|---|
| Sidebar "Staffs" → `staff.html` | ✅ | ✅ (Batch 07) | ✅ via GM normalization | ❌ | ❌ |
| Sidebar "Staff Profiles" entry | — removed for every role (Batch 07); page still routable | | | | |
| `staff.html` page gate | ✅ | ✅ (Batch 07) | ✅ via GM normalization | ❌ → 403 page | ❌ → 403 page |
| `staff-profile.html` page gate (unchanged) | ✅ | ✅ | ✅ via GM normalization | ✅ | ❌ → 403 page |
| `approvals.html` inbox (capability-filtered) | ✅ all queues | ✅ decidable queues | ✅ 5 queues | ✅ expense queue only if holding `expense.approve` (per-user grant) | only queues the account can decide |

`staff.html` hides account write controls (New staff, Edit, Activate/Deactivate,
owner-account shortcut, Permission Matrix tab) from non-administrators; the
server re-checks every write regardless.

## 4. Full resulting capability sets (staff-relevant excerpt)

`Y` = granted in `ROLE_CAPABILITY_CODES` and persisted seed. ADMIN holds the
entire catalogue by construction.

| Capability | ADMIN | GM | MGR | ACCT | PROC | STORE | RECP | WAIT | CHEF | K.ASS | HOUSE | PORT | SEC |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `leave.approve` | ALL | Y | Y | – | – | – | – | – | – | – | – | – | – |
| `inventory.adjust.approve` | ALL | Y | Y | – | – | – | – | – | – | – | – | – | – |
| `procurement.approve` | ALL | Y | Y | – | – | – | – | – | – | – | – | – | – |
| `payroll.approve` | ALL | Y | Y | – | – | – | – | – | – | – | – | – | – |
| `expense.approve` | ALL | Y | Y | – | – | – | – | – | – | – | – | – | – |
| `account.view` | ALL | Y | Y | – | – | – | – | – | – | – | – | – | – |
| `account.manage` | ALL | – | – | – | – | – | – | – | – | – | – | – | – |
| `staff.view` / `staff.profile.manage` / `staff.manage` | ALL | Y | Y | – | Y | – | – | – | – | – | – | – | – |
| `leave.request` | ALL | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | – |
| `payroll.view` / `payroll.manage` | ALL | Y | Y | Y | –¹ | – | – | – | – | – | – | – | – |
| `expense.manage` | ALL | Y | Y | Y | – | – | – | Y | – | – | – | – | – |
| `procurement.manage` / `inventory.manage` | ALL | Y | Y | – | Y | Y | – | – | – | – | – | – | – |
| `attendance.manage` / `shift.manage` | ALL | Y | Y | – | Y | – | – | – | – | – | – | – | – |
| `payment.refund.approve` | ALL | Y | Y | – | – | – | – | – | – | – | – | – | – |
| `reports.financial.view` | ALL | Y | Y | Y | – | – | – | – | – | – | – | – | – |
| `audit.view` | ALL | – | – | – | – | – | – | – | – | – | – | – | – |
| `terminal.manage` | ALL | Y | Y | – | – | – | – | – | – | – | – | – | – |
| `settings.manage` | ALL | – | – | – | – | – | – | – | – | – | – | – | – |

¹ Procurement is hard-blocked from every `payroll.*` capability at resolution
time (`capability_codes_for_user`), even via overrides — pre-existing control,
unchanged by Batch 07.

Roles: GM = General Manager, MGR = Manager, ACCT = Accountant,
PROC = Admin Manager/Procurement, STORE = Store Keeper, RECP = Receptionist,
WAIT = Waiters/Waitresses, CHEF = Chefs, K.ASS = Kitchen Assistant,
HOUSE = Housekeepers, PORT = Porters, SEC = Security.

## 5. What Batch 07 deliberately did NOT change

* No capability was granted to any department staff role (waiter, chef,
  receptionist, housekeeping, storekeeper, porter, security, accountant,
  procurement officer) — verified by test.
* No capability was revoked from Administrator, General Manager or Manager.
* No new role, department, or hierarchy level was created.
* The administrator-only override policy (`ALLOW_ADMIN_APPROVAL_OVERRIDE`),
  maker-checker separation, financial/payroll validations, and all status
  machines are byte-for-byte untouched.
