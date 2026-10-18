# Data-Integrity Report — J-ONE Hotel & Lodge (2026-10-18)

Scope: integrity of persistence behavior observed during the audit (isolated local DB + read-only production inspection). Verdict: **no integrity violations found**; one design gap recorded (OBS-2, folio terminal state) with no data-loss or double-count consequence.

---

## 1. Exactly-once semantics (replay safety) — VERIFIED

| Operation | Replay behavior observed | Evidence |
|---|---|---|
| Public booking create (same `idempotency_key`) | Original response returned; **no duplicate booking** | Scenario A `J1-20261018-15634C61` replay |
| QR service request (same `idempotency_key`) | Original response echoed; single `SRQ` row | Scenario D room-QR replay |
| Goods receipt note (same `idempotency_key`) | **400 refused** once PO fully RECEIVED; stock balance **unchanged** (22 → 22); no second movement row | Scenario F3 `GRN-20261018-640E5C4BD0D5` replay |
| Checkout (second call) | Idempotent no-op; balance stays 0; no duplicate FIN transactions | Scenario E |
| Settle-balance | Single `J1P-…-1956209A1C` ₦26,000; folio due → 0.00 | Scenario E |

Note: consumed keys never create second effects — the exact property that protects bookings/payments from double-submits on flaky hotel Wi-Fi.

## 2. Stock ledger accuracy — VERIFIED

GRN posting for item 9 (DEMO-B-COK-50) at DEMO-STORE: balance **12 → 22 (+10.0000 exact)** with exactly one movement `STM-20261018-B098E1582C45`; replay produced **no movement and no balance change**. Balances endpoint filters (`item_id`, `location_id`, `reorder`, `item_type`) work as coded (earlier "ignored filter" observation was an auditor param-name error — `item` vs `item_id` — confirmed against `StockBalanceListView` source).

## 3. Financial double-entry — VERIFIED

- Posted ledger headers immutable (`FolioPosting(ImmutableFinancialModel)`; finance transactions list shows typed entries only: POS_CHARGE ×8, PAYMENT_COLLECTION ×4, PAYMENT_ALLOCATION ×1, CASH_OPEN ×2 in audit window).
- Double-entry balance proven in POS E2E (tender = charge totals; drawer expected-vs-counted reconciliation on session close with PENDING_REVIEW path for discrepancies).
- Night audit report exposes `discrepancies` + `cash_drawers` collections (200 live).
- AR aging buckets reconcile with outstanding balances (PO outstanding 0.00 after payment; folio due 0.00 after settlement).

## 4. Payment-state honesty — VERIFIED

- Gateway failure (502) leaves payment **PENDING** and booking intact — never falsely SUCCESS (Scenario A; production list shows 2× PENDING PAYSTACK from real historical attempts, still honestly pending).
- Payment success determined by provider/backend verification, not browser redirect (design review + `payment-verify.html` behavior).
- Cash tender requires an **open cash session** (BY DESIGN gate) — no orphan drawer money.

## 5. Approval & authorization integrity (maker-checker) — VERIFIED

- Requester ≠ approver enforced: GM self-approve → **403** with explicit message (`PO-…-E1035F647952`).
- Two-person approval recorded on the record: `requested_by` (demo.generalmanager) / `approved_by` (test.admin) both persisted and returned (`PO-…-AEF4D8C911EC` clean, no marker).
- Admin self-approval possible **only** through the deliberate, env-gated override (`ALLOW_ADMIN_APPROVAL_OVERRIDE=True`), which appends `[ADMIN_OVERRIDE] …` to PO notes **and** writes an `ADMIN_OVERRIDE` audit event (×2 observed) — override is visible, attributable, never silent.
- Capability matrix enforced server-side (ACCOUNTS_MANAGER has finance/payroll/expense.approve only — procurement 403; CASHIER/CHEF/STOREKEEPER boundaries all held). **No control was weakened at any point to make a test pass.**

## 6. Audit trail — VERIFIED append-only

Every sensitive scenario action surfaced in `/api/admin/audit-logs/` with actor + reference metadata: `PURCHASE_ORDER_CREATED ×4`, `PURCHASE_ORDER_APPROVED ×3`, `ADMIN_OVERRIDE ×2`, `GOODS_RECEIPT_POSTED` (GRN ref), `SUPPLIER_PAYMENT_RECORDED ×2` (PO refs), `SERVICE_QR_REQUEST_CREATED`, `SERVICE_QR_ROTATED ×2`, `PORTAL_SESSION_CREATED`, `PORTAL_ACCESS_CHALLENGE_CREATED`, `STAFF_SIGN_IN ×8`. Service-request event streams append-only (comments/status history immutable in Scenario D).

## 7. Isolation & production-data protection — VERIFIED

- All scenarios ran against the **local SQLite DB** with clearly-marked demo data (DEMO-* suppliers/items/locations, "Ada Portal" test guests, rooms 201–203 in-house test stays). Production MySQL never connected.
- Production touched **read-only only** (Doc 05 §5): the single POST probe was constructed to be guaranteed-non-creating (validation-failing payload: 23-char phone + past dates) and returned 400 — **zero production rows created/modified/deleted**; no resets, no truncates, no genuine guest/financial record touched; stale payment `J1P-20261010-199B12784F` left untouched pending owner (B2).
- Cleanup posture: test-run records live only in the isolated local DB (fine to retain as evidence; disposable environment). No test artifacts remain in production.
- Secrets hygiene: credentials only in `/tmp/j1audit/creds.json` (chmod 600, outside deliverables); evidence JSONs scanned for password/token patterns → clean; QR/portal tokens not persisted in artifacts.

## 8. Known integrity-adjacent gap (no data loss observed)

**OBS-2 — folios never reach CLOSED:** `close_folio()` has zero production callers and no endpoint exists, so folios stay OPEN after checkout/settlement. Consequences assessed: checkout is guarded by **folio clearance** (zero-balance check) so guests cannot depart with debt; post-checkout postings remain possible (arguably a feature for late charges) and stay fully audited; "open folio" style reporting would over-count historical folios. No double-charge, no balance drift, no orphan money observed. Owner decision requested (auto-close at zero-balance checkout vs. controlled close endpoint) — deliberately **not** implemented here (new API contract).

## 9. Consistency spot-checks

- Dashboard KPI ↔ scenario state: in_house 3 = the three local "Ada Portal" stays; rooms 18 total / 15 available / 0 OOO; occupancy report day-grid consistent.
- Booking ↔ stay ↔ folio ↔ transactions cross-references resolve (room assignments, folio postings, payment allocations all linked by reference in API responses).
- Room dual-status model (`status` vs `housekeeping_status`) transitions coherently (checkout → DIRTY; HK complete → CLEAN 18/18).
