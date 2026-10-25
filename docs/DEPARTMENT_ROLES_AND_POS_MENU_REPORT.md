# Department roles, POS menu costing, and the receipt-email fix

**Date:** 2026-10-24  ·  **Scope:** `backend/apps/accounts`, `backend/apps/notifications`,
`backend/apps/pos`, `frontend/`  ·  **Migrations added:** 4 (accounts), 1 (pos), 1 (inventory)

> **Current-source audit addendum — 2026-10-25:** The earlier notes below describe
> the implementation at that report's date and overstated which account-write
> paths enforced the rule. The current authoritative registry and verified
> corrections are recorded immediately below. Do not treat legacy role rows in
> the generated matrix or older UI as assignable roles.
>
> `DEPARTMENT_ROLES` currently has one unique canonical role for each of 14
> operational departments: Management—`MANAGER`; Accounts—`ACCOUNTANT`;
> Admin Management / Procurement—`PROCUREMENT_OFFICER`; Kitchen—`CHEF`;
> Restaurant—`WAITER`; Porter—`PORTER`; Front Desk—`RECEPTIONIST`;
> Housekeeping—`HOUSEKEEPING`; Inventory / Stores—`STOREKEEPER`;
> Security—`SECURITY`; Cashier—`CASHIER`; Bar—`BARTENDER`;
> Maintenance—`MAINTENANCE`; Human Resources—`HR_OFFICER`. `ADMIN` and
> `GENERAL_MANAGER` remain global oversight roles, not departmental roles.
> Admin Management / Procurement is represented once by the existing
> `PROCUREMENT` department code and has no separate Admin role. Security's
> default grants are only `guest_request.manage` and `attendance.clock`; the
> procurement role retains `procurement.manage`, `attendance.clock`, and
> `leave.request`, with no procurement approval grant. Kitchen Assistant is a
> `StaffProfile.job_title` designation under the `CHEF` role; the old
> `KITCHEN_ASSISTANT` role code remains only for historical compatibility and
> is rejected by current account writers.
>
> The source audit found that the earlier API/admin guards did not cover the
> secondary staff-profile create/patch and lifecycle transfer paths, and that a
> persisted profile department could override a conflicting canonical role.
> Those specific gaps are now closed: create/update/profile/transfer paths
> validate department-role pairs; role changes synchronize the profile; Django
> admin profile edits validate the same pair; and the permission-matrix writer
> refuses retired role aliases. Conflicting operational account/profile identity
> returns no department and fails capability/DRF authorization checks closed. The staff form now offers
> canonical roles only while retaining historical role filters. Existing
> capability grants, override records, approval rules, and business workflow
> endpoints were not rewritten. No schema migration was added: existing
> `StaffProfile.job_title`, department, role, and capability tables suffice.
> Automated evidence for this focused change is in the 2026-10-25 final report
> and `backend/tests/test_department_roles.py`; it is not a claim that every
> endpoint in the hotel system has undergone a fresh object-scope review.

This report covers four changes that were asked for directly by the owner and two
defects found while making them. Every claim here is backed by a test in
`backend/tests/` or `frontend/tests-js/`; the commands are at the end.

---

## 1. One standard role per department — no ladder inside a department

> "Each department must have exactly one standard departmental role, and there
> must be no higher- or lower-grade staff role inside that department."

### What was wrong

The role catalogue had grown one role per *job title*, not one per *department*,
which produced two parallel ladders:

| Department | The ladder that existed | Now |
| --- | --- | --- |
| Bar | `BAR_MANAGER` → `BARTENDER` | `BARTENDER` |
| Restaurant | `RESTAURANT_MANAGER` → `WAITER` | `WAITER` |
| Kitchen | `KITCHEN_MANAGER` → `CHEF` / `KITCHEN_ASSISTANT` | `CHEF` |
| Housekeeping | `HOUSEKEEPING_MANAGER` → `HOUSEKEEPER` / `HOUSEKEEPING` | `HOUSEKEEPING` |
| Front desk | `FRONT_DESK_SUPERVISOR` → `RECEPTIONIST` | `RECEPTIONIST` |
| Accounts | `ACCOUNTS_MANAGER` → `ACCOUNTANT` | `ACCOUNTANT` |
| Stores | `INVENTORY_CLERK` (a second name for `STOREKEEPER`) | `STOREKEEPER` |
| Human resources | `HR_MANAGER` → *(no worker role existed at all)* | `HR_OFFICER` (new) |
| Maintenance | `MAINTENANCE_TECHNICIAN` → `MAINTENANCE` | `MAINTENANCE` |

Two people doing the same work in the same room held two different permission
surfaces purely because one of them had "manager" in their title.

### What changed

