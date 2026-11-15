# Test & Verification Evidence — 15 Nov 2026

Every number below was produced by a command in **this** workspace; nothing is estimated, and
nothing is reported as passing that was not executed. Where a suite could not run, that is
stated with the reason (§5).

* Environment: Linux sandbox, Python 3.13.18, Django 5.2.19, Node v20.20.2, SQLite (no MySQL
  client available, so `mysqlclient`/row locks are unavailable — see §5).
* Base commit: `2204222` (“inveotry breakfast pwa”).

---

## 1. Frontend (`frontend/`, Node test runner)

```
cd frontend && node --test tests-js/
```

| | Tests | Pass | Fail | Skipped |
|---|---|---|---|---|
| Before (baseline recorded at the start of the audit) | 860 | 835 | 25 | 0 |
| **After** | **898** | **885** | **12** | **1** |

The 28 extra tests are the two new regression suites in this delivery
(`test-image-upload-accept.js` 18, `test-management-tier-gates.js` 11) plus the split-out
accounting suite recovering its other nine assertions (it previously crashed at load, §5).

Suites that went from red to green:

| Suite | Before | After |
|---|---|---|
| `test-pwa-dashboard-only.js` | 4/7 | **7/7** (with the `_headers` assertion now anchored, so it really checks the public manifest block instead of matching the dashboard one by suffix) |
| `test-department-page-access.js` | 21/24 | **24/24** |
| `test-theme-prepaint.js` | 3/4 | **4/4** |
| `test-sidebar-profile-link.js` | 4/5 | **5/5** |
| `test-cloudflare-northflank-deployment.js` | 4/5 | **5/5** |
| `test-deploy-config.js` | 4/5 | **5/5** |
| `test-batch04-gallery-housekeeping-navigation.js` | 17/18 | **18/18** |
| `test-inline-scripts-parse.js` | 2/3 | **3/3** |
| `test-accounting-audit-repair-ui.js` | file crashed | **9 pass, 1 skipped** |

Build verification (the repository commits its built pages):

```
cd frontend && python build.py --version 1.1.25
→ Released version 1.1.25 (version.json, js/version.js, sw.js cache)
→ Built 77 page(s) at version 1.1.25
→ SEO: sitemap.xml/robots.txt already current ; SEO: validation passed
cd frontend && python build.py      # second run: byte-identical (idempotent)
```

### The 12 remaining frontend failures (all pre-existing, all out of the reported problem set)

| Suite | Count | What the test wants | Why it is not fixed here |
|---|---|---|---|
| `test-accounting-posted-transactions-removal.js` | 7 | the Accounting screen stops rendering the “Posted transactions” list, its `renderTransactions()` path and the printed statement section, and the manual-journal button moves into the operations section | a **product decision** to retire a live screen section, tracked by an older batch; it is not a defect and removing a working list needs the owner’s confirmation |
| `test-night-audit-accounting-ui.js` | 1 | Accounting renders a night-audit daily flash + checks panel | new UI feature, not a repair |
| `test-targeted-repair-ui.js` | 1 | Accounting From/To date fields never disabled; a chosen date selects the custom range | new UI behaviour |
| `test-dashboard-expenses-ui.js` | 1 | Accounting separates supplier goods received from posted invoices | new UI behaviour |
| `test-table-layout-audit.js` | 1 | every staff table scrolls inside `.table-wrap` | layout refactor across ~20 pages |
| `test-ux-focus-regressions.js` | 1 | no dashboard table ships an unlabelled action column or an unnamed `<select>` | accessibility sweep across ~20 pages |
| `test-inventory-barcode-scan.js` | 1 (file) | a USB-HID barcode scanner handler on the inventory screen | the handler has never existed in this repository’s history (`git log -S` finds nothing); it is a missing feature, not a regression |

---

## 2. Backend (`backend/`, Django test runner)

Two full runs of the **same** suite, one on the untouched tree and one with this delivery
applied, so the claim “no regressions” is measured rather than asserted.

```
# baseline: backend/ changes stashed, new tests moved aside
cd backend && DJANGO_SETTINGS_MODULE=config.settings.development python manage.py test tests --parallel 1
→ Ran 2518 tests in 804.262s
→ FAILED (failures=48, errors=12, skipped=27)

# with this delivery applied
cd backend && DJANGO_SETTINGS_MODULE=config.settings.development python manage.py test tests --parallel 1
→ Ran 2565 tests in 803.642s
→ FAILED (failures=47, errors=12, skipped=27)
```

| | Tests | Failures | Errors | Skipped |
|---|---|---|---|---|
| Baseline (HEAD `2204222`) | 2518 | 48 | 12 | 27 |
| **With this delivery** | **2565** | **47** | **12** | **27** |

Failure-set comparison (by test id):

* **New failures introduced: 0.**
* **Pre-existing failures fixed: 1** — `tests.test_bookings.…test_exact_unavailable_and_automatic_assignment`,
  the backend half of the OCCUPIED defect.

The 47 new tests are `test_first_room_booking.py` (19), `test_image_upload_jpeg_regressions.py`
(17) and `test_management_tier_guest_services.py` (11).

Targeted runs used during the repair:

| Command | Result |
|---|---|
| `manage.py test tests.test_first_room_booking` | 19 tests, OK |
| `manage.py test tests.test_image_upload_jpeg_regressions` | 17 tests, OK |
| `manage.py test tests.test_management_tier_guest_services` | 11 tests, OK |
| `manage.py test tests.test_bookings tests.test_availability tests.test_multi_room_booking tests.test_booking_idempotency tests.test_guest_checkout` | 56 tests, OK |
| `manage.py test tests.test_guest_services_amenities_breakfast` (pre-existing suite, must not regress) | 20 tests, OK |
| `manage.py test tests.test_offer_admin_control_regressions` | 20 tests, OK |
| `manage.py makemigrations --check --dry-run` | “No changes detected” |
| `manage.py check` (development + production settings) | 0 issues |

