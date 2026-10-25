# J-ONE Hotel & Lodge — Role & Permission Matrix

**Snapshot generated:** 2026-10-22  ·  **Persisted role codes:** 28  ·  **API endpoints:** 321  ·  **Capabilities:** 56  ·  **Canonical operational departments:** 14

This document is the human-readable half of `docs/role_permission_matrix.json`.
Both are derived, never hand-maintained: endpoint rows come from a live walk of
the Django URL resolver, grants from `apps.accounts.capabilities`, page
visibility from `frontend/js/dashboard.js` `PAGE_ACCESS`, and landing pages
from `frontend/js/auth.js` `defaultStaffLanding()`.

> **Current source-of-truth note (2026-10-25):** the role inventory below lists
> persisted role codes for compatibility and audit, including retired titles.
> Only the 14 canonical departmental roles in Section 2 are assignable as new
> standard staff roles. The `DEPARTMENT_ROLES` registry in
> `backend/apps/accounts/departments.py` is authoritative; `RoleCapability`
> rows for retired aliases are historical compatibility data, not assignable
> identities. The JSON/report snapshot may contain older department labels and
> should not be used as an account-creation catalog.

Regenerate:

```bash
# 1. capability + endpoint rows (from backend/)
DJANGO_SETTINGS_MODULE=config.settings.development \
  PYTHONPATH=. python ../docs/tools/build_role_permission_matrix.py
# 2. menu each role actually renders (from repository root)
node docs/tools/export_page_access.js
# 3. repeat 1 so the export's visible_nav is merged back in, then reformat
python ../docs/tools/build_role_permission_matrix.py
```

**Authority.** The API is the security boundary. `PAGE_ACCESS` and the sidebar
decide what a person is *shown*; protected endpoints must enforce their
appropriate server-side authentication, role/capability, ownership, and object
scope checks. Not every public or legacy endpoint uses a capability class, and a
page is never proof that its data is readable. This matrix is a 2026-10-22
snapshot; Section 2 is the current department-role registry, while sections on
endpoint counts retain the snapshot's audit scope and limitations.

---

## 1. How to read the matrix

| Column | Meaning |
| --- | --- |
| **Department** | Department association shown by the matrix snapshot. The canonical current mapping is the registry in Section 2; historical aliases and global roles are explained there. |
| **Login destination** | The page `defaultStaffLanding()` sends the role to after sign-in. A department subdomain can override it via `APP_CONFIG.DEPARTMENT_SUBDOMAINS.landingPath`. |
| **View** | Read access, derived from read capabilities (`*.view`, `*.read`). |
| **Create / Edit** | Mutation access, derived from the `*.manage`, `*.capture`, `*.post`, `*.check_in/out` and `*.assign` capabilities. The catalogue has no separate create/edit distinction — a holder of `booking.manage` can do both. |
| **Delete** | Deletion is **not** a capability in this system. Only administrator surfaces delete rows (`IsAdminRole`), and the only operational delete endpoint is review moderation. Everything else is voided, cancelled or reversed so the ledger stays auditable. |
| **Approval** | Capabilities ending `*.approve`. These gate the Approvals queue. |
| **Restrictions** | The boundaries that matter operationally — what the role is deliberately *not* given. |

`—` means "no capability of that kind". An entry naming a domain does not grant
every action inside it: the endpoint still checks the specific capability, the
role, and (where relevant) object ownership.

---

## 2. Authoritative department → standard-role mapping

The registry defines one standard role for each operational department. The
current code is authoritative over the legacy snapshot rows below.

| Department | One standard role | Notes |
| --- | --- | --- |
| Management | `MANAGER` | Department role. `GENERAL_MANAGER` and `ADMIN` remain global oversight identities under existing policy. |
| Accounts | `ACCOUNTANT` | Cashier remains a separate operational department. |
| Admin Management / Procurement | `PROCUREMENT_OFFICER` | One combined department, represented internally by `Workstation.Department.PROCUREMENT`; never separate Admin and Procurement standard roles. |
| Kitchen | `CHEF` | Kitchen Assistant is a staff-profile `job_title`/designation under `CHEF`, not another role. |
| Restaurant | `WAITER` | Waiters and waitresses share the canonical role. |
| Porter | `PORTER` | — |
| Front Desk | `RECEPTIONIST` | — |
| Housekeeping | `HOUSEKEEPING` | — |
| Inventory / Stores | `STOREKEEPER` | — |
| Security | `SECURITY` | Only `guest_request.manage` and `attendance.clock` by default; no guest-booking, finance, payroll, or inventory grants. |
| Cashier | `CASHIER` | Preserved as a distinct operational department. |
| Bar | `BARTENDER` | Preserved as a distinct operational department. |
| Maintenance | `MAINTENANCE` | Preserved as a distinct operational department. |
| Human Resources | `HR_OFFICER` | Existing operational department retained; sensitive approvals remain explicit. |

Retired departmental titles are rejected by account creation, role update,
profile creation/update, and department-transfer APIs. For historical records,
`canonical_role()` resolves aliases without rewriting stored values at runtime:
`FRONT_DESK_SUPERVISOR→RECEPTIONIST`, `BAR_MANAGER→BARTENDER`,
`RESTAURANT_MANAGER→WAITER`, `KITCHEN_MANAGER` / `KITCHEN_ASSISTANT→CHEF`,
`HOUSEKEEPING_MANAGER` / `HOUSEKEEPER→HOUSEKEEPING`,
`MAINTENANCE_TECHNICIAN→MAINTENANCE`, `ACCOUNTS_MANAGER→ACCOUNTANT`,
`HR_MANAGER→HR_OFFICER`, and `INVENTORY_CLERK→STOREKEEPER`.

Role/department identity is read from persisted account and staff-profile data.
Conflicting operational identity fails closed for capability checks and the
existing DRF permission classes. Explicit global oversight roles keep their
existing global policy. This is not a blanket row filter: each protected API
still relies on its established capability, role, object-scope, ownership, and
approval checks; shared workflows are not automatically blocked by department.

## 3. Role inventory (persisted codes, including retired compatibility aliases)

This generated inventory is a compatibility/audit view, **not** the current role
picker. The subsequent matrix rows include historical grants for retired titles
so existing rows and migration history remain reviewable. A retired title is not
an assignable role; Section 2 is the canonical assignment policy.

