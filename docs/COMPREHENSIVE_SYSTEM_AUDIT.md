# J-ONE Hotel & Lodge — Comprehensive System Audit

**Repository:** `github.com/Jonehotel-06/j1-hotel-management-system`
**Audited revision:** `ea56da5` (`main`, "hosting"), working tree inspected 2026-10-22
**Audit type:** source-level audit with live API probes, measured performance baselines and regression tests
**Companion documents:** `docs/ROLE_PERMISSION_MATRIX.md`, `docs/role_permission_matrix.json`, `docs/PERFORMANCE_AUDIT.md`

---

## 1. Executive summary

The system is a Django 5 / DRF backend (22 apps, 316 routes) with a static
HTML/JS dashboard (80 pages, 21,516 lines of JS) behind JWT authentication, and a
27-role capability model that has been added incrementally on top of an older
three-role permission system.

Nine confirmed defects are recorded in §4.1; each is repaired (or has its
durable fix applied) and covered by tests. Six further structural limitations
are documented in §4.2 with the smallest safe change rather than a wholesale
rewrite. The two user-reported failures have concrete, verified root causes:

* **Part C — the access-code message.** `clock_attendance()` checked identity
  (`actor == staff`) and capability (`attendance.clock`) in one condition and
  raised one message. The accounts manager is the only staff role that never
  held `attendance.clock` — in the code catalogue *and* in the persisted
  `RoleCapability` rows. When that person stood at the terminal, the terminal
  correctly opened an attendance record *as them*, the capability half of the
  condition failed, and the service told them "Attendance clocking is limited to
  the active staff member." The message was describing a permission gap as an
  identity/session problem.
* **Part D — staff missing from Workforce → Attendance.** The backend returned
  all 12 clocked-in staff. The page requested `scope=mine` with
  `staff_id=<me>` by default, restricted team scope to the legacy
  admin/manager/general-manager ladder even though `attendance.manage` is
  broader, and always bound the query to `business_date=today`, which hides
  anyone still clocked in from a previous business date (night shift).

Parts A, E and F produced measured findings rather than sweeping changes:
the backend is not systemically slow (most list endpoints run in 1–6 queries),
the dominant per-request cost is password hashing on login and two redundant
ledger projections on the dashboard, and the security posture of the production
settings is sound (TLS/HSTS, secure cookies, exact-origin CORS/CSRF, throttled
auth entry points, rotation + blacklist on refresh tokens).

Nothing was disabled to make a symptom disappear. No permission was widened
beyond what the capability catalogue already grants the same role, and every
correction is recorded in §5 with its blast radius.

---

## 2. Scope and method

| Area | How it was verified |
| --- | --- |
| URL surface | Live walk of Django's resolver (`django.urls.get_resolver`) under the development settings; paths normalised, DRF router fragments and the media route removed. 316 distinct API rows. |
| Roles and grants | `User.Role` (27 values), `ROLE_CAPABILITY_CODES`, `CAPABILITY_DEFINITIONS` (53 codes), persisted `RoleCapability` rows, `UserCapabilityOverride`. |
| UI promises | `frontend/js/dashboard.js` `PAGE_ACCESS` (45 pages), `NAV` / `BOTTOM_NAV`, `defaultStaffLanding()` (login redirect), per-page `JONE.dashboard.boot()` gates. |
| Authorization | A live GET probe as each of the 25 staff roles against 13 representative endpoints (dashboard, bookings, rooms, housekeeping, maintenance, POS menu/orders, inventory, finance, revenue report, attendance, users, offers), plus targeted POST probes in tests. |
| Attendance | Service, view, serializer and terminal code paths read end-to-end; 13 regression tests cover issuance, refusal, scope, filtering, pagination and clock-out. |
| Performance | `CaptureQueriesContext` query counts plus wall-clock timings over a seeded dataset (60 rooms, 200 bookings, 40 staff, ~2 400 attendance rows) on SQLite; frontend weight census over `frontend/`. |
| Security | Settings review (production/base), auth entry-point review, object-scope review, race/transaction review (`select_for_update`, `transaction.atomic`), token lifecycle, throttle inventory, terminal trust boundary. |
| Tests | Full backend suite plus frontend `node --test` suites; baseline recorded before changes and re-run after. |

### 2.1 Limitations — what this audit did **not** verify

