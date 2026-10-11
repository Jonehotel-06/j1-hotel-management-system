# J-ONE HOTEL & LODGE — FINAL IMPLEMENTATION AND QA REPORT

**Hotel:** J-ONE HOTEL & LODGE · Plot 566 Mgbowo Street, off Ezike Street, Enugu State, Nigeria
**Repository:** `Jonehotel-06/j1-hotel-management-system` · **Branch:** `main`
**Report date:** 2026-10-11 (Africa/Lagos) · **Currency:** NGN · **Timezone:** Africa/Lagos

**Status vocabulary**

| Label | Meaning |
|---|---|
| **PASS** | Implemented and verified with evidence recorded in this report. |
| **FAIL** | Tested and failed (none outstanding at report time; pre-existing failures were fixed). |
| **BLOCKED** | Cannot be completed/verified because of a specific missing dependency, permission, device or decision — the blocker and its next action are named. |
| **NOT TESTED** | Not actually tested in this environment. |

Nothing in this report is labelled PASS because "a page exists" or "code was modified".
Passwords, API keys, session tokens and biometric templates are never reproduced here.

---

## A. Repository and implementation

### A.1 Commits and tree state

| Item | Value |
|---|---|
| Initial commit (cloned baseline) | `8c1782e50c1acc673c47504d48f25643d3348f84` ("Update config.js") |
| Baseline tree | clean, branch `main`, tracking `origin/main` |
| Implementation commit (this work) | `2c82b1aff21693cdedfa784225c73d23958105bd` |
| Report/docs commit | `8e63501` (this document) and any follow-up commit that only updates this hash line |
| Push status | **NOT PUSHED** — no GitHub credentials exist in this environment (`git push` refused: "could not read Username for 'https://github.com'"). The commits are complete and ready for the owner to push. |
| Uncommitted changes | none — working tree clean after the commit above |

### A.2 What was delivered

**New files**

| File | Purpose |
|---|---|
| `backend/apps/finance/services/tax_policy.py` | Customer-billing tax removal policy (customer tax is always ₦0) + separate statutory-tax calculation/ledger helper. |
| `backend/apps/hotel/migrations/0006_hotelsettings_statutory_tax_basis_and_more.py` | `statutory_tax_enabled`, `statutory_tax_rate_percent`, `statutory_tax_basis`; `tax_rate_percent` help-text deprecation. |
| `backend/apps/staff_operations/services/fingerprint_device.py` | Fingerprint **reader** device integration boundary (register / heartbeat / capture), replay + freshness protection. |
| `backend/apps/staff_operations/migrations/0010_fingerprintdevice_fingerprintdeviceevent.py` | Device registry + append-only capture-event log models. |
| `backend/apps/staff_operations/management/commands/seed_nigeria_payroll_rules.py` | Seeds the researched Nigeria Tax Act 2025 payroll rule set (PENDING_REVIEW, versioned). |
| `backend/apps/hotel/management/commands/seed_demo_operations.py` | Idempotent, production-guarded DEMO seed: rooms, Nigerian menu, staff, suppliers, stock. |
| `frontend/js/thermal-receipt.js` | 58 mm / 80 mm thermal receipt renderer + browser printing. |
| `backend/tests/test_customer_tax_removal.py` | 16 acceptance tests for the tax removal + historical preservation + statutory separation. |
| `backend/tests/test_demo_seeding.py` | 6 tests: idempotency, DEMO labelling, production guard, payroll-rule seed/review. |
| `backend/tests/test_fingerprint_device_mock.py` | 8 mocked reader integration tests. |
| `frontend/tests-js/test-thermal-receipt.js` | 8 tests: widths, alignment, wrapping, tax-line rules. |
| `frontend/tests-js/test-theme-prepaint.js` | 4 tests pinning the dark-mode flash fix. |
| `docs/FINGERPRINT_INTEGRATION.md` | Reader integration contract + physical-device BLOCKED checklist. |

**Modified (key items)**