| Role | Label | Department(s) | Landing | Capabilities | Pages |
| --- | --- | --- | --- | --- | --- |
| ADMIN | Administrator | MANAGEMENT, ADMIN_PROCUREMENT | `/dashboard/index.html` | 56 | 43 |
| MANAGER | Manager | MANAGEMENT, ADMIN_PROCUREMENT | `/dashboard/index.html` | 53 | 38 |
| RECEPTIONIST | Receptionist | FRONT_DESK | `/dashboard/index.html` | 18 | 24 |
| CASHIER | Cashier | ACCOUNTS | `/dashboard/pos.html` | 11 | 6 |
| HOUSEKEEPING | Housekeeping | HOUSEKEEPING | `/dashboard/housekeeping.html` | 4 | 7 |
| MAINTENANCE | Maintenance | PORTERING | `/dashboard/maintenance.html` | 4 | 7 |
| INVENTORY_CLERK | Inventory Clerk | STORES_INVENTORY | `/dashboard/inventory.html` | 4 | 8 |
| FRONT_DESK_SUPERVISOR | Front Desk Supervisor | FRONT_DESK | `/dashboard/index.html` | 19 | 25 |
| GENERAL_MANAGER | General Manager | MANAGEMENT | `/dashboard/index.html` | 53 | 38 |
| RESTAURANT_MANAGER | Restaurant Manager | RESTAURANT_BAR | `/dashboard/pos.html` | 11 | 5 |
| WAITER | Waiter / Server | RESTAURANT_BAR | `/dashboard/pos.html` | 6 | 7 |
| BAR_MANAGER | Bar Manager | RESTAURANT_BAR | `/dashboard/pos.html` | 10 | 5 |
| BARTENDER | Bartender | RESTAURANT_BAR | `/dashboard/pos.html` | 6 | 7 |
| KITCHEN_MANAGER | Kitchen Manager | KITCHEN | `/dashboard/pos.html` | 6 | 5 |
| CHEF | Chef / Kitchen Staff | KITCHEN | `/dashboard/pos.html` | 4 | 5 |
| HOUSEKEEPING_MANAGER | Housekeeping Manager | HOUSEKEEPING | `/dashboard/housekeeping.html` | 8 | 7 |
| HOUSEKEEPER | Housekeeper | HOUSEKEEPING | `/dashboard/housekeeping.html` | 4 | 7 |
| MAINTENANCE_TECHNICIAN | Maintenance Technician | PORTERING | `/dashboard/maintenance.html` | 4 | 7 |
| ACCOUNTS_MANAGER | Accounts Manager | ACCOUNTS | `/dashboard/payments.html` | 17 | 10 |
| ACCOUNTANT | Accountant | ACCOUNTS | `/dashboard/payments.html` | 11 | 10 |
| HR_MANAGER | HR Manager | MANAGEMENT | `/dashboard/workforce.html` | 11 | 7 |
| HR_OFFICER | HR Officer | — | `/dashboard/index.html` | 9 | 0 |
| SECURITY | Security | PORTERING | `/dashboard/index.html` | 2 | 6 |
| PROCUREMENT_OFFICER | Procurement Officer | ADMIN_PROCUREMENT | `/dashboard/procurement.html` | 3 | 6 |
| STOREKEEPER | Storekeeper | STORES_INVENTORY | `/dashboard/inventory.html` | 4 | 8 |
| PORTER | Porter | PORTERING | `/dashboard/service-requests.html` | 3 | 6 |
| KITCHEN_ASSISTANT | Kitchen Assistant | KITCHEN | `/dashboard/pos.html` | 4 | 5 |
| GUEST | Guest | — | `/index.html` | 0 | 0 |

---

## 4. Master matrix (includes historical compatibility rows)