* **Production database.** No MySQL client exists in the audit environment; the
  suite's one failing test is `Error loading MySQLdb module`. All query plans
  and timings are SQLite. They are valid for comparing *before vs after* on the
  same dataset and engine, not as production latency predictions.
* **Production data volume.** The seeded dataset is described above; the real
  hotel's cardinalities are unknown.
* **External services.** Paystack, SMTP delivery, Cloudflare/Northflank edge
  configuration, and real fingerprint hardware were not exercised.
* **Real staff records.** No production account was read, created or modified.

---

## 3. System inventory

| Item | Count |
| --- | --- |
| Django apps | 22 |
| API routes (normalised) | 316 |
| Capabilities defined | 53 |
| Roles | 27 (26 staff + guest) |
| Departments | 9 (`MANAGEMENT`, `ACCOUNTS`, `ADMIN_PROCUREMENT`, `FRONT_DESK`, `KITCHEN`, `RESTAURANT_BAR`, `HOUSEKEEPING`, `PORTERING`, `STORES_INVENTORY`) |
| Dashboard HTML pages | 45 |
| Static frontend pages (total) | 80 |
| JS source | 21,516 lines / 556 KB |
| Backend tests | 125 test modules (plus 4 added by this audit) |

The authoritative machine-readable inventory is `docs/role_permission_matrix.json`
(`capabilities`, `departments`, `roles`, `endpoints`).

---

## 4. Findings

### 4.1 Confirmed defects — repaired

#### D1. Dashboard refused the roles the frontend routes to it
* **Symptom:** `GENERAL_MANAGER` and `FRONT_DESK_SUPERVISOR` are sent to
  `dashboard/index.html` after sign-in and see the Dashboard link, but
  `GET /api/admin/dashboard/` answered **403**. The page rendered and then
  failed its only data call.
* **Evidence:** live probe (before): both roles `403`; after: `200`.
* **Cause:** `DashboardView.permission_classes = [IsStaffRole]`, and
  `IsStaffRole` is the legacy tuple `("ADMIN", "MANAGER", "RECEPTIONIST")`.
* **Fix:** `apps/core/permissions.py` defines `DASHBOARD_ROLES` (the two legacy
  roles plus front desk supervisor and general manager) and `IsDashboardViewer`;
  `apps/reports/views.py` uses it. The payload's financial block keeps its
  separate `is_manager_or_admin` gate, so a front desk supervisor gets the
  dashboard without revenue, ledger, payroll or expense cards (asserted in
  `tests/test_dashboard_role_access.py`).
* **Blast radius:** one endpoint, two additional roles, no new data.

#### D2. General manager was denied surfaces its own capabilities grant
* **Symptom:** `403` on `GET /api/admin/reports/revenue/`, `GET /api/admin/offers/`,
  `GET /api/admin/rooms/`, and no financial block on the dashboard.
* **Evidence:** `ROLE_CAPABILITY_CODES["GENERAL_MANAGER"]` is the same 50
  capability codes as `MANAGER` (`reports.financial.view`, `payment.*`,
  `payroll.*`, `booking.manage`, …), yet the permission classes did not know the
  role.
* **Cause:** `MANAGER_ROLES = ("ADMIN", "MANAGER")` predates the general manager
  role; `IsManagerOrAdmin` and `is_manager_or_admin` used it directly, and
  `IsStaffReadOnlyManagerWrite` required legacy staff membership.
* **Fix:** `MANAGER_ROLES` includes `GENERAL_MANAGER`; `User.is_manager_or_admin`
  matches; `IsStaffReadOnlyManagerWrite` reads from the explicit
  `LEGACY_READ_ROLES` tuple.
* **Blast radius:** general manager only, on: reports ×3, booking reschedule,
  enquiry refund processing, dashboard financial block, and read/write on the
  legacy configuration endpoints (rooms, room types, amenities, facilities,
  gallery, guest discounts, offers, policies). It grants no capability the
  catalogue withholds — the role already holds every one of them.
* **Regression:** `tests/test_permission_alignment.py::GeneralManagerParityTests`.

#### D3. Front desk supervisor could not read room inventory
* **Symptom:** `403` on `GET /api/admin/rooms/` although `rooms.html` lists
  `front_desk_supervisor` in `PAGE_ACCESS` and check-in needs room state.
* **Cause:** same legacy read tuple as D2.
* **Fix:** `LEGACY_READ_ROLES = STAFF_ROLES + ("FRONT_DESK_SUPERVISOR", "GENERAL_MANAGER")`;
  writes remain `MANAGER_ROLES`.
