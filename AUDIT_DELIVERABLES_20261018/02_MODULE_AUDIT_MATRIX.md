# Module-by-Module Audit Matrix — J-ONE Hotel & Lodge (2026-10-18)

**Legend** — Status: `PASS` = audited, no defect · `PASS†` = passes with recorded observation (Doc 03 OBS-x) · `FIXED` = defect found, repaired, re-verified · `BLOCKED` = cannot be fully verified in this environment (reason given; never marked PASS) · `N/R` = reported issue Not Reproducible in code.
**Methods:** `BW` = real-browser sweep (Chromium/Playwright, console+network captured) · `API` = live end-to-end API scenario against real persistence · `UT` = unit/integration suite · `CR` = source code review · `AXE` = axe-core a11y · `M` = 390px mobile viewport check.
Evidence files under `/home/user/audit-evidence/` and `scenarios/` (refs quoted per row).

## A. Public / guest-facing surfaces (sweep: `browser_public_results.json`, 23 pages, 0 issues)

| # | Module / page | Methods | Status | Evidence & notes |
|---|---|---|---|---|
| 1 | Landing / index | BW, AXE, M | PASS | Loads clean; availability widget wired to `/api/rooms/availability/`. |
| 2 | Rooms & rates (public) | BW, API | PASS | Availability returns 3 room types locally **and in production** (prod smoke). |
| 3 | Booking engine (quote → create) | BW, API, UT | **FIXED** | DEFECT #3 (phone). Post-fix: browser flow `J1-20261018-FAAE4BD9` 201 with formatted phone; quote 201/200 local+prod; idempotent replay returns original; Scenario A refs `J1-20261018-15634C61/-09892E3D/-6789BCCF`; upstream 502 → graceful user messaging (no fake success). |
| 4 | Booking confirmation / payment-verify | BW, CR | PASS | Live Vercel confirmed serving repaired confirmation page; **never marks payment successful from redirect alone** — verification goes through provider/backend (CR + Scenario A). |
| 5 | Enquiries (public form) | BW, UT | PASS | Creates enquiry; admin queue row appears (sweep + suite). |
| 6 | Offers / gallery / reviews (public) | BW | PASS | Read surfaces render; review moderation lives in admin (row 27). |
| 7 | Facilities page | BW | PASS | Clean. |
| 8 | Portal login (`/portal/login.html`) | BW, API | PASS | Magic-link challenge→consume→`X-Portal-Session`; after consume, portal index lists 3 CHECKED_IN "Ada Portal" stays (shot `portal-index-after-consume.png`); staff JWT never accepted on portal routes (header separation enforced). |
| 9 | Portal guest hub (`/portal/index.html`) | BW, API | PASS | Scenario D: request created `SRQ-20261018-A3F45C348178`, comment, RESOLVED; cancel path `SRQ-…-369DF6D3FAC7`; session token memory-bearer only. |
| 10 | QR service page (`/qr-service.html`) | BW, API | PASS | Room QR `QR-54038A3EA22BB2B8B091` → `SRQ-…-98DF12A97905` (+ idempotent replay, bad token 404); table QR `QR-57FBAEC38BB640413B7F` FOOD_BEVERAGE-only (wrong category 400) → linked POS BAR order `POS-20261018-ADAC3AEF310A`; browser-form submit 201 `SRQ-20261018-7007189D6784` (shots `qr-service-*.png`). Barcodes unaffected — QR here is the **existing** guest service-link feature; no new QR introduced. |
| 11 | Offline / 403 pages | BW, UT | PASS | Render standalone; PWA suite covers offline shell. |

## B. Staff authentication & devices