| Department | Role | Login destination | View | Create | Edit | Delete | Approval | Restrictions |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| MANAGEMENT<br>ADMIN_PROCUREMENT | **ADMIN** | `/dashboard/index.html` | Accounts, Audit log, Bookings, Financial reports, Folio, Kitchen queue, Payments, Payroll, Sign-in freedom (any device), Staff records | Bar menu, Bar orders, Bookings, Cash sessions, Check-in, Check-out, Expenses, Finance controls, Folio charges, Guest requests, Hotel settings, Housekeeping assignment, Housekeeping tasks, Inventory, Kitchen tickets, Leave requests, Own attendance, POS item returns, POS menu, POS orders, Payments, Payroll, Procurement, Receipts, Refund requests, Request assignment, Restaurant menu, Restaurant orders, Restaurant tables, Service links, Shifts, Staff accounts, Staff profiles, Staff records, Team attendance, Terminals, Work orders, Work-order assignment | Bar menu, Bar orders, Bookings, Cash sessions, Check-in, Check-out, Expenses, Finance controls, Folio charges, Guest requests, Hotel settings, Housekeeping assignment, Housekeeping tasks, Inventory, Kitchen tickets, Leave requests, Own attendance, POS item returns, POS menu, POS orders, Payments, Payroll, Procurement, Receipts, Refund requests, Request assignment, Restaurant menu, Restaurant orders, Restaurant tables, Service links, Shifts, Staff accounts, Staff profiles, Staff records, Team attendance, Terminals, Work orders, Work-order assignment | Reviews | Expenses, Leave, POS return payouts, Payroll, Procurement, Refunds, Stock adjustments | Every capability; destructive admin endpoints (users, reviews, gallery, audit) are `IsAdminRole`. Operational records are voided/cancelled, not deleted. |
| MANAGEMENT<br>ADMIN_PROCUREMENT | **MANAGER** | `/dashboard/index.html` | Accounts, Bookings, Financial reports, Folio, Kitchen queue, Payments, Payroll, Sign-in freedom (any device), Staff records | Bar menu, Bar orders, Bookings, Cash sessions, Check-in, Check-out, Expenses, Finance controls, Folio charges, Guest requests, Housekeeping assignment, Housekeeping tasks, Inventory, Kitchen tickets, Leave requests, Own attendance, POS item returns, POS menu, POS orders, Payments, Payroll, Procurement, Receipts, Refund requests, Request assignment, Restaurant menu, Restaurant orders, Restaurant tables, Service links, Shifts, Staff profiles, Staff records, Team attendance, Terminals, Work orders, Work-order assignment | Bar menu, Bar orders, Bookings, Cash sessions, Check-in, Check-out, Expenses, Finance controls, Folio charges, Guest requests, Housekeeping assignment, Housekeeping tasks, Inventory, Kitchen tickets, Leave requests, Own attendance, POS item returns, POS menu, POS orders, Payments, Payroll, Procurement, Receipts, Refund requests, Request assignment, Restaurant menu, Restaurant orders, Restaurant tables, Service links, Shifts, Staff profiles, Staff records, Team attendance, Terminals, Work orders, Work-order assignment | — | Expenses, Leave, POS return payouts, Payroll, Procurement, Refunds, Stock adjustments | Legacy configuration write (rooms, room types, amenities, policies, offers, gallery) is role-gated `ADMIN`/`MANAGER`; no audit log, no user administration. |
| FRONT_DESK | **RECEPTIONIST** | `/dashboard/index.html` | Bookings, Folio, Payments | Bookings, Check-in, Check-out, Folio charges, Guest requests, Housekeeping assignment, Housekeeping tasks, Leave requests, Own attendance, Payments, Receipts, Refund requests, Request assignment, Work orders, Work-order assignment | Bookings, Check-in, Check-out, Folio charges, Guest requests, Housekeeping assignment, Housekeeping tasks, Leave requests, Own attendance, Payments, Receipts, Refund requests, Request assignment, Work orders, Work-order assignment | — | — | No finance reports, no payroll, no procurement, no dashboard financial block; staff profile card limited to self. |
| ACCOUNTS | **CASHIER** | `/dashboard/pos.html` | — | Cash sessions, Expenses, Folio charges, Leave requests, Own attendance, POS item returns, POS orders, Payments, Refund requests | Cash sessions, Expenses, Folio charges, Leave requests, Own attendance, POS item returns, POS orders, Payments, Refund requests | — | POS return payouts | Cash and POS settlement only; no folio view, no inventory, no bookings management. |
| HOUSEKEEPING | **HOUSEKEEPING** | `/dashboard/housekeeping.html` | — | Guest requests, Housekeeping tasks, Leave requests, Own attendance | Guest requests, Housekeeping tasks, Leave requests, Own attendance | — | — | Legacy alias of HOUSEKEEPER (same grant set). |
| PORTERING | **MAINTENANCE** | `/dashboard/maintenance.html` | — | Guest requests, Leave requests, Own attendance, Work orders | Guest requests, Leave requests, Own attendance, Work orders | — | — | Legacy alias of MAINTENANCE_TECHNICIAN. |
| STORES_INVENTORY | **INVENTORY_CLERK** | `/dashboard/inventory.html` | — | Inventory, Leave requests, Own attendance, Procurement | Inventory, Leave requests, Own attendance, Procurement | — | — | Inventory/procurement execution; no approvals, no POS, no bookings. |
| FRONT_DESK | **FRONT_DESK_SUPERVISOR** | `/dashboard/index.html` | Bookings, Folio, Payments | Bookings, Check-in, Check-out, Folio charges, Guest requests, Housekeeping assignment, Housekeeping tasks, Leave requests, Own attendance, Payments, Receipts, Refund requests, Request assignment, Team attendance, Work orders, Work-order assignment | Bookings, Check-in, Check-out, Folio charges, Guest requests, Housekeeping assignment, Housekeeping tasks, Leave requests, Own attendance, Payments, Receipts, Refund requests, Request assignment, Team attendance, Work orders, Work-order assignment | — | — | Read-only on legacy configuration (rooms, room types, offers); no refund approval, no payroll, no financial reports. |
| MANAGEMENT | **GENERAL_MANAGER** | `/dashboard/index.html` | Accounts, Bookings, Financial reports, Folio, Kitchen queue, Payments, Payroll, Sign-in freedom (any device), Staff records | Bar menu, Bar orders, Bookings, Cash sessions, Check-in, Check-out, Expenses, Finance controls, Folio charges, Guest requests, Housekeeping assignment, Housekeeping tasks, Inventory, Kitchen tickets, Leave requests, Own attendance, POS item returns, POS menu, POS orders, Payments, Payroll, Procurement, Receipts, Refund requests, Request assignment, Restaurant menu, Restaurant orders, Restaurant tables, Service links, Shifts, Staff profiles, Staff records, Team attendance, Terminals, Work orders, Work-order assignment | Bar menu, Bar orders, Bookings, Cash sessions, Check-in, Check-out, Expenses, Finance controls, Folio charges, Guest requests, Housekeeping assignment, Housekeeping tasks, Inventory, Kitchen tickets, Leave requests, Own attendance, POS item returns, POS menu, POS orders, Payments, Payroll, Procurement, Receipts, Refund requests, Request assignment, Restaurant menu, Restaurant orders, Restaurant tables, Service links, Shifts, Staff profiles, Staff records, Team attendance, Terminals, Work orders, Work-order assignment | — | Expenses, Leave, POS return payouts, Payroll, Procurement, Refunds, Stock adjustments | Manager-equivalent. No audit log, no user administration, no reviews moderation (administrator-only surfaces). |
| RESTAURANT_BAR | **RESTAURANT_MANAGER** | `/dashboard/pos.html` | Kitchen queue | Leave requests, Own attendance, POS item returns, Payments, Restaurant menu, Restaurant orders, Restaurant tables, Shifts, Team attendance | Leave requests, Own attendance, POS item returns, Payments, Restaurant menu, Restaurant orders, Restaurant tables, Shifts, Team attendance | — | POS return payouts | Restaurant surface and team attendance; no bar menu, no hotel finance. |
| RESTAURANT_BAR | **WAITER** | `/dashboard/pos.html` | — | Guest requests, Leave requests, Own attendance, POS item returns, Payments, Restaurant orders | Guest requests, Leave requests, Own attendance, POS item returns, Payments, Restaurant orders | — | — | Order capture and own attendance; no menu editing, no team scope. |
| RESTAURANT_BAR | **BAR_MANAGER** | `/dashboard/pos.html` | Kitchen queue | Bar menu, Bar orders, Leave requests, Own attendance, POS item returns, Payments, Shifts, Team attendance | Bar menu, Bar orders, Leave requests, Own attendance, POS item returns, Payments, Shifts, Team attendance | — | POS return payouts | Bar surface; no restaurant menu, no hotel reports. |
| RESTAURANT_BAR | **BARTENDER** | `/dashboard/pos.html` | — | Bar orders, Guest requests, Leave requests, Own attendance, POS item returns, Payments | Bar orders, Guest requests, Leave requests, Own attendance, POS item returns, Payments | — | — | Bar orders and own attendance; no menu editing. |
| KITCHEN | **KITCHEN_MANAGER** | `/dashboard/pos.html` | Kitchen queue | Kitchen tickets, Leave requests, Own attendance, Shifts, Team attendance | Kitchen tickets, Leave requests, Own attendance, Shifts, Team attendance | — | — | Kitchen queue and team attendance; no POS settlement, no inventory. |
| KITCHEN | **CHEF** | `/dashboard/pos.html` | Kitchen queue | Kitchen tickets, Leave requests, Own attendance | Kitchen tickets, Leave requests, Own attendance | — | — | Kitchen tickets and own attendance; no team scope. |
| HOUSEKEEPING | **HOUSEKEEPING_MANAGER** | `/dashboard/housekeeping.html` | — | Guest requests, Housekeeping assignment, Housekeeping tasks, Leave requests, Own attendance, Request assignment, Shifts, Team attendance | Guest requests, Housekeeping assignment, Housekeeping tasks, Leave requests, Own attendance, Request assignment, Shifts, Team attendance | — | — | Housekeeping team and attendance; no maintenance, no front desk operations. |
| HOUSEKEEPING | **HOUSEKEEPER** | `/dashboard/housekeeping.html` | — | Guest requests, Housekeeping tasks, Leave requests, Own attendance | Guest requests, Housekeeping tasks, Leave requests, Own attendance | — | — | Own attendance and assigned housekeeping tasks only; no team scope. |
| PORTERING | **MAINTENANCE_TECHNICIAN** | `/dashboard/maintenance.html` | — | Guest requests, Leave requests, Own attendance, Work orders | Guest requests, Leave requests, Own attendance, Work orders | — | — | Own attendance and assigned work orders only. |
| ACCOUNTS | **ACCOUNTS_MANAGER** | `/dashboard/payments.html` | Accounts, Financial reports, Folio, Payments, Payroll | Cash sessions, Expenses, Finance controls, Folio charges, Own attendance, Payments, Payroll, Refund requests | Cash sessions, Expenses, Finance controls, Folio charges, Own attendance, Payments, Payroll, Refund requests | — | Expenses, Payroll, Refunds | No booking/POS/housekeeping operations; no staff records; attendance limited to own clocking after migration 0024. |
| ACCOUNTS | **ACCOUNTANT** | `/dashboard/payments.html` | Financial reports, Folio, Payments, Payroll | Expenses, Folio charges, Leave requests, Own attendance, Payments, Payroll, Refund requests | Expenses, Folio charges, Leave requests, Own attendance, Payments, Payroll, Refund requests | — | — | Finance execution and payroll preparation; no approvals except none listed; no bookings, no staff records. |
| MANAGEMENT | **HR_MANAGER** | `/dashboard/workforce.html` | Payroll, Staff records | Leave requests, Own attendance, Payroll, Shifts, Staff profiles, Staff records, Team attendance | Leave requests, Own attendance, Payroll, Shifts, Staff profiles, Staff records, Team attendance | — | Leave, Payroll | Staff records, attendance, leave approval, payroll; no bookings, no finance reports, no POS, no inventory. |
| — | **HR_OFFICER** | `/dashboard/index.html` | Payroll, Staff records | Leave requests, Own attendance, Payroll, Shifts, Staff profiles, Staff records, Team attendance | Leave requests, Own attendance, Payroll, Shifts, Staff profiles, Staff records, Team attendance | — | — | — |
| PORTERING | **SECURITY** | `/dashboard/index.html` | — | Guest requests, Own attendance | Guest requests, Own attendance | — | — | Guest requests, own attendance, audit log read; no operational editing. |
| ADMIN_PROCUREMENT | **PROCUREMENT_OFFICER** | `/dashboard/procurement.html` | — | Leave requests, Own attendance, Procurement | Leave requests, Own attendance, Procurement | — | — | Procurement approval; no inventory adjustment approval, no finance reports. |
| STORES_INVENTORY | **STOREKEEPER** | `/dashboard/inventory.html` | — | Inventory, Leave requests, Own attendance, Procurement | Inventory, Leave requests, Own attendance, Procurement | — | — | Inventory and procurement execution; approval of stock adjustments is not granted. |
| PORTERING | **PORTER** | `/dashboard/service-requests.html` | — | Guest requests, Leave requests, Own attendance | Guest requests, Leave requests, Own attendance | — | — | Guest requests and own attendance; no housekeeping assignment. |
| KITCHEN | **KITCHEN_ASSISTANT** | `/dashboard/pos.html` | Kitchen queue | Kitchen tickets, Leave requests, Own attendance | Kitchen tickets, Leave requests, Own attendance | — | — | Own attendance only; no order or ticket management. |
| — | **GUEST** | `/index.html` | — | — | — | — | — | No staff API. Portal session only; every `/api/admin/*` route answers 403. |

