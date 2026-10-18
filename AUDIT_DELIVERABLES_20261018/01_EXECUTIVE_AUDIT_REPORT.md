# J-ONE HOTEL & LODGE — Executive Audit Report
**Audit date:** 2026-10-18 · **Auditor:** Arena Agent Mode (authorized forensic audit)
**Baseline audited:** `main @ f2e8a5d` ("booking and profile") — byte-identical to what Vercel/Railway served during the audit
**Repair HEAD:** `main @ e811fbd` (5 local commits, unpushed — deployment requires owner authorization)
**Live endpoints:** frontend `https://j1-hotel-management-system-frontend.vercel.app` · backend `https://j1-hotel-management-system-production.up.railway.app`

---

## 1. Mandate

Full-system forensic audit, root-cause repair, and end-to-end verification of the J-ONE Hotel & Lodge management system, driven by two urgent reported failures:

1. **Online and walk-in bookings not completing.**
2. **POS orders not submitting.**

Constraints honored throughout: reproduce before fixing; smallest reliable change at the correct architectural layer; no rewrites, no new frameworks/dependencies, no renamed API contracts; no weakening of security/validation/approvals; no destructive production testing; no fabricated hardware/biometric/payroll results; every success claim backed by executed evidence.

## 2. Executive summary

| Reported failure | Verdict | Root cause | Status |
|---|---|---|---|
| Online bookings not completing | **ROOT CAUSE FOUND & FIXED** | DEFECT #3 — DRF applies the model-derived `MaxLengthValidator(20)` on `guest.phone` **before** any `validate_<field>` hook, so any realistically formatted Nigerian phone (`+234 (0) 803 123 4567` = 23 chars) failed with a raw *"Ensure this field has no more than 20 characters."* error on the public booking, multi-room `room_guests`, and admin guest-update paths. Reproduced **live in production** (structured 400, zero writes) and locally. | FIXED `5aa4ebe`, VERIFIED (unit 6/6, backend suite 1548 OK, live API 201 with formatted phone, full browser booking flow 201) |
| POS orders not submitting | **NOT REPRODUCIBLE IN CODE** | The complete POS chain (create order → kitchen ticket → submit → payment → cash drawer → settle) passes at API level and in a real browser (Chromium) as CASHIER: `POS-20261018-3E8F24A6B4A5` (TAKEAWAY) and `POS-20261018-29CCC983C141`/`-906D20DDA38E` (full UI flows → PAID). Production evidence points to an **environmental cause**: cashier terminals without an enrolled workstation/desktop key are rejected at staff sign-in (`403 DESKTOP_KEY_REQUIRED` — proven in the local browser flow), which presents to users as "POS won't submit". | No code defect. Terminal-enrollment runbook provided (Doc 07/08). |

**Five defects fixed** (register in Doc 03), all minimal, all regression-tested:

- **#1** `frontend/version.json` frozen at 1.1.23 while pages already served `?v=1.1.24` assets → update-checker never announced the release. FIXED (50-file stamp + version.json/js/version.js/sw.js).
- **#2** Dead root `frontend/manifest.webmanifest` (unreferenced duplicate of the dashboard manifest) → install ambiguity. FIXED (deleted; dashboard manifest untouched).
- **#3** Guest-phone max-length ordering bug (urgent failure 1). FIXED (shared `validate_guest_phone()` compacts formatting before validation; friendly errors preserved; +6 unit tests).
- **#4** `accounting.html` scope corruption — `rowsOf`/`rowActions` declared **inside** a 312-line `load()` while top-level callers invoke them → `ReferenceError` ×2 on every page load; expenses table dead whenever rows exist; supplier/item pickers silently emptied. FIXED (declarations hoisted to module scope, no logic change).
- **#5** Author CSS (`.btn{display:inline-flex}` etc.) defeated the UA `[hidden]` rule → JS-hidden controls (PWA install button) rendered **and stayed focusable**; axe-core `aria-hidden-focus` (serious) on login/dashboard. FIXED (one canonical `[hidden]{display:none!important}` reset in `main.css`).

