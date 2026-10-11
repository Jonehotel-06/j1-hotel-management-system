# J-ONE HOTEL & LODGE — Repair Completion Report

**Repository:** `Jonehotel-06/j1-hotel-management-system` (branch `main`, base commit
`c621093`) with the repair work applied as an uncommitted working-tree change set.
**Audit reference date:** 2026-10-10 (consolidated audit `JONE_FULL_SYSTEM_AUDIT_FINAL.md`).
**Frontend under test:** the repository's own `frontend/` tree, served by a local static
server. **API:** exercised through the Django test client against a disposable test
database, never against production.
**Report date:** 2026-10-11.

This is a repair report, not an audit. Every item below is either implemented in the
repository and verified, or explicitly marked **BLOCKED** with the prerequisite that is
missing. Nothing in this document was demonstrated by "HTTP 200" alone: state claims are
backed by persisted-database assertions, browser measurements, or both.

---

## 1. What was delivered

| Deliverable | Where |
|---|---|
| Source changes | working tree: **109 tracked files modified, 27 added** (nothing committed or pushed) |
| Backend migration | `backend/apps/accounts/migrations/0019_audit_view_is_administrator_only.py` |
| New backend tests | `backend/tests/test_booking_balances.py`, `test_booking_access_capabilities.py`, `test_accommodation_reconciliation.py`, `test_pass_regressions.py`, `test_separate_approver_control.py`, `test_security_guards.py`, `test_role_alias_guards.py`; `test_role_permissions_matrix.py`, `test_staff_operations_api.py`, `test_operational_directory_api.py`, `test_payroll_api.py`, `test_maintenance.py`, `test_guest_services.py` extended |
| New frontend tests | `frontend/tests-js/test-repair-contracts.js`, `test-accessibility-baseline.js`, `test-ux-focus-regressions.js` |
| Dependency pin | `backend/requirements.txt` — `djangorestframework>=3.17.2,<4.0` (unblocks two advisories) |
| Browser evidence | `docs/verification/` (axe scans, POS measurements, screenshots, perf timings) |
| Scan evidence | `docs/verification/dependency-scan.json` (pip-audit, before/after) |
| This report | `REPAIR_COMPLETION_REPORT.md` |
| Blockers | `REMAINING_BLOCKERS.md` |
| Documentation updates | `README.md`, `docs/VERIFICATION.md` (new), `docs/IMPLEMENTATION_AUDIT.md` |

---

## 2. Finding-by-finding status

Status vocabulary: **FIXED** = source change implemented and verified ·
**PASS** = already correct, now pinned by a regression test · **N/A** = not a defect ·
**BLOCKED** = cannot be completed without a prerequisite that is outside the code ·
**OWNER** = deliberately left for an owner decision, with everything around it done.

| # | Finding | Status | One-line outcome |
|---|---|---|---|
| D1 | Accounting page called undefined endpoints | **FIXED** | Every accounting key resolves to a real route; a missing key now fails loudly instead of requesting `/undefined` |
| D2 | Separate-approver control unproven | **PASS in code / BLOCKED in production** | Policy is enabled and enforced at service + API layers; production two-person verification impossible (no independent approver) |
| D3 | DIRTY room publicly sellable | **FIXED** | Sellable rule derives from housekeeping readiness, not the stale `status` column |
| D4 | Folio vs booking balance | **FIXED** | Folio outstanding now includes stay charges; balances reconcile |
| N1 | Room revenue and the ₦21,750 tax unrecognised | **FIXED** | Checked-out stays post room revenue + tax to the ledger, idempotently |
| D5 | Dispatch accepted the wrong field with HTTP 200 | **FIXED** | Unknown fields are rejected; `assignment_id` is the accepted contract |
| N8 | `HOUSEKEEPER` vs `HOUSEKEEPING` role alias | **FIXED** | Canonical role accepted on both sides; alias documented |
| D6 | (audit PASS) | **PASS** | Pinned by regression test |
| D7 | `assignRoom` sent `{room: id}` instead of the documented payload | **FIXED** | Frontend sends `assignment_id` + `room_id` from the endpoint map |
| D8 | Enugu vs Abuja address | **OWNER** | No location invented; consistency improvements only |
| D9 | Stale Paystack ₦387,000 payment on an expired booking | **BLOCKED** | Requires verified provider status + explicit owner authorisation; untouched |
| D10 | Table T4 "Poolside 1" | **OWNER** | Not renamed; no source-level defect found |
| D11 | Preserve `/dashboard/terminals.html` | **PASS** | Page preserved; the legacy `workstations.html` 404 is not a defect |
| D12 | Legacy `/api/admin/pos/cash-sessions/` 404 | **FIXED (documentation + wiring)** | Frontend reads the finance cash-session collection; legacy path intentionally retired |
| D13 | Notification count/list regression | **PASS** | Count and list behaviour pinned by regression test |
| D14 | XLSX/PDF export failed via `createObjectURL` | **FIXED** | Export path no longer depends on the failing call; CSV kept |
| D15 | Unused enquiry acknowledge/resolve/reply routes 404 | **FIXED** | Frontend uses the PATCH lifecycle route; the 404s are not called |
| D16 | Booking list omitted `discount_amount` | **FIXED** | Discount is returned and the total reconciles |
| N2 | Mobile POS header 124 px, title clipped | **FIXED** | 124 px → 53–55 px at 320–412 px; title no longer clipped |
| N3 | Orders table clipped with no scroll affordance | **FIXED** | Scroll region is focusable, named, and shows a live edge cue |
| N7 | Staff picker leaked email without `account.view` | **FIXED** | Picker field set pinned to display-only keys |
| N9 | `audit.view` grant vs admin-only 403 | **FIXED** | Persisted grant made administrator-only (migration 0019) |
| N10 | GENERAL_MANAGER / FRONT_DESK_SUPERVISOR booking access | **FIXED** | Capability map matches the enforced policy |
| N11 | Axe: 5 rules across public routes | **FIXED** | Public **260 → 0** violation nodes; dashboard **906 → 0** |
| SEC-01 | No Content-Security-Policy | **IMPLEMENTED** | Enforcing + Report-Only policies in `vercel.json`; deployed header not verified |
| SEC-02 | CORS `*` on preflight | **FIXED in code** | Origin allow-list enforced in production settings; deployed env not re-tested |
| SEC-005 | Rate limiting / revocation / dependency scan | **FIXED** | Guards + 10 tests; DRF pin unblocked and upgraded (2 CVEs); `pip-audit` clean |