---

## 5. Department detail

Each role below lists the menu its own account renders after the capability
read settles (`navItemAllowed()`), not the raw `NAV` table. Page names in the
sidebar are a promise the API must keep; section 7 records where it did not.

### Management (`MANAGEMENT`)

Landing page for the department: `/dashboard/index.html`

**ADMIN** — lands on `/dashboard/index.html`; sidebar: Overview (2) · Operations (18) · Finance (5) · Content (3) · Communication (3) · Administration (5).

Capabilities (56): `account.manage`, `account.view`, `attendance.clock`, `attendance.manage`, `audit.view`, `bar.menu.manage`, `bar.order.manage`, `booking.manage`, `booking.read`, `cash_session.close`, `cash_session.open`, `expense.approve`, `expense.manage`, `finance.controls.manage`, `folio.charge.post`, `folio.view`, `guest_request.assign`, `guest_request.manage`, `housekeeping.task.assign`, `housekeeping.task.manage`, `inventory.adjust.approve`, `inventory.manage`, `kitchen.queue.view`, `kitchen.ticket.manage`, `leave.approve`, `leave.request`, `maintenance.work_order.assign`, `maintenance.work_order.manage`, `payment.capture`, `payment.read`, `payment.receipt.send`, `payment.refund.approve`, `payment.refund.request`, `payroll.approve`, `payroll.manage`, `payroll.view`, `pos.menu.manage`, `pos.order.manage`, `pos.return.record`, `pos.return.refund`, `procurement.approve`, `procurement.manage`, `reports.financial.view`, `restaurant.menu.manage`, `restaurant.order.manage`, `restaurant.table.manage`, `service_qr.manage`, `settings.manage`, `shift.manage`, `sign_in.any_device`, `staff.manage`, `staff.profile.manage`, `staff.view`, `stay.check_in`, `stay.check_out`, `terminal.manage`

**MANAGER** — lands on `/dashboard/index.html`; sidebar: Overview (2) · Operations (18) · Finance (5) · Content (3) · Communication (2) · Administration (2).