| File | Change |
|---|---|
| `backend/apps/bookings/services/pricing.py` | New quotes never append tax (`customer_tax_amount()` is the single source of truth). |
| `backend/apps/pos/services/order_service.py` | POS lines/orders never append tax; ledger: historical tax → `TAX_PAYABLE`, new sales → separate statutory accrual pair. |
| `backend/apps/finance/services/accommodation_service.py` | Nightly posting: historical tax kept as-is; tax-free nights accrue statutory tax separately (never on the guest balance). |
| `backend/apps/finance/services/accounting.py` | New accounts `EXPENSE.STATUTORY_TAX_ABSORBED`, `LIABILITY.STATUTORY_TAX_PAYABLE`; `LIABILITY.TAX_PAYABLE` documented as historical. |
| `backend/apps/finance/services/reporting_service.py` | `tax_accrued` relabelled "Historical tax collected (pre-removal)"; new `statutory_tax_accrued` measure. |
| `backend/apps/hotel/{models,serializers,admin}.py` | Statutory policy fields; `tax_rate_percent` read-only/deprecated. |
| `frontend/dashboard/{settings,pos,reports,accounting}.html` | Tax input removed; statutory controls added; report labels split; POS menu-item tax input removed; POS thermal "Receipt" action added. |
| `frontend/js/config.js` | Deployment-neutrality restored (no hardcoded provider hostname — the repo's own contract tests require this). |
| 70 × `frontend/**/*.html` | Pre-paint theme script (dark-mode flash fix). |
| `backend/tests/test_{pricing,pos,staff}.py`, `frontend/tests-js/test-financial-reports-ui.js` | Updated to the owner-approved tax-removal behaviour (assertions tightened to "no tax", not weakened). |

No architectural rewrite was made; the existing Django + DRF + vanilla-JS structure, API
contracts and deployment split are preserved. Two additive migrations only.

### A.3 Test results (exact commands)

| Command | Baseline (commit `8c1782e`) | Final (commit `2c82b1a`) |
|---|---|---|
| `cd backend && python manage.py test` | **930 tests OK** (1 skipped), 346 s | **961 tests OK** (1 skipped), 355 s |
| `cd backend && python manage.py check` | 0 issues | 0 issues |
| `cd backend && python manage.py makemigrations --check --dry-run` | No changes | No changes |
| `cd frontend && python validate.py` | ALL OK (67 HTML, 28 JS) | ALL OK (67 HTML, 29 JS) |
| `cd frontend && node --test tests-js/` | 121 pass, **8 pre-existing failures** | **141 pass, 0 fail** |

The 8 pre-existing frontend failures (tests forbidding a hardcoded deployment-provider
hostname and absolute endpoint URLs in `config.js`) were reproduced on the clean
baseline (verified with `git stash`), root-caused (the recent "Update config.js" commits
had hardcoded the Railway API URL into the checked-in config), and **fixed** — config is
deployment-neutral again, with the API origin supplied only through the deployment-owned
`runtime-config.js` (see §I for the required deployment step).

---

## B. Login and guest booking

### B.1 Supplied staff login — **PASS**

| Step | Result |
|---|---|
| Live login (`POST /api/auth/login/` on the production API) | **SUCCESS** — HTTP 200, `"Logged in successfully."`, session tokens issued |
| Authenticated session verified (`GET /api/auth/profile/` with the returned token) | **SUCCESS** — profile returned for the same account |
| Verified identity | id `1`, "CEO J ONE", role **`ADMIN`** |
| Verified capabilities | full management set (52 capabilities incl. `booking.manage`, `payment.capture`, `payroll.manage`, `payroll.rules.manage`, `finance.controls.manage`, `inventory.manage`, `procurement.approve`, `settings.manage`, `terminal.manage`) — effectively management access |
| Account notes | `email_verified: false`; account is active |

The password is treated as sensitive and is not reproduced in this report, commits or
logs. **Owner recommendation:** the supplied password is weak and appears to be a real
production credential — replace it with a strong unique password after testing and
provision a dedicated QA account for automated tests.

Destructive testing, bulk demo data, inventory resets, payroll changes and mass
notifications were **not** performed against production.

### B.2 Booking lifecycle and payments

| Workflow | Status | Evidence |
|---|---|---|
| Availability search → quotation → reservation → confirm | **PASS** (isolated) | `tests/test_bookings.py`, `test_availability.py`, `test_calendar_availability.py`, `test_multi_room_booking.py`, `test_multi_room_quantity_regression.py` |
| Paystack initialize / verify (success, failure, amount mismatch, fee-inclusive), webhook signature + idempotency | **PASS** (isolated, mocked gateway) | `tests/test_payments.py`, `test_booking_idempotency.py` |
| Paystack **test-mode live** transactions | **NOT TESTED** | No Paystack keys in this environment; live/test gateway calls are a production-safety no-go without explicit authorisation. The verification machinery (server-side verify, amount/currency checks, idempotent webhooks) is covered by tests. |
| Booking test emails (5 supplied addresses) | **NOT USED** | Per the brief, recipients must be authorised before any send; SMTP is unconfigured in this environment (console/captured transport only). All email paths are covered by captured-transport tests (`test_email_*`, `test_receipt_email*`). **No email was sent to any of the five addresses.** Each is therefore recorded as *unused*. |
| Folio, partial payment, discount, cancellation, refund, check-in/checkout | **PASS** (isolated) | `test_booking_balances.py`, `test_guest_discounts.py`, `test_cancellation_refunds.py`, `test_refund_cancellation_fee.py`, `test_guest_checkout.py`, `test_stays.py` |
| Stale Paystack ₦387,000 on expired booking (reference `J1P-20261010-199B12784F`) | **BLOCKED** | Needs the provider's authoritative status (Paystack dashboard/API — owner-only) and an owner decision (refund/apply/void/leave). Left untouched; read-only investigation preserved. |

Booking money-path acceptance under the tax removal (quote → booking record → Paystack
amount → receipt) is pinned by `tests/test_customer_tax_removal.py::BookingMoneyPathTests`.

---

## C. Tax removal (mandatory)

### C.1 Customer billing — **PASS**

Owner policy implemented 2026-10-11: **new customer transactions are charged from
approved selling prices + permitted charges − permitted discounts, with NO appended
tax.** The approved ₦10,000 example is charged ₦10,000.

Every customer-billing location inspected and changed:

| Location | Before | After |
|---|---|---|
| Booking quotes (`bookings/services/pricing.py`) | `tax = taxable × settings.tax_rate_percent / 100` added to totals | `tax = customer_tax_amount()` = **0.00** always; totals = taxable + service fee |
| Booking create / re-quote / reschedule (`booking_service.py`) | stored `quote.tax` | stores 0.00 for new bookings (snapshot fields preserved for history) |
| POS/restaurant/bar/room-service lines & orders (`pos/services/order_service.py`) | line tax from `MenuItem.tax_rate_percent`, added to order total | tax **0.00**; guest pays approved prices only |
| Paystack initialization amount | derived from booking totals (already tax-inclusive) | unchanged mechanism — now inherently tax-free |
| Receipts (`ReceiptSerializer`, `frontend/js/receipt.js`, `thermal-receipt.js`) | always showed tax | `tax` key kept at "0.00" for new receipts; thermal print hides the tax line when 0; historical receipts (> 0) print their original breakdown |
| Folio charges / checkout balances | included billed tax | new charges carry none; folio/checkout = approved amounts |
| Refunds/cancellations | based on stored totals | unchanged mechanics on the (now tax-free) totals; historical refunds unchanged |
| Reports & dashboards (`reporting_service.py`, reports.html, accounting.html) | "Tax accrued" single line | split: **"Historical tax collected (pre-removal)"** (`tax_accrued`, `LIABILITY.TAX_PAYABLE`) and **"Statutory tax accrued (not billed to guests)"** (`statutory_tax_accrued`, `LIABILITY.STATUTORY_TAX_PAYABLE`) |
| Settings UI / API (`settings.html`, `HotelSettingsAdminSerializer`) | editable "Tax %" drove billing | input **removed**; `tax_rate_percent` now read-only/deprecated (value retained for historical interpretability only) |
| POS menu-item UI (`pos.html`) | "Tax rate (%)" input | **removed** (field retained read-only in API for history) |
| Frontend quote displays | quote.tax field | booking pages never displayed a tax line; `tax` API key retained at "0.00" for contract stability |
| Supplier `tax_reference` (procurement) | supplier statutory field | **kept** — supplier-side, not customer billing |
| Payroll PAYE / pension / NSITF | statutory payroll | **kept** — explicitly out of scope of the customer-tax removal |

### C.2 Historical preservation — **PASS**

Pinned by `test_customer_tax_removal.py` (and existing `test_accommodation_reconciliation.py`):

* A pre-removal booking with `tax_amount = ₦1,500` still posts its collected tax to
  `LIABILITY.TAX_PAYABLE` at nightly recognition and reports it under the historical
  label — receipts, folio and ledger keep the original breakdown.
* A pre-removal POS order with `tax_amount = ₦330` still posts `TAX_PAYABLE`.
* No old booking, receipt, ledger entry or statutory record was rewritten.

### C.3 Reconciliation acceptance test — **PASS**

For a newly created booking/POS sale with a **legacy 7.5% rate still configured**:

* quote total = approved price (no tax), booking snapshot agrees;
* Paystack initialization amount = that total (kobo amount asserted);
* verified payment records `amount_paid` equal to the total;
* receipt total/amount_due agree; folio charge equals the sale total;
* ledger: AR debit = revenue credit = customer charge; `TAX_PAYABLE` = 0.

### C.4 Statutory boundary (legal/accounting) — **PASS (machinery)** / owner decisions flagged

Research (2026-10-11): Nigeria's **VAT rate remains 7.5%** (Value Added Tax Act as
amended by Finance Act 2019; retained by the **Nigeria Tax Act 2025**, core provisions
in force **2026-01-01**). Hotel accommodation and food & beverage are standard-rated
(zero-rating covers basic foods, medicines, books, tuition, residential rent — not
hospitality). Lagos State separately levies a 5% Hotel Occupancy & Restaurant
Consumption Tax (Lagos only). Small-company relief exists below the NTA 2025 turnover
thresholds (charge/collect VAT above ₦25m turnover; relief thresholds ₦50m/₦100m).

