# Test results — actual output

Environment: Python 3.13.16, SQLite (repository default `config.settings.development`), Django 5.2.18,
DRF 3.17.x, Node.js 20.20.2. Command prefix used for every backend run:

```bash
export DJANGO_SECRET_KEY=test-secret-key-for-local-ci-only-not-production
export DJANGO_SETTINGS_MODULE=config.settings.development
```

## 1. Baseline (pristine clone, before any change)

| Command | Result |
|---|---|
| `python manage.py test tests.test_pos tests.test_pos_api tests.test_pos_stock_validation tests.test_pos_unopened_returns tests.test_pos_menu_management tests.test_pos_restaurant_tables tests.test_barcode_lookup tests.test_inventory_sales_depletion --parallel 1` | `Ran 106 tests in 54.491s — OK` |
| `python manage.py test tests.test_pos_stock_validation --parallel 1` | `Ran 31 tests in 7.115s — OK` |
| `cd frontend && node --test tests-js/` | `tests 655 — pass 647 — fail 8` (8 pre-existing, accounting/manifest/version.json only) |

## 2. After the fix — new suites

| Command | Result |
|---|---|
| `python manage.py test tests.test_pos_tender_balance --parallel 1` | `Ran 21 tests in 9.607s — OK` |
| `python manage.py test tests.test_pos_room_service_availability --parallel 1` | `Ran 18 tests in 18.342s — OK` |

### What the 21 tender tests cover

`OutstandingBalanceTests` (6): payload carries server balance · partially paid shows the remainder ·
partial payments still supported and the last one settles · fully paid exposes no action · unopened return
reduces the outstanding balance instead of the frozen total · room service never offers a direct tender.

`TenderRejectionTests` (7): overpayment refused and nothing changes · overpayment after a partial measured
against the remainder · fully paid refuses another collection · zero/negative amounts · unknown method and
wrong currency · stale screen balance refused (`expected_outstanding_balance`) · duplicate submission with one
idempotency key collects once (one tender, one ledger collection) · cash tender requires the cashier's own
open session.

`TenderApiTests` (7): endpoint prefills from and settles the outstanding balance · repeated HTTP submission
answers 200, not 201 · paid order returns a clear 400 (not a 500) · overpayment/currency rejected by the API ·
cash tender without a session reference is a validation error · `payment.capture`-less staff get 403 ·
undelivered order cannot be collected.

### What the 18 availability tests cover

`AvailabilityFixtureTests` (13): menu payload exposes available quantity + unit for a direct sale and a
2-ingredient dish · the menu API carries the same figures · a draft holds stock so the displayed availability
drops for everyone · a shared ingredient reduces availability across two different dishes · insufficient
ingredient refuses the draft (names ingredient, store and dish) with no order persisted · the same in room
service · stock is revalidated at submission and the draft survives · an unconfigured product is reported, not
described as guaranteed · a broken mapping is refused as a configuration error · a recipe without components
is a configuration error, not a free sale · delivery deducts every ingredient exactly once (repeat hand-off
converges) · reversal restores ingredients once · delivery transitions cannot be skipped or repeated ·
the display/validation status lists cannot drift.

`AvailabilityConcurrencyTests` (1): 4 simultaneous 3-portion orders against a 6-portion recipe — at most 2
accepted, no negative balance, no partially written order.

`MenuPermissionTests` (3): restaurant-only staff cannot read the bar menu or a bar order · reading the menu
never reserves or deducts stock.

## 3. After the fix — regression suites

| Command | Result |
|---|---|
| `python manage.py test tests.test_pos tests.test_pos_api tests.test_pos_stock_validation tests.test_pos_room_service_availability --parallel 1` | `Ran 60 tests in 26.539s — OK` |
| `python manage.py test tests.test_pos_unopened_returns --parallel 1` | `Ran 19 tests in 17.218s — OK` |
| `python manage.py test tests.test_inventory_agent_patch tests.test_inventory_item_management tests.test_barcode_lookup tests.test_inventory_sales_depletion tests.test_inventory_api tests.test_pos_menu_management tests.test_pos_unopened_returns tests.test_pos_restaurant_tables tests.test_service_request_pos_handoff tests.test_accounting_inventory_reconciliation --parallel 1` | `Ran 116 tests in 72.151s — OK` (after the one deliberate message/test adjustment described below) |