| # | Module / page | Methods | Status | Evidence & notes |
|---|---|---|---|---|
| 12 | Login (`/login.html`) | BW, API, AXE | PASS† | Desktop-key gate proven: non-management roles without `X-JONE-Desktop-Key` → 403 at sign-in; exempt roles (ADMIN/MANAGER/GENERAL_MANAGER) pass without key — GM login 200 (Scenario F2). OBS-6: this gate is the production POS suspect. AXE 0 violations post-#5. |
| 13 | Workstations / terminals enrollment | BW, API | PASS | Terminal enrolled + workstation session created in local browser flow (key stored `jone.receptionistDesktopKey`); POS then fully operable. Physical dedicated terminal hardware: BLOCKED (B3-style, no device in env). |
| 14 | Staff management & profiles | BW, UT | PASS | Pages clean in 42-page sweep; capability map verified (`apps/accounts/capabilities.py`): CASHIER no guest-request create (403 proven = BY DESIGN), CHEF kitchen queue only, PROCUREMENT_OFFICER/STOREKEEPER manage-only, ACCOUNTS_MANAGER finance-only (no procurement caps — 403 proven = BY DESIGN, approvals sit with ADMIN/GENERAL_MANAGER). |
| 15 | Audit logs / audit-log-details | BW, API, UT | PASS | Append-only trail verified live: `PURCHASE_ORDER_CREATED ×4`, `PURCHASE_ORDER_APPROVED ×3`, **`ADMIN_OVERRIDE ×2`**, `GOODS_RECEIPT_POSTED`, `SUPPLIER_PAYMENT_RECORDED ×2`, `SERVICE_QR_REQUEST_CREATED`, `SERVICE_QR_ROTATED ×2`, `PORTAL_SESSION_CREATED`, `PORTAL_ACCESS_CHALLENGE_CREATED`, `STAFF_SIGN_IN ×8` (`scenario_gi` run + `prod` n/a). |
| 16 | Settings | BW | PASS | Loads clean; admin-only guard 403 page verified for staff role. |

## C. Front office

