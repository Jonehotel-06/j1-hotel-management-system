# Verification guide

How every claim in [`REPAIR_COMPLETION_REPORT.md`](../REPAIR_COMPLETION_REPORT.md) was
produced, and how to reproduce it. Two kinds of check live here:

1. **Repository tests** — run anywhere, no browser, no network. These are the ones to run
   in CI.
2. **Browser measurements** — run in a real Chrome against the static frontend, with no
   backend involved. These are the ones that produced the numbers in the report, and the
   raw output is checked in under `docs/verification/`.

---

## 1. Repository tests

```bash
# Backend: unit, API, DB and integration layers (disposable test database)
cd backend
python manage.py test                 # expect: Ran 930 tests ... OK (skipped=1)
python manage.py check                # expect: no issues
python manage.py makemigrations --check --dry-run   # expect: No changes detected

# Backend dependency vulnerability scan (SEC-005). pip-audit is a developer tool,
# not a project dependency: install it into a throwaway environment.
../.venv/bin/pip install pip-audit
../.venv/bin/pip-audit                          # expect: No known vulnerabilities found
../.venv/bin/pip-audit -r requirements.txt --no-deps   # expect: No known vulnerabilities found

# Frontend: behaviour + contract + accessibility guards
cd ../frontend
python validate.py                    # expect: Checked 67 HTML files ... ALL OK
node --test tests-js/                 # expect: # pass 129, # fail 0
```

The frontend suite is deliberately layered so each audit finding has a guard that survives
on a machine without a browser:

| File | What it pins |
|---|---|
| `tests-js/test-repair-contracts.js` | the endpoint map resolves every finance/POS/export path the pages use, and no page can build a `/undefined` URL |
| `tests-js/test-accessibility-baseline.js` | one H1 per reachable screen, no heading-level skips, `aria-hidden` containers hold no focusable content, labelled controls contain their visible text, and the design-token contrast maths for both themes on all four light surfaces plus the footer |
| `tests-js/test-ux-focus-regressions.js` | the source-level cause of each browser-only Axe finding: the icon-carrier rule, the stepper's content-derived accessible name, the date-button labelling, the N2 phone topbar rules, the N3 scroll cue and region semantics, empty-state heading levels, the loading-skeleton text rule, and the "no hard-coded API path" rule |

Contrast is checked arithmetically (WCAG relative-luminance formula) against the values
parsed out of `css/variables.css` and `css/themes.css`, so a token change that drops any
text below AA fails the suite even with no rendering.