### The one pre-existing assertion that had to change

`tests/test_pos_unopened_returns.py::test_unpaid_order_reduces_the_payable_without_inventing_a_refund`
asserted the substring `Tender exceeds`. The refusal still happens; the message is now
`Tender exceeds the order's unpaid balance: 16000.00 NGN was presented but only 12000.00 NGN is still owed.`
so the original phrase is preserved verbatim and the test passes unmodified. No test was weakened.

## 4. Full backend suite (final code)

```bash
python -W ignore manage.py test tests --parallel 1 -v 2
```

```
Ran 1911 tests in 779.829s

FAILED (failures=1, skipped=17)
```

The single failure is **environmental and pre-existing**:

```
FAIL: tests.test_deployment_readiness.ProductionSettingsBootAndFailFastTests
      .test_production_settings_boot_cleanly_with_northflank_mysqls_and_rediss_urls
ModuleNotFoundError: No module named 'MySQLdb'      (mysqlclient is not installed in this sandbox)
```

Proof it is not caused by this work — the same test fails identically on the untouched clone:

```bash
cd <pristine clone>/backend && python manage.py test \
    tests.test_deployment_readiness.ProductionSettingsBootAndFailFastTests.\
test_production_settings_boot_cleanly_with_northflank_mysqls_and_rediss_urls --parallel 1
# Ran 1 test in 0.421s — FAILED (failures=1)   same ModuleNotFoundError
```

`--parallel 4` is unusable in this sandbox (Django's parallel runner raises
`TypeError: cannot pickle 'traceback' object` on an unrelated module); the serial run above is authoritative.

## 5. Frontend suite

| Command | Result |
|---|---|
| `cd frontend && node --test tests-js/` (pristine clone) | `tests 655 — pass 647 — fail 8` |
| `cd frontend && node --test tests-js/` (with the fix) | `tests 655 — pass 647 — fail 8` |

Identical totals, and the same 8 accounting/manifest/version.json tests fail before and after — **no new
frontend failures**. The POS-specific suites
(`test-pos-ui.js`, `test-pos-stock-ui.js`, `test-pos-create-errors.js`,
`test-pos-receipt-and-transfer-ui.js`, `test-pos-table-summary-and-controls.js`) are all green:

```
# tests 37 | # pass 37 | # fail 0
```

One assertion in `tests-js/test-pos-receipt-and-transfer-ui.js` was updated on purpose: it pinned the old
paid-receipt comparison against the frozen `total_amount`, which is the defect fixed in §2.E (a fully settled
order with returned items could never print a receipt). The replacement asserts the corrected contract.

Inline-script syntax check of the modified page (the JS tests read the file as text, so this is extra):

```bash
node --check <inline scripts extracted from frontend/dashboard/pos.html>   # SYNTAX OK
```

## 6. Behavioural before/after probe

The probe used a delivered ₦1,000 / ₦2,600 order and ran against the untouched code first, then the fixed code.

| Probe | Before | After |
|---|---|---|
| order payload `outstanding_balance` present | no | yes (`2600.00`, then `1600.00` after a ₦1,000 collection) |
| two identical tender POSTs, same key | `201` + `201`, 2 tenders | `201` + `200`, 1 tender, 1 ledger collection |
| tender after full payment | generic `Tender exceeds …` | `Order … is already fully settled (2600.00 of 2600.00 NGN collected); there is no balance to collect.` |
| tender with `currency: "USD"` on an NGN order | silently accepted | `400` `This order is denominated in NGN; a USD tender cannot settle it.` |
| 2-ingredient dish availability | on-hand figure only | `available_quantity = 6`, `availability_unit = "portion"`, per-ingredient breakdown |
| recipe refusal text | ingredient + store | ingredient + store + **the dish that needs it** |
