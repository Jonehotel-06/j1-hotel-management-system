# Defect & Observation Register — J-ONE Hotel & Lodge (2026-10-18)

Statuses used: **FIXED** (root-caused, repaired, committed) · **VERIFIED** (fix proven by executed evidence) · **OPEN** (recorded, deliberately not repaired in this engagement — scope/owner decision) · **BLOCKED** (cannot verify without owner input/hardware/authorization) · **NOT REPRODUCIBLE** (reported symptom could not be reproduced in code; evidence-backed alternative cause given).
Severity: CRIT / HIGH / MED / LOW. Every fix is the smallest reliable change at the correct layer; nothing was weakened to pass tests.

---

## Fixed defects

### DEF-1 — Release version frozen; update-checker never fires · HIGH · **FIXED + VERIFIED**
- **Symptom:** `frontend/version.json` said `1.1.23` while deployed pages already referenced `?v=1.1.24` assets; `tests-pwa/test_pwa` "update checker" expectation failed; staff never got the update prompt for a shipped release.
- **Root cause:** release stamp bump applied to pages but never to the version manifest trio (`version.json`, `js/version.js`, `sw.js` cache key).
- **Fix:** commit `cc56902` — pure version-string diff, 50 pages + trio → **1.1.24** (`sw.js` cache `jone-v1.1.24` after deploy).
- **Verification:** `test_pwa` working tree **149/151** vs **HEAD baseline 148/151** — the single flipped check is exactly the update-checker expectation; `validate.py` ALL OK; live Vercel still 1.1.23 (correct — undeployed).

### DEF-2 — Dead root PWA manifest · MED · **FIXED + VERIFIED**
- **Symptom:** `frontend/manifest.webmanifest` (71 lines) unreferenced by any page (real manifest: `/dashboard/manifest.webmanifest`); risk of stale-install ambiguity and audit noise.
- **Fix:** commit `29364c5` — file deleted; dashboard manifest untouched.
- **Verification:** PWA install suite green post-fix; `test_pwa` "old root manifest gone" check **FAIL at HEAD → PASS at repair HEAD** (baseline worktree comparison executed).

### DEF-3 — Online/walk-in bookings fail on formatted guest phone (URGENT FAILURE 1) · CRIT · **FIXED + VERIFIED**
- **Symptom:** public booking create returns raw DRF error `guest.phone: "Ensure this field has no more than 20 characters."` for any realistically formatted phone (e.g. `+234 (0) 803 123 4567` = 23 chars). **Reproduced live in production** (structured 400, zero rows persisted — `prod_smoke_readonly.json`) and locally pre-fix.
- **Root cause (architectural layer = serializer field pipeline):** DRF runs model-derived `MaxLengthValidator(20)` during **field parsing**, *before* any `validate_<field>` hook — so the app's own normalization/friendly-error logic never got a chance to run. Affects `GuestWriteSerializer` (public booking), `RoomGuestInputSerializer` (multi-room), `AdminGuestUpdateSerializer` (admin guest edit).
- **Fix:** commit `5aa4ebe` — shared `validate_guest_phone()` compacts whitespace/formatting **at field level** in all three serializers before length validation; app-friendly errors preserved (≥7 digits, ≤20 compact); stored values compact. No model change, no contract rename, no validation weakening, no migrations.
- **Verification:** 6 new unit tests (`PublicBookingPhoneNormalisationTests`, suite `test_bookings` 23 green); **full backend suite 1548 OK (skipped=15)**; live-API booking 201 with formatted phone (`J1-20261018-15634C61`); full browser booking flow 201 (`J1-20261018-FAAE4BD9`); Scenario A replay-idempotency + guest-token isolation intact.

### DEF-4 — `accounting.html` scope corruption kills approvals/expenses rendering · HIGH · **FIXED + VERIFIED**
- **Symptom:** every page load threw `ReferenceError: rowsOf is not defined` ×2 (from `refreshApprovalsSection`); expenses table dead whenever rows existed (`rowActions` undefined in `renderExpenses`); supplier/item pickers silently emptied (`expenseOptions` → `rowsOf` inside try/catch).
- **Root cause:** a 312-line `load()` (L642–954) swallowed the declarations of `rowsOf`/`rowActions` that **top-level** code calls — classic function-scope trap after a refactor.
- **Fix:** commit `3e17720` — declarations hoisted to module scope exactly where their own comments describe (`rowsOf` beside `listRows`, `rowActions` beside `renderExpenses`). **+54/−51, zero logic change** (pure movement).
- **Verification:** page load **0 console errors**; approvals/expenses/payables/cash tbody render; create-expense draft end-to-end **`EXP-20261018-403D2C4C4C19` 201**; pinned contract test `tests-js/test-expense-approval-ui.js` **12/12**; FE suite **545 pass / 0 fail**; frame-scan across **70 pages → 0 similar scope violations**; evidence `accounting_fix_verification.json` + shots.

### DEF-5 — `[hidden]` defeated by author CSS; dead PWA install button rendered & focusable · MED · **FIXED + VERIFIED**
- **Symptom:** "Install J-ONE Staff App" button visible in every dashboard sidebar on non-installable browsers (click = nothing); axe-core `aria-hidden-focus` (**serious**) on login/dashboard; any JS-hidden control could render.
- **Root cause:** author rules (`.btn{display:inline-flex}` etc.) out-specify the UA's `[hidden]{display:none}`.
- **Fix:** commit `e811fbd` — one canonical reset in `main.css`: `[hidden]{display:none!important}` (+6 lines).
- **Verification:** computed `display:none`, rect 0×0, not focusable; **axe 0 violations** (dashboard index + pos re-checked); all 12 PWA scripts re-run (no new failures); FE suite 545 pass; `validate.py` ALL OK; shots `sidebar-footer-*.png`.