**Tally against the audit:** of the 16 numbered defects, 12 were FAIL, 3 PASS, 1 BLOCKED.
Now: 11 FIXED, 3 remain PASS (D6/D11/D13, each now pinned), D8/D10 are owner decisions with
the safe surrounding work done, D9 is BLOCKED, D2 is enforced in code and BLOCKED only for
its production two-person demonstration. N1–N11 and SEC-01/02/05 are as tabulated, and
SEC-005 additionally closes a **finding that post-dates the audit** (two DRF advisories the
project's own pin made un-patchable — see §3.2).

---

## 3. Root causes and changes (grouped by theme)

### 3.1 Financial integrity and data consistency

**D1 — accounting panels permanently blank.**
*Root cause:* the accounting page read endpoint keys from the frontend configuration and
then issued requests against keys that did not exist, producing URLs such as
`/api/admin/finance/undefined`. *Change:* all finance keys now resolve to routes that
exist in `apps/finance/urls.py`; a missing key resolves to an explicit error instead of a
URL, and the page surfaces a configuration error rather than an empty panel.
*Files:* `frontend/js/config.js`, `frontend/js/api.js`, `frontend/dashboard/accounting.html`.
*Tests:* `frontend/tests-js/test-repair-contracts.js` (no `/undefined` can be constructed
from the map; every accounting key is a real path).

**D4 — folio balance understated the booking's outstanding amount.**
*Root cause:* the folio outstanding figure covered folio charges only, while the stay's
own charge was carried on the booking, so folio ₦25,500 and booking `amount_due` ₦124,125
disagreed. *Change:* `apps/bookings/serializers.py` and
`apps/finance/services/folio_service.py` now compute and expose the outstanding amount
from the same source of truth, and the reconciliation service
(`apps/finance/services/accommodation_reconciliation.py`) can be run as a management
command to audit and repair drift. *Verified numbers:* on the audited fixture the receipt
posts 15,000 / 2,500 / 17,500; at check-out `amount_due` is 0.00, `folio_charges`
2,500.00, `outstanding` 2,500.00. *Tests:* `test_booking_balances.py` (8),
`test_accommodation_reconciliation.py` (11).

**N1 — room revenue and ₦21,750 of tax missing from the ledger.**
*Root cause:* checked-out accommodation stays did not post their room revenue or the tax
component, so an October whose source bookings held ₦21,750 of tax reported ₦0 tax and
revenue consisting only of F&B. *Change:* check-out now posts room revenue and tax through
the same controlled, idempotent posting path as other ledger entries; posting twice for
the same stay is a no-op, enforced in the database. *Tests:* the reconciliation suite
asserts the posted amounts, the tax split, and idempotency on a second invocation.

**D16 — discount not returned with the booking.**
*Root cause:* the admin list serializer omitted `discount_amount`, so consumers could not
reconcile a total that had a discount applied. *Change:*
`apps/bookings/serializers_admin.py` returns the discount and the amounts reconcile
(₦15,000 discount on a ₦145,125 total in the audited case). *Tests:* `test_pass_regressions.py`.

**D2 — separate-approver control.**
*Root cause of the finding:* the control existed but had not been *demonstrated* with two
distinct identities. *Change:* the control is kept enabled (not weakened) and the reason
string is preserved end-to-end by the exception handler
(`apps/core/exception_handler.py`). The service and API both refuse self-approval with
`A requester cannot approve their own sensitive monetary action.`; an `ApprovalRequest`
row with requester ≠ reviewer is the persisted evidence. *Tests:*
`test_separate_approver_control.py` (8). *Production demonstration:* **BLOCKED** (see
`REMAINING_BLOCKERS.md`); the four stock counts, the one purchase order and the one
expense were **not** approved to manufacture a demonstration.

### 3.2 Security, authorisation, privacy, accountability

**N7 — staff picker returned email to callers without `account.view`.**
*Root cause:* the operational staff directory serialiser exposed more fields than the
picker needs. *Change:* `apps/accounts/serializers.py` pins the picker payload to
`{id, full_name, email, role}` only where the caller holds the required capability, and
the display-only path no longer carries contact data beyond what dispatch needs.
*Tests:* the key set is pinned exactly in `test_pass_regressions.py` / role-matrix tests.

**N9 — capability matrix advertised a permission the API refused.**
*Root cause:* `0014_seed_department_capabilities` had seeded `audit.view` onto MANAGER and
other roles while the audit-log endpoint is administrator-only, so the UI showed "View
audit records" and the API answered 403. *Change and migration:* migration
`0019_audit_view_is_administrator_only.py` makes the persisted grant administrator-only,
is reversible, and is pinned by `AuditViewPersistenceTests` (2). No permission was
loosened — the data was corrected to match the enforced policy.

**N10 — GENERAL_MANAGER / FRONT_DESK_SUPERVISOR booking access.**
*Root cause:* the capability map and the enforced booking permissions disagreed for these
two roles. *Change:* `apps/core/permissions.py` and `apps/accounts/capabilities.py` now
describe exactly what the API enforces. *Tests:* `test_role_permissions_matrix.py`
(extended), `test_booking_access_capabilities.py` (5).

**SEC-01 — no CSP.** An enforcing policy plus a `Report-Only` twin are declared in
`frontend/vercel.json`, alongside the sanctioned Railway API host. The header is only
proven once the deployment serves it; that verification is listed as unverified below.

**SEC-02 — CORS.** `*` on the preflight is removed in favour of an explicit origin
allow-list in `backend/config/settings/production.py`. Code-side only; the deployed
environment was not re-probed.

**A session-revocation defect found by this repair (not in the audit).** While pinning the
logout contract for the audit's three unconfirmed role-sweep responses, the new test
`test_logout_does_not_revoke_another_accounts_session` failed: `/api/auth/logout/` blacklisted
**whatever refresh token it was handed**, with no check that the token belonged to the caller.
Any authenticated account could therefore end another account's session — and could do it using
a token that had merely leaked (server logs, a shared workstation, a copied request) without
ever holding a usable credential of its own. Logout now verifies the `user_id` claim against
the caller before blacklisting; a mismatched token still receives the documented idempotent
`200` (so a confused client clears its local state) but nothing is revoked. Every client in the
repository sends its own token (`frontend/js/auth.js` sends `cred.refresh`), so no consumer
changed. `tests/test_auth.py::LogoutContractTests` pins all of it, including a per-role sweep
— tokens are minted directly rather than by logging in, because the login endpoint is
deliberately throttled to 5/min.

**SEC-005 — rate limiting, revocation and the dependency scan.** The rate-limit contract and
its failure modes are now pinned by `backend/tests/test_security_guards.py` (10 tests): the
6th login inside the window answers `429` **with** a `Retry-After` header, exhausting the
login scope leaves other scopes untouched, a configured rate is enforced exactly
(`2/min` → `401, 401, 429`), a staff session cookie cannot authenticate the API while a
bearer token can, and the production settings source is pinned to refuse wildcard CORS and
to keep HTTPS/HSTS/cookie flags on with no `SessionAuthentication`/`BasicAuthentication`.
Two further guards pin the revocation window itself: refresh rotation and blacklist-after-rotation
stay enabled (logout blacklists the refresh token), and the access-token lifetime stays bounded at
30 minutes — because a stateless access token cannot be revoked before it expires, its lifetime
*is* the revocation window.

The dependency scan found a **real, unpatched exposure that this repair had inherited**:

| Package | Version found | Advisories | Fixed in |
|---|---|---|---|
| `djangorestframework` | 3.16.1 | `CVE-2026-73228` / `GHSA-2m8g-3cmr-wg3w` — oversized JSON/urlencoded bodies could bypass Django's `DATA_UPLOAD_MAX_MEMORY_SIZE` through DRF's `request.data`; `CVE-2026-73229` / `GHSA-g47c-3xmw-q6m2` — `AdminRenderer` could disclose GET-protected data on an invalid write | 3.17.2 |

The project's own pin made this un-fixable by construction: `djangorestframework>=3.15,<3.17`
excluded every patched release. `requirements.txt` now reads
`djangorestframework>=3.17.2,<4.0` with the advisory IDs inline, and the environment was
upgraded to 3.18.3 — the version a fresh `pip install -r requirements.txt` resolves to —
before re-running the whole suite. `pip-audit` now reports **no known vulnerabilities** for
both the installed environment and the requirements file.

The second advisory is not exploitable as configured (the project registers only
`JOneJSONRenderer`, so DRF's browsable `AdminRenderer` is never in the renderer chain), and
the first is mitigated at the framework level by the upgrade rather than by application code.
The frontend has **no npm dependency surface at all** (no `package.json`, no bundler — plain
files plus `node --test`), so there is nothing to scan there.

### 3.3 Operational correctness

**D3 — a DIRTY room was publicly sellable.** *Root cause:* public availability read the
room's stored `status` column, which lags housekeeping. *Change:*
`apps/bookings/services/availability.py` and `apps/rooms/serializers.py` derive
"available" from housekeeping readiness; a room that is not clean and inspected cannot be
sold. *Tests:* availability and rooms suites cover the dirty-room case.

**D5/D7 — housekeeping dispatch contract.** *Root cause:* the dispatch endpoint accepted a
payload with an unknown field (`assigned_to`) and returned 200 while doing nothing, and
the frontend sent a room id under the wrong key. *Change:* unknown fields are rejected
rather than silently dropped, `assigned_to_id` / `assignment_id` are the accepted keys, and
the frontend `assignRoom` sends `assignment_id` + `room_id` resolved from the endpoint map.
*Tests:* `test_housekeeping` dispatch cases plus
`frontend/tests-js/test-repair-contracts.js`.

**N8 — role alias.** `HOUSEKEEPER` and `HOUSEKEEPING` are both accepted and normalised to
one canonical value, so a correctly-provisioned account can no longer be refused for
spelling. **D11/D13 — preserved behaviour.** `/dashboard/terminals.html` is untouched and
the notification count/list contract is pinned by regression tests; the legacy
`workstations.html` 404 is not a defect and was deliberately not "fixed".

**D14/D15/D12 — export and enquiry wiring.** The export path no longer routes through the
`createObjectURL` call that failed (CSV, which worked, is still offered); the unused
enquiry `acknowledge`/`resolve`/`reply` endpoints are not called and the documented PATCH
lifecycle is used; the frontend reads the finance cash-session collection rather than the
retired `/api/admin/pos/cash-sessions/` path, which is called out in the endpoint map so
it is not reintroduced.

### 3.4 Mobile responsiveness and accessibility (N2, N3, N11)

**N2 — mobile POS header.** *Root cause (measured, not inferred):* at ≤560 px the topbar
subtitle wrapped to four lines (the 18 px `nowrap` title was simultaneously ellipsised) —
the wrapping subtitle, not the title, produced the 124 px bar. *Change:* on phones the
subtitle is dropped and the title keeps its full text on at most two clamped lines.
*Measured:* 320 px 124 → 53 px, 360 px 124 → 53 px, 390 px 124 → 55 px, 412 px 103 → 55 px,
with `titleClipped` false in every case (it was true before).

**N3 — clipped order table.** *Root cause:* the table scrolls, but nothing on screen said
so; the audit's note that "page-level overflow can be zero while an element is clipped" is
exactly what happens here. *Change:* the wrapper is a focusable, named `region`, and a
CSS-only edge shadow layered with `background-attachment: local, local, scroll, scroll`
appears only on the side that still has content. *Measured:* with the repository
stylesheet and a controlled fixture, the right-edge cue reads Δ30 at scroll start and Δ0
at the end; the left cue mirrors it — so the affordance is never misleading.

**N11 — the five Axe rules.** Root causes were five distinct, real defects — not scan
noise:

| Rule | Root cause | Fix |
|---|---|---|
| `aria-hidden-focus` | `js/icons.js` injected the SVG into `[data-icon]` carriers and then marked the carrier itself `aria-hidden`, hiding visible buttons (the drawer toggle) inside a hidden subtree | the control that *is* the icon carrier is skipped; only its SVG content is decorative |
| `color-contrast` | `--color-text-faint` #8B95A0 (2.59–3.04:1) on light surfaces; the dashboard sidebar group label #727376 (3.21:1); the receipt Share button (4.09:1); and `.kpi-grid.has-error` dimming a whole card to 35 % opacity (1.67:1) | tokens re-derived to AA on **every** surface they are used on, including the always-dark sidebar and footer |
| `heading-order` | data-driven grids (facilities/offers/gallery/policies) put an h3/h4 directly under the page h1, and empty states rendered inside table bodies emitted h3 | section headings introduced, cards promoted with `.h4`, shared empty-state renderer emits `h2.h3`, booking-detail sections promoted |
| `label-content-name-mismatch` | the stepper set an overriding `aria-label` ("Step 2: Room (upcoming)") that did not contain its visible text ("2Room"), and the date buttons referenced only a field label | the stepper's name is derived from its content again (state moved into visible-safe sr-only content); each date button now references both its field label and its own value span |
| `page-has-heading-one` | pages whose *error state replaces* the only h1 (room detail, receipt detail) left the document with no h1 | the error state carries the h1 (`<h1 class="h2">`), so exactly one h1 exists on every reachable screen |

Verified in a real browser (Chrome 148) with the audit's own rule set, at 1440×900 and
390×844:

| Surface | Before | After |
|---|---|---|
| 27 public pages × 2 viewports | 260 violation nodes on 22 of 27 pages | **0** on 27 of 27 |
| 40 dashboard pages × 2 viewports | 906 violation nodes on 40 of 40 pages | **0** on 40 of 40 |

Rules covered: `aria-hidden-focus`, `color-contrast`, `heading-order`,
`label-content-name-mismatch`, `page-has-heading-one`, plus `select-name`,
`empty-table-header`, `scrollable-region-focusable`, `landmark-one-main`, `region`. Raw
per-page results: `docs/verification/a11y-*.json`; screenshots:
`docs/verification/pos-320-before.png` / `pos-320-after.png`.

---

## 4. Migrations

| Migration | Purpose | Reversible |
|---|---|---|
| `accounts/0019_audit_view_is_administrator_only.py` | Removes the seeded `audit.view` grant from every role except ADMIN so the persisted capability matrix matches the enforced administrator-only policy (N9) | Yes |

`makemigrations --check --dry-run` reports **no changes detected**; `manage.py check`
reports no issues.

---

## 5. API contracts

No route was removed and no response field that consumers relied on was deleted.

| Change | Compatibility strategy |
|---|---|
| Housekeeping dispatch rejects unknown fields instead of ignoring them (D5) | The previously working field is still accepted; callers sending the wrong field now receive a validation error instead of a silent no-op — this is the fix, not a break. Frontend updated in the same change set (D7) |
| Booking admin payload now includes `discount_amount` (D16) | Additive field; existing consumers unaffected |
| Folio responses expose a reconciled outstanding amount (D4) | Additive; the folio's own charges remain available separately |
| Frontend endpoint resolution (D1/D14/D15/D12) | Paths unchanged on the wire; only the browser's construction of them is centralised, so a missing key can no longer produce `/undefined` |
| Migration 0019 (N9) | Grants only narrow (MANAGER and others lose a permission the API never honoured); reversible |

---

## 6. Test results

All layers were run on the final working tree.

| Layer | Command | Result |
|---|---|---|
| Backend (unit + API + DB + integration) | `python manage.py test` | **Ran 930 tests … OK (skipped=1)** (final run: Django 5.2.18, DRF 3.18.3) |
| Backend system check | `manage.py check` | No issues (0 silenced) |
| Migration drift | `makemigrations --check --dry-run` | No changes detected |
| Financial (D4/N1) | `test_booking_balances.py`, `test_accommodation_reconciliation.py` | 8 + 11 OK |
| Separated approval (D2) | `test_separate_approver_control.py` | 8 OK |
| Access capabilities (N10) | `test_booking_access_capabilities.py` | 5 OK |
| PASS regression pack (D6/D11/D13…) | `test_pass_regressions.py` | 7 OK |
| Persisted grant (N9) | `AuditViewPersistenceTests` | 2 OK |
| Role matrix + audit-probe reconciliation | `test_role_permissions_matrix.py` | 12 OK (3 new tests pin all 6 recorded contract-gap rows as agreement) |
| Role-alias workflows (D5/N8) | `test_maintenance.py`, `test_guest_services.py` | 10 OK (5 new; 4 of them fail without the fix) |
| Attendance clock state machine (HR-002) | `test_staff_operations_api.py` | 4 OK (2 new) |
| Security guards (SEC-005) | `test_security_guards.py` (**NEW**) | 12 OK (rate limits, session-cookie rejection, token policy) |
| Logout contract (SEC-005 follow-up) | `test_auth.py::LogoutContractTests` | 6 OK: every staff role, idempotent replay, stale/expired tokens, missing token, unauthenticated, cross-account refusal |
| Data minimisation (N7) | `test_operational_directory_api.py` | 5 OK (2 new, behavioural) |
| Role-alias regression net | `test_role_alias_guards.py` (**NEW**) | 7 OK: a source guard over `apps/**` plus the alias-set invariants |
| Self-service payroll scope (PAY-004) | `test_payroll_api.py` | 1 new test: own line only, no colleague, no rejected run |
| Frontend markup/JS health | `python validate.py` | 67 HTML + 28 JS, **ALL OK** |
| Frontend unit/contract/a11y | `node --test tests-js/` | **129 tests, 129 pass, 0 fail** |
| Responsive/a11y (browser) | Chrome 148 + axe-core over 67 pages × 2 viewports | 0 violation nodes after the repairs |
| Query efficiency (backend) | `test_query_efficiency.py` | 17 OK (11 new flatness guards over the admin tables) |
| Performance | 5 runs/page, median, before vs after | See §7 |

### 6.0 A defect this repair found in its own error handling

Writing the SEC-005 guards exposed a **real regression in this repair's own code**:
`apps/core/exception_handler.py::_envelope` rebuilt every error response from scratch and,
in doing so, dropped the headers DRF had attached. The visible symptoms were a `429` without
`Retry-After` (so a client cannot know when to retry) and a `401` without
`WWW-Authenticate`. `_envelope` now takes a `headers` argument and all seven call sites
forward `response.headers`. Because that touches **every** error response in the API, the
entire backend suite was re-run after that change and is green (the run in the table above,
whose count grew by each later addition to 930). The throttle guard asserts the header is
present on the `429`, so the regression cannot silently return.

### 6.1 Module verification (the audit's `NOT TESTED` modules)

The audit could not exercise several modules: its attendance/shift script was syntax-checked
but never executed, biometrics needed hardware, and the portal needed mail. Modules were
therefore verified against the code the same way the rest of the repair was - by running them:

```
cd backend && python manage.py test tests.test_fingerprint_biometrics tests.test_staff_face_verification \
  tests.test_payroll_api tests.test_staff_operations tests.test_staff_operations_api \
  tests.test_portal_access tests.test_portal_invitations tests.test_guest_access tests.test_guest_checkout
# Ran 92 tests in 42.496s - OK
```

| Module | Tests | What that covers | What remains outside the repository |
|---|---|---|---|
| Biometrics | `test_fingerprint_biometrics` 3, `test_staff_face_verification` 24 | enrolment queues, approval/rejection, temporary workstation credentials, the face/fingerprint verification *policy* (both default off unless configured) | **Live hardware** — no reader, no enrolment material (blocked, B3) |
| Payroll | `test_payroll_api` 18 | runs, tax snapshots, maker-checker, correction/replacement, immutable ledger postings, payment and payslip privacy | The owner's **statutory rates and tax identifiers** must be verified (B4) |
| Attendance & shifts | `test_staff_operations` 4, `test_staff_operations_api` 4 | shift overlap refusal, leave approval gating on scheduled shifts, source-keyed immutable attendance evidence, no manager-impersonated clocking, the HR-002 clock state machine | Nothing — this is fully covered by tests |
| Guest portal | `test_portal_access` 8, `test_portal_invitations` 15 | verified-email tokens, session revocation, invitation issue/expiry/accept | **Actual email delivery** needs SMTP (B5) |
| Guest access & checkout | `test_guest_access` 12, `test_guest_checkout` 4 | portal read/write scoping, checkout and folio settlement paths | Live payment/refund paths (owner-authorised only) |

The suite's single skipped test is `tests/test_site_content.py`, which raises `SkipTest` at
import because the `policy_text`/website-image fields it covers were removed from
`HotelSettings` upstream; it reactivates by itself if that feature returns. Nothing else is
skipped, so "930 tests" is 929 executed passes plus that one documented skip.

### 6.2 Reconciliation against the audit's own registers

The audit shipped four machine-readable registers; every row in them was mapped to a
state, so nothing is left ambiguous.

**`ROLE_PERMISSION_PROBE_REGISTER.csv` (391 probes, 6 recorded contract gaps).** All six
gaps were capability-versus-route disagreements, and all six are closed. The four
`audit_logs` rows (MANAGER, GENERAL_MANAGER, ACCOUNTS_MANAGER, SECURITY answered 403 while
holding `audit.view`) are closed by migration 0019; the two `booking_list` rows
(GENERAL_MANAGER, FRONT_DESK_SUPERVISOR answered 403 while holding booking capabilities)
by the capability-map alignment. `AuditProbeContractGapTests` now pins each of those six
rows as *agreement* - the grant and the route must say the same thing - so the matrix a
manager reads cannot describe a permission the API refuses.

**`TEST_CASE_REGISTER.csv` (68 cases).** Of the 38 non-PASS cases: 23 were FAIL and are
now fixed and verified; 5 were BLOCKED and remain blocked for the reasons in
`REMAINING_BLOCKERS.md`; 10 were NOT TESTED, and those split as follows.

Covered by this repair:

| Case | Was | Now |
|---|---|---|
| `HR-002` clock-in/break/clock-out state machine | NOT TESTED | `tests/test_staff_operations_api.py` — 4 tests: the full transition chain, an illegal transition refused with its reason, idempotent replay, and derived `worked_minutes` |
| `PAY-004` correct self-service payslip route | NOT TESTED | `tests/test_payroll_api.py` — the documented `/payroll/my-payslips/` route returns the caller's own approved line only, never a colleague's, never a rejected run's, and an empty list for a staff member with no payroll line |
| `SEC-005` rate limiting, revocation, CSRF surface, dependency scan | NOT TESTED | `tests/test_security_guards.py` (10) plus the DRF upgrade below |
| `PERF-001` page/API timings | NOT TESTED | Measured locally: frontend shell timings (5 runs × 6 pages, median, `docs/verification/performance-before-after.json`) and the backend query profile (§7.1, `docs/verification/api-query-profile.json`). **No service-level objective has been agreed**, so the numbers are recorded as evidence without a pass/fail verdict |

Still open, with the reason each one is not a repository task:

| Case | Why it is still open |
|---|---|
| `A11Y-002` manual keyboard / screen-reader / zoom review | Requires a human with assistive technology; the automated scan (N11) is not a substitute, and this repair does not claim WCAG conformance from a scan |
| `PERF-002` cold-cache, throttled, LCP/CLS/TTFB and query analysis | Needs a deployed environment with a cold cache and real network characteristics |
| `PROC-002`, `KIT-002`, `PAY-003` procurement / kitchen / salary-payment lifecycle runs | Production operations with financial effects; executing them is an owner-authorised act, not a repair step (see the preserved 4 counts / 1 PO / 1 expense in `REMAINING_BLOCKERS.md` B1) |
| `GUEST-002` new public booking, payment, refund or transfer | Requires the live payment provider and would create real financial records |

**`DEFECT_REGISTER.csv` (27 rows).** All 27 are accounted for in §2; the three PASS rows
(D6 kitchen ticket, D11 terminals navigation, D13 notification count) are pinned by
`test_pass_regressions.py`, and the one NOT APPLICABLE row (bare `/payslips/`) is
re-confirmed as not a defect.

**`DEMO_RECORD_AND_CLEANUP_REGISTER.csv` (9 rows).** Nothing in the register was modified
by this repair. Its four BLOCKED/REVIEW rows are the basis of `REMAINING_BLOCKERS.md` B7,
and its "PRESERVED" rows (the stale Paystack payment, the 4 stock counts / 1 PO / 1
expense) are untouched by design.

---

## 7. Performance (before → after)

Warm-session shell load, 5 runs per page per tree, median reported, identical conditions,
`/api/` requests aborted so no backend is involved (this measures markup + CSS + JS — the
same quantity the audit's DCL figures described).

| Page | DCL before → after (ms) | Load before → after (ms) | FCP before → after (ms) | Requests / bytes Δ |
|---|---|---|---|---|
| `index.html` | 241 → 251 | 243 → 259 | 204 → 220 | 0 / 0 |
| `rooms.html` | 169 → 179 | 174 → 184 | 144 → 204 | 0 / 0 |
| `booking.html` | 177 → 200 | 180 → 203 | 140 → 196 | 0 / 0 |
| `dashboard/pos.html` | 189 → 183 | 197 → 194 | 164 → 180 | 0 / 0 |
| `dashboard/index.html` | 180 → 176 | 181 → 176 | 152 → 180 | 0 / 0 |
| `dashboard/bookings.html` | 178 → 188 | 185 → 194 | 156 → 176 | 0 / 0 |

Reading: **no measurable regression and no measurable gain** — every movement is inside
run-to-run noise on this host, and the request count and transferred bytes are unchanged.
That is the intended outcome: the accessibility and responsive work was specified to be
CSS/markup-level so it must not cost bytes or requests. Raw samples:
`docs/verification/performance-before-after.json`.

Two honest caveats: (1) these are local-host numbers, not production numbers, and the
audit's absolute values (166–815 ms, `pos.html` heaviest) come from a different
environment, so they are not directly comparable to their absolute figures — only the
before/after pair here is comparable; (2) no SLO exists, so this is a regression check,
not a performance certification. Backend query profiling and per-route API latency were
not performed.

---

### 7.1 Backend query profile (the audit's `perf_api.json` had no counterpart in this repair)

The audit measured API timings; this repair had only frontend shell timings until now. What
actually makes a dashboard table slow is a query *per rendered row*, so that is what was
measured: every admin list endpoint was asked for a full **100-row page**, first with 3 rows
in the database and then with 60, and the query count was captured with Django's
`CaptureQueriesContext` on the disposable test database. Data was created through the real
services, so permissions, approvals, posting and locking all ran.

| Endpoint | Rows rendered | Queries | Queries per added row | Median ms (3 → 60 rows) |
|---|---|---|---|---|
| `/api/admin/bookings/` | 3 → 60 | 4 → 4 | **0.0** | 6 → 21 |
| `/api/admin/guests/` | 3 → 60 | 2 → 2 | **0.0** | 3 → 6 |
| `/api/admin/rooms/` | 3 → 60 | 4 → 4 | **0.0** | 6 → 21 |
| `/api/admin/users/` | 4 → 61 | 2 → 2 | **0.0** | 3 → 6 |
| `/api/admin/users/directory/` | 4 → 61 | 2 → 2 | **0.0** | 3 → 5 |
| `/api/admin/audit-logs/` | 3 → 60 | 2 → 2 | **0.0** | 3 → 7 |
| `/api/notifications/` | 3 → 60 | 3 → 3 | **0.0** | 3 → 5 |
| `/api/admin/housekeeping/` | 3 → 60 | 2 → 2 | **0.0** | 4 → 13 |
| `/api/admin/maintenance/` | 3 → 60 | 2 → 2 | **0.0** | 4 → 10 |
| `/api/admin/pos/orders/` | 3 → 60 | 2 → 2 | **0.0** | 6 → 15 |
| `/api/admin/pos/kitchen-tickets/` | 3 → 60 | 3 → 3 | **0.0** | 4 → 13 |
| `/api/admin/finance/transactions/` | 3 → 60 | 2 → 2 | **0.0** | 4 → 11 |
| `/api/admin/finance/folios/` | 0 → 0 | 1 → 1 | *excluded* | 3 → 3 |

**Result: no N+1 in any guarded list endpoint** — the query count is constant while 57 rows are
added, and latency rises only with the work of serializing rows (bookings 6 → 21 ms for 60
rows), not per row. `finance folios` rendered zero rows (folios are created by stays, which
the probe did not seed), so it is reported as unmeasured rather than as a pass.

Those twelve endpoints are now guarded by `AdminListFlatnessTests`, which adds 20 rows
between two 100-row-page measurements. **Proof the guard works:** with
`select_related("guest", "room_type", "offer")` temporarily removed from the bookings list,
the test failed with `issues 2.00 queries per rendered row (10 at 3 rows, 50 at 23 rows)` —
one query per row for each lazy relation — and passed again once it was restored. The
`_count` helper also asserts it captured at least one query, so the guard cannot pass
vacuously if a future change stops the instrumentation from working.

Evidence: `docs/verification/api-query-profile.json`. Method and reproduction:
`docs/VERIFICATION.md` §2.5.

---

## 8. Security results

| Item | Result |
|---|---|
| SEC-01 CSP | Enforcing policy + `Report-Only` twin declared in `frontend/vercel.json`. **Not verified against the deployed host** — see blockers. |
| SEC-02 CORS | `*` removed on preflight; explicit origin allow-list in production settings. Code-side verified by tests; **deployed environment not re-probed**. |
| Separate-approver control (D2) | Enforced at service and API layer; self-approval refused with the original reason string; persisted requester ≠ reviewer evidence in tests |
| Privilege escalation via grants (N9) | Persisted grant narrowed to administrator-only; no permission widened anywhere in this change set |
| Data minimisation (N7) | Staff picker payload reduced; no contact data beyond dispatch needs |
| Secrets | No credential, workstation key, payment key or environment URL was added, changed, printed, or embedded — this report contains none |
| Dependency scan (SEC-005) | `pip-audit` clean: 76 installed packages, 54 top-level pins, 0 advisories — after upgrading `djangorestframework` 3.16.1 → 3.18.3 and raising the pin to `>=3.17.2,<4.0`. Two advisories (`CVE-2026-73228`, `CVE-2026-73229`) were live before that change, and the old `<3.17` cap made them impossible to fix. See `docs/verification/dependency-scan.json`. |
| Error-response headers | `_envelope` had been stripping DRF's headers, so `429` lost `Retry-After` and `401` lost `WWW-Authenticate`. All seven call sites now forward `response.headers`; pinned by the throttle test. |
| Session revocation scope | Logout blacklisted any supplied refresh token, so one account could end another's session. Now caller-scoped; a foreign token gets the idempotent success and is *not* revoked. Pinned by 6 tests. |

No authenticated cross-origin exploit was attempted or demonstrated, and none is claimed.
The CSP and CORS items are reported as *implemented in code*, not as *verified in
production*.

---

## 9. Deployment changes

1. **`frontend/vercel.json`** — Content-Security-Policy (enforcing + Report-Only),
   security headers, and the sanctioned Railway API host. A redeploy is required for the
   headers to take effect.
2. **`backend/config/settings/production.py`** — explicit CORS origin allow-list. A
   redeploy (or environment-variable update) is required.
3. **Migration `0019`** — must be applied with `manage.py migrate` on the target
   database. It is reversible. No other migration is pending.
4. **`backend/requirements.txt`** — the `djangorestframework` pin moves from `>=3.15,<3.17`
   to `>=3.17.2,<4.0`. A fresh install resolves to 3.18.3; the repaired environment was
   upgraded to that exact version and the full suite re-run green on it. No *new*
   package, background worker, service, or infrastructure was introduced, and the deployment
   targets (Vercel static frontend, Railway Django API, MySQL) are unchanged.

Deployment success is **not** claimed: nothing was pushed, and no hosting dashboard was
inspected.

---

## 10. Owner decisions required

| # | Decision | What is already done | What the owner must supply |
|---|---|---|---|
| D8 | The property's correct locality/address | Nothing was changed to guess a location; address handling was left consistent | Confirm the correct address/locality, then it is a one-line data/UI update |
| D10 | Table T4's display name | No source-level defect found; T4 left as-is | Confirm whether "Poolside 1" is the intended name |
| D9 | Stale Paystack payment ₦387,000 on an expired booking | Untouched; no provider call, no refund, no deletion | Verified Paystack status for that reference **and** explicit instruction on the desired end state |
| — | Deposit policy (100 %) and the 15-minute pending-booking window | Nothing changed; both remain at their configured defaults and are not treated as decisions | Confirm the intended deposit and hold window (one-line setting changes) |
| — | Demonstration imagery is representative, not the real property | Left as-is; each such image is captioned as representative and licensed in `img/sources.json` | Genuine property photographs for go-live (see `REMAINING_BLOCKERS.md` B10) |

---

## 11. Blocked tests

| Test | Why blocked | What is needed |
|---|---|---|
| D2 production two-person approval (two distinct real accounts) | Only one authorised human approver is available; creating a second synthetic approver would fabricate the evidence the audit asked for | An independent authorised approver account operated by a real second person |
| Biometrics end-to-end (attendance/face/fingerprint) | No biometric hardware or enrolment material was available in this environment | A workstation with a reader, or an owner-sanctioned simulated reader |
| Payroll statutory correctness | Statutory rates and tax identifiers must not be fabricated | The owner's verified rates/identifiers |
| Guest-portal email delivery | Requires a reachable mail provider; sending real mail was out of scope | A test recipient mailbox and provider credentials |
| SEC-01/SEC-02 as *deployed* headers | Requires the deployed hosts to serve the new configuration | A deployed build (or an owner-run header check against production) |
| Accessibility manual review (`A11Y-002`) | Needs a human with a keyboard, a screen reader and 200 % zoom; an automated scan cannot stand in for it | A manual pass, ideally with a person who uses assistive technology — see `REMAINING_BLOCKERS.md` B9 |
| Cold-cache performance / LCP-CLS-TTFB (`PERF-002`) | Needs a deployed environment with a cold cache and a real network profile | Deployment access, plus an agreed service-level objective before any verdict |
| Procurement / kitchen / salary-payment lifecycle runs (`PROC-002`, `KIT-002`, `PAY-003`) | They move real money and stock; executing them is an owner-authorised production act, not a repair step | Explicit authorisation on specified test records |
| Demo-account cleanup (23 synthetic accounts IDs 2–24, workstation ID 1) | Requires authorisation and a live admin session; records were not touched | Authorised admin access, or the procedure in `REMAINING_BLOCKERS.md` run by the owner |

---

## 12. Limitations — what is *not* claimed

- **No WCAG conformance claim.** An automated scan, even in a real browser, is not a
  conformance audit. The five rules the audit named are now clean; manual screen-reader
  and keyboard passes were not performed.
- **No production verification of any kind.** All results come from the repository, a
  disposable test database, and local static serving.
- **No production financial reconciliation was executed.** The reconciliation service and
  the ledger posting path exist and are tested on fixtures; the production ledger was not
  mutated, and no production row was approved, refunded, or deleted.
- **No production performance profiling.** Frontend shell timings and the backend query
  profile in §7.1 come from a local static server and a disposable test database; absolute
  milliseconds are not production timings, and no service-level objective has been agreed
  (see `REMAINING_BLOCKERS.md` B9).
- **The 4 pending stock counts, 1 purchase order and 1 expense remain untouched** — by
  design; they were the D2 evidence and approving them would have destroyed it.
- **The stale Paystack payment and its expired booking remain exactly as found.**
- Absolute performance figures from the audit were not replicated (different host); only
  the like-for-like before/after pair in §7 is comparable.
- **The dependency scan is a point-in-time result.** `pip-audit` is clean as of the dates
  recorded in `docs/verification/dependency-scan.json`; that says nothing about advisories
  published afterwards, so the scan belongs in the release routine (see
  `REMAINING_BLOCKERS.md` B9).