* **Blast radius:** read-only on 22 configuration endpoints for one additional
  role — the same read-only records a receptionist already sees. No mutation.

#### D4. Staff profile card was closed to the roles whose pages link to it
* **Symptom:** `403` on `GET /api/admin/users/staff/<pk>/` for `HR_MANAGER` and
  `GENERAL_MANAGER`, while `staff-profile.html` is in both roles' page table and
  both hold `staff.view` / `staff.profile.manage`.
* **Cause:** `CanViewStaffProfile` began with `role in STAFF_ROLES`.
* **Fix:** the class now allows self, or any account holding `staff.view`.
  ADMIN resolves the full catalogue, MANAGER and GENERAL_MANAGER are granted it,
  HR_MANAGER is granted it; every other role still sees only its own card.
* **Regression:** `StaffProfileCardTests` (positive for HR/GM/manager, negative
  for accountant/cashier/chef, guest refused).

#### D5. Accounts manager could not clock in and received the Part C message
See §5.1. Fixed in the service guard plus migration `0024_accounts_manager_attendance_clock`.

#### D6. Security officers were sent to a page that bounces them
* **Symptom:** `defaultStaffLanding("security")` returned `dashboard/index.html`,
  which `PAGE_ACCESS` does not admit for `security` — so a successful sign-in
  ended on `/403.html`.
* **Fix:** `frontend/js/auth.js` routes `security` to `dashboard/workforce.html`
  (a page it is admitted to and needs for clocking in).
* **Regression:** `frontend/tests-js/test-staff-landing-access.js` asserts, for
  every operational role, that the landing page both exists in `PAGE_ACCESS`
  and admits the role. The existing department suite could not catch this: it
  proves page-table ↔ page-gate agreement, not landing ↔ page-table.

#### D7. Staff profiles list: 82 queries per page
See §6.1. Fixed with two prefetches; 4 queries after.

#### D8. Workforce → Attendance hid clocked-in staff
See §5.2.

#### D9. `attendance.clock` was missing from the persisted catalogue for one role
Same root as D5; the migration is the durable half (the code catalogue is only a
fallback when a role has no persisted rows).

### 4.2 Confirmed defects — documented, deliberately not changed

| ID | Finding | Why it is not changed | Proposed smallest safe change |
| --- | --- | --- | --- |
| L1 | `IsStaffReadOnlyManagerWrite` (22 endpoints) and `IsStaffRole` (8 endpoints) still encode the three-role world; departmental roles are refused even where a capability exists | Both classes gate whole families (rooms, amenities, policies, gallery, offers, room types). Migrating them piecemeal requires a product decision per family; `apps/core/permissions.py` explicitly says they are migrated individually, and the module warns that adding operational roles wholesale would silently grant access before migration | Move one family at a time to `HasCapability`, with the role probe (appendix B) run before and after |
| L2 | `RECEPTIONIST` can read the offers/policies/rooms family although `offers.html` is management-only | Over-grant of read-only configuration inherited from L1; it is not an escalation and the desk needs room/room-type reads | Migrate the offer family to a capability and drop the desk from it |
| L3 | ADMIN's role row lists 53 codes but its database grants contain 51 (`payment.read`, `payment.receipt.send` are unseeded) | ADMIN resolves the live catalogue, so behaviour is correct today; only a deployment that ran an old seed and edited the catalogue could drift | Seed both codes to ADMIN in the next capability migration, or remove them if unused |
| L4 | `IsOwnerOrStaff` is defined in `apps/core/permissions.py` and referenced by no view | Dead code, not a vulnerability: guest object scope is enforced by queryset ownership (`Booking.objects.filter(guest__user=request.user)`, `_OwnedBookingMixin`) | Delete the class or apply it on the endpoints that currently scope by queryset |
| L5 | `X-JONE-Terminal` requests may perform a conditional `UPDATE` of `Workstation.last_seen_at` on a `GET` | It is bounded (only when the stored value is >2 minutes stale) and keeps terminal liveness accurate; changing it would touch every terminal call | Move the heartbeat to an explicit `POST …/heartbeat/` if strict GET idempotence is required |
| L6 | Two `ledger` projections per dashboard request (14 queries, ~70 % of dashboard time) | `financial_summary()` is the accounting projection used by the Accounting page; deriving one range from the other changes its contract and risks financial correctness | Add an opt-in `include_breakdowns=False` for the day range, or compute both ranges in one conditional aggregate; measure with the harness in `docs/PERFORMANCE_AUDIT.md` |