* **`backend/apps/accounts/departments.py`** is the single source of truth:
  `DEPARTMENT_ROLES` (one role per department, 14 departments),
  `RETIRED_DEPARTMENTAL_ROLES` (the historical titles and what they resolve to),
  `canonical_role()`, `department_for_role()`, `department_for_user()`,
  `is_cross_department()`, and `assert_assignable_role()`.
* **`HR_OFFICER`** is added as the HR department's one role. It carries the HR
  *work* — staff records, shifts, attendance administration, leave requests,
  payroll preparation — and none of the sensitive approvals. Approving leave,
  payroll or an expense stays a maker-checker decision granted **per person**.
* **`CASHIER` is now a department** in its own right (`Workstation.Department`),
  so the till function is a department rather than a supervisory rung inside the
  front desk. Adding it is what lets the one-role-per-department rule hold for
  both roles at once, with no new permissions for anybody.
* **Both write paths refuse retired titles.** `AdminUserCreateSerializer` and
  `AdminUserUpdateSerializer` call `assert_assignable_role()`, and the Django
  admin's user forms do the same (`apps/accounts/admin.py`) — they no longer
  even list a retired title in the role select. If only the API enforced the
  rule, an administrator could recreate the hierarchy from the admin and the
  rule would be theatre.
* **A request body can never choose a department.** `department` on a staff
  account must be a configured department and must agree with the role; the
  account's department is otherwise read from stored records only
  (`User.department`), never from a payload or URL.
* **Migration `accounts.0027_normalize_department_roles`** moves every existing
  account onto its department's role, and for each capability the person held
  only through the retired title it writes an explicit, reasoned
  `UserCapabilityOverride` GRANT first — so **nobody loses access to work they
  can already do**, and each extra right becomes individually visible and
  revocable. Each move is written to the append-only audit trail
  (`STAFF_DEPARTMENT_ROLE_NORMALIZED`, with `preserved_capabilities` and a flag
  on the sensitive ones). The migration is idempotent, and its reverse restores
  the stored title from that audit trail.
* **Grants stay exactly what is on record.** `capability_codes_for_user()` was
  deliberately *not* changed to union a retired title with its department role:
  an authorization decision must be traceable to persisted rows, and a silently
  wider grant is the kind of surprise least privilege exists to prevent. The
  migration preserves capabilities explicitly (as reviewed, reasoned overrides)
  instead of hiding them behind a runtime rule.

### What deliberately did **not** change

* `ADMIN` and `GENERAL_MANAGER` are **global oversight** roles, not rungs on a
  departmental ladder, and their policy is untouched.
* No stored `User.Role` value was renamed or deleted, so historical audit rows,
  migrations and API consumers stay valid.
* Nothing here grants one department another department's capabilities. One role
  per department is emphatically not "everybody may do everything".

---

## 2. "Sign in from any device" is now an authority, not a job title

Management used to be exempt from the front-desk-desktop sign-in rule through a
hard-coded role list. With titles collapsing, that freedom had to be expressed as
what it is: **the `sign_in.any_device` capability** (migration
`accounts.0028_sign_in_any_device_capability`).

* Seeded to exactly the three roles that were already exempt
  (`ADMIN`, `MANAGER`, `GENERAL_MANAGER`) — nobody gains or loses freedom.
* Anyone else receives it only as an explicit, reasoned, audited
  `UserCapabilityOverride` — never by being handed a bigger-sounding title.
* `apps.accounts.desktop_policy.may_sign_in_anywhere()` is the one predicate,
  and `attendance_fallback_service` delegates to it, so "who counts as
  management" cannot drift between signing in and recording attendance.

A manager signs in with **email + password only** — no attendance code, no
workstation key, no fingerprint — and lands on the manager screens. Cashiers and
other operators still sign in at the front desk, exactly as before.

---

## 3. Restaurant / bar menu: cost, margin and a one-click "sold out"

The menu-management API and screen now answer the question an owner actually
asks: *what does this plate cost me, and am I selling it at a loss?*

* `GET /api/admin/pos/menu/items/?with_margin=true` adds a `margin` object per
  row: `unit_cost`, `cost_basis` (`RECIPE` / `STOCK_ITEM`), `margin_amount`,
  `margin_percent`, `below_cost`, `comparable`, and a plain-English `note`.
* The number comes from `menu_item_cost_basis()` — **the same function the
  save-time price/cost guard enforces** — so the figure on screen and the rule on
  save can never drift apart. When no cost is on record, or the cost is in
  another currency, the API says so (`comparable: false`) instead of inventing a
  margin.
* It is computed **only when asked**, because the cost basis reads recipes and
  goods-receipt averages per row; a bounded page an operator is reading is worth
  that, and every other consumer of the catalogue is not made to pay for it.
