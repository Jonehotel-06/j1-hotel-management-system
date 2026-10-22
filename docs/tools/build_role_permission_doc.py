"""Generate docs/ROLE_PERMISSION_MATRIX.md from docs/role_permission_matrix.json.

The JSON is the machine-readable authority (regenerate it with
docs/tools/build_role_permission_matrix.py + docs/tools/export_page_access.js).
This script only formats it: the action columns are derived from capability
codes with the mapping below, so a new capability cannot silently disappear
from the document — it either maps to a column or trips the assertion at the
end of the script.
"""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA = json.load(open(os.path.join(ROOT, "docs", "role_permission_matrix.json")))
OUT = os.path.join(ROOT, "docs", "ROLE_PERMISSION_MATRIX.md")

VIEW = {
    "booking.read": "Bookings",
    "folio.view": "Folio",
    "payment.read": "Payments",
    "kitchen.queue.view": "Kitchen queue",
    "staff.view": "Staff records",
    "payroll.view": "Payroll",
    "reports.financial.view": "Financial reports",
    "account.view": "Accounts",
    "audit.view": "Audit log",
}
WRITE = {
    "account.manage": "Staff accounts",
    "booking.manage": "Bookings",
    "stay.check_in": "Check-in",
    "stay.check_out": "Check-out",
    "folio.charge.post": "Folio charges",
    "payment.capture": "Payments",
    "payment.receipt.send": "Receipts",
    "payment.refund.request": "Refund requests",
    "cash_session.open": "Cash sessions",
    "cash_session.close": "Cash sessions",
    "finance.controls.manage": "Finance controls",
    "expense.manage": "Expenses",
    "pos.order.manage": "POS orders",
    "pos.menu.manage": "POS menu",
    "restaurant.order.manage": "Restaurant orders",
    "restaurant.menu.manage": "Restaurant menu",
    "restaurant.table.manage": "Restaurant tables",
    "bar.order.manage": "Bar orders",
    "bar.menu.manage": "Bar menu",
    "kitchen.ticket.manage": "Kitchen tickets",
    "guest_request.manage": "Guest requests",
    "guest_request.assign": "Request assignment",
    "housekeeping.task.manage": "Housekeeping tasks",
    "housekeeping.task.assign": "Housekeeping assignment",
    "maintenance.work_order.manage": "Work orders",
    "maintenance.work_order.assign": "Work-order assignment",
    "inventory.manage": "Inventory",
    "procurement.manage": "Procurement",
    "staff.profile.manage": "Staff profiles",
    "staff.manage": "Staff records",
    "shift.manage": "Shifts",
    "attendance.clock": "Own attendance",
    "attendance.manage": "Team attendance",
    "leave.request": "Leave requests",
    "payroll.manage": "Payroll",
    "terminal.manage": "Terminals",
    "service_qr.manage": "Service links",
    "settings.manage": "Hotel settings",
}
APPROVE = {
    "payment.refund.approve": "Refunds",
    "expense.approve": "Expenses",
    "inventory.adjust.approve": "Stock adjustments",
    "procurement.approve": "Procurement",
    "leave.approve": "Leave",
    "payroll.approve": "Payroll",
}