The frontend has no `package.json` and no bundler — it is plain HTML/CSS/JS served as files,
so `node --test` (Node's built-in runner) is the only test tool required and there is no npm
dependency surface to audit. Anything that needs a browser lives in §2 instead.

The backend guards added by the second repair round, and what each one protects:

| File | What it pins |
|---|---|
| `tests/test_security_guards.py` | the login throttle (`429` **with** `Retry-After`), per-scope throttle isolation, an exactly-configured rate, that a session cookie cannot authenticate the API while a bearer can, the production settings source (wildcard CORS refused, HTTPS/HSTS/cookie flags on, no session/basic auth, finite rates on the sensitive scopes), and that refresh rotation/blacklisting stay on with the access-token lifetime bounded at 30 minutes |
| `tests/test_role_alias_guards.py` | a source-level guard over `apps/**`: no workflow code may compare a role against a single member of the `HOUSEKEEPING`/`HOUSEKEEPER` or `MAINTENANCE`/`MAINTENANCE_TECHNICIAN` pairs — it must route through `apps/accounts/capabilities.py`. Verified to fail against the pre-fix sources. |
| `tests/test_role_permissions_matrix.py` | the six capability-versus-route gaps recorded in the audit's own probe register, pinned as *agreement* |
| `tests/test_operational_directory_api.py` | N7: the staff picker returns 200 with `email: null` and exactly `{id, full_name, email, role}` without `account.view`, and the address with it |
| `tests/test_payroll_api.py` | PAY-004: `/payroll/my-payslips/` is scoped to the caller — no colleague's line, no rejected run's line |
| `tests/test_staff_operations_api.py` | HR-002: the attendance clock state machine, its refusals and idempotent replay |
| `tests/test_query_efficiency.py` | no list endpoint issues a query per rendered row (see §1.1) |
| `tests/test_auth.py::LogoutContractTests` | the logout contract: every staff role can log out and its refresh token dies, replay is idempotent, stale/expired/malformed tokens are tolerated, a missing token is a 400, and one account cannot blacklist another account's token |

`requirements.txt` pins `djangorestframework>=3.17.2,<4.0`: 3.17.2 is the first release fixing
`CVE-2026-73228` (oversized-body bypass of `DATA_UPLOAD_MAX_MEMORY_SIZE` via DRF's
`request.data`) and `CVE-2026-73229` (`AdminRenderer` disclosure on invalid writes). The
previous `,<3.17` cap made both advisories un-patchable by construction.

### 1.1 Backend query profile

The backend has a guard suite for the failure mode that actually slows a dashboard table: a
query issued per rendered row. Run it with:

```bash
cd backend
python manage.py test tests.test_query_efficiency      # expect: Ran 17 tests ... OK
```

Eleven of those tests (`AdminListFlatnessTests`) fetch a full **100-row page** twice, adding
20 rows in between, and require the query count to be identical. They also fail if the
measurement captures zero queries, so they cannot pass vacuously. Proof they bite: removing
`select_related("guest", "room_type", "offer")` from the bookings list makes the guard report
`2.00 queries per rendered row (10 at 3 rows, 50 at 23 rows)`.

The profile behind the guards - query counts and median latency at 3 versus 60 rows, with
rows created through the real services - is stored in
`docs/verification/api-query-profile.json`. Every guarded endpoint is flat (0 queries per
added row); `finance folios` rendered no rows in that probe and is excluded from the claim.
These are local test-database figures, not production timings, and no service-level objective
has been agreed.
---

## 2. Browser measurements

### 2.1 Method

- Headless **Chrome 148** driven by Puppeteer, axe-core 4.14 injected from disk.
- Pages are served from a local static server over the repository's own `frontend/` tree.
  A second server can serve the pre-repair tree (see §2.4) for a like-for-like baseline.
- Dashboard pages are behind an auth guard, so the harness seeds a **synthetic ADMIN
  profile and a placeholder token** into `localStorage`/`sessionStorage` before any page
  script runs, and **aborts every `/api/` request**. That means: no credential, no
  backend call, no production data — the shell renders and only geometry/rules are read.
- Rule set: `wcag2a, wcag2aa, wcag21a, wcag21aa, best-practice` at 1440×900 and 390×844.

### 2.2 What was measured

| Measurement | Command shape | Output checked in |
|---|---|---|
| Public pages, both viewports | `node axe-css.js <baseUrl> <pages…> <out.json>` | `docs/verification/a11y-public-pages-{before,after}.json` |
| Dashboard pages, both viewports | `node axe-dash.js <baseUrl> <page names…>` | `docs/verification/a11y-dashboard-pages-{before,after}.json` |
| POS topbar height / title clipping / table scroll (N2, N3) | `node pos-measure.js <baseUrl> <label> <out.json>` | `docs/verification/responsive-pos-measurements.json` |
| Shell load timings, 5 runs, median | `node perf.js <label> <baseUrl> <out.json>` | `docs/verification/performance-before-after.json` |

The backend counterpart (§1.1) is stored in `docs/verification/api-query-profile.json`, and
the dependency scan in `docs/verification/dependency-scan.json`.

Results, in one line each: public violations **260 → 0** nodes (22 → 0 pages of 27);
dashboard **906 → 0** nodes (40 → 0 pages of 40); POS topbar **124 → 53–55 px** at
320–412 px with no title clipping; shell load timings **unchanged** (all movements within
run-to-run noise, identical request counts and bytes).

### 2.3 The scroll-cue check

"N3 is fixed" is a claim about pixels, so it was checked in pixels. A controlled fixture
(one `.table-wrap` with a 7-column table, the repository's real stylesheet) is screenshotted
at scroll start, mid-scroll and end; a 1-px vertical strip at the wrapper's bottom edge is
sampled with PIL. The cue is present exactly while content remains on that side:

| State | Left-edge delta | Right-edge delta |
|---|---|---|
| scroll start | 0 | −30 |
| mid-scroll | −31 | −30 |
| scroll end | −31 | 0 |

Digits are RGB channel differences against the mid-strip sample (0–255); non-zero = cue
visible. `docs/verification/responsive-pos-measurements.json` records the numbers.

### 2.4 Reproducing the baseline

The before/after pair needs the pre-repair tree, which is just the repository's committed
state:

```bash
# in a scratch directory
git -C /path/to/repo archive HEAD frontend | tar -x -C /tmp/before        # /tmp/before/frontend
python3 -m http.server 8899 --directory /path/to/repo/frontend           # after
python3 -m http.server 8898 --directory /tmp/before/frontend             # before
```

Then point the harnesses at port 8898 (before) and 8899 (after). Note that while a
measurement is running, the static server must stay alive for the whole run — if it exits,
Chrome renders its own error page and axe reports a *clean* result for the wrong document.
The `pos-measure.js` and `perf.js` harnesses write the label and base URL into the JSON so a
result can never be mistaken for the other tree.

---

## 3. What these checks cannot establish

- **Not a WCAG conformance audit.** Five automated rules are clean in a real browser;
  manual screen-reader, keyboard-only and cognitive passes were not performed.
- **Nothing about production.** No deployed host, database or credential was used.
- **No backend performance data.** Only frontend shell timings were compared, on one host.
- **No financial state change.** The reconciliation and posting paths are exercised on
  fixtures in the test database; production records were not touched.

Anything beyond those bounds is reported in the completion report as implemented-but-not-
verified, or as a blocker in [`REMAINING_BLOCKERS.md`](../REMAINING_BLOCKERS.md).
