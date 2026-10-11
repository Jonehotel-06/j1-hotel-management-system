# REMAINING BLOCKERS

Only items that **cannot be completed from the repository** are listed here. Everything
else in the audit's defect register is either implemented, pinned by a regression test, or
recorded as an owner decision in `REPAIR_COMPLETION_REPORT.md` §10.

Each entry gives: the exact issue, the evidence that establishes it, why it is blocked, the
credential or decision required, and the safest next action.

---

## B1 — D2: production two-person approval cannot be demonstrated

**Issue.** The separate-approver control must be *shown* refusing a self-approval and
*shown* accepting an approval by a second, distinct person, against real records.

**Evidence that it is blocked (not broken).**
- `backend/tests/test_separate_approver_control.py` → 8 OK: the service and the API both
  refuse a requester approving their own sensitive monetary action, with the reason string
  `A requester cannot approve their own sensitive monetary action.`, and the permitted case
  persists an `ApprovalRequest` whose requester ≠ reviewer.
- `backend/apps/core/exception_handler.py` preserves that reason in the error body.
- The control policy reads TRUE (enabled) in code; it was **not** disabled to make
  anything pass.
- Production holds 4 pending stock counts, 1 submitted purchase order and 1 pending
  expense. **They were deliberately left pending**: approving them would consume the only
  real evidence the audit can use.

**Why blocked.** Demonstrating the control in production requires a second authorised
human approver. There is exactly one authorised approver available. Creating a second
synthetic approver and approving *as* it would fabricate the very evidence the audit
asked for, so it was not done.

**Required.** A second, real, authorised approver account operated by a different person.

**Safest next action.** Leave the 4 counts / 1 PO / 1 expense pending. When the second
approver exists, the verification is: (1) approver A requests approval on one synthetic
record, (2) approver A attempts to approve it and must receive the 403 with the reason
string above, (3) approver B approves it and the `ApprovalRequest` row must show
requester ≠ reviewer. Until then the code-level evidence above stands and the production
claim is **BLOCKED**.

---

## B2 — D9: stale Paystack payment ₦387,000 on an expired booking

**Issue.** Reference `J1P-20261010-199B12784F` (₦387,000) is attached to a booking that
has expired. The audit flagged it as a reconciliation risk.

**Evidence.** The payment and booking exist in production as found; no provider call was
made from this environment and nothing was altered, deleted, refunded or re-posted.

**Why blocked.** Resolving it requires (a) the provider's authoritative status for that
reference — which only the owner can obtain from the Paystack dashboard or API — and
(b) an explicit instruction on the desired end state. Guessing either would risk
fabricating a financial entry, which the repair brief forbids.

**Required.** Verified Paystack status for that reference **and** an owner decision
(refund / apply / void / leave).

**Safest next action.** Read-only: view the reference in the Paystack dashboard and
compare its status with the stored payment row. Then, in a maintenance window, apply the
owner's instruction through the application's own controlled endpoints — never by direct
database edit, so the ledger stays consistent and auditable.

---

## B3 — N5-adjacent: biometrics end-to-end (attendance / face / fingerprint)

**Issue.** The attendance, shift and biometric flows could only be verified at the API
level; no enrolment or verification was executed.

**Evidence.** Lists respond with 0 rows; the attendance/shift scripts are syntax-checked;
the biometric endpoints and their permission tests pass. No biometric material exists in
this environment.

**Why blocked.** No fingerprint reader, no camera enrolment material, and no
owner-sanctioned synthetic template. Producing a "biometric result" without hardware would
be fabrication.

**Required.** A workstation with the reader attached (or an owner-approved simulated
device), plus one consented staff member for the enrolment.

**Safest next action.** Run the recorded procedure on the reception workstation: enrol one
consented test staff member, clock in and out, and confirm the attendance rows and audit
entries. Until then biometrics remain **BLOCKED**, not passed.

---

## B4 — Payroll statutory correctness

**Issue.** Payroll periods, compensation, statutory rules and tax-identity lists exist but
contain 0 rows; no draft or salary action was performed.