### 4.3 Potential issues (need production evidence)

* **Attendance visibility after midnight.** The page now requests open records
  without a business-date bound. Backend retention/purge jobs were not executed
  here, so how long an abandoned open record survives in production is unknown.
* **Terminal enrolment drift.** `_resolve_reception_terminal` accepts a desk by
  key or by registered IP; if a workstation moves network without an
  administrator updating `registered_desktop_ip`, ordinary staff lose the
  terminal (management keeps an audited off-desk route). This is a policy
  trade-off, not a defect, but it is the most likely operational complaint.
* **Frontend page weight.** `accounting.html` (106 KB), `pos.html` (83 KB),
  `inventory.html` (72 KB), `payroll.html` (66 KB) and `workforce.html` (61 KB)
  are mostly inline script and style. On a slow connection this delays first
  paint more than any API call measured here.

### 4.4 Intentional behaviour — verified, not defects

* **Guests are refused every `/api/admin/*` route** (`403 FORBIDDEN`), and guest
  object access is scoped to `guest__user=request.user`.
* **The reception terminal authenticates the desk, not the person.** Ordinary
  staff must be at an enrolled desk; ADMIN/MANAGER/GENERAL_MANAGER may use it
  off-desk, and each such use is audited as `ATTENDANCE_OFF_DESK_TERMINAL`.
  A staff session alone is *not* sufficient — this was tested, not bypassed.
* **`clock_attendance` refuses an actor clocking in somebody else.** The identity
  rule is unchanged by this audit and is still covered by tests.
* **Page access fails closed**: a page missing from `PAGE_ACCESS` cannot be
  linked to and is not rendered in the menu.
* **The dashboard is not a financial surface for the desk.** A receptionist gets
  operations only; the financial block is manager/admin/general-manager.
* **Frontend gates are UX, not security.** Direct URL access to any dashboard
  page still results in API 403s for roles without the capability.

### 4.5 Missing functionality (not bugs)

* No server-side export/print for attendance or payroll registers (the pages
  print via the browser).
* No audit trail viewer for non-administrators, even for actions they performed.
* No bulk re-assignment of attendance records when a person changes department.

### 4.6 Security findings

See §7 for the full review. No exploitable vulnerability was found in the
audited surface. Four hardening observations are recorded (L4, L5, and two
below):

* The terminal endpoints declare `permission_classes = []` and rely entirely on
  `_resolve_reception_terminal()`. This is deliberate (the desk is the
  credential) and the function refuses unknown callers, but it deserves a
  dedicated test for every new action added to the endpoint.
* Attendance codes are numeric, time-boxed and single-use; the audit confirmed a
  refused issuance writes **no** attendance record and **no** credential.

### 4.7 Unverifiable in this environment

Payment provider callbacks, SMTP deliverability, MySQL query plans, real device
biometrics, and production data volumes. Claims in this document that depend on
them are marked as pending.

---

## 5. Root-cause analyses

### 5.1 Part C — "Attendance clocking is limited to the active staff member"

**Reported symptom.** An administrator creates a staff member, selects them on
the Workforce page's reception terminal, and receives
*"Attendance clocking is limited to the active staff member"* instead of an
access code.

**The full issuance path** (traced end-to-end):

1. `frontend/js/biometrics.js` captures a probe and calls
   `POST /api/admin/staff-operations/biometrics/reception-terminal/`
   (or `…/biometrics/temporary-credential/`) with the desk key header.
2. `ReceptionAttendanceTerminalView.post` (`apps/staff_operations/views_biometrics.py`)
   authenticates the *desk* via `_resolve_reception_terminal()`, resolves the
   staff member by email / employee code / id, matches the fingerprint against
   the active templates, then calls
   `reception_attendance_workstation_action(...)`.
3. That function calls `ensure_open_attendance_record(staff=staff, …)`
   (`apps/staff_operations/services/staff_operations_service.py`), which finds
   or creates the open record by calling
   `clock_attendance(staff=staff, actor=staff, action="CLOCK_IN", …)`.
4. `clock_attendance` began with:

   ```python
   if actor.pk != staff.pk or not has_capability(actor, "attendance.clock"):
       raise PermissionDenied("Attendance clocking is limited to the active staff member.")
   ```

