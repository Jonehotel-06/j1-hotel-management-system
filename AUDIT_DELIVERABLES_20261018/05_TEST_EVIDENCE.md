# Test Evidence — separated by category (all executed 2026-10-18, isolated environment unless stated)

Environment: local clone `main` (baseline `f2e8a5d` → repair HEAD `e811fbd`), Django runserver `:8000` (`--noreload`), static frontend `:5500` + PWA suite origin `:8080`, **SQLite isolated DB** with seed data (no MySQL client available locally; production MySQL never touched). Real app code and real persistence for every scenario — nothing mocked away at the DB layer. Browser = real Chromium via Playwright (`/home/user/.venv-j1`). Node v20.20.2. Artifacts: `/home/user/audit-evidence/` (JSON + `shots/` + `logs/` + `scenarios/`).

---

## 1. Unit / integration (backend)

| Run | Result | Log |
|---|---|---|
| Full Django suite, **pre-fix baseline** | **1542 OK** | `logs/backend_tests_run2.log` |
| Full Django suite, **repair HEAD** | **1548 OK, skipped=15** (+6 = DEF-3 tests) | `logs/backend_tests_run3.log` |
| `tests/test_bookings.py` focused | 23 green (incl. `PublicBookingPhoneNormalisationTests` 6/6) | run3 log |
| Email-suite environment artifact | 29 failures with `.env.example` defaults (brevo + empty key) → **all green** with `EMAIL_PROVIDER=django`; `emails.py` untouched (OBS-5) | run logs |

Notes: `--parallel` crashes on py3.13 in this env → serial runs (documented, not an app defect). Skipped=15 are provider/hardware-gated skips that ship with the repo.

## 2. Unit / contract (frontend)

| Suite | Result | Evidence |
|---|---|---|
| `frontend/validate.py` | **ALL OK** (70 HTML docs, 30 JS files, asset refs resolve, no `/undefined` endpoints) | run output in session log; re-run post every fix |
| `node --test tests-js/` (75 files) | **546 tests → 545 pass / 0 fail / 1 skip** | TAP output |
| `tests-js/test-expense-approval-ui.js` (pins accounting anchor order) | **12/12** post-DEF-4 | TAP output |
| Version-stamp audit | `version.json=1.1.24` ↔ 50 pages `?v=1.1.24` consistent; live Vercel = repo HEAD byte-identical during audit | `prod_smoke_readonly.json` + prior fetch comparisons |

## 3. Browser E2E (real Chromium; console + network captured; screenshots in `shots/`)

**Public sweep** — `browser_public_results.json`: **23 pages, 0 issues**. Includes full booking UI flow: search → room select → guest form **with formatted phone** → **201 `J1-20261018-FAAE4BD9`**; upstream-payment **502 → graceful structured failure messaging, booking stays PENDING (no fake success)**; axe **0 violations** (index, booking); 390px mobile **no horizontal overflow**.