**Evidence.** Lists return 200 with 0 rows; no payslip was created; the documented
self-service route (`/payroll/my-payslips/`) was used rather than the bare collection
(the audit's N5 bare-path 404 is **not** a defect and was not "fixed").

**Why blocked.** Statutory rates and tax identifiers must come from the owner. Inventing
them would produce incorrect salaries and incorrect statutory filings.

**Required.** The owner's verified statutory rates and tax identifiers for the operating
jurisdiction.

**Safest next action.** Enter the owner-supplied rates in the configuration, then run a
draft period with a test employee and verify the computed tax before any real run.

---

## B5 — Guest portal email delivery

**Issue.** The verified-email guest portal depends on outbound mail; delivery was not
verified.

**Evidence.** The email pipeline is covered by tests against a captured transport; no real
message was sent.

**Why blocked.** Requires a reachable provider and a real test recipient; sending mail to
real guests from this environment was prohibited by the repair brief.

**Required.** Provider credentials (or a test mailbox) and one authorised test recipient.

**Safest next action.** Configure a test recipient, request a portal link, and verify the
message and the opaque-session flow end to end.

---

## B6 — SEC-01 / SEC-02 as deployed headers

**Issue.** The Content-Security-Policy and the CORS origin allow-list are implemented in
the repository, but what the *deployed* hosts serve was not verified.

**Evidence.** `frontend/vercel.json` contains the enforcing policy plus a `Report-Only`
twin; `backend/config/settings/production.py` contains the allow-list. Local tests
exercise the code paths, not the deployed edge.

**Why blocked.** No deployment was performed from this environment, and no hosting
dashboard was inspected — claiming a deployed header without seeing it would violate the
brief.

**Required.** a redeploy (or an owner-run `curl -I` against both hosts).

**Safest next action.** After redeploy, run:
`curl -sI https://j1-hotel-management-system-frontend.vercel.app | grep -i content-security-policy`
and an `OPTIONS` preflight against an allow-listed and a foreign origin. Roll the policy
out with the `Report-Only` twin already in place and only enforce once the reports are
clean.

---

## B7 — Demo/test-data cleanup (23 synthetic accounts IDs 2–24, workstation ID 1)

**Issue.** 23 synthetic role accounts (labelled DEMO QA) and one demo front-desk
workstation remain active; the workstation key was never rotated.

**Evidence.** `evidence/demo_staff_register.csv` in the audit package lists them; the
audit's handover brief marks the cleanup as incomplete. Nothing in this repair touched
them.

**Why blocked.** Deactivation is an authorised workflow action against production records
and requires admin access; the repair brief forbids deleting or mutating production data
without positive identification plus authorisation. No raw workstation key, password or
token is printed anywhere in this repository or this report.

**Required.** Authorised admin access (or the owner running the procedure), plus
confirmation that the identified IDs are the audit's temporary test accounts.

**Safest next action, in order:**
1. Sign in as an administrator and open the staff list; confirm each of IDs 2–24 still
   carries the audit's DEMO QA label and has no bookings, shifts or approvals attributed
   to it.
2. Deactivate them through the normal staff workflow (do not delete rows, so the audit
   trail survives).
3. Rotate or deactivate the demo workstation (ID 1) through the terminals page, then
   re-issue a key to the real reception desk and confirm that stored logins issued with
   the old key stop working.
4. Record each deactivation's audit-log entry as the evidence.

Until step 1 confirms the identification, this item stays **BLOCKED** rather than done.

**On the audit's "junk probe expense" (`EXP-20261010-5D665B63E07A`, ₦1.00).** The vendor
report records it as "undeletable via API: 405 DELETE / 403 review" and suggests a
vendor-side DB delete. Both refusals are **by design, and are the correct behaviour**:

- the API exposes no `DELETE` on an expense at all (hence 405), and
- the model itself refuses both mutation and deletion once an expense is posted -
  `posted.save()` raises `ValidationError: ... immutable` and `posted.delete()` raises
  `... cannot be deleted`. That is pinned by `backend/tests/test_expenses.py`
  (`test_...` maker-checker/immutable-ledger test, lines 48-52), so a future change cannot
  quietly make financial records deletable.

The 403 on `review/` is the separate-approver control working (see B1), not a defect. So the
record can only be removed by an owner-initiated database action; because it is labelled, ₦1.00
and carries an audit trail, leaving it is the recommended option.

---

## B8 — Owner decisions (not blockers, but not ours to make)

- **D8** — the correct property address/locality. No location was invented.
- **D10** — whether "Poolside 1" is the intended name for table T4. No source-level defect
  was found; the table was not renamed.

Both are one-line updates once the owner answers; nothing else in the repair depends on
them.

---

## B9 — Accessibility manual review and cold-cache performance are not repository tasks

Neither is a defect and neither is faked here.

- **`A11Y-002` (manual keyboard / screen-reader / zoom-reflow review).** The automated scan
  is clean - public pages **260 → 0** violation nodes across 27 pages, dashboard **906 → 0**
  across 40, plus the pixel-level POS checks - but an automated scan is not a substitute for
  a human with assistive technology, and this repair does not claim WCAG conformance from
  one. **Required:** a manual pass (keyboard-only walkthrough, one screen reader, 200 % zoom).
- **`PERF-002` (cold cache, throttled network, LCP/CLS/TTFB).** The local before/after timings
  show no regression and no gain (`docs/verification/performance-before-after.json`), which is
  all a local static server can show. The *query analysis* half of this case is done: the
  backend query profile (`docs/verification/api-query-profile.json`) shows every guarded list
  endpoint flat at 0 queries per added row, and `AdminListFlatnessTests` keeps it that way.
  **Still required:** a deployed environment, a cold cache and a real network profile, against
  an agreed service-level objective - none has been agreed, so no pass/fail verdict is claimed.

**Dependency scanning is done, and must stay done.** `pip-audit` is clean for both the
environment (76 packages) and `requirements.txt` (54 top-level pins), and the scan caught a
genuine inherited exposure: `djangorestframework` 3.16.1 carried `CVE-2026-73228` and
`CVE-2026-73229`, and the old `,<3.17` pin excluded every fixed release. The pin is now
`>=3.17.2,<4.0` and the environment is on 3.18.3. **Recurring step for the owner:** run
`pip-audit` in CI (or before each release) - a clean scan today says nothing about the next
advisory. The frontend has no npm dependency surface, so there is nothing to scan there.

---

## B10 — Vendor handover items the audit raised that are neither defects nor repo tasks

From the audit's own closure report (section G), recorded here so they are not mistaken for
unfinished repair work. None of them is a code defect, and none was invented or guessed:

| Item | Status | What the owner must do |
|---|---|---|
| Demonstration imagery is representative, not the real property | Unchanged by design | Replace with genuine property photographs before go-live. Every such image is captioned as representative and `img/sources.json` lists licences/attribution. |
| SMTP is not configured, so receipts / portal links / review invites fail at the mail stage | Blocked (see B5) | Configure SMTP and a test mailbox. The code path and its tests exist; delivery cannot be demonstrated without credentials. |
| Deposit policy (100 %) and the 15-minute pending-booking window are defaults, not decisions | Awaiting owner | Confirm both. They are one-line setting changes, not code work, and nothing was changed to guess them. |
| QR codes should be verified before printing | Audit verified 5 destinations working | Re-verify after the address decision (B8) and before printing. |
| Console cleanliness "32 of 33 pages" | Resolved | The remaining page was `accounting.html` (D1), now fixed and covered by the endpoint-map contract tests. |

---

## Not blockers (recorded so they are not re-opened)

- **Bare `/payslips/` 404 (audit N5)** — not a defect; the documented routes are
  `/payroll/my-payslips/` and `/payroll/payslips/{reference}/`.
- **Legacy `workstations.html` 404 (D11)** — not a defect; `/dashboard/terminals.html` is
  the supported page and is preserved.
- **Legacy `/api/admin/pos/cash-sessions/` 404 (D12)** — the path is intentionally retired;
  the frontend reads the finance cash-session collection.
- **`acknowledge` / `resolve` / `reply` enquiry 404s (D15)** — not called by the frontend;
  the PATCH lifecycle route is the supported contract.
- **Audit cases `HR-002`, `PAY-004` and `SEC-005` (previously NOT TESTED)** — now covered by
  tests, not left open: the attendance clock state machine (4 tests), the self-service
  payslip scope for `/payroll/my-payslips/` (own line only, never a colleague's, never a
  rejected run's), and the rate-limit / session-cookie / production-settings guards (10
  tests) plus the dependency scan above.
- **`PERF-001`** — local timings are recorded in `docs/verification/performance-before-after.json`;
  with no agreed service-level objective this is evidence, not a verdict.
- **The audit's "3/23 role-sweep logout responses unconfirmed"** — explained and pinned rather
  than left open. Logout is deliberately idempotent (a stale, expired or malformed refresh token
  still answers 200, because the session is already dead server-side), so an unconfirmed response
  in a sweep is a harness-side observation, not a role that cannot log out. All of it is now
  covered by `test_auth.py::LogoutContractTests`, which also runs the full per-role sweep - and
  which found a real defect while being written: logout used to blacklist *any* supplied token,
  so one account could end another's session. That is fixed and pinned.
- **The six capability-versus-route gaps in the audit's probe register** — all six are closed
  and pinned as agreement by `test_role_permissions_matrix.py`; the register's other 385 probe
  rows matched the source gate at audit time.