**Root cause.** Step 3 passes the staff member as *both* staff and actor, so the
identity half can never fail on this path. The capability half can — and did.
`ROLE_CAPABILITY_CODES["ACCOUNTS_MANAGER"]` did not contain `attendance.clock`,
and because the role has persisted `RoleCapability` rows, the code catalogue was
never consulted at runtime; migration `0014_seed_department_capabilities` had
seeded the same omission into every deployment. The accounts manager was the
only staff role in the system missing a capability that all 25 others hold
(measured: 25/26 staff roles issued a code; ACCOUNTS_MANAGER alone was refused).

The operator standing at the terminal *was* the active staff member, so the
message sent them looking for a session/identity bug that did not exist.

**Fix — three parts, none of which weakens attendance controls:**

1. **Separate the failures** (`clock_attendance`): the identity rule keeps its
   original message; a missing capability now raises a message that names the
   role, the missing `attendance.clock` code, and the two ways to correct it,
   and logs a warning with `staff_id`/`role` (no PII beyond the staff id).
2. **Correct the grant** (`apps/accounts/capabilities.py` and migration
   `0024_accounts_manager_attendance_clock`): one `RoleCapability` row for
   `ACCOUNTS_MANAGER` / `attendance.clock`. No management capability, no
   override, no other role touched; the migration is reversible.
3. **Pin the boundary** with tests that prove a missing grant still refuses the
   code and still writes nothing.

**Verification:** `GET /api/admin/staff-operations/attendance/` and the clock
endpoint now answer `200` for the accounts manager; the probe shows every staff
role holding `attendance.clock`; the identity rule remains covered by the test
that a manager cannot clock another person in.

### 5.2 Part D — clocked-in staff absent from Workforce → Attendance

**Reported symptom.** Several staff have clocked in, but the Attendance tab does
not list all of them.

**What the backend actually did** (verified with a seeded dataset of 12 on-duty
staff across 3 departments): `GET /api/admin/staff-operations/attendance/`
returned all 12 rows for a manager, honouring `status`, `business_date`,
`staff_id` and pagination. The data layer was not losing anyone.

**The three client-side causes:**

1. **Wrong default scope.** `workforce.html` initialised the scope selector to
   `mine` and sent `staff_id=<my id>`. A supervisor opening the page saw their
   own record(s) only.
2. **Team scope was narrower than the permission model.** The selector was shown
   only when `isManager` was true, which is the rank ladder for
   admin/manager/general-manager. Every other role holding `attendance.manage`
   (front desk supervisor, restaurant/bar/kitchen/housekeeping managers, HR
   manager) could legitimately read the whole hotel's attendance but was never
   offered the option.
3. **`business_date=today` on every query.** Anyone still clocked in from a
   previous business date — a night shift that started yesterday and has not
   been closed — was filtered out even when their record was open.

**Fix (frontend only, `frontend/dashboard/workforce.html`):**

* team scope is available to the role list that holds `attendance.manage`, and
  is upgraded from the authoritative capability list once
  `Auth.loadCapabilities()` resolves;
* `applyAttendanceScope()` sends no `staff_id` in team scope and no `scope`
  filtering beyond what the server supports;
* a new **Currently clocked in** option sends `open=true` with **no**
  business-date bound, so an open night-shift record is visible;
* the automatic default is team scope for those roles; a personal view is one
  selection away, and the personal clock controls remain in the summary card.

**Verification:** 13 backend tests cover multi-staff, multi-department,
pagination, status filtering, clock-out history and authorisation; JS UI tests
cover the scope selector and the open-record query.

---

## 6. Performance

Full numbers and methodology: `docs/PERFORMANCE_AUDIT.md`. Summary:

### 6.1 Fixed

| Endpoint | Before | After | Change |
| --- | --- | --- | --- |
| `GET /api/admin/staff-operations/profiles/` (40 staff) | 82 queries / 52.1 ms | 4 queries / 17.0 ms | −95 % queries, −67 % wall clock |

Cause: `_profile_queryset()` loaded each profile's fingerprint templates and
open attendance per row inside the serializer. Two `Prefetch` objects
(`to_attr`, `.only(...)`) make them page-wide; the serializer keeps its per-row
fallback for callers that load a profile outside this queryset.

### 6.2 Measured, not changed