Capabilities (53): `account.view`, `attendance.clock`, `attendance.manage`, `bar.menu.manage`, `bar.order.manage`, `booking.manage`, `booking.read`, `cash_session.close`, `cash_session.open`, `expense.approve`, `expense.manage`, `finance.controls.manage`, `folio.charge.post`, `folio.view`, `guest_request.assign`, `guest_request.manage`, `housekeeping.task.assign`, `housekeeping.task.manage`, `inventory.adjust.approve`, `inventory.manage`, `kitchen.queue.view`, `kitchen.ticket.manage`, `leave.approve`, `leave.request`, `maintenance.work_order.assign`, `maintenance.work_order.manage`, `payment.capture`, `payment.read`, `payment.receipt.send`, `payment.refund.approve`, `payment.refund.request`, `payroll.approve`, `payroll.manage`, `payroll.view`, `pos.menu.manage`, `pos.order.manage`, `pos.return.record`, `pos.return.refund`, `procurement.approve`, `procurement.manage`, `reports.financial.view`, `restaurant.menu.manage`, `restaurant.order.manage`, `restaurant.table.manage`, `service_qr.manage`, `shift.manage`, `sign_in.any_device`, `staff.manage`, `staff.profile.manage`, `staff.view`, `stay.check_in`, `stay.check_out`, `terminal.manage`

**GENERAL_MANAGER** — lands on `/dashboard/index.html`; sidebar: Overview (2) · Operations (18) · Finance (5) · Content (3) · Communication (2) · Administration (2).

Capabilities (53): `account.view`, `attendance.clock`, `attendance.manage`, `bar.menu.manage`, `bar.order.manage`, `booking.manage`, `booking.read`, `cash_session.close`, `cash_session.open`, `expense.approve`, `expense.manage`, `finance.controls.manage`, `folio.charge.post`, `folio.view`, `guest_request.assign`, `guest_request.manage`, `housekeeping.task.assign`, `housekeeping.task.manage`, `inventory.adjust.approve`, `inventory.manage`, `kitchen.queue.view`, `kitchen.ticket.manage`, `leave.approve`, `leave.request`, `maintenance.work_order.assign`, `maintenance.work_order.manage`, `payment.capture`, `payment.read`, `payment.receipt.send`, `payment.refund.approve`, `payment.refund.request`, `payroll.approve`, `payroll.manage`, `payroll.view`, `pos.menu.manage`, `pos.order.manage`, `pos.return.record`, `pos.return.refund`, `procurement.approve`, `procurement.manage`, `reports.financial.view`, `restaurant.menu.manage`, `restaurant.order.manage`, `restaurant.table.manage`, `service_qr.manage`, `shift.manage`, `sign_in.any_device`, `staff.manage`, `staff.profile.manage`, `staff.view`, `stay.check_in`, `stay.check_out`, `terminal.manage`

**HR_MANAGER** — lands on `/dashboard/workforce.html`; sidebar: Overview (1) · Operations (1) · Finance (1) · Communication (1) · Administration (1).

Capabilities (11): `attendance.clock`, `attendance.manage`, `leave.approve`, `leave.request`, `payroll.approve`, `payroll.manage`, `payroll.view`, `shift.manage`, `staff.manage`, `staff.profile.manage`, `staff.view`

### Accounts & Finance (`ACCOUNTS`)

Landing page for the department: `/dashboard/accounting.html`

**CASHIER** — lands on `/dashboard/pos.html`; sidebar: Operations (3) · Communication (1).

Capabilities (11): `attendance.clock`, `cash_session.close`, `cash_session.open`, `expense.manage`, `folio.charge.post`, `leave.request`, `payment.capture`, `payment.refund.request`, `pos.order.manage`, `pos.return.record`, `pos.return.refund`

**ACCOUNTS_MANAGER** — lands on `/dashboard/payments.html`; sidebar: Overview (1) · Operations (1) · Finance (4) · Communication (1).

Capabilities (17): `account.view`, `attendance.clock`, `cash_session.close`, `cash_session.open`, `expense.approve`, `expense.manage`, `finance.controls.manage`, `folio.charge.post`, `folio.view`, `payment.capture`, `payment.read`, `payment.refund.approve`, `payment.refund.request`, `payroll.approve`, `payroll.manage`, `payroll.view`, `reports.financial.view`

**ACCOUNTANT** — lands on `/dashboard/payments.html`; sidebar: Operations (1) · Finance (4) · Communication (1).

Capabilities (11): `attendance.clock`, `expense.manage`, `folio.charge.post`, `folio.view`, `leave.request`, `payment.capture`, `payment.read`, `payment.refund.request`, `payroll.manage`, `payroll.view`, `reports.financial.view`

### Administration & Procurement (`ADMIN_PROCUREMENT`)

Landing page for the department: `/dashboard/procurement.html`

**ADMIN** — lands on `/dashboard/index.html`; sidebar: Overview (2) · Operations (18) · Finance (5) · Content (3) · Communication (3) · Administration (5).

Capabilities (56): `account.manage`, `account.view`, `attendance.clock`, `attendance.manage`, `audit.view`, `bar.menu.manage`, `bar.order.manage`, `booking.manage`, `booking.read`, `cash_session.close`, `cash_session.open`, `expense.approve`, `expense.manage`, `finance.controls.manage`, `folio.charge.post`, `folio.view`, `guest_request.assign`, `guest_request.manage`, `housekeeping.task.assign`, `housekeeping.task.manage`, `inventory.adjust.approve`, `inventory.manage`, `kitchen.queue.view`, `kitchen.ticket.manage`, `leave.approve`, `leave.request`, `maintenance.work_order.assign`, `maintenance.work_order.manage`, `payment.capture`, `payment.read`, `payment.receipt.send`, `payment.refund.approve`, `payment.refund.request`, `payroll.approve`, `payroll.manage`, `payroll.view`, `pos.menu.manage`, `pos.order.manage`, `pos.return.record`, `pos.return.refund`, `procurement.approve`, `procurement.manage`, `reports.financial.view`, `restaurant.menu.manage`, `restaurant.order.manage`, `restaurant.table.manage`, `service_qr.manage`, `settings.manage`, `shift.manage`, `sign_in.any_device`, `staff.manage`, `staff.profile.manage`, `staff.view`, `stay.check_in`, `stay.check_out`, `terminal.manage`

**MANAGER** — lands on `/dashboard/index.html`; sidebar: Overview (2) · Operations (18) · Finance (5) · Content (3) · Communication (2) · Administration (2).

Capabilities (53): `account.view`, `attendance.clock`, `attendance.manage`, `bar.menu.manage`, `bar.order.manage`, `booking.manage`, `booking.read`, `cash_session.close`, `cash_session.open`, `expense.approve`, `expense.manage`, `finance.controls.manage`, `folio.charge.post`, `folio.view`, `guest_request.assign`, `guest_request.manage`, `housekeeping.task.assign`, `housekeeping.task.manage`, `inventory.adjust.approve`, `inventory.manage`, `kitchen.queue.view`, `kitchen.ticket.manage`, `leave.approve`, `leave.request`, `maintenance.work_order.assign`, `maintenance.work_order.manage`, `payment.capture`, `payment.read`, `payment.receipt.send`, `payment.refund.approve`, `payment.refund.request`, `payroll.approve`, `payroll.manage`, `payroll.view`, `pos.menu.manage`, `pos.order.manage`, `pos.return.record`, `pos.return.refund`, `procurement.approve`, `procurement.manage`, `reports.financial.view`, `restaurant.menu.manage`, `restaurant.order.manage`, `restaurant.table.manage`, `service_qr.manage`, `shift.manage`, `sign_in.any_device`, `staff.manage`, `staff.profile.manage`, `staff.view`, `stay.check_in`, `stay.check_out`, `terminal.manage`