| # | Module / page | Methods | Status | Evidence & notes |
|---|---|---|---|---|
| 17 | Dashboard (index KPIs) | BW, API, AXE, M | PASS | `/api/admin/dashboard/` 200 (today: in_house 3, rooms 18 total/15 available — matches Scenario D state exactly). AXE 0 post-#5. |
| 18 | Approvals inbox | BW, API, UT | PASS | `/api/admin/reports/approvals/` 200 (`total_pending`, `categories`); finance approvals list+drill exercised in accounting verification. |
| 19 | Bookings admin (list/details/missed) | BW, API, UT | PASS | Scenario A/B: admin detail via guest token isolation (404 cross-booking ✓); `room_assignments` resolve; missed-bookings page clean. |
| 20 | Availability / occupancy (calendar grid) | BW, API | PASS† | Availability dict-shape consumed correctly by UI; occupancy report 200 (`reports_endpoints.json`). Calendar endpoint needs its documented params (my probe 400 = wrong param, page itself green in sweep). |
| 21 | Check-in | BW, API, UT | PASS | Scenario B walk-in `J1-20261018-0B34AC8D` → check-in; **future-date check-in correctly 409**; Scenario D in-house `J1-20261018-3AF38426` (room 203). |
| 22 | Check-out & folio clearance | BW, API, UT | PASS† | Scenario E: settle ₦26,000 `J1P-…-1956209A1C` → checkout due 0 → duplicate checkout idempotent; room → DIRTY → HK task `HKT-…-1D451A07E192` → CLEAN 18/18. Folio `FOL-20261018-81E1EC04E7A0` stays OPEN post-checkout — **shipped design** (checkout guarded by folio *clearance*, not folio status); `close_folio()` has zero production callers → OBS-2 recorded. |
| 23 | Guests | BW, API, UT | PASS | Guest create/update incl. phone normalization fix (DEFECT #3 path 3); profile merge/guest_id linkage verified via Scenario D (`guest_id 8`). |
| 24 | Rooms / room-details | BW, API | PASS† | Room status vs housekeeping_status separation correct; `max_guests` gate enforced (BY DESIGN). OBS-4: deep-link without params logs one cosmetic `/api/rooms//` 404. |
| 25 | Enquiries admin / offers admin | BW, UT | PASS | Sweep clean; suite green. |
| 26 | Receipts / receipt-details | BW, API | PASS | Scenario A receipt fetch via guest token; receipt detail renders (sweep). |
| 27 | Reviews admin / gallery admin | BW | PASS | Sweep clean. |
| 28 | Notifications / notification-details | BW, UT | PASS | Sweep clean; email outbox verified in unit env (`EMAIL_PROVIDER=django`; the 29 brevo failures are an `.env.example` onboarding artifact — OBS-5, not app code). |

## D. POS, F&B and guest services

| # | Module / page | Methods | Status | Evidence & notes |
|---|---|---|---|---|
| 29 | POS (`pos.html`) | BW, API, UT | **N/R (code)** | Urgent failure 2 **not reproducible**: full UI flows green as CASHIER → `POS-20261018-29CCC983C141`, `-906D20DDA38E` (PAID, kitchen ticket, drawer `CSH-…-DDE93DA85D13`), plus API chain `POS-20261018-3E8F24A6B4A5` and Scenario C/E orders. Category rules enforced (TAKEAWAY/RESTAURANT reject BAR items = BY DESIGN; cash requires open session = BY DESIGN). Production hypothesis: unenrolled terminal → 403 at sign-in (OBS-6, runbook in Doc 07/08). |
| 30 | Room service | BW, API | PASS | Scenario E: RS order `POS-…-9A58A2566398` charged to stay; Scenario D table-QR → linked BAR order delivered. |
| 31 | Guest requests (`service-requests.html`) | BW, API | PASS | Full lifecycle OPEN→ACK→IN_PROGRESS→RESOLVED; claim/comment; cancel; conversion to HK task `HKT-20261018-0332539EF111` and maintenance WO `MNT-20261018-9B4AECDFB860` (backlinked); events append-only; resolved-request re-service 400 ✓. |
| 32 | Service links (`service-qr.html`) | BW, API | PASS | Link issue/rotate/duplicate-400 semantics; QR ROOM requires exactly one IN_HOUSE stay (BY DESIGN gate). |
| 33 | Housekeeping | BW, API, UT | PASS | Cycle CLEAN→DIRTY→task→complete; admin cannot CLAIM (BY DESIGN); HK cannot COMPLETE own/shortcuts (BY DESIGN); 18/18 rooms CLEAN at Scenario E end. |
| 34 | Laundry | BW, API | PASS | `guestLaundry` collection 200; laundry page clean in sweep. Physical laundry hardware: n/a. |
| 35 | Maintenance | BW, API | PASS | WO lifecycle from converted request (Scenario D). |
| 36 | Facilities admin | BW | PASS | Sweep clean. |

## E. Inventory, procurement, finance

| # | Module / page | Methods | Status | Evidence & notes |
|---|---|---|---|---|
| 37 | Inventory (`inventory.html`) | BW, API, UT | PASS | Balances endpoint with `item_id`/`location_id`/`reorder` filters (working as designed); movement `STM-20261018-B098E1582C45` posted on GRN; DEMO-STORE Coke balance 12→22 exactly +10; replay refused with **no double receipt**. |
| 38 | Barcode labels (`barcode-labels.html`) | BW, API, CR | PASS/BLOCKED | Label generation + barcode lookup exercised at API level (POS barcode scan path, item lookup 200). **Physical scanner/printer hardware: BLOCKED (B3)** — not fabricated. No QR substitution introduced (constraint honored). |
| 39 | Procurement (`procurement.html`) | BW, API, UT | PASS | Scenario F complete: two-person approval (`PO-20261018-AEF4D8C911EC`: admin raised → **GM approved, no override marker**); maker-checker strict rule (`PO-…-E1035F647952`: GM self-approve **403** "A requester cannot approve their own purchase order…"); admin override only via `ALLOW_ADMIN_APPROVAL_OVERRIDE=True`, marked `[ADMIN_OVERRIDE]` in notes **and audit-logged**; lifecycle APPROVED→ORDERED→GRN→RECEIVED→PAID (outstanding 0.00); state gates (pay-before-approve 400, GRN-on-SUBMITTED 400, GRN-on-PAID 400). |
| 40 | Payments (`payments.html`) | BW, API, UT | PASS | Prod payment list: 2× PENDING PAYSTACK (never falsely marked successful — 502 resilience Scenario A) + 1× SUCCESS CASH (settlement `J1P-…-1956209A1C`). Live Paystack test-key gateway round-trip: BLOCKED (dummy key in local env; B5). Stale prod payment `J1P-20261010-199B12784F` ₦387,000: BLOCKED pending owner (B2). |
| 41 | Accounting (`accounting.html`) | BW, API, UT, AXE | **FIXED** | DEFECT #4 (scope) + OBS-1. Post-fix: 0 page errors; approvals/expenses/payables/cash tables render; create-expense draft E2E `EXP-20261018-403D2C4C4C19` 201; pinned anchor-order test 12/12; frame-scan of 70 pages → 0 similar violations elsewhere. |
| 42 | Reports (`reports.html`) | BW, API | PASS | All 9 endpoints 200 with correct shapes (`reports_endpoints.json`): finance summary, night audit, balance sheet, cash flow, AR aging, occupancy, revenue, bookings, approvals. Params: reports take `start_date`/`end_date`; finance summary takes `start`/`end`+`currency` — UI already calls them correctly. |
| 43 | Payroll (`payroll.html`) | BW, UT | **BLOCKED** | Page loads clean; payroll app covered by backend suite (green). Business validation (rates, tax IDs, statutory rules) **BLOCKED (B4)** — owner must supply inputs; nothing invented per constraint. |
| 44 | Workforce (`workforce.html`) | BW, UT | PASS | Scheduling/attendance surfaces clean in sweep; suite green. Biometric clock-in hardware: BLOCKED (B3) — fingerprint path not substituted or faked. |
| 45 | PWA layer (sw.js, install, offline) | BW, UT | **FIXED** | DEFECT #1/#2/#5. Post-fix: `update-checker` suite flips to PASS (149/151 vs HEAD 148/151 — the flipped check **is** the #2 proof); install button `display:none`/0×0/unfocusable when hidden; offline shell green; `test_pwa` install-control + `test_table_layout` phone-wrap failures proven **pre-existing at HEAD** (test debt, documented — not touched to force green). |

## F. Cross-cutting

| # | Area | Methods | Status | Evidence & notes |
|---|---|---|---|---|
| 46 | API envelope & errors | API, CR | PASS† | Structured `VALIDATION_ERROR`/`RESOURCE_NOT_FOUND` envelopes consistent on routed endpoints (local + prod). OBS-3: unmatched routes fall to Django default **HTML** 404 (low impact; frontend only calls known routes). |
| 47 | AuthN/AuthZ & tokens | API, CR | PASS | JWT + role/capability gates enforced at view layer (403s proven, never bypassed to make tests pass); guest `X-Guest-Access-Token`, portal `X-Portal-Session`, QR `X-Service-QR-Token`, desktop `X-JONE-Desktop-Key` — four cleanly separated token realms; cross-realm usage rejected (404/403). Throttling active (429 after failures). |
| 48 | Idempotency & replay safety | API | PASS | Booking create, GRN, QR request, settle, checkout — replay-safe (Doc 06). |
| 49 | Responsive/mobile | BW, M | PASS | 390px: no horizontal overflow across sweeps; phone table strategy (`width:max-content` + `table-fit.js`) working; 2 `test_table_layout` wrap expectations are stale tests (pre-existing at HEAD). |
| 50 | Accessibility | AXE | PASS | 0 violations on representative pages pre/post fixes (index, booking, dashboard, pos, login post-#5). |
| 51 | Versioning/release hygiene | UT, BW | **FIXED** | DEFECT #1/#2; `version.json` now 1.1.24 in tree; live Vercel still 1.1.23 **until authorized deploy** (Doc 08). |

**Coverage claim:** every page enumerated in the mandate (incl. all discovered pages: portal, qr-service, booking-confirmation, payment-verify, offline, 403, terminals, missed-bookings, receipt-details, notification-details, audit-log-details, room-details, staff-profile, service-qr) appears above and was loaded in a real browser with console+network capture; every backend app (`accounts, audit, bookings, finance, guest_services, housekeeping, inventory, portal, pos, reports, rooms, service_links, staff, stays, workforce…`) is covered by the 1548-test suite plus the scenario matrix.