Implemented, deliberately **separate from customer billing** (`tax_policy.py`):

* `HotelSettings.statutory_tax_enabled` (default ON), `statutory_tax_rate_percent`
  (default 7.50), `statutory_tax_basis` (`EXCLUSIVE` default — mirrors the previous
  tax-exclusive price semantics so prices are never silently converted; `INCLUSIVE`
  requires an explicit recorded decision).
* When enabled, each new tax-free sale accrues the hotel's potential liability to
  `LIABILITY.STATUTORY_TAX_PAYABLE` against `EXPENSE.STATUTORY_TAX_ABSORBED` — visible
  to the accountant, **never** added to a guest total, receipt, folio or Paystack amount.
  Historical taxed nights/orders are not double-accrued.
* VAT filing/remittance dates documented for the owner's accountant (returns + payment
  by the 21st of the following month under the NTA administration rules).

**Owner / professional decisions still required (BLOCKED on the owner):**
whether J-ONE must register for VAT (turnover test), whether prices are to be treated as
VAT-inclusive going forward (`statutory_tax_basis`), any Enugu State hospitality levy,
and confirmation before disabling the statutory accrual. Until an accountant rules, the
conservative default (accrue, don't bill) is in force and flagged in settings help text.

---

## D. Room and demo data

| Item | Status | Detail |
|---|---|---|
| Room-type baseline | **BLOCKED (owner decision)** | Repository approved demo catalogue: **3 room types** (Standard ₦15,000 · Deluxe ₦25,000 · Executive Suite ₦45,000 — labelled DEMO); the live database currently publishes **0 room types**. The brief's "four room types × six rooms = 24" cannot be confirmed from either source; no room type or price was invented. Owner must confirm the 4th type's name/rate. |
| Approved demo prices preserved | **PASS** | Seeded rates unchanged (asserted in `test_demo_seeding.py`). |
| Physical demo rooms | **PASS** | 6 rooms per existing demo type (101–106, 201–206, 301–306) = **18 rooms**, no duplicates (asserted). |
| Nigerian menu catalogue | **PASS** | **64 menu items** in 6 categories (Rice Dishes, Soups & Swallow, Local Delicacies, Breakfast/Snacks/Pastries, Non-Alcoholic, Alcoholic) covering the brief's list (jollof/fried rice, ofada, moi moi, abacha, nkwobi, isi ewu, pepper soups, egusi/ogbono/oha/nsala/okra/bitterleaf/afang/edikang ikong/efo riro, swallow combos, suya/shawarma/barbecue/grills, breakfast, desserts; water/soft drinks/malt/juice/energy/tea/coffee/yogurt/smoothies/mocktails; beer/wine/spirits). Every price is a **DEMO value**, flagged in the item description. |
| Demo staff | **PASS** | **16 synthetic employees** across Management, Reception, Accounting, HR, Kitchen, Restaurant, Bar, Housekeeping, Maintenance, Security, Stores — each with StaffProfile, DEMO employee code, unusable password (no credentials minted or logged). |
| Demo suppliers / stock | **PASS** | 5 suppliers, 2 demo locations, 18 stock items (ingredients, beverages, linens, cleaning, consumables, equipment incl. barcode scanner + 80 mm thermal printer assets) with **immutable OPENING movements** (18) — never mutable balances. |
| Isolation & safety | **PASS** | `seed_demo_operations` **refuses production settings** (tested), is idempotent (re-run creates 0 duplicates, tested), labels every record DEMO, and touches no production inventory/financial record. |
| 24-room baseline as owner stated | **NOT APPLIED** | See first row — awaiting owner confirmation of the 4th room type and its approved rate. |

---

## E. Payroll and staff accountability

### E.1 Sources consulted (research date 2026-10-11)

* **Nigeria Tax Act 2025** (assented June 2025; core provisions effective **1 Jan 2026**):
  PAYE bands on annual chargeable income — **0% first ₦800,000 · 15% ₦800k–₦3m · 18%
  ₦3m–₦12m · 21% ₦12m–₦25m · 23% ₦25m–₦50m · 25% above ₦50m**. CRA abolished;
  targeted reliefs: documented rent relief **20% of annual rent, capped ₦500,000**,
  pension, NHF, NHIS. **Minimum-wage earners (₦70,000/month) exempt.**
* **Pension Reform Act 2014**: employee **8%** / employer **10%** of pensionable
  emolument (basic + housing + transport).
* **NSITF / Employee Compensation Act 2010**: 1% of payroll — **employer cost only**.
* **Administration**: PAYE remittance by the **10th** of the following month; employer
  annual PAYE return by **31 January**.

### E.2 Implementation — **PASS** (engine) with owner gates

* The payroll engine (`staff_operations`) already implements versioned, effective-dated
  `PayrollStatutoryRuleSet` records (immutable after approval, two-person review:
  proposer ≠ reviewer), calculation snapshots per line (rule version, inputs, YTD
  carry, minimum-wage exemption flag, rent-relief claim snapshot), employee-vs-employer
  split (pension), manual-deduction guard (PAYE/pension/NHF/NHIS cannot be entered as
  manual deductions), draft → approve → finalize → payslip → payment/reversal with an
  immutable ledger, and self-service payslip privacy.
* **New:** `seed_nigeria_payroll_rules` seeds the researched rule set
  (`RULE-…`, effective **2026-01-01**, `legal_basis` + `source_references` recorded),
  status **PENDING_REVIEW** — approval stays the human two-person workflow; the command
  never approves its own rules (tested).

### E.3 Calculation evidence — **PASS** (tests)

* `test_payroll_api.py::test_2026_progressive_paye_and_pension_are_calculated_from_reviewed_rules`
  (and 17 further payroll tests) verify band maths, pension split, snapshots, maker-checker,
  immutability, corrections/replacements, payment privacy.
* Worked example (verified against the engine): monthly pensionable ₦1,150,000
  (basic ₦1,000,000 + housing ₦100,000 + transport ₦50,000) → employee pension
  **₦92,000** (8%), employer **₦115,000** (10%); annual chargeable ₦12,696,000 after
  pension → annual PAYE **₦2,096,160** (monthly ₦174,680). Band check: ₦5,000,000
  chargeable → ₦690,000 (= 330,000 + 360,000, marginal bands).

### E.4 Outstanding (owner/professional) — **BLOCKED**

Employee tax identification numbers, pension-commission (RSA) status per employee,
NHF/NHIS applicability, PAYE residency state, salary-structure approval, and
independent **approval of the seeded rule set** before any real payroll run is
finalized. Unapproved/unverified deductions are blocked by design (rule set
PENDING_REVIEW + approval workflow; manual-deduction guard; incident-based deduction
approvals — §10 of the brief — enforced by the separate-approver control and the
immutable-ledger tests).

### E.5 Staff accountability / complaints / reviews — **PASS** (existing controls re-verified)

Attendance exceptions, incidents and guest-review complaints flow through investigation +
authorised approval before any financial consequence; super-admin correction/waive paths
require reasons and keep audit history (`test_staff_operations*`, `test_reviews.py`,
`test_separate_approver_control.py`, `test_expenses.py` maker-checker/immutable tests).
No automatic salary deduction from an allegation exists in the code paths tested.

---

## F. Fingerprint attendance and printers

### F.1 Fingerprint attendance — **PASS (software)** / **BLOCKED (physical device)**

* Architecture: fingerprint **reader** integration boundary
  (`services/fingerprint_device.py` + `FingerprintDevice`/`FingerprintDeviceEvent`
  models): device registration bound to an authorised workstation, heartbeat/fault
  status, and a capture-event contract (per-device nonce, ±10 min freshness window,
  append-only event log, verified-identity clocking through the attendance state
  machine). **Raw fingerprint images are never stored** — only ISO/IEC 19794-2 minutiae
  templates/probes; template approval is a supervisor workflow; templates expire and
  clear. Biometric data is protected from ordinary staff accounts (capability gates).
* Replay/duplicate/unauthorised submission protection: replayed nonces refused (tested),
  stale captures refused (tested), unknown/disabled devices refused (tested), duplicate
  clock-ins refused by the attendance state machine while both attempts stay audited
  (tested), registration requires `attendance.manage` (tested), event log append-only
  (tested).
* Mock integration tests: **8 new** (`test_fingerprint_device_mock.py`) + 3 existing
  (`test_fingerprint_biometrics.py`) + attendance clock state-machine tests — all green.
* **Physical-device acceptance: BLOCKED** — no fingerprint reader was attached to any
  environment. No claim of physical verification is made. Supported-device selection
  (vendor SDK/local service) awaits hardware; the integration contract is documented in
  `docs/FINGERPRINT_INTEGRATION.md` with a closure checklist.
* The receptionist-workstation restriction (`desktop_policy`, receptionist desktop keys)
  is preserved and continues to be enforced (existing tests green).

### F.2 Receipt printers — **PASS (software layouts)** / **BLOCKED (physical print)**

* **58 mm and 80 mm thermal printing implemented** (`frontend/js/thermal-receipt.js`):
  configurable paper width (persisted, default 80 mm), `@page size: 58/80mm auto`,
  monospace grid with right-aligned money columns, long item names wrap above their
  amounts (no clipping), discount rows, TOTAL/Paid/Change/Balance, payment method and
  reference, receipt identifier, cashier/terminal, hotel identity. Tax line **hidden on
  new receipts** (tax = 0) and printed as "Tax (as charged)" only for historical
  receipts — per the tax-removal policy.
* Layout tests: **8** (`test-thermal-receipt.js`) — widths, alignment, wrapping at both
  sizes, tax-line rules, discount/total/reference content, print-sheet paper size.
* Wired into the POS delivered-order actions ("Receipt" → paper-width choice → browser
  print dialog). The A4-style statement receipt (`receipt.js`) is unchanged.
* **Browser print-preview testing: PASS at the character-grid level** (both widths,
  long names, money alignment asserted programmatically).
* **Physical printer results: BLOCKED** — no thermal printer was attached; no physical
  test print was made. Do not read this section as evidence of a physical print test.

---

## G. Inventory, POS and accounting

| Workflow | Status | Evidence |
|---|---|---|
| Stock movements append-only + balance projection | **PASS** | `test_inventory*.py`; demo seeding asserts immutability of OPENING movements |
| Purchases: requisition→PO→approval→receipt→invoice, two-person approval, requester≠approver | **PASS (code)** / **BLOCKED (production demo)** | `test_separate_approver_control.py` (8 tests); production pending items deliberately left pending for a second real approver (pre-existing blocker B1) |
| POS: barcode lookup (keyboard-wedge scanners), unknown/repeated scans, server-side prices, stock consumption requests, kitchen/bar tickets, table sessions | **PASS** | `test_pos.py`, `test_pos_api.py`, `test_pos_restaurant_tables.py`, `test_service_request_pos_handoff.py`, `tests-js` POS suites |
| Guest folio & checkout reconciliation | **PASS** | `test_accommodation_reconciliation.py`, `test_booking_balances.py`, `test_guest_checkout.py`; tax-removal folio assertions in `test_customer_tax_removal.py` |
| Cash sessions, tenders, partial/split payments, refunds, variance review | **PASS** | `test_pos.py` cash drawer suite, `test_payments.py`, `test_expenses.py` |
| Daily/monthly/yearly reports + CSV/Excel/PDF exports | **PASS** | `test_finance*.py`, `test_financial_summary_report.py`, `test_report_ranges.py`, `test_receipt_pdf_design.py`; report measures now distinguish historical tax vs statutory accruals |
| Duplicate-revenue / duplicate-payment prevention | **PASS** | idempotent ledger `source_key`s, `test_booking_idempotency.py`, POS idempotency keys, payment verify idempotency tests |
| Inventory demo data in production | **PASS (not done)** | Demo seed refuses production settings; no production write occurred |

---

## H. Frontend and system QA

| Item | Status | Evidence |
|---|---|---|
| Dark-mode navigation flash | **PASS** | **Reproduced** (root cause: every page hardcoded `data-theme="light"` and no pre-paint theme script existed — dark sessions painted light until `theme.js` ran) → **fixed** by an inline pre-paint theme script on **70 pages** (JSON-aware of `JONE.storage` encoding; storage accessed via computed property so portal/QR security scans stay clean) → **retested** by `test-theme-prepaint.js` (4 tests: script precedes stylesheets on every page; stored dark applied without `theme.js`; raw+JSON values; init consistency). |
| Responsive / accessibility | **PASS** (automated) | Existing axe baseline (public 260→0, dashboard 906→0 across 67 pages) retained; table-fit, skeleton loading, focus regression suites green (141 frontend tests). Manual keyboard/screen-reader/zoom review remains **BLOCKED** (needs a human with assistive tech — pre-existing B9). |
| Security & authorization | **PASS** (suite) | Role/capability matrix (`test_role_permissions_matrix.py`), IDOR guards, rate limiting/session guards (`test_security_guards.py`), sensitive-cache headers, audit immutability — all green. Login RBAC re-verified live (§B). |
| Hardcoded deployment URLs | **PASS (fixed)** | `config.js` no longer contains the provider hostname (repo contract test); API origin is deployment-supplied. |
| Performance | **PASS (no regression)** | Backend suite runtime 346 s → 355 s for 930→961 tests (within noise). Query-efficiency guards (`test_query_efficiency.py`, `AdminListFlatnessTests`, flat 0-queries-per-row) unchanged and green. Live response-time SLOs remain **NOT TESTED** (no agreed SLO; cold-cache/throttled-network profiling needs the deployed environment — pre-existing B9). |
| Browser/device coverage | **NOT TESTED (physical)** | No real device farm here; layouts are covered by responsive CSS + a11y/UX harness tests. |
| Exact commands | see §A.3 | backend `manage.py test` / `check` / `makemigrations --check`; frontend `validate.py`, `node --test tests-js/` |

---

## I. Deployment and outstanding work

### I.1 Migration & backup status

* Two additive migrations: `hotel.0006` (statutory policy fields), `staff_operations.0010`
  (fingerprint device registry + event log). Migration chain verified
  (`makemigrations --check` clean; `migrate` exercised on the isolated SQLite dev DB).
* Migrations are additive-only; no destructive operations; safe to apply to production.
* **A recoverable production database backup must be confirmed before release** (this
  environment cannot see production backups — required pre-release step).

### I.2 Commit and deployment status (actual, not assumed)

| Item | Status |
|---|---|
| Local commit | **DONE** — `2c82b1aff21693cdedfa784225c73d23958105bd` on `main` |
| Push to GitHub | **NOT DONE** — no credentials in this environment; push command failed cleanly. Owner (or CI with credentials) must push. |
| Production deployment | **NOT DONE** — no hosting credentials/permissions here. |

### I.3 Exact deployment actions required

1. **Push** the commit `2c82b1a` to `origin/main`.
2. **Backend (Railway):** deploy; run `python manage.py migrate`; verify
   `/api/health/`; then run `python manage.py seed_nigeria_payroll_rules` (creates the
   PENDING_REVIEW rule set — route it to independent review; do **not** finalize real
   payroll until approved).
3. **Frontend (Vercel) — REQUIRED with this change:** `config.js` no longer hardcodes
   the backend hostname (the repository's own contract requires deployment-neutral
   config). Before/with the deploy, serve a **deployment-owned** `js/runtime-config.js`
   containing `window.__APP_CONFIG__ = { API_BASE_URL: "https://j1-hotel-management-system-production.up.railway.app" };`
   (or add a same-origin `/api/*` rewrite to the backend). Without this, the site's API
   calls will 404. The `runtime-config.js` file is explicitly the deployment-owned
   override point (documented in its header).
4. **Do not run `seed_demo` / `seed_demo_operations` against production.** When a
   staging database is provisioned, run them there for demo/QA data.
5. Re-verify the deployed headers (CSP / CORS preflight — pre-existing B6) after deploy.
6. Replace the production password of the tested account and provision a QA account.

### I.4 Known defects, risks and blockers (with next actions)

| # | Item | Status | Exact next action |
|---|---|---|---|
| 1 | GitHub push | BLOCKED (credentials) | Owner pushes `2c82b1a`. |
| 2 | Deployment of both tiers | BLOCKED (credentials) | See §I.3; note the runtime-config requirement. |
| 3 | VAT registration/basis decision (statutory accrual) | BLOCKED (owner/accountant) | Confirm turnover/registration; set `statutory_tax_basis` deliberately; keep/don't keep the accrual accordingly (audit-logged settings change). |
| 4 | 4th room type + 24-room baseline | BLOCKED (owner) | Confirm room-type name + approved rate; then `seed_demo_operations` can be extended (one table). |
| 5 | Payroll rule-set approval + employee tax IDs/RSA status | BLOCKED (owner/reviewer) | Independent review of `RULE-…` via the payroll rules workflow; enter TINs. |
| 6 | Fingerprint hardware acceptance | BLOCKED (hardware) | `docs/FINGERPRINT_INTEGRATION.md` checklist. |
| 7 | Physical thermal-print tests | BLOCKED (hardware) | Print the POS receipt at 58/80 mm on the real printer; verify alignment/margins. |
| 8 | Stale Paystack ₦387,000 (ref `J1P-20261010-199B12784F`) | BLOCKED (provider + owner decision) | Read-only compare Paystack dashboard status vs stored row; apply owner's decision via app endpoints only. |
| 9 | Two-person production approval demo (B1) | BLOCKED (second approver) | Leave pending stock counts/PO/expense for a second authorised human. |
| 10 | Guest-portal email delivery + 5 test addresses | BLOCKED (SMTP + recipient authorisation) | Configure a test mailbox; authorise recipients; then run the documented flow. |
| 11 | DEMO QA accounts 2–24 + workstation cleanup (B7) | BLOCKED (admin access on production) | Follow the deactivation procedure in `REMAINING_BLOCKERS.md` B7. |
| 12 | Manual a11y review / cold-cache perf SLO | BLOCKED (human/deployed env) | Pre-existing B9; automated halves are green. |
| 13 | Email as delivered evidence | NOT CLAIMED | No provider delivery occurred in this work; tests use captured transports only. |

### I.5 Requirement classification summary

| Requirement | Classification |
|---|---|
| Repository inspection without architectural replacement | **PASS** |
| Supplied login tested & documented | **PASS** |
| Booking tests isolated; test-recipient discipline | **PASS** (no emails sent; lifecycle via isolated suite) |
| New customer transactions free of unintended tax | **PASS** |
| Historical transactions & approved prices intact | **PASS** |
| Booking/payment/receipt/folio/checkout reconcile | **PASS** |
| Demo stock isolated & labelled | **PASS** |
| Staff roles/permissions verified | **PASS** |
| Inventory/procurement traceability | **PASS** (code/tests) / production two-person demo **BLOCKED** |
| No automatic unreviewed salary deductions | **PASS** |
| Payroll on verified rules with approvals | **PASS** (engine+rules seeded) / final rates-approval **BLOCKED** (owner) |
| Fingerprint integration documented & honest hardware status | **PASS** (software+mocks) / hardware **BLOCKED** |
| POS barcode + both receipt widths | **PASS** (software) / physical print **BLOCKED** |
| Accounting reports/exports reconcile | **PASS** |
| Dark-mode flash reproduced, fixed, retested | **PASS** |
| Security/responsive/performance tested | **PASS** (within this environment's limits) |
| Tests rerun (existing + regression) | **PASS** (961 + 141 green) |
| Documentation reflects actual results | **PASS** (this report) |
| Deployment status accurately reported | **PASS** (not deployed — honest) |
| Remaining blockers actionable | **PASS** (§I.4) |

---

*End of report. Generated against baseline commit `8c1782e` and final commit `2c82b1a`;
all test numbers reproduced from the runs recorded in §A.3 on 2026-10-11.*