**PROCUREMENT_OFFICER** — lands on `/dashboard/procurement.html`; sidebar: Operations (2) · Communication (1).

Capabilities (3): `attendance.clock`, `leave.request`, `procurement.manage`

### Front Desk / Reception (`FRONT_DESK`)

Landing page for the department: `/dashboard/bookings.html`

**RECEPTIONIST** — lands on `/dashboard/index.html`; sidebar: Overview (1) · Operations (13) · Finance (2) · Communication (2).

Capabilities (18): `attendance.clock`, `booking.manage`, `booking.read`, `folio.charge.post`, `folio.view`, `guest_request.assign`, `guest_request.manage`, `housekeeping.task.assign`, `housekeeping.task.manage`, `leave.request`, `maintenance.work_order.assign`, `maintenance.work_order.manage`, `payment.capture`, `payment.read`, `payment.receipt.send`, `payment.refund.request`, `stay.check_in`, `stay.check_out`

**FRONT_DESK_SUPERVISOR** — lands on `/dashboard/index.html`; sidebar: Overview (1) · Operations (13) · Finance (2) · Communication (2).

Capabilities (19): `attendance.clock`, `attendance.manage`, `booking.manage`, `booking.read`, `folio.charge.post`, `folio.view`, `guest_request.assign`, `guest_request.manage`, `housekeeping.task.assign`, `housekeeping.task.manage`, `leave.request`, `maintenance.work_order.assign`, `maintenance.work_order.manage`, `payment.capture`, `payment.read`, `payment.receipt.send`, `payment.refund.request`, `stay.check_in`, `stay.check_out`

### Kitchen (`KITCHEN`)

Landing page for the department: `/dashboard/pos.html`

**KITCHEN_MANAGER** — lands on `/dashboard/pos.html`; sidebar: Operations (2) · Communication (1).

Capabilities (6): `attendance.clock`, `attendance.manage`, `kitchen.queue.view`, `kitchen.ticket.manage`, `leave.request`, `shift.manage`

**CHEF** — lands on `/dashboard/pos.html`; sidebar: Operations (2) · Communication (1).

Capabilities (4): `attendance.clock`, `kitchen.queue.view`, `kitchen.ticket.manage`, `leave.request`

**KITCHEN_ASSISTANT** — lands on `/dashboard/pos.html`; sidebar: Operations (2) · Communication (1).

Capabilities (4): `attendance.clock`, `kitchen.queue.view`, `kitchen.ticket.manage`, `leave.request`

### Restaurant & Bar Service (`RESTAURANT_BAR`)

Landing page for the department: `/dashboard/pos.html`

**RESTAURANT_MANAGER** — lands on `/dashboard/pos.html`; sidebar: Operations (2) · Communication (1).

Capabilities (11): `attendance.clock`, `attendance.manage`, `kitchen.queue.view`, `leave.request`, `payment.capture`, `pos.return.record`, `pos.return.refund`, `restaurant.menu.manage`, `restaurant.order.manage`, `restaurant.table.manage`, `shift.manage`

**WAITER** — lands on `/dashboard/pos.html`; sidebar: Operations (4) · Communication (1).

Capabilities (6): `attendance.clock`, `guest_request.manage`, `leave.request`, `payment.capture`, `pos.return.record`, `restaurant.order.manage`

**BAR_MANAGER** — lands on `/dashboard/pos.html`; sidebar: Operations (2) · Communication (1).

Capabilities (10): `attendance.clock`, `attendance.manage`, `bar.menu.manage`, `bar.order.manage`, `kitchen.queue.view`, `leave.request`, `payment.capture`, `pos.return.record`, `pos.return.refund`, `shift.manage`

**BARTENDER** — lands on `/dashboard/pos.html`; sidebar: Operations (4) · Communication (1).

Capabilities (6): `attendance.clock`, `bar.order.manage`, `guest_request.manage`, `leave.request`, `payment.capture`, `pos.return.record`

### Housekeeping (`HOUSEKEEPING`)

Landing page for the department: `/dashboard/housekeeping.html`

**HOUSEKEEPING** — lands on `/dashboard/housekeeping.html`; sidebar: Operations (4) · Communication (1).

Capabilities (4): `attendance.clock`, `guest_request.manage`, `housekeeping.task.manage`, `leave.request`

**HOUSEKEEPING_MANAGER** — lands on `/dashboard/housekeeping.html`; sidebar: Operations (4) · Communication (1).

Capabilities (8): `attendance.clock`, `attendance.manage`, `guest_request.assign`, `guest_request.manage`, `housekeeping.task.assign`, `housekeeping.task.manage`, `leave.request`, `shift.manage`

**HOUSEKEEPER** — lands on `/dashboard/housekeeping.html`; sidebar: Operations (4) · Communication (1).

Capabilities (4): `attendance.clock`, `guest_request.manage`, `housekeeping.task.manage`, `leave.request`

### Portering / Maintenance (`PORTERING`)

Landing page for the department: `/dashboard/service-requests.html`

**MAINTENANCE** — lands on `/dashboard/maintenance.html`; sidebar: Operations (4) · Communication (1).

Capabilities (4): `attendance.clock`, `guest_request.manage`, `leave.request`, `maintenance.work_order.manage`

**MAINTENANCE_TECHNICIAN** — lands on `/dashboard/maintenance.html`; sidebar: Operations (4) · Communication (1).

Capabilities (4): `attendance.clock`, `guest_request.manage`, `leave.request`, `maintenance.work_order.manage`

**SECURITY** — lands on `/dashboard/index.html`; sidebar: Operations (3) · Communication (1).

Capabilities (2): `attendance.clock`, `guest_request.manage`

**PORTER** — lands on `/dashboard/service-requests.html`; sidebar: Operations (3) · Communication (1).

Capabilities (3): `attendance.clock`, `guest_request.manage`, `leave.request`

### Stores & Inventory (`STORES_INVENTORY`)

Landing page for the department: `/dashboard/inventory.html`

**INVENTORY_CLERK** — lands on `/dashboard/inventory.html`; sidebar: Operations (4) · Communication (1).

Capabilities (4): `attendance.clock`, `inventory.manage`, `leave.request`, `procurement.manage`