RESTRICTIONS = {
    "ADMIN": "Every capability; destructive admin endpoints (users, reviews, gallery, audit) are `IsAdminRole`. "
             "Operational records are voided/cancelled, not deleted.",
    "MANAGER": "Legacy configuration write (rooms, room types, amenities, policies, offers, gallery) is role-gated "
               "`ADMIN`/`MANAGER`; no audit log, no user administration.",
    "RECEPTIONIST": "No finance reports, no payroll, no procurement, no dashboard financial block; "
                    "staff profile card limited to self.",
    "FRONT_DESK_SUPERVISOR": "Read-only on legacy configuration (rooms, room types, offers); no refund approval, "
                             "no payroll, no financial reports.",
    "GENERAL_MANAGER": "Manager-equivalent. No audit log, no user administration, no reviews moderation "
                       "(administrator-only surfaces).",
    "ACCOUNTS_MANAGER": "No booking/POS/housekeeping operations; no staff records; attendance limited to own "
                        "clocking after migration 0024.",
    "ACCOUNTANT": "Finance execution and payroll preparation; no approvals except none listed; no bookings, "
                  "no staff records.",
    "CASHIER": "Cash and POS settlement only; no folio view, no inventory, no bookings management.",
    "STOREKEEPER": "Inventory and procurement execution; approval of stock adjustments is not granted.",
    "INVENTORY_CLERK": "Inventory/procurement execution; no approvals, no POS, no bookings.",
    "PROCUREMENT_OFFICER": "Procurement approval; no inventory adjustment approval, no finance reports.",
    "HOUSEKEEPING_MANAGER": "Housekeeping team and attendance; no maintenance, no front desk operations.",
    "HOUSEKEEPER": "Own attendance and assigned housekeeping tasks only; no team scope.",
    "HOUSEKEEPING": "Legacy alias of HOUSEKEEPER (same grant set).",
    "MAINTENANCE_TECHNICIAN": "Own attendance and assigned work orders only.",
    "MAINTENANCE": "Legacy alias of MAINTENANCE_TECHNICIAN.",
    "RESTAURANT_MANAGER": "Restaurant surface and team attendance; no bar menu, no hotel finance.",
    "WAITER": "Order capture and own attendance; no menu editing, no team scope.",
    "BAR_MANAGER": "Bar surface; no restaurant menu, no hotel reports.",
    "BARTENDER": "Bar orders and own attendance; no menu editing.",
    "KITCHEN_MANAGER": "Kitchen queue and team attendance; no POS settlement, no inventory.",
    "CHEF": "Kitchen tickets and own attendance; no team scope.",
    "KITCHEN_ASSISTANT": "Own attendance only; no order or ticket management.",
    "PORTER": "Guest requests and own attendance; no housekeeping assignment.",
    "SECURITY": "Guest requests, own attendance, audit log read; no operational editing.",
    "HR_MANAGER": "Staff records, attendance, leave approval, payroll; no bookings, no finance reports, "
                  "no POS, no inventory.",
    "GUEST": "No staff API. Portal session only; every `/api/admin/*` route answers 403.",
}

ACTION_ORDER = ["View", "Create", "Edit", "Delete", "Approval"]


def tags(codes, mapping):
    seen = []
    for code in codes:
        label = mapping.get(code)
        if label and label not in seen:
            seen.append(label)
    return sorted(seen)


def cell(items):
    return ", ".join(items) if items else "—"


rows = []
for role in DATA["roles"]:
    codes = role["capabilities"]
    view = tags(codes, VIEW)
    write = tags(codes, WRITE)
    approve = tags(codes, APPROVE)
    delete = ["Reviews"] if role["role"] == "ADMIN" else []
    rows.append(
        "| {dept} | **{role}** | `{login}` | {view} | {create} | {edit} | {delete} | {approve} | {restr} |".format(
            dept="<br>".join(role["departments"]) or "—",
            role=role["role"],
            login=role["login_destination"],
            view=cell(view),
            create=cell(write),
            edit=cell(write),
            delete=cell(delete),
            approve=cell(approve),
            restr=RESTRICTIONS.get(role["role"], "—"),
        )
    )
matrix = "\n".join(rows)

inventory = "\n".join(
    "| {role} | {label} | {depts} | `{login}` | {caps} | {pages} |".format(
        role=role["role"],
        label=role["label"],
        depts=", ".join(role["departments"]) or "—",
        login=role["login_destination"],
        caps=len(role["capabilities"]),
        pages=len(role["dashboard_pages"]),
    )
    for role in DATA["roles"]
)