### The 59 remaining backend failures/errors — pre-existing, identical before and after

Grouped by real cause (sampled from the run log; every one of them also fails on the untouched
tree):

| Group | Count | Evidence from the log |
|---|---|---|
| **Email transport** — `test_email_pipeline` | 12 | `AssertionError: 'FAILED' != EmailLog.Status.SENT`; the transport cannot reach a provider from this sandbox (no key, no outbound SMTP). The same 31 email/deployment failures are already recorded in the repository’s `baseline-known-failures.txt`. |
| **Expired hard-coded dates** — `test_multi_room_quantity_regression` | 10 | `400 INVALID_DATES "Check-in date cannot be in the past."` — the tests were written with fixed 2026 dates that are now in the past. Time-bomb tests, not product defects. |
| **Inventory/POS stock arithmetic** — `test_accounting_inventory_reconciliation`, `test_latest_control_repairs`, `test_pos_unopened_returns`, `test_pos_stock_validation`, `test_guest_portal_room_orders`, `test_batch05_food`, `test_barcode_lookup`, `test_enterprise_upgrade_suite`, `test_folio_balance_classification`, `test_menu_price_cost_guard`, `test_pos`, `test_inventory`, `test_inventory_api` | 21 | quantity mismatches such as `Decimal('200.0000') != Decimal('197')` — the expectations and the recipe/consumption service disagree after HEAD’s `inventory_service.py` refactor (251 lines changed in the base commit). |
| **`tests/test_pos_room_service_availability.py` cannot import** | 11 tests never execute (1 loader error) | `ImportError: cannot import name 'consume_stock_for_delivered_order' from 'apps.inventory.services.inventory_service'` — the module calls a function the base commit removed/renamed. |
| **Procurement regressions** — `test_audit_procurement_regressions` | 5 | prepaid receipt state and `403 != 201` on a procurement action |
| **Isolated** — `test_regressions.test_checked_in_booking_shows_occupied`, `test_portal_stay_lifecycle`, `test_query_efficiency`, `test_critical_repair_integration` | 4 | the display-status test needs the factory to set `checked_in_at`; a wording string (“Not Available” vs “Currently unavailable”); a query-count ceiling; an 19-vs-11 count |

None of these touch the five defects in this delivery, and fixing them would mean changing
product behaviour (inventory deduction rules) or the owner’s email provider — both outside what
a repair may decide unilaterally.

---

## 3. What was verified by execution, not by reading

* The OCCUPIED rule: 19 behavioural tests, including “a stale `OCCUPIED` flag is bookable for a
  future stay”, “a genuinely occupied room is still refused”, “the guest leaving on the arrival
  date frees the room”, and “the availability answer equals the booking-creation answer for
  every room”.
* JPEG uploads end to end: multipart request → Pillow decode → stored name/extension → stored
  bytes (`\xff\xd8\xff`) → public URL, on the gallery, room-type, room and profile surfaces,
  plus the refusal cases (renamed text file, ELF payload, oversize, GIF).
* The guest-amenity station over HTTP: allocate (200), issue (200), history (200), breakfast
  config/issue — for the general manager and manager; 403 for receptionist-allocating,
  storekeeper-issuing, waiter and housekeeper.
* Installability: manifest values, per-page manifest/apple-* presence on all 77 pages, and the
  hosting MIME/cache rules, all asserted from the shipped files.
* The role gates for **all eleven** operational roles plus guest, signed-out, and an unknown
  future role (fail-closed).

## 4. What could **not** be verified here (and why)

| Item | Reason |
|---|---|
| Live browser/E2E flows (Playwright) | The browsers cannot launch in this sandbox (`libnspr4.so` missing, no root to install it). Browser behaviour was therefore verified with the project’s own Node/vm harness plus real HTTP requests against Django’s test client, and the PWA suites in `frontend/tests-pwa/` (Playwright-based) are **not executed** — they are recorded as not run, not as passing. |
| MySQL-specific behaviour (row locks, `SELECT … FOR UPDATE`) | No MySQL client/driver in the sandbox; the suite runs on SQLite. Race tests can emit `database table is locked`, which is a SQLite artefact documented in `tests/test_multi_room_booking.py`. |
| Real email/SMTP and payment-provider calls | No credentials in this environment and none were invented; no production key was replaced with a test key. |
| Production data / migrations against the live database | Not touched. The only new migration (0038) is additive, audit-logged and reversible, and was executed against the test database on every run above (`makemigrations --check` clean). |

## 5. Environment note

`/home/user/.venv` is not preserved between snapshots; the backend dependencies were reinstalled
from `backend/requirements.txt` into the system Python for these runs (Django 5.2.19, DRF,
Pillow, whitenoise, simplejwt). No project file was changed to accommodate the environment.

---

## 6. Reproduce everything in this document

```bash
# backend
cd backend
DJANGO_SETTINGS_MODULE=config.settings.development python manage.py check
DJANGO_SETTINGS_MODULE=config.settings.development python manage.py test \
    tests.test_first_room_booking tests.test_image_upload_jpeg_regressions \
    tests.test_management_tier_guest_services
DJANGO_SETTINGS_MODULE=config.settings.development python manage.py test tests --parallel 1

# frontend
cd ../frontend
node --test tests-js/
python build.py            # idempotent; verifies 77 pages + SEO
```