| Endpoint | Queries | Wall (SQLite) | Note |
| --- | --- | --- | --- |
| `GET /api/admin/dashboard/` | 34 | 76.6 ms | 14 queries / 54 ms are two ledger projections (L6) |
| `…/ledger` day summary alone | 7 | 27.0 ms | duplicates work already inside the month projection |
| `…/ledger_month` | 7 | 26.9 ms | |
| `GET …/attendance/` (100 rows) | 2 | 6.4 ms | |
| `GET /api/admin/bookings/` | 5 | 24.7 ms | |
| `GET /api/admin/users/` | 2 | 9.8 ms | |
| `POST /api/auth/login/` | 8 | 224.8 ms | PBKDF2 hashing dominates; not a query problem |

### 6.3 Frontend

* Assets 2.1 MB (images 1.9 MB), CSS 240 KB (`dashboard.css` 105 KB), JS 556 KB.
* Largest pages are inline-heavy: `accounting.html` 106 KB, `pos.html` 83 KB,
  `inventory.html` 72 KB, `payroll.html` 66 KB, `workforce.html` 61 KB.
* Dashboard scripts are 13 external versioned tags at the end of `<body>`
  (correct placement) plus per-page inline blocks.
* No new framework, bundler or dependency was introduced by this audit.

---

## 7. Security and data-integrity review

| Area | Finding |
| --- | --- |
| **Tokens** | Access 30 min, refresh 7 days, rotation **and** blacklist after rotation; signing key can be separated from `SECRET_KEY`; `Authorization: Bearer` only. |
| **Password handling** | Four Django validators (similarity, minimum length, common, numeric). Login, registration and both password-reset endpoints carry `ScopedRateThrottle` (5/min, 20/h, 10/h). |
| **Transport / headers** | Production enforces `SECURE_SSL_REDIRECT`, `SECURE_PROXY_SSL_HEADER`, HSTS 30 days with subdomains and preload, `X_FRAME_OPTIONS = DENY`, `nosniff`, `Referrer-Policy: same-origin`, secure session/CSRF cookies. |
| **CORS / CSRF** | Production refuses `*` and path-bearing origins with a `RuntimeError`; only exact scheme+host origins are accepted. |
| **Object-level access** | Guest data is scoped by queryset (`guest__user=request.user`, `_OwnedBookingMixin`); staff attendance is scoped to the caller unless `attendance.manage`. `IsOwnerOrStaff` exists but is unused (L4 — dead code, not a hole). |
| **Race conditions** | 253 `select_for_update()` uses and 185 `transaction.atomic` blocks; attendance, payroll, cash sessions, folio posting and booking state transitions all take row locks before mutation. Attendance source keys are idempotent per event, so a double-submitted clock request cannot duplicate the event. |
| **Timezones** | Business dates go through `apps.core.utils.hotel_today()` (hotel timezone), not `date.today()`. The audit confirmed `business_date` filtering is business-day based; the frontend bug in Part D was a client-side filter, not a timezone error. |
| **Terminal trust** | Desk key or registered desktop IP, verified server-side; ordinary staff are desk-bound; management off-desk use is audited with account, reason and authority. A refused issuance writes no record and no credential. |
| **Secrets / PII** | No secret, token, password or personal record is printed by the audit, and none appears in the delivered documents. Logging is console-only at INFO and does not emit tokens. |

Two hardening follow-ups are listed in §4.2 (L4, L5).

---

## 8. Role probe — final matrix

`1` = HTTP 200, `0` = HTTP 403. Columns:
**D**ashboard, **B**ookings, **R**ooms, **H**ousekeeping, **M**aintenance,
**P**OS menu, **p**OS orders, **I**nventory, **F**inance cash sessions,
**V**enue revenue report, **A**ttendance, **U**sers directory, **O**ffers.