* The screen shows `cost ₦300.00 · +57.14%` under the price, in red when the
  price is below cost.
* **"Mark sold out" / "Mark available"** is a one-click partial update
  (`PATCH {is_available}`) that cannot smuggle a price change past the cost
  guard, and never touches past orders (order lines stay frozen snapshots).
* Search (`≤100` chars), `service_area`, `category`, `is_active`,
  `is_available`, and a whitelisted `ordering` are all validated server-side; an
  unknown ordering key is a `400`, not an ORM call. A department manager sees
  only their own service areas and gets a `403` if they ask for another.

---

## 4. Defect fixed: guest receipts were failing outright on Django 6

Not asked for — found while running the full suite, and it was breaking
production behaviour.

The SMTP provider nested the inline logo by setting Django's undocumented
`EmailMessage.mixed_subtype` attribute. **Django 6 removed it**, so
`message()` raised `AttributeError` the moment anything was attached: every
"Send receipt" returned `502 EMAIL_DELIVERY_FAILED`, and every styled
notification (booking confirmation, portal invitation, password reset, checkout
review request) failed with it — 25 baseline tests were failing for this one
reason.

`apps/notifications/providers.py` now builds the RFC-correct tree itself in
`_InlineImageMessage`, instead of depending on a removed private attribute:

```
multipart/mixed                (only when a real attachment exists)
└── multipart/related
    ├── multipart/alternative
    │   ├── text/plain
    │   └── text/html
    └── image/*  Content-ID: <logo>, disposition inline
```

Header injection is blocked at the same time (CR/LF in subject or address is
stripped). `tests/test_email_mime_structure.py` pins the tree, so a future
upgrade cannot break delivery silently again.

---

## 5. Deployment hygiene brought up to date

* **Asset version stamping:** the repository carried five different `?v=` values
  across its 72 pages while `version.json` still said `1.1.23`, so some pages
  would keep serving a stale `api.js`/`config.js` after a deploy. Everything is
  now stamped `?v=1.1.27` and `version.json` agrees.
* **Two obsolete artefacts removed** — `frontend/worker/index.js` (an abandoned
  same-origin Cloudflare Worker proxy that `wrangler.jsonc` no longer referenced)
  and `frontend/manifest.webmanifest` (the old root-scope PWA manifest that would
  have made the *public* site installable next to the staff app). Both were
  required to be gone by the project's own deployment tests.
* **`docs/ROLE_PERMISSION_MATRIX.md` + `role_permission_matrix.json`
  regenerated** from the live URL resolver and capability catalogue (28 roles,
  321 endpoints, 56 capabilities; the three unmapped capability codes are now
  classified).
* `HR_OFFICER` is wired through `frontend/js/auth.js`, `dashboard.js`
  `PAGE_ACCESS`, `config.js` and the four self-gating pages, so the new HR role
  lands on the workforce page and can open the people surfaces it is supposed to,
  and no others.

---

## Verification

```bash
cd backend
python manage.py test                                    # 1734 tests, OK (17 skipped), 0 failures
python manage.py check                                   # no issues
python manage.py makemigrations --check --dry-run        # No changes detected
python manage.py test tests.test_department_roles        # 16 tests OK
python manage.py test tests.test_pos_menu_management     # 8 tests OK
python manage.py test tests.test_email_mime_structure    # 6 tests OK
python manage.py test tests.test_pos_unopened_returns    # 19 tests OK
python manage.py test tests.test_receipt_email tests.test_email_notices \
  tests.test_portal_invitations tests.test_guest_access_links \
  tests.test_email_branding_and_portal_links tests.test_synchronous_email \
  tests.test_reviews                                     # 127 tests OK

cd ../frontend
python validate.py                                       # 72 pages, 31 JS files, ALL OK
node --test tests-js/                                    # 593 tests, 0 failures
```

`node --test tests-js/` was **590 pass / 3 fail** before this work (unstamped
asset versions, the leftover worker proxy, the root manifest). It is now
**593 pass / 0 fail**.

The full backend suite was **1690 tests / 25 failures** on the untouched
checkout (all 25 in the email paths, plus one that needed `mysqlclient`, which
production installs and the sandbox did not). It is now **1734 tests, 0
failures, 17 skipped**.

### Applying it

```bash
cd backend
python manage.py migrate accounts    # 0026 schema, 0027 role normalisation, 0028 sign-in capability
python manage.py migrate pos         # 0008 return ledger columns
python manage.py migrate inventory   # 0012 POS_RETURN movement type
```

Run the `accounts` migrations during a quiet moment: `0027` rewrites `role` for
every account still carrying a retired title and writes one audit row per
account. It preserves every capability those accounts held (as explicit
overrides) and can be reversed.
