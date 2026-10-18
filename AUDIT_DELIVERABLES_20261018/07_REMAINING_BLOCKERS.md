# Remaining Blockers — J-ONE Hotel & Lodge (2026-10-18, post-repair)

Supersedes root `REMAINING_BLOCKERS.md` (B1–B4). None of these blocks the **release of the repaired code**; they block full closure of specific audit lines. Each has an owner action and a verification step.

---

## B1 — Production second approver for procurement · Owner: hotel management
- **Status:** demonstrated **in isolation** (GM account id 1 + ADMIN: clean two-person approval `PO-20261018-AEF4D8C911EC`; self-approval 403; override marked + audit-logged). Production still needs a **real second authorized account** (GENERAL_MANAGER) with a strong password and, ideally, `ALLOW_ADMIN_APPROVAL_OVERRIDE=False` in the production `.env` so the maker-checker rule is absolute.
- **Action:** create/confirm the GM account in prod; decide the override flag (recommended: `False` in prod — the flag exists precisely so small properties can operate single-admin, but with two approvers available it should be off).
- **Verify:** raise a test PO in prod (or staging) as admin → approve as GM → confirm no `[ADMIN_OVERRIDE]` marker; attempt self-approve → expect 403.

## B2 — Stale production payment `J1P-20261010-199B12784F` (₦387,000) · Owner: hotel management
- Real financial record predating the audit; reconciliation/inspection requires owner authorization (no production writes were permitted). Paystack dashboard reference cross-check needed.
- **Action:** owner (or authorized agent with prod DB/Paystack access) reconciles against provider settlement records; if orphaned PENDING, resolve per Paystack test/live-mode evidence.
- **Verify:** payment status matches provider truth; audit log entry for any manual resolution.

## B3 — Physical hardware (fingerprint biometrics, barcode scanner/printer, dedicated terminals) · Owner: site IT
- No hardware in the audit environment; **results were not fabricated**. Logical layers verified (barcode lookup API, label page, attendance APIs, enrollment flow).
- **Action:** on-site pass — scan a real label into POS item lookup; print a receipt; enroll a fingerprint; clock in/out.
- **Verify:** each device operation produces the expected record (movement/order/attendance row).

## B4 — Payroll statutory inputs · Owner: hotel management/finance
- Payroll module loads and its test suite is green, but rates, tax IDs, and statutory rules must come from the owner (invention explicitly prohibited).
- **Action:** provide statutory configuration; run one supervised payroll cycle in staging.
- **Verify:** payslip totals match owner-provided expectations to the kobo.

## B5 — Paystack gateway round-trip in test mode · Owner: hotel management (keys)
- Local env has a dummy key → gateway calls 502 (which the app handles **correctly**: PENDING, honest messaging, no fake success — Scenario A). The repaired booking flow still deserves one **test-mode** end-to-end: initialize → redirect → callback → verify.
- **Action:** supply Paystack **test** keys to staging only; run one test-card booking; confirm webhook/verify path marks SUCCESS only on provider confirmation.
- **Verify:** payment row SUCCESS with provider reference; booking PAID; no real charge (test mode).

## B6 — Folio close lifecycle (OBS-2) · Owner: product decision
- `Folio.Status.CLOSED`/`close_folio()` unreachable in the shipped product; folios stay OPEN after checkout (clearance-guarded, so no integrity loss found).
- **Action:** decide between (a) auto-close folio on zero-balance checkout inside `check_out_booking` (smallest change, no new contract) or (b) expose a controlled close endpoint for late-charge workflows. Either is a **new behavior** → outside this repair mandate.
- **Verify:** after implementation, Scenario E re-run should show folio CLOSED (option a) or close-on-demand with nonzero-balance guard (option b).

## B7 — Production POS terminal enrollment (OBS-6, urgent failure 2) · Owner: site IT
- POS code fully verified; the plausible production cause is terminals without enrolled workstation/desktop keys (sign-in 403 → "POS won't submit").
- **Action:** at each POS device: `terminals.html` → enroll workstation → sign in as cashier → confirm key persisted (`jone.receptionistDesktopKey`) → place a test order. If failures persist **with** enrollment confirmed, capture the exact HTTP response body + console from the device and reopen with that evidence.
- **Verify:** one complete order → PAID per device.

## B8 — Deployment authorization · Owner: hotel management
- All fixes are committed locally (`main @ e811fbd`) and **unpushed** per the deploy-only-when-authorized constraint. Live Vercel still serves 1.1.23 (with DEF-4/#5 present in prod pages) and Railway serves pre-fix backend (DEF-3 live — repro proven read-only).
- **Action:** authorize push/deploy; follow Doc 08 exactly (no migrations; rollback trivial).
- **Verify:** post-deploy smoke per Doc 08 §4 (version.json 1.1.24; formatted-phone booking probe on staging first).