**STOREKEEPER** — lands on `/dashboard/inventory.html`; sidebar: Operations (4) · Communication (1).

Capabilities (4): `attendance.clock`, `inventory.manage`, `leave.request`, `procurement.manage`


---

## 6. Page visibility (`PAGE_ACCESS`)

45 dashboard pages carry an access rule. The personal surfaces
(`notifications.html`, `notification-details.html`, `profile.html`) are open to
every operational role (`everyone`), and the public site pages are marked
`public`. Role-specific pages are declared explicitly — there is no implicit
hierarchy, so a department manager does not inherit the front desk's screens.

Key pages and the roles admitted (full list in the JSON `roles[].dashboard_pages`):

| Page | Roles |
| --- | --- |
| `index.html` (dashboard) | admin, manager, general_manager, receptionist, front_desk_supervisor, staff |
| `bookings.html` | front desk roles + management |
| `rooms.html` | front desk roles + management |
| `offers.html`, `reports.html`, `facilities.html`, `gallery.html`, `terminals.html` | management (`admin`, `manager`, `general_manager`) |
| `accounting.html` | admin, manager, general_manager, accounts_manager, accountant |
| `payroll.html` | above + hr_manager |
| `staff.html` | administrator only |
| `staff-profile.html` | admin, manager, general_manager, hr_manager |
| `audit-logs.html`, `reviews.html`, `settings.html` | administrator only |
| `workforce.html` | every operational role |

An unclassified page fails closed (`canOpenPage` returns false), so a link can
never silently appear for a page nobody has graded.

---

## 7. API authorization model

Four layers, all server-side:

1. **Authentication** — JWT (`WorkstationJWTAuthentication`), with the workstation
   credential resolved per request. Portal guests use a separate portal session
   (`HasPortalSession`), and the reception terminal authenticates the *desk*, not
   a person (desk key + registered desktop IP).
2. **Role gate** — the permission class on the view. New operational endpoints use
   `HasCapability`; a legacy group still uses three-role tuples (see below).
3. **Capability gate** — `required_capability` / `required_capabilities` with
   `require_any_capability`. Resolution order: explicit per-user override →
   persisted `RoleCapability` rows → `ROLE_CAPABILITY_CODES` only when the role
   has no rows at all. ADMIN resolves the full active catalogue. **Fails closed.**
4. **Object gate** — ownership checks (`IsOwnerOrStaff`) and per-row scoping in
   querysets (`staff=request.user` unless `attendance.manage`).

Permission classes in use (316 normalised endpoint rows):

| Class | Rows | Semantics |
| --- | --- | --- |
| `HasCapability` | 174 | Capability-gated operational APIs (the target model). |
| `AllowAny` | 48 | Public site, auth entry points, enquiry/booking intake, QR service. |
| `IsStaffReadOnlyManagerWrite` | 22 | Legacy configuration: read for desk roles, write for managers. |
| `IsAuthenticated` | 15 | Signed-in users (portal/profile mix). |
| `HasPortalSession` | 12 | Guest portal session. |
| `IsAdminRole` | 10 | Administrator-only (users, audit, reviews, permissions matrix). |
| `BookingManageAccess` / `BookingReadAccess` / `BookingAccess` | 15 | Booking endpoints with legacy allowlist **or** capability grant. |
| `IsStaffRole` | 8 | Legacy staff tuple. |
| `IsManagerOrAdmin` | 5 | Manager reports, refunds, reschedule. |
| `IsDashboardViewer` | 1 | Roles the frontend routes to `index.html`. |
| `CanViewStaffProfile` | 1 | `staff.view` or self. |
| `CanApproveSomething` | 1 | Approvals queue. |
| *(none)* | 4 | Login, token refresh, two biometric terminal views; each enforces its own credential. |

### Capability catalogue

| Code | Name | Category |
| --- | --- | --- |
| `account.view` | View staff accounts | Identity |
| `account.manage` | Manage staff accounts | Identity |
| `booking.read` | View reservations | Reservations |
| `booking.manage` | Manage reservations | Reservations |
| `stay.check_in` | Check in guests | Stays |
| `stay.check_out` | Check out guests | Stays |
| `folio.view` | View folios | Finance |
| `folio.charge.post` | Post normal folio charges | Finance |
| `finance.controls.manage` | Manage finance controls | Finance |
| `payment.read` | Read payment ledger | Finance |
| `payment.capture` | Capture collections | Finance |
| `payment.receipt.send` | Send guest receipts | Finance |
| `payment.refund.request` | Request refunds | Finance |
| `payment.refund.approve` | Approve sensitive money actions | Finance |
| `cash_session.open` | Open cash sessions | Cashier |
| `cash_session.close` | Close cash sessions | Cashier |
| `expense.manage` | Manage expenses | Finance |
| `expense.approve` | Approve expenses | Finance |
| `pos.order.manage` | Manage general POS orders | POS |
| `pos.menu.manage` | Manage all POS menus | POS |
| `restaurant.menu.manage` | Manage restaurant menu | Restaurant |
| `restaurant.order.manage` | Manage restaurant orders | Restaurant |
| `restaurant.table.manage` | Manage restaurant tables | Restaurant |
| `bar.order.manage` | Manage bar orders | Bar |
| `bar.menu.manage` | Manage bar menu | Bar |
| `pos.return.record` | Record unopened-item returns | POS |
| `pos.return.refund` | Pay out return refunds | POS |
| `kitchen.queue.view` | View production queues | Kitchen |
| `kitchen.ticket.manage` | Progress production tickets | Kitchen |
| `guest_request.manage` | Manage guest requests | Guest services |
| `guest_request.assign` | Dispatch guest requests | Guest services |
| `housekeeping.task.manage` | Manage housekeeping tasks | Operations |
| `housekeeping.task.assign` | Dispatch housekeeping tasks | Operations |
| `maintenance.work_order.manage` | Manage maintenance work orders | Operations |
| `maintenance.work_order.assign` | Dispatch maintenance work orders | Operations |
| `inventory.manage` | Manage inventory | Inventory |
| `inventory.adjust.approve` | Approve inventory adjustments | Inventory |
| `procurement.manage` | Manage procurement | Inventory |
| `procurement.approve` | Approve procurement | Inventory |
| `staff.profile.manage` | Manage staff operational profiles | Staff operations |
| `staff.view` | View staff records | Staff operations |
| `staff.manage` | Manage staff records | Staff operations |
| `shift.manage` | Manage staff shifts | Staff operations |
| `attendance.clock` | Clock attendance | Staff operations |
| `attendance.manage` | Manage attendance | Staff operations |
| `leave.request` | Request leave | Staff operations |
| `leave.approve` | Approve leave | Staff operations |
| `payroll.view` | View payroll | Payroll |
| `payroll.manage` | Prepare payroll | Payroll |
| `payroll.approve` | Approve payroll | Payroll |
| `reports.financial.view` | View financial reports | Reports |
| `audit.view` | View audit records | Security |
| `terminal.manage` | Manage workstation registry | Security |
| `service_qr.manage` | Manage guest service QR links | Guest services |
| `settings.manage` | Manage hotel settings | Administration |
| `sign_in.any_device` | Sign in from any device | Identity |