| Role | D B R H M P p I F V A U O |
| --- | --- |
| ADMIN | 1 1 1 1 1 1 1 1 1 1 1 1 1 |
| MANAGER | 1 1 1 1 1 1 1 1 1 1 1 1 1 |
| GENERAL_MANAGER | 1 1 1 1 1 1 1 1 1 1 1 1 1 |
| FRONT_DESK_SUPERVISOR | 1 1 1 1 1 0 0 0 0 0 1 1 1 |
| RECEPTIONIST | 1 1 1 1 1 0 0 0 0 0 1 1 1 |
| ACCOUNTS_MANAGER | 0 0 0 0 0 0 0 0 1 0 1 1 0 |
| ACCOUNTANT | 0 0 0 0 0 0 0 0 1 0 1 1 0 |
| CASHIER | 0 0 0 0 0 1 1 0 1 0 1 0 0 |
| HR_MANAGER | 0 0 0 0 0 0 0 0 0 0 1 1 0 |
| RESTAURANT_MANAGER | 0 0 0 0 0 1 1 0 0 0 1 1 0 |
| WAITER | 0 0 0 0 0 1 1 0 0 0 1 0 0 |
| BAR_MANAGER | 0 0 0 0 0 1 1 0 0 0 1 1 0 |
| BARTENDER | 0 0 0 0 0 1 1 0 0 0 1 0 0 |
| KITCHEN_MANAGER | 0 0 0 0 0 0 0 0 0 0 1 1 0 |
| CHEF | 0 0 0 0 0 0 0 0 0 0 1 0 0 |
| KITCHEN_ASSISTANT | 0 0 0 0 0 0 0 0 0 0 1 0 0 |
| HOUSEKEEPING_MANAGER | 0 0 0 1 0 0 0 0 0 0 1 1 0 |
| HOUSEKEEPER | 0 0 0 1 0 0 0 0 0 0 1 0 0 |
| HOUSEKEEPING | 0 0 0 1 0 0 0 0 0 0 1 0 0 |
| MAINTENANCE_TECHNICIAN | 0 0 0 0 1 0 0 0 0 0 1 0 0 |
| MAINTENANCE | 0 0 0 0 1 0 0 0 0 0 1 0 0 |
| STOREKEEPER | 0 0 0 0 0 0 0 1 0 0 1 0 0 |
| INVENTORY_CLERK | 0 0 0 0 0 0 0 1 0 0 1 0 0 |
| PROCUREMENT_OFFICER | 0 0 0 0 0 0 0 0 0 0 1 0 0 |
| PORTER | 0 0 0 0 0 0 0 0 0 0 1 0 0 |
| SECURITY | 0 0 0 0 0 0 0 0 0 0 1 0 0 |
| GUEST (control) | 0 0 0 0 0 0 0 0 0 0 0 0 0 |

Rows 3–5 were `0` on the first three columns before this audit (D1–D3), and
`ACCOUNTS_MANAGER` was `0` in column **A** (D5). Everything else is unchanged.

---

## 9. Test evidence

| Suite | Result |
| --- | --- |
| Backend, before changes | 1 652 tests, **1 failure** (`Error loading MySQLdb module` — MySQL client absent from the audit container), 17 skipped, 670.6 s |
| Backend, after changes (full suite) | 1 685 tests, **1 failure** (same environmental MySQL client), 17 skipped, 703.0 s |
| Backend targeted re-run after changes | 259 tests OK (`test_staff`, `test_auth`, `test_capabilities`, `test_role_permissions_matrix`, `test_permission_alignment`, `test_dashboard_role_access`, `test_fingerprint_staff_management`, `test_staff_face_verification`, `test_admin_*`, `test_booking_access_capabilities`, `test_dashboard_*`) |
| Attendance / permission suites | 21 tests OK (`test_attendance_code_and_visibility`, `test_capabilities`) |
| New regression tests | `test_attendance_code_and_visibility.py` (13), `test_dashboard_role_access.py` (5), `test_permission_alignment.py` (15), `frontend/tests-js/test-staff-landing-access.js` (4) — 33 new backend tests, which is exactly the 1 652 → 1 685 delta |
| Frontend JS | 78 suites, 75 pass; the 3 failures (`test-cloudflare-northflank-deployment.js`, `test-deploy-config.js`, `test-pwa-dashboard-only.js`) are pre-existing and reproduce unchanged on a clean checkout of `HEAD` |

Commands are listed in `docs/ROLE_PERMISSION_MATRIX.md` §9 and in
`docs/PERFORMANCE_AUDIT.md`.

---

## 10. Files changed