## Observations — OPEN by decision (documented, not repaired here)

### OBS-1 — Delegated listeners re-registered inside `load()` (accounting) · LOW · OPEN
Each `load()` re-binds delegated document listeners; harmless today because every sensitive action passes singleton-gated confirm flows, but repeated reloads accumulate no-op duplicate handlers. Recommend restructuring in a planned maintenance release (not a defect-repair change). Evidence: CR of `accounting.html` event wiring.

### OBS-2 — Folio `CLOSED` state unreachable in shipped product · LOW-MED · OPEN (owner decision)
`Folio.Status.CLOSED` exists and `close_folio()` (with `allow_nonzero_balance` control) is implemented in `apps/finance/services/folio_service.py:73` — but it has **zero production callers** and finance URLs expose no close endpoint; `check_out_booking` never touches folio status. Consequence: folios remain OPEN forever after checkout/settlement (e.g. `FOL-20261018-81E1EC04E7A0` OPEN after Scenario E checkout with due 0). Checkout itself is safely guarded by **folio clearance** (zero balance), and post-departure postings remain possible/auditable — so no integrity loss was found. Adding an endpoint or auto-close is a **new feature/API contract** = outside this mandate; owner decision requested (Doc 07 B6).

### OBS-3 — Unmatched API routes return Django default HTML 404 · LOW · OPEN
Routed endpoints consistently return the structured JSON envelope (verified local + prod), but an unknown path (e.g. `/api/definitely-not-a-route/`) falls through to an HTML 404. Frontend only calls known routes (config.js endpoint map validated), so impact is cosmetic for API consumers. Changing global 404 handling = behavioral change beyond defect repair.

### OBS-4 — `room-details.html` deep-link console noise · LOW · OPEN
Opening the page without query params issues one `/api/rooms//` request → 404 console noise; UI degrades gracefully (empty state). Guard is a one-liner but touches a working page for cosmetics — deferred.

### OBS-5 — `.env.example` email onboarding trap · LOW · OPEN (docs/env)
`.env.example` ships `EMAIL_PROVIDER=brevo` with an empty API key → fresh environments produce 29 email-suite failures that look like app defects but are environment artifacts (proven: `EMAIL_PROVIDER=django` → all green). Recommend annotating `.env.example` comments in a docs pass. **No app code touched** (emails.py deliberately not "fixed").

### OBS-6 — POS "not submitting" in production: environmental cause (URGENT FAILURE 2) · HIGH-impact · **NOT REPRODUCIBLE in code**
Complete POS chain verified twice in browser + API-level (Doc 02 row 29): order create → kitchen ticket → submit → tender → cash drawer → settle → PAID. Capability gates behave exactly as designed (cash requires open session; CASHIER cannot create from QR request). The one production-plausible failure mode **reproduced deliberately**: staff sign-in from a terminal without enrolled workstation/desktop key → `403 DESKTOP_KEY_REQUIRED` — the user experiences "POS won't submit". Runbook: enroll each POS terminal (terminals.html → workstation session → key persisted `jone.receptionistDesktopKey`) then retest; if it persists with enrolled terminals, capture the exact response body from the device (Doc 07 B7).

## Blocked items (never marked PASS)

| Ref | Item | Why blocked |
|---|---|---|
| B1 | Second-approver account **in production** for procurement | Demonstrated fully in isolation (GM id 1 + ADMIN, two-person `PO-…-AEF4D8C911EC`, self-approve 403). Production needs a real second authorized account — owner action. |
| B2 | Stale production payment `J1P-20261010-199B12784F` ₦387,000 | Real financial record — inspection/reconciliation requires owner authorization (no production writes permitted). |
| B3 | Biometric (fingerprint) + physical barcode scanner/printer + dedicated terminal hardware | No hardware in audit environment; results not fabricated. Barcode **logic** verified at API level. |
| B4 | Payroll statutory validation (rates, tax IDs, rules) | Owner must supply real statutory inputs; invention explicitly prohibited. Module loads + suite green. |
| B5 | Paystack live-gateway round trip | Only dummy key available locally; requires Paystack **test-mode** keys in staging (no real charges ever). Redirect-only success marking already disproven by design review + Scenario A (502 → PENDING, never fake success). |
| B6 | Folio close lifecycle decision (OBS-2) | Owner product decision. |
| B7 | Production POS terminal enrollment state | Requires on-site/device access (OBS-6 runbook provided). |

## Test-debt findings (pre-existing, NOT app defects — deliberately not "fixed" to force green)

| Test | Failures | Proof pre-existing |
|---|---|---|
| `tests-pwa/test_pwa` (install-control expectations) | 2 | Ran at **HEAD baseline worktree**: same failures (148/151) as working tree minus the DEF-1 flip (149/151). |
| `tests-pwa/test_table_layout` (phone-wrap expectations) | 2 | Identical failures at HEAD; current phone-table strategy (`width:max-content` + `table-fit.js`) is newer than the test's expectations. CSS/tests left untouched per constraint. |