---

## 8. UI visibility vs API enforcement

The audit ran a live GET probe as each of the 25 staff roles against 13
representative endpoints and compared the result with `PAGE_ACCESS`. Six
confirmed defects were found — five API/page mismatches and one post-login
landing page — and all six are fixed and covered by tests.

### 7.1 Fixed

| # | Symptom | Root cause | Fix |
| --- | --- | --- | --- |
| 1 | `GENERAL_MANAGER` and `FRONT_DESK_SUPERVISOR` were routed to `index.html` but `GET /api/admin/dashboard/` answered 403 — a dashboard that never loads | `DashboardView` used `IsStaffRole` (legacy `ADMIN`/`MANAGER`/`RECEPTIONIST`) | `DASHBOARD_ROLES` + `IsDashboardViewer` in `apps/core/permissions.py`; the payload's financial block keeps its own `is_manager_or_admin` gate |
| 2 | General manager: 403 on revenue reports, offers, rooms and the dashboard financial block | `IsManagerOrAdmin` and `IsStaffReadOnlyManagerWrite` did not know the role, although `ROLE_CAPABILITY_CODES["GENERAL_MANAGER"]` is a byte-for-byte copy of MANAGER's 50 capabilities | `MANAGER_ROLES` now includes `GENERAL_MANAGER`; `is_manager_or_admin` matches; read allowlist extended for legacy configuration |
| 3 | Front desk supervisor: 403 listing rooms, although `rooms.html` is in its page table | `IsStaffReadOnlyManagerWrite` read tuple was the legacy three roles | `LEGACY_READ_ROLES` adds `FRONT_DESK_SUPERVISOR` (read only — writes stay with managers) |
| 4 | HR manager and general manager could not open the staff profile card their navigation links to | `CanViewStaffProfile` decided on the legacy role tuple | Decides on `staff.view` (or self), the capability the pages already require |
| 5 | Accounts manager: 403 on attendance list/clock and the misleading `Attendance clocking is limited to the active staff member` when issuing a code | `attendance.clock` was missing from `ROLE_CAPABILITY_CODES["ACCOUNTS_MANAGER"]` and from its persisted `RoleCapability` rows — the only staff role without it; the service merged the identity check and the capability check into one message | Guard split in `staff_operations_service.clock_attendance`; grant added to the catalogue and to migration `0024_accounts_manager_attendance_clock` |
| 6 | `SECURITY` was sent to `index.html` after sign-in, which immediately bounced it to `/403.html` | `defaultStaffLanding()` had no entry for the role | `security` → `dashboard/workforce.html`; a new JS suite asserts every role lands on a page that admits it |

### 7.2 Remaining, documented — not changed

| Area | Why it remains | Smallest safe change if it is wanted |
| --- | --- | --- |
| Legacy classes still gate 22 configuration endpoints and 8 staff-wide endpoints | Migrating them wholesale to capabilities would change the write surface of several departments at once. `apps/core/permissions.py` explicitly says they are migrated *individually*. | Move one endpoint family at a time to `HasCapability` with an offer/policy capability code, with a role-by-role probe before and after. |
| `RECEPTIONIST` reads the offers/policies/rooms family even though `offers.html` is management-only | Over-grant of read-only configuration data inherited from the legacy class; it is not an escalation and reception needs room/room-type reads. | Remove `offer` reads from the desk by migrating that endpoint family; requires a product decision on promo visibility. |
| ADMIN's code catalogue (53) vs its database grants (51) | `payment.read` and `payment.receipt.send` exist in code but are not seeded to any role; ADMIN only receives them because it resolves the live catalogue | Seed the two codes to ADMIN in the next capability migration, or drop them from the catalogue if unused. |
| `IsStaffRole` still gates 8 endpoints | Same legacy migration backlog; none of these endpoints is linked from a page a department role can open. | Migrate to `HasCapability` with `staff.view`. |

---

## 9. Direct-URL access

A dashboard page is plain HTML. Opening `dashboard/hr.html` by hand is not
itself a breach: the page's boot gate bounces a role it does not admit to
`/403.html`, the sidebar is rendered from the role/capability tables, and every
XHR carries the JWT and is authorised server-side. The frontend gate is a
courtesy, not a boundary — verified by requesting the API directly with curl as
each role (section 9).

Two consequences worth stating plainly:

* editing the HTML or the JS cannot grant data — it can only reveal empty
  screens, because the API answers 403;
* the inverse mistake (an API broader than the page) is invisible to the UI
  test suite. `frontend/tests-js/test-department-page-access.js` proves
  `PAGE_ACCESS` ↔ page gates agree; it cannot see the API. That is why this
  audit added the live role probe.

---

## 10. Verification

**Live probe** (25 staff roles × 13 endpoints, GET, temporary harness during the
audit) compared status codes across dashboard, bookings, rooms, housekeeping,
maintenance, POS menu/orders, inventory, finance cash sessions, revenue report,
attendance, users directory and offers. The final matrix is in
`docs/COMPREHENSIVE_SYSTEM_AUDIT.md` §"Role probe".

**Regression tests**

| Test | What it pins |
| --- | --- |
| `backend/tests/test_dashboard_role_access.py` | the five dashboard roles load it; six department workers and guests do not; receptionist still gets no financial block |
| `backend/tests/test_permission_alignment.py` | general-manager parity, front-desk supervisor read/write split, staff-profile card follows `staff.view`, accounts-manager self-service |
| `backend/tests/test_attendance_code_and_visibility.py` | accurate refusal when a grant is missing; every other role receives a code; attendance scope/pagination/filtering |
| `backend/tests/test_role_permissions_matrix.py` | administrator-only surfaces stay administrator-only |
| `frontend/tests-js/test-staff-landing-access.js` | every role lands on a page that admits it |

Run:

```bash
cd backend
DJANGO_SETTINGS_MODULE=config.settings.development python manage.py test \
  tests.test_dashboard_role_access tests.test_permission_alignment \
  tests.test_attendance_code_and_visibility tests.test_role_permissions_matrix
cd ../frontend && node --test tests-js/test-staff-landing-access.js
```