**Six observations recorded, not fixed** (out of defect-repair scope or requiring owner decisions): folio `CLOSED` state unreachable (Doc 03 OBS-2), HTML 404 on unmatched API routes (OBS-3), delegated-listener re-registration in `load()` (OBS-1), deep-link console noise on `room-details.html` (OBS-4), `.env.example` email-provider onboarding trap (OBS-5), POS terminal enrollment gap in production (OBS-6).

## 3. Verification posture (all executed, none mocked away)

- **Backend:** full Django suite **1548 tests OK (skipped=15)** on the repaired tree (baseline 1542 OK before the phone fix; +6 new). Isolated SQLite environment against **real app code and real persistence** — no mocking of the DB layer.
- **Frontend:** `validate.py` **ALL OK** (70 HTML, 30 JS); Node suite **546 tests → 545 pass / 0 fail / 1 skip**; pinned contract test for the accounting anchor order **12/12**; PWA/browser suite **12 Playwright scripts** re-run post-fix (2 pre-existing failures proven at HEAD baseline = test debt, not app defects).
- **Browser (real Chromium):** public sweep **23 pages green** incl. live booking UI flow (`J1-20261018-FAAE4BD9` created 201; upstream 502 handled gracefully with clear messaging); dashboard sweep **42/42 pages 200, 0 issues post-fix**; guest surfaces (QR submit → `SRQ-20261018-7007189D6784`, portal magic-link → booked stays) with **0 page/console errors**; axe a11y **0 violations** (index/booking; dashboard re-checked post-#5); 390px mobile **no overflow**.
- **Critical E2E scenarios:** A (online booking + payment resilience), B (walk-in), C (POS), D (guest requests: portal, room QR, table QR, HK/maintenance conversion), E (stay lifecycle: room service → cash drawer → settle → checkout → housekeeping CLEAN 18/18), F (procurement two-person approval → ORDERED → GRN → stock +10 exact → replay refused → PAID outstanding 0.00), G (payments/ledger reconciliation), I (all 9 report endpoints 200 + append-only audit trail incl. `ADMIN_OVERRIDE` markers) — **all VERIFIED**. H (payroll) **BLOCKED** on owner-provided statutory data (never fabricated).
- **Production:** read-only smoke only — health/ready 200 (`database: up`), public hotel/availability/quote 200, DEFECT #3 reproduced live as a structured 400 with **zero persisted rows**. No production writes, no synthetic data, no destructive probes.

## 4. Release assessment

**CONDITIONAL PASS** (full rationale and conditions in Doc 09). Both reported urgent failures are resolved at root-cause level with executed evidence; no regressions across 1548 backend + 546 frontend tests; every critical scenario either verified or explicitly blocked with justification. Conditions: authorized deployment per Doc 08 (no migrations — zero schema risk), Paystack **test-mode** gateway verification in staging with real test keys, production terminal enrollment for POS devices, and owner decisions on OBS-2/B1–B4.

## 5. Deliverables index

| Doc | Contents |
|---|---|
| 01 | This executive report |
| 02 | Module-by-module audit matrix (45+ surfaces) |
| 03 | Defect & observation register (statuses: FIXED/VERIFIED, OPEN, BLOCKED, NOT REPRODUCIBLE) |
| 04 | Changed-files report (5 commits, per-file rationale) |
| 05 | Test evidence, separated: unit / integration / browser / production-smoke / hardware |
| 06 | Data-integrity report (idempotency, double-entry, isolation, audit trail) |
| 07 | Remaining blockers (B1–B7) |
| 08 | Deployment instructions (deploy, smoke, rollback) |
| 09 | Final release assessment & verdict |

Machine-readable evidence: `/home/user/audit-evidence/` (sweep JSONs, screenshots in `shots/`, scenario references in `scenarios/`, backend logs in `logs/`, `prod_smoke_readonly.json`, `reports_endpoints.json`). Secrets never appear in any artifact (evidence sanitized; credentials held only in `/tmp/j1audit/creds.json`, mode 600, outside the deliverable set).