**Dashboard sweep** — `browser_dashboard_results.json`: **42/42 pages HTTP 200, 0 console errors post-fix** (pre-fix baseline captured DEF-4's ReferenceError ×2 on accounting; post-fix accounting verification `accounting_fix_verification.json`: 0 errors, 4 tbodies render, expense draft **`EXP-20261018-403D2C4C4C19` 201**). POS UI flows ×2 green as CASHIER (full chain → PAID; drawer open). DEF-5 verification: install button `display:none` / 0×0 / unfocusable; axe dashboard index+pos **0 violations** (shots `sidebar-footer-*.png`). Desktop-key gate reproduced: unenrolled non-management sign-in → 403 (OBS-6 evidence).

**Guest surfaces** — `browser_guest_results.json`: QR page form submit → **201 `SRQ-20261018-7007189D6784`**, success state "Reference … · OPEN" rendered, **0 page errors** (shot `qr-service-success.png`; earlier generic-selector failure was a test-harness selector bug, fixed by using exact ids `#qr-summary`/`[data-qr-submit]`); portal magic-link consume → `portal/index.html` lists **3 CHECKED_IN stays** (shot `portal-index-after-consume.png`).

**PWA suite** — 12 Playwright scripts: **10/12 fully clean**; `test_pwa` working tree **149/151 vs HEAD baseline 148/151** (the one flip = DEF-1/2 proof; remaining 2 install-control failures pre-existing at HEAD = test debt); `test_table_layout` 2 phone-wrap failures **identical at HEAD** = test debt. Neither CSS nor tests modified to force green.

## 4. Critical E2E scenarios (API-level, real persistence; refs in `scenarios/*.json`)

| Scenario | Verdict | Key references & assertions |
|---|---|---|
| **A** Online booking + payment resilience | **VERIFIED** | quote → create **₦81,000** `J1-20261018-15634C61`; idempotent replay → original response; guest-token detail + receipt; cross-booking isolation 404; gateway 502 → booking survives PENDING (`-09892E3D`, `-6789BCCF`) |
| **B** Walk-in | **VERIFIED** | 201 `J1-20261018-0B34AC8D`; check-in; **future-date check-in → 409** |
| **C** POS | **VERIFIED** | BAR order `POS-20261018-3E8F24A6B4A5`; UI chains `POS-…-29CCC983C141`, `-906D20DDA38E` → PAID; category/session gates enforced (BY DESIGN) |
| **D** Guest requests (portal/QR/table-QR) | **VERIFIED** | walk-in `J1-20261018-3AF38426` (Ada Portal, room 203); portal `SRQ-…-A3F45C348178` → RESOLVED + cancel `SRQ-…-369DF6D3FAC7`; room QR `QR-54038A3EA22BB2B8B091` → `SRQ-…-98DF12A97905` + replay + bad-token 404 → HK `HKT-20261018-0332539EF111` full lifecycle; maintenance `SRQ-…-05577C2F95BE` → WO `MNT-20261018-9B4AECDFB860` backlinked; table QR rotate `QR-57FBAEC38BB640413B7F` → FOOD_BEVERAGE-only (wrong-category 400) → POS BAR `POS-20261018-ADAC3AEF310A` → deliver → resolved-request re-service 400 ✓; browser submit 201 (above) |
| **E** Stay lifecycle + settlement + HK | **VERIFIED** | `J1-20261018-378EE508` → RS `POS-…-9A58A2566398` → drawer `CSH-…-DDE93DA85D13` → settle **₦26,000** `J1P-…-1956209A1C` → checkout due 0 (dup checkout idempotent) → DIRTY → `HKT-…-1D451A07E192` → CLEAN **18/18**; folio `FOL-20261018-81E1EC04E7A0` OPEN post-checkout = shipped design (OBS-2) |
| **F** Procurement two-person approval | **VERIFIED** | `PO-20261018-AEF4D8C911EC` admin-raised → **GM approved, no override marker**; `PO-…-E1035F647952` GM-raised → GM self-approve **403** → admin approve 200 → ORDERED → GRN **`GRN-20261018-640E5C4BD0D5`** + movement `STM-20261018-B098E1582C45` + balance **12→22 (+10 exact)** → replay **400, balance unchanged (no double receipt)** → PAY BANK_TRANSFER 200 → **PAID, outstanding 0.00**, requested_by GM / approved_by admin recorded; override policy `[ADMIN_OVERRIDE]` marker + audit event (admin-only, `ALLOW_ADMIN_APPROVAL_OVERRIDE=True`); ACCOUNTS_MANAGER 403 on approve+create = BY DESIGN (no procurement caps); state gates: pay-before-approve 400, GRN-on-SUBMITTED 400, GRN-on-PAID 400 |
| **G** Payments/ledger reconciliation | **VERIFIED (read-only)** | payments list: 2× PENDING PAYSTACK (never faked successful) + 1× SUCCESS CASH; finance transactions typed (POS_CHARGE ×8, PAYMENT_COLLECTION ×4, PAYMENT_ALLOCATION, CASH_OPEN ×2); double-entry balance previously proven in POS E2E (posted ledger headers immutable) |
| **H** Payroll | **BLOCKED (B4)** | module loads + suite green; statutory/rate validation requires owner inputs — nothing invented |
| **I** Reports + audit trail | **VERIFIED** | **9/9 report endpoints 200** with correct shapes (`reports_endpoints.json`: finance summary, night audit, balance sheet, cash flow, AR aging, occupancy, revenue, bookings, approvals); dashboard KPI 200 consistent with scenario state (in_house 3, rooms 18/15); audit trail append-only incl. **`ADMIN_OVERRIDE ×2`**, `GOODS_RECEIPT_POSTED`, `SUPPLIER_PAYMENT_RECORDED ×2`, `SERVICE_QR_*`, `PORTAL_*` events |

## 5. Production smoke (READ-ONLY ONLY — `prod_smoke_readonly.json`)

| Probe | Result |
|---|---|
| `GET /api/health/` + `/api/health/ready/` | **200** `{"success":true,"status":"ok","database":"up"}` |
| `GET` Vercel `/version.json` | 200 → **1.1.23** (repair not yet deployed — expected) |
| `GET /api/hotel/` | 200 (hotel profile, Enugu address) |
| `GET /api/rooms/availability/` (+30d, 2 adults) | 200 → **3 room types** |
| `POST /api/bookings/quote/` (with `room_type`) | **200** (2 nights, NGN, Standard Room) — pricing engine live; quote persists nothing |
| `POST /api/bookings/` **DEF-3 repro** (past dates + 23-char formatted phone, idempotency key `prod-readonly-smoke-do-not-create-0001`) | **400** `guest.phone: "Ensure this field has no more than 20 characters."` — defect live in prod; **zero rows persisted** (structured 400 = nothing created; past-date payload doubly guaranteed non-creating) |
| Unknown API route | HTML 404 (OBS-3) |

No authenticated production probes (no login attempts against prod accounts — throttle/lockout and credential-exposure risk); no writes, no synthetic data, no destructive operations. Live frontend confirmed byte-identical to `f2e8a5d` (Vercel serves audited baseline; DEF-4/#5 pages therefore live-unpatched until deploy).

## 6. Hardware (BLOCKED where physical, verified where logical — never fabricated)

| Hardware | Logical verification | Physical | 
|---|---|---|
| Barcode scanner | Barcode/SKU lookup + label generation exercised via API (POS item lookup 200; `barcode-labels.html` clean) | **BLOCKED (B3)** — no scanner; **no QR substitution introduced** |
| Fingerprint/biometric clock-in | Workforce module loads; attendance APIs covered by suite | **BLOCKED (B3)** — never substituted or simulated |
| Thermal receipt printer | Receipt endpoints 200 (Scenario A receipt fetch; receipt-details page) | **BLOCKED (B3)** |
| Cash drawer | Kick command flow verified E2E (`CSH-…-DDE93DA85D13` opened in Scenario E via API-driven drawer endpoint) | Physical device n/a in env |
| POS terminal device | Enrollment + desktop-key flow proven in browser (workstation session created; key persisted; sign-in gate 403 without it) | **BLOCKED (B7)** — production device state unknown; runbook provided |

## 7. Negative/security tests executed (sample — all against real code)

Cross-booking guest-token isolation **404** · portal staff-JWT rejection · QR bad-token **404** · wrong-category table-QR request **400** · resolved-request re-service **400** · duplicate service-QR link **400** (rotate path works) · GM self-approval **403** · ACCOUNTS_MANAGER procurement **403** · CASHIER guest-request create **403** · admin HK claim **403** · HK self-complete/shortcut **403** · future check-in **409** · pay-before-approve / GRN-on-wrong-status **400** · consumed-idempotency-key returns **original response** (no duplicates) · login throttle **429** after failures · payment success **never** inferred from redirect.