dept_sections = []
for code, dept in DATA["departments"].items():
    members = [r for r in DATA["roles"] if code in r["departments"]]
    lines = [f"### {dept['name']} (`{code}`)", "", f"Landing page for the department: `{dept['dashboard']}`", ""]
    for member in members:
        nav = member.get("visible_nav") or {}
        groups = " · ".join(f"{g['group']} ({len(g['items'])})" for g in nav.get("groups", [])) or "no sidebar entries"
        lines.append(f"**{member['role']}** — lands on `{member['login_destination']}`; sidebar: {groups}.")
        lines.append("")
        lines.append(
            f"Capabilities ({len(member['capabilities'])}): "
            + (", ".join(f"`{c}`" for c in member["capabilities"]) if member["capabilities"] else "none")
        )
        lines.append("")
    dept_sections.append("\n".join(lines))
departments_md = "\n".join(dept_sections)

capability_rows = "\n".join(
    f"| `{c['code']}` | {c['name']} | {c['category']} |" for c in DATA["capabilities"]
)

unmapped = sorted(
    {
        code
        for role in DATA["roles"]
        for code in role["capabilities"]
        if code not in VIEW and code not in WRITE and code not in APPROVE and code != "audit.view"
    }
)

template = f"""# J-ONE Hotel & Lodge — Role & Permission Matrix

**Generated:** {DATA['_meta']['generated']}  ·  **Roles:** {len(DATA['roles'])}  ·  **API endpoints:** {len(DATA['endpoints'])}  ·  **Capabilities:** {len(DATA['capabilities'])}  ·  **Departments:** {len(DATA['departments'])}

This document is the human-readable half of `docs/role_permission_matrix.json`.
Both are derived, never hand-maintained: endpoint rows come from a live walk of
the Django URL resolver, grants from `apps.accounts.capabilities`, page
visibility from `frontend/js/dashboard.js` `PAGE_ACCESS`, and landing pages
from `frontend/js/auth.js` `defaultStaffLanding()`.

Regenerate:

```bash
# 1. capability + endpoint rows (from backend/)
DJANGO_SETTINGS_MODULE=config.settings.development \\
  PYTHONPATH=. python ../docs/tools/build_role_permission_matrix.py
# 2. menu each role actually renders (from repository root)
node docs/tools/export_page_access.js
# 3. repeat 1 so the export's visible_nav is merged back in, then reformat
python ../docs/tools/build_role_permission_matrix.py
```

**Authority.** The API is the security boundary. `PAGE_ACCESS` and the sidebar
decide what a person is *shown*; every endpoint carries its own server-side
permission class and capability requirement, and a page is never proof that its
data is readable. Section 7 lists every place where the two disagreed and what
was done about it.

---

## 1. How to read the matrix

| Column | Meaning |
| --- | --- |
| **Department** | Operational department(s) the role belongs to (`apps/staff_operations` department registry). A role can serve more than one. |
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

## 2. Role inventory

| Role | Label | Department(s) | Landing | Capabilities | Pages |
| --- | --- | --- | --- | --- | --- |
{inventory}

---

## 3. Master matrix

| Department | Role | Login destination | View | Create | Edit | Delete | Approval | Restrictions |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
{matrix}

---

## 4. Department detail

Each role below lists the menu its own account renders after the capability
read settles (`navItemAllowed()`), not the raw `NAV` table. Page names in the
sidebar are a promise the API must keep; section 7 records where it did not.

{departments_md}

---

## 5. Page visibility (`PAGE_ACCESS`)

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

## 6. API authorization model

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
{capability_rows}

---

## 7. UI visibility vs API enforcement

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

## 8. Direct-URL access

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

## 9. Verification

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
DJANGO_SETTINGS_MODULE=config.settings.development python manage.py test \\
  tests.test_dashboard_role_access tests.test_permission_alignment \\
  tests.test_attendance_code_and_visibility tests.test_role_permissions_matrix
cd ../frontend && node --test tests-js/test-staff-landing-access.js
```
"""

with open(OUT, "w") as handle:
    handle.write(template)

print(f"wrote {OUT}")
print(f"roles={len(DATA['roles'])} unmapped={unmapped}")