| File | Change |
| --- | --- |
| `backend/apps/core/permissions.py` | `DASHBOARD_ROLES`, `IsDashboardViewer`, `LEGACY_READ_ROLES`; `MANAGER_ROLES` includes `GENERAL_MANAGER`; `IsStaffReadOnlyManagerWrite` and `CanViewStaffProfile` reworked |
| `backend/apps/reports/views.py` | dashboard view uses `IsDashboardViewer` |
| `backend/apps/accounts/models.py` | `is_manager_or_admin` includes `GENERAL_MANAGER` |
| `backend/apps/accounts/capabilities.py` | `attendance.clock` added to `ACCOUNTS_MANAGER` |
| `backend/apps/accounts/migrations/0024_accounts_manager_attendance_clock.py` | new reversible data migration (one `RoleCapability` row) |
| `backend/apps/staff_operations/services/staff_operations_service.py` | `clock_attendance` guard split + precise message + warning log |
| `backend/apps/staff_operations/views.py` | `_profile_queryset()` prefetches; no response change |
| `backend/apps/staff_operations/serializers.py` | serializer reads prefetched attributes with per-row fallback |
| `frontend/dashboard/workforce.html` | attendance scope, "currently clocked in" filter, capability upgrade |
| `frontend/js/auth.js` | security landing page |
| `frontend/tests-js/test-attendance-auto-clockin-ui.js` | context for the new scope control |
| `backend/tests/test_attendance_code_and_visibility.py` | 13 tests (new) |
| `backend/tests/test_dashboard_role_access.py` | 5 tests (new) |
| `backend/tests/test_permission_alignment.py` | 15 tests (new) |
| `frontend/tests-js/test-staff-landing-access.js` | 4 tests (new) |
| `docs/page_access_export.json`, `docs/tools/*` | derived artifacts and their generators |
| `docs/ROLE_PERMISSION_MATRIX.md`, `docs/role_permission_matrix.json`, `docs/PERFORMANCE_AUDIT.md`, `docs/COMPREHENSIVE_SYSTEM_AUDIT.md` | deliverables |

No URL pattern, model field, public API shape or service contract was removed or
renamed.

---

## 11. Database changes

One data migration: `accounts/0024_accounts_manager_attendance_clock`.

* adds `RoleCapability(role="ACCOUNTS_MANAGER", capability="attendance.clock")`;
* creates the `Capability` row only if a database predates the catalogue seed;
* reverse deletes the row;
* no schema change, no column, no index, no data rewrite, no destructive step.

Apply with `python manage.py migrate accounts` during the deployment window; it
is idempotent (`get_or_create`) and safe to run before or after the code deploy
(the code catalogue also carries the grant, but only for roles with no persisted
rows — the migration is what fixes existing databases).

---

## 12. Deployment impact

* **Restart required** for the permission/capability changes.
* **Migration required** (`accounts.0024`).
* **Cache:** role capability resolution is cached per user object, not in a
  shared cache; signed-in sessions pick up the new grant on their next profile
  fetch. No cache flush needed.
* **Frontend:** `workforce.html` and `auth.js` changed — deploy with the static
  bundle and bump the asset version so browsers do not serve the old page.
* **No downtime step, no data backfill, no external service change.**

---

## 13. Remaining issues and recommendations

1. **Migrate the legacy permission classes** (L1/L2). This is the structural
   cause of most future role surprises. Do it one endpoint family at a time and
   keep the probe in appendix B as the acceptance test.
2. **Seed or retire the two unseeded payment codes** (L3).
3. **Reduce the dashboard's double ledger projection** (L6) — the single
   biggest measured backend cost on the landing page.
4. **Remove `IsOwnerOrStaff` or use it** (L4).
5. **Move the terminal heartbeat off `GET`** (L5) if strict idempotence matters.
6. **Trim the inline page weight** on `accounting.html` and `pos.html`; it is
   larger than every JS file in the bundle combined.
7. **Add an attendance retention check** for open records older than a configurable
   window, and a report for supervisors (potential issue, not a defect).

---

## Appendix A — Reproduction

```bash
# backend
cd backend
DJANGO_SETTINGS_MODULE=config.settings.development python manage.py test \
  tests.test_dashboard_role_access tests.test_permission_alignment \
  tests.test_attendance_code_and_visibility tests.test_role_permissions_matrix
# frontend
cd ../frontend
node --test tests-js/test-staff-landing-access.js
node --test tests-js/test-department-page-access.js

# regenerate the role documents
cd ..
node docs/tools/export_page_access.js
cd backend
DJANGO_SETTINGS_MODULE=config.settings.development PYTHONPATH=. \
  python ../docs/tools/build_role_permission_matrix.py
python ../docs/tools/build_role_permission_doc.py
```

The audit's temporary probe and measurement harnesses were removed from the
final tree; their method is described here and in `docs/PERFORMANCE_AUDIT.md` so
they can be recreated against the target environment.
