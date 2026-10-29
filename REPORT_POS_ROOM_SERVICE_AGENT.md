# POS, Room Service, Tender Processing and Stock Validation — Audit & Repair

**Repository audited:** `https://github.com/Jonehotel-06/j1-hotel-management-system`
(commit cloned as a read-only reference; nothing was pushed and no original file was modified in place)

**Deliverable folder:** `ai_updates/pos_room_service_agent/`

**Date of audit:** 2026-10-29

---

## 0. Isolation, method and scope

* The upstream project was cloned once and treated as **read-only evidence**.
* All work happened in a **separate working copy**; the eight changed/created files were then copied into
  `ai_updates/pos_room_service_agent/` at their **exact repository-relative paths**.
* Nothing was committed, pushed, migrated against a real database, or pointed at production data.
* No score was invented and no data was fabricated: every number in the tests is created by the test itself
  from the repository's own models and services, or read from the code that computes it.
* Files outside POS / Room Service / tender / the directly necessary order–inventory integration were **not
  touched**. Dashboard-level follow-ups that this assignment does not cover are documented in §7 rather than
  silently implemented.

### How the audit was performed

1. Traced the frontend handlers (`frontend/dashboard/pos.html`, `frontend/js/api.js`), the POS API
   (`apps/pos/views.py`, `urls.py`), serializers, services (`order_service.py`, `return_service.py`,
   `table_service.py`), order models, the inventory service (`inventory_service.py`), finance ledger
   integration (`create_posted_transaction`, `CashSession`), permissions (`HasCapability`,
   `_ensure_order_mode`, `_ensure_order_access`) and the existing 140-module test suite.
2. Ran the existing POS/inventory/JS suites to establish a **baseline** (see §6).
3. Wrote a throw-away probe test against the untouched code to *prove* the reported defects instead of
   assuming them (results quoted in §2.A).
4. Fixed only what the evidence supported, and re-ran the baseline plus new coverage.

---

## 1. Existing architecture (what was preserved)

| Concern | Existing implementation | Treatment |
|---|---|---|
| Order statuses | `PosOrder.Status`: `DRAFT, SUBMITTED, PREPARING, READY, DELIVERED, CANCELLED, VOIDED` | **Unchanged.** No new status was invented. |
| Settlement states | `PosOrder.SettlementStatus`: `NOT_APPLICABLE, UNPAID, PARTIALLY_PAID, PAID, PARTIALLY_REFUNDED, REFUNDED` | Unchanged |
| Charge vs collection | Delivery posts an immutable `FINANCIAL` charge (`_post_order_charge`); tender captures a separate `PAYMENT_COLLECTION` | Unchanged (the two remain distinct) |
| Stock control | `StockRecipe` (versioned BOM) **or** explicit `StockItem.direct_sale_*` configuration; nothing inferred | Unchanged |
| Stock hand-off | Delivery creates an idempotent `StockConsumptionRequest` and resolves it in the same transaction | Unchanged |
| Money | Server-derived only; `net_total = total_amount − returned_amount` | Unchanged; now **exposed** to the client |
| Permissions | Capability-based (`payment.capture`, `pos.order.manage`, `restaurant.order.manage`, `bar.order.manage`) | Unchanged |

---

## 2. Root-cause analysis

### A. “Tender amount must autofill”

**Reported symptom:** after an order is delivered and becomes payable, the amount field opens empty and the
cashier must retype what is owed.

**Verdict: the defect is real, and it is *not* a wrong balance formula. It is a combination of a missing
initial form value, a payload that never carried the balance, and an unguarded duplicate-submission path.**

Evidence, in order of causality:

1. **Missing initial form value (primary cause).**
   `frontend/dashboard/pos.html:866–885` (`captureTender`) builds the amount field as

   ```js
   {name:"amount",label:"Amount ("+(order.currency||"NGN")+")",type:"number",step:"0.01",required:true,placeholder:"Server rejects an amount above the unpaid balance."}
   ```

   There is **no `value:`**, so `JONE.dashboard.formModal` (`frontend/js/dashboard.js:1371`,
   `pre = opts.values ? opts.values[f.name] : f.value`) renders an empty input. The field text even says
   “Server rejects an amount above the unpaid balance”, i.e. the UI knowingly delegated the whole
   calculation to the cashier and the server.

2. **The order payload carried no balance to prefill from (why it could not be fixed in the browser alone).**
   `apps/pos/serializers.py:325` (`PosOrderListSerializer`) exposed only `subtotal`, `tax_amount` and the
   frozen `total_amount`. `net_total` is a **model *property*** (`apps/pos/models.py:319`) and DRF does not
   serialise model properties; neither the captured tender total nor the remaining balance existed anywhere
   in the response. Confirmed by probe on the untouched code:

   ```
   PROBE net_total present: False
   PROBE outstanding present: False
   PROBE amount_paid present: False
   PROBE returned_amount present: False
   ```

   This also explains a second, quieter bug: the unopened-item returns table (`pos.html:993`) renders
   `order.amount_paid`, but no such field existed, so **“paid” always showed ₦0.00** there.

3. **No duplicate-collection protection.** `capture_direct_tender` created a fresh `PosTender` and keyed only
   the *ledger* entry (on the tender's own reference), so a double click, a network retry or a repeated
   request created a second collection. Probe on the untouched code — two identical POSTs, each carrying an
   `idempotency_key` in the body (the serializer had no such field, so it was ignored):

   ```
   PROBE api r1: 201 r2: 201
   PROBE tenders created: 2
   PROBE captured sum: 1000.00   (2 × 500.00 on a 1,000.00 order)
   ```

4. **A stale-screen hazard had no guard.** Nothing tied the submitted amount to the balance the cashier was
   looking at; a concurrent part-payment by a second cashier could land between "open form" and "submit".

What was **already correct** and was therefore preserved (do not "fix" these):
`capture_direct_tender` already rejected room-service orders, non-`DELIVERED`/uncharged orders, unknown
methods, `amount <= 0`, amounts above `net_total`, and a cash session that is not open or not the acting
cashier's. The balance ceiling was already the live `net_total` (returns reduce it) — the *formula* was right,
the *plumbing* was missing.

**Fix (A)**

*Backend*
* `apps/pos/services/order_service.py`
  * new `tender_captured_amount(order)` — sum of `CAPTURED` tenders, using the prefetched `tenders` cache when
    available (no N+1);
  * new `order_outstanding_balance(order, *, captured=None)` — `max(net_total − captured, 0.00)`;
  * `capture_direct_tender(...)` now: recomputes `captured` **under the order row lock**, derives
    `outstanding` on the server, refuses a settled order with an explicit “already fully settled” message,
    refuses overpayment *relative to the remainder*, validates the presented `currency` against the order
    currency, supports `expected_outstanding_balance` (stale-screen refusal) and `idempotency_key`
    (repeat-safe), and records a `TENDER_CAPTURED` event carrying `balance_before` / `balance_after`.
    Return type stays `PosTender` (no service-contract break); an idempotent replay is flagged with
    `tender.idempotent_replay = True`.
* `apps/pos/serializers.py`
  * `PosOrderListSerializer` (and therefore the detail serializer) now exposes, read-only:
    `net_total`, `returned_amount`, `amount_paid`, `outstanding_balance`, `settlement_action_available`.
  * `PosTenderCaptureSerializer` accepts three **optional, backwards-compatible** inputs:
    `currency`, `idempotency_key`, `expected_outstanding_balance`.
* `apps/pos/views.py`: `_order_queryset()` / `_order_detail_queryset()` annotate `captured_amount` as a
  correlated subquery (a second aggregate would multiply `line_count`), and the tender view forwards the new
  inputs and answers **200** for an idempotent replay (201 for a new collection).

*Frontend*
* `captureTender()` is rewritten: it refuses room service with an explanation (folio settlement), refuses a
  non-delivered order, refuses a settled order, renders a balance panel
  (`Order total → − returns → charge now due → already collected → outstanding`), and **prefills the amount
  box with `order.outstanding_balance`**. It sends `currency`, `expected_outstanding_balance` and one
  `idempotency_key` per opened form, validates the range client-side for speed, and reports the state after
  the collection (“Remaining balance: ₦…” / “fully paid”).
* The order table now shows `Due ₦…` under the settlement pill and the action button reads
  `Collect ₦…`, gated on the server's `settlement_action_available`.

### B. “Stock validation for direct-sale products”

**Verdict: the server-side validation already existed and was correct; the *displayed* number was the one
that lied, and a refusal did not name the dish.**

Verified present in the untouched code (unchanged, and now covered by new tests):

* `_validate_order_lines_stock` runs **before anything is persisted** in `create_order` and again in
  `submit_order` (which excludes the order's own lines, so a draft is not counted against itself);
* only stock-tracked products are validated; a *claimed but broken* mapping (inactive stock item, missing or
  inactive store, zero per-sale quantity, recipe without components) is **refused** rather than silently
  passed (`stock_not_configured`);
* demand from other **open** orders (`DRAFT/SUBMITTED/PREPARING/READY`) is subtracted, so two open orders
  cannot collectively promise more than the store holds;
* a refusal is HTTP **409 `POS_STOCK_CONFLICT`** with a machine-readable `items[]` payload, the POS page keeps
  the cart and reloads the menu (`if(err&&err.code==="POS_STOCK_CONFLICT")loadMenu(state.menuArea)`).

Defects found and fixed:

1. **The menu showed physical on-hand (`saleable_quantity`) instead of what validation would accept.**
   `pos_stock_summaries` subtracted nothing, while `_validate_order_lines_stock` subtracted open-order
   commitments. An operator could therefore read “5 available”, build a cart of 5 while another open order
   held 3, and be refused at submission — exactly the “POS accepts what it should not / rejects what it should
   not” complaint. Fixed by publishing a second, authoritative figure (`available_quantity`) computed by a
   **shared** helper, and by having the POS display it.
2. **Duplicated commitment logic in two places.** The “stock other open orders already promised” computation
   now lives once in `apps/inventory/services/inventory_service.py::open_pos_order_demand()` and is called by
   both the validator (`apps/pos/services/order_service.py`) and the display projection.
3. **A recipe refusal named only the ingredient.** For a multi-ingredient dish the 409 said
   “Kitchen Store has 3 kgs of Chicken on hand, but this order needs 3.5 kgs” — no dish name, so the operator
   could not tell which line to reduce. It now reads “…needs 3.5 kgs **for Jollof rice & chicken**”.

No silent “add anyway” path was introduced: `addCartLine` refuses before adding, and a rejection from the
server is surfaced, never swallowed. The draft/confirmed distinction is preserved and stated in the UI: the
draft is a soft commitment (no reservation row) and the figure is labelled as a live snapshot, never as a
guarantee.

### C. “Recipe-based food availability”

**Verdict: the authoritative recipe path already existed; the *availability surface* and the *refusal text*
were the defects, and the multi-ingredient/concurrency behaviour had no test coverage.**

Verified present in the untouched code (preserved):

* `_active_recipe_snapshots` resolves **only** an active `StockRecipe` or an explicit direct-sale claim —
  nothing is inferred from a matching SKU or name, and no recipe row is ever created implicitly;
* unit conversion happens **once, at recipe creation** (`create_stock_recipe` → `convert_stock_quantity`), so
  `StockRecipeLine.quantity_per_menu_unit` is always in the component's own base unit; the typed value is kept
  beside it as evidence only;
* every component is checked (`pos_stock_rules` returns all components or marks the product `unconfigured`);
* delivery deducts **exactly once** (source-keyed `POS_CONSUMPTION` movements per `request/location/item`) and
  is all-or-nothing (a shortage parks the hand-off `FAILED` and posts nothing);
* `reverse_pos_stock_consumption` is idempotent (`resolution_payload.reversed`).

Defects found and fixed:

1. `pos_stock_summaries` reported a recipe dish’s availability in **one ingredient's unit** (or as “units”
   when the recipe mixed units). A dish limited by chicken but measured in kilograms is unreadable to a
   waiter. Recipes now report `availability_unit: "portion"` (direct sales keep their base unit) and the UI
   prints “N portions”, plus a per-ingredient breakdown (“Recipe (2 ingredients): 0.25 KG Rice + 0.5 KG
   Chicken makes 1 portion — 6 portions can be made now”).
2. Recipe availability ignored open-order commitments (see B.1) — fixed by the same shared helper.
3. Nothing proved the multi-ingredient path, so the new suite in
   `backend/tests/test_pos_room_service_availability.py` locks it down (limits on the scarcest ingredient,
   shared ingredients across two dishes, out-of-stock ingredient, submission-time revalidation, exactly-once
   deduction, idempotent reversal, threaded burst).

**Deliberate, documented behaviour that was *not* changed:** if stock disappears between submission and
delivery, the delivery still commits and the inventory hand-off is parked `FAILED` with review evidence (see
`consume_stock_for_delivered_order`‘s docstring: “Never blocks a completed sale”). Changing that would let a
stock shortfall block a meal that has already been served and charged; the residual gap is listed in §8.

### D. “Available quantity on Restaurant POS and Room Service menus”

Existing state: the menu endpoint (`PosMenuView`) and barcode lookup already returned a per-item `stock`
object from the **shared** inventory resolver — no stock arithmetic happens in browser JavaScript.

Defect: the number returned was `saleable_quantity` (physical balance) — see B.1 — and it had no recipe-aware
unit.

**Fix (D)** — `apps/inventory/services/inventory_service.py::pos_stock_summaries` now also returns, per item:

| Field | Meaning |
|---|---|
| `available_quantity` | whole menu units the **order validator will accept right now** (on-hand minus open-order commitments, resolved through the same recipe/direct-sale rules) |
| `available_sufficient_for_one` | `available_quantity >= 1` |
| `committed_quantity` | whole menu units already promised to other open orders |
| `availability_unit` | `"portion"` for a recipe-based dish, the stock item's base unit for a direct sale |
| `components[].quantity_committed` / `.quantity_available` | the same arithmetic per ingredient |

`saleable_quantity`, `sufficient_for_one` and `stock_unit` are **unchanged** (existing API contract and
existing tests still pass), so nothing that already consumed them breaks.

The POS now displays `available_quantity` in the picker suffix, the item hint, the cart line and the “+”
ceiling, and every one of those places carries the staleness caveat in words: *“Live figure: stock can change
before the order is submitted.”* Submission-time validation remains authoritative, and a 409 reloads the menu
so the displayed figures become current again.

### E. “Delivery, payment and billing states”

Audit result (all verified in code, and covered by tests):

| Transition | Allowed? | Evidence |
|---|---|---|
| `DRAFT → SUBMITTED` | yes (`submit_order`), idempotent | returns early if already `SUBMITTED` |
| `DRAFT → DELIVERED` | **no** (must submit first) | `transition_order` `allowed` map |
| `SUBMITTED/PREPARING/READY → DELIVERED` | yes; posts the immutable charge, one `KitchenTicket` completion, one stock hand-off | `transition_order` |
| `DELIVERED → anything` | **no** (only returns/refunds move money afterwards) | `allowed` map has no `DELIVERED` key |
| `DRAFT/SUBMITTED/PREPARING/READY → CANCELLED` | yes; no charge, no stock movement | `transition_order` |
| `VOIDED` | exists on the model but **no POS endpoint sets it** (unchanged; documented in §8) |
| `UNPAID → PARTIALLY_PAID → PAID` | yes, via tender capture only | `capture_direct_tender` |
| `PAID → PARTIALLY_REFUNDED/REFUNDED` | via the unopened-item return workflow | `return_service` |

Defects found and fixed:

1. **“Print receipt” could never succeed after a partial return.** `pos.html:820` compared the captured
   tenders with the **frozen** `total_amount`; after unopened items came back, `total_amount` stays as
   evidence while the amount due is `net_total`, so a fully settled order was refused a receipt with
   “Captured payment records do not cover this order total”. Now compared against the server's `net_total`
   and `outstanding_balance == 0`, and the printed receipt shows the net total and the true balance due.
2. **Tender button visibility was browser-derived.** It now follows the server's
   `settlement_action_available`, which mirrors every server precondition (delivered, charged, direct-sale
   mode, positive balance) — the UI can no longer offer a collection the API will refuse.
3. **Returns panel money column was reading a non-existent field** (`amount_paid`) — fixed by exposing it.
4. `capture_direct_tender` now sets `PAID` on `>=` the net total (guarding legacy over-collected rows) and
   records before/after balances on the order event.

### F. “Form and UI cleanup”

Every POS/Room-service form field was traced to its consumer before anything was considered for removal:

| Field | Backend consumer | Verdict |
|---|---|---|
| `mode` | `create_order(mode=…)` | keep |
| `stay_id` (room service) | `_room_service_context`, DB `CheckConstraint` | keep |
| `folio_id` | derived server-side; never sent by the page | keep |
| `guest_name` | `PosOrder.guest_name` snapshot (ignored for room service, which snapshots the stay guest) | keep |
| `table_number` | `PosOrder.table_number` (bar/QR) | keep |
| `table_session_reference` | `lock_open_table_session`, QR matching | keep |
| `service_request_reference` | QR hand-off, `ServiceRequestEvent` | keep |
| `delivery_location` | `PosOrder.delivery_location` | keep |
| `lines[].menu_item_id / quantity / modifier_ids / notes` | `_snapshot_line` | keep |
| `idempotency_key` | `create_order` de-duplication | keep |
| tender: `method`, `amount`, `cash_session_reference`, `external_reference`, `notes` | `capture_direct_tender` | keep |
| tender: `currency`, `expected_outstanding_balance`, `idempotency_key` | **new**, additive | added |

**No field was removed**: each one is read by the backend, a report, or the audit trail. The “redundant field”
complaint was really the *empty amount box* (A) and the *misleading availability figure* (B/D), both fixed.

Readability/feedback work (no restyling of unrelated dashboards):

* tender modal gains a balance panel above the form and a prefilled, explained amount;
* the order table states `Due ₦…`; the action button states `Collect ₦…`;
* cart lines state the store, the physical availability, **what is left to order now**, and how much other
  open orders hold;
* item hints state the recipe breakdown and the staleness caveat;
* success toasts state the resulting balance; refusals state product, store, quantity and the next action.
* Existing responsive classes (`.table-wrap`, `hide-sm`, `.pos-cart-row`, `.pos-action-stack`) were left intact;
  no new fixed-width layout was introduced.

---

## 3. Exact changed-file manifest

All paths below are repository-relative and exist verbatim inside `ai_updates/pos_room_service_agent/`.

| # | File | Status | Purpose |
|---|---|---|---|
| 1 | `backend/apps/pos/services/order_service.py` | modified | balance helpers, hardened `capture_direct_tender`, shared commitment helper, dish-naming refusal |
| 2 | `backend/apps/pos/serializers.py` | modified | server-derived balance fields; additive tender inputs |
| 3 | `backend/apps/pos/views.py` | modified | `captured_amount` annotations; tender endpoint forwards new inputs, 200 on replay |
| 4 | `backend/apps/inventory/services/inventory_service.py` | modified | `open_pos_order_demand()`; `available_quantity`/`available_*`/`committed_quantity`/`availability_unit` in `pos_stock_summaries` |
| 5 | `backend/tests/test_pos_tender_balance.py` | **new** | 21 tests: autofill source, partial/full payment, overpayment, duplicates, currency, stale screen, permissions |
| 6 | `backend/tests/test_pos_room_service_availability.py` | **new** | 18 tests: direct-sale + multi-ingredient availability, commitments, concurrency, deduction/reversal, transitions, permissions |
| 7 | `frontend/dashboard/pos.html` | modified | tender autofill + guards, balance panel, `Due`/`Collect` states, receipt net-total fix, authoritative availability display |
| 8 | `frontend/tests-js/test-pos-receipt-and-transfer-ui.js` | modified | one assertion updated to the corrected paid-receipt contract (frozen total → net total) |
| 9 | `PATCH.diff` | **new (deliverable artefact)** | unified diff of files 1–4 and 7–8 for review/rollback |

Also in this folder: `REPORT_POS_ROOM_SERVICE_AGENT.md` (this document) and
`MANUAL_CHANGES.md` (the short manual-edit list from §9).

**Not modified (verified):** every other app (`finance`, `stays`, `bookings`, `accounts`, `reports`,
`guest_services`, …), every other dashboard page, all migrations, all settings, `frontend/js/*`.
No `.env`, credential or deployment file was touched.

---

## 4. API and schema changes

### 4.1 Database schema

**None.** No migration was created, no column was added or dropped, no data was rewritten. This is deliberate:
the tender idempotency key is stored in the existing `PosTender.metadata` JSON column and the balance is
derived, not persisted. Recommended (but *not* applied) follow-up is in §7.

### 4.2 Response contract — `GET /api/admin/pos/orders/` and `…/orders/<ref>/` (additive only)

New read-only fields on every POS order object:

| Field | Example | Notes |
|---|---|---|
| `net_total` | `"2600.00"` | `total_amount − returned_amount`; the collection ceiling |
| `returned_amount` | `"1300.00"` | money credited back for unopened items |
| `amount_paid` | `"1000.00"` | sum of `CAPTURED` tenders |
| `outstanding_balance` | `"1600.00"` | `max(net_total − amount_paid, 0)` |
| `settlement_action_available` | `true/false` | whether `POST …/tenders/` can legally succeed now |

Nothing was removed or renamed; existing clients keep working. `captured_amount` is an internal annotation
used to build `amount_paid` in a single query.

### 4.3 Request contract — `POST /api/admin/pos/orders/<ref>/tenders/`

Existing body (`method`, `amount`, `cash_session_reference`, `external_reference`, `notes`) is unchanged.
New **optional** fields:

| Field | Type | Effect |
|---|---|---|
| `currency` | 3-letter code | must equal the order currency, else `400` |
| `idempotency_key` | ≤96 chars | repeated key returns the existing tender, HTTP **200** instead of `201` |
| `expected_outstanding_balance` | decimal ≥ 0 | if it differs from the server's balance, `400` "outstanding balance changed" and nothing is collected |

Unchanged error behaviour: `400` for validation/refusal, `403` without `payment.capture`, `404` unknown order.
Response body shape (`{success, message, data}`) is unchanged.

### 4.4 Response contract — `GET /api/admin/pos/menu/` and `…/menu/barcode-lookup/` (additive only)

Each item's `stock` object keeps every existing key and adds:

```
available_quantity           "6"
available_sufficient_for_one true
committed_quantity           "4"
availability_unit            "portion"
components[].quantity_committed "2.0000"
components[].quantity_available "1.0000"
```

### 4.5 Service-layer signatures

* `capture_direct_tender(*, order, method, amount, actor, cash_session=None, external_reference="", notes="",
  currency="", idempotency_key="", expected_outstanding_balance=None)` → `PosTender` (**same return type**;
  `tender.idempotent_replay` is set on a replay).
* New: `tender_captured_amount(order)`, `order_outstanding_balance(order, *, captured=None)`,
  `open_pos_order_demand(required_keys, *, exclude_order=None)`,
  `pos_stock_summaries(menu_item_ids, *, include_open_order_demand=True)`.

---

## 5. What each fix protects against (quick matrix)

| Risk | Protected by | Test |
|---|---|---|
| Empty tender amount | server-provided `outstanding_balance` prefills the field | `test_order_payload_carries_the_server_derived_balance_for_a_new_delivery` |
| Charging the original total on a partially paid order | balance recomputed server-side from `net_total − captured` | `test_partially_paid_order_reports_the_remaining_amount_not_the_original_total` |
| Second collection on a settled order | explicit refusal + `settlement_action_available` | `test_fully_paid_order_refuses_another_collection_with_a_clear_reason` |
| Overpayment | `amount > outstanding` refused with both figures | `test_overpayment_is_refused_and_changes_nothing` |
| Stale screen collecting twice | `expected_outstanding_balance` mismatch refused | `test_a_stale_screen_balance_is_refused_instead_of_collected_twice` |
| Double click / retried POST | `idempotency_key` → one tender, one ledger entry | `test_duplicate_submission_with_one_idempotency_key_collects_once`, `test_repeated_http_submission_answers_200_and_does_not_collect_twice` |
| Two cashiers racing the same balance | `select_for_update()` on the order, captured rows re-summed inside the lock | `test_a_stale_screen_balance_is_refused_instead_of_collected_twice` |
| Wrong currency / unknown method | server-side validation | `test_unknown_method_and_wrong_currency_are_refused` |
| Cash tender with someone else's drawer | session ownership check | `test_cash_tender_requires_the_cashier_s_own_open_session` |
| Out-of-stock direct sale in a draft | pre-persist validation, 409 with figures | (existing `test_pos_stock_validation.py` + new suite) |
| Underserved multi-ingredient dish | whole-recipe validation, scarcest ingredient limits | `test_menu_payload_shows_available_quantity_and_unit_for_both_product_kinds` |
| Two dishes sharing one ingredient | shared-component demand | `test_shared_ingredient_reduces_availability_across_different_dishes` |
| Oversell from concurrent orders | row locks + aggregated demand | `test_simultaneous_orders_cannot_oversell_shared_ingredients` |
| Duplicate stock deduction | source-keyed movements | `test_delivery_deducts_every_ingredient_exactly_once` |
| Reversal inflating stock | idempotent reversal flag | `test_reversal_restores_the_ingredients_once` |
| Missing/broken recipe bypassing stock control | `unconfigured` + `stock_not_configured` refusal | `test_a_recipe_without_components_is_a_configuration_error_not_a_free_sale`, `test_a_broken_mapping_is_refused_as_a_configuration_error` |
| Invalid lifecycle transition | server-side transition map | `test_delivery_transitions_cannot_be_skipped_or_repeated` |
| Service-area permission leak | `_ensure_order_mode` / `_ensure_order_access` | `test_bar_only_staff_cannot_read_the_restaurant_menu_and_vice_versa`, `test_order_access_is_scoped_to_the_caller_s_service_area` |
| Refunded/returned order refused a receipt | receipt gate on `net_total` + outstanding | `frontend/tests-js/test-pos-receipt-and-transfer-ui.js` |

---

## 6. Test commands and actual results

Environment used (Python 3.13, SQLite, the repository's own `config.settings.development`):

```bash
python -m venv ../venv && ../venv/bin/pip install -r backend/requirements.txt
cd backend
export DJANGO_SECRET_KEY=test-secret-key-for-local-ci-only-not-production
export DJANGO_SETTINGS_MODULE=config.settings.development
```

### 6.1 Baseline before any change (pristine clone)

```bash
python manage.py test tests.test_pos tests.test_pos_api tests.test_pos_stock_validation \
    tests.test_pos_unopened_returns tests.test_pos_menu_management tests.test_pos_restaurant_tables \
    tests.test_barcode_lookup tests.test_inventory_sales_depletion --parallel 1
# Ran 106 tests … OK

cd frontend && node --test tests-js/
# tests 655 | pass 647 | fail 8   (the 8 failures are pre-existing and unrelated: accounting/
#                                  manifest/version.json assertions — identical in the pristine clone)
```

### 6.2 New suites (with the fixes applied)

```bash
python manage.py test tests.test_pos_tender_balance --parallel 1
# Ran 21 tests in 9.607s — OK

python manage.py test tests.test_pos_room_service_availability --parallel 1
# Ran 18 tests in 15.342s — OK
```

### 6.3 Regression suites around the change

```bash
python manage.py test tests.test_pos tests.test_pos_api tests.test_pos_stock_validation \
    tests.test_pos_unopened_returns tests.test_pos_menu_management tests.test_pos_restaurant_tables \
    tests.test_barcode_lookup tests.test_inventory_sales_depletion tests.test_inventory_api \
    tests.test_inventory_agent_patch tests.test_inventory_item_management \
    tests.test_service_request_pos_handoff tests.test_accounting_inventory_reconciliation --parallel 1
```

| Suite group | Result |
|---|---|
| POS workflow + stock validation + webhooks of the POS API | **OK** |
| Inventory API / depletion / item management / barcode lookup | **OK** |
| Unopened-item returns (money + stock + refunds) | **OK** (19 tests) |
| Accounting–inventory reconciliation | **OK** |

### 6.4 Frontend suite

```bash
cd frontend && node --test tests-js/
# tests 655 | pass 647 | fail 8   ← byte-identical to the pristine baseline (no new failures)
```

Plus a syntax check of the page's inline scripts (the JS tests only read the file as text):

```bash
node --check <extracted inline scripts of pos.html>   # SYNTAX OK
```

### 6.5 Full backend suite (final code)

Run serially (`--parallel 4` hits an unrelated multiprocessing pickling error in this sandbox, not a code
defect):

```bash
python -W ignore manage.py test tests --parallel 1 -v 2
# Ran 1911 tests in 779.829s
# FAILED (failures=1, skipped=17)
```

The single failure is **environmental and pre-existing** — it fails identically on the untouched clone:

```
FAIL: tests.test_deployment_readiness...test_production_settings_boot_cleanly_with_northflank_mysqls_and_rediss_urls
ModuleNotFoundError: No module named 'MySQLdb'   (mysqlclient is not installed in this sandbox)
```

Full command/output details are in `TEST_RESULTS.md` in this folder.

### 6.6 Behaviour before → after (same probe, unchanged inputs)

| Probe | Before | After |
|---|---|---|
| Order detail contains `outstanding_balance` | ❌ absent | ✅ `"2600.00"` → `"1600.00"` after ₦1,000 |
| Two identical tender POSTs with the same key | 201 / 201, **2 tenders** | 201 / 200, **1 tender** |
| Fully paid order, another ₦0.01 | `Tender exceeds …` (generic) | `Order … is already fully settled (2600.00 of 2600.00 NGN collected)` |
| Tender with `currency: "USD"` on an NGN order | **accepted silently** | `400` “This order is denominated in NGN …” |
| Menu availability for a 2-ingredient dish | `saleable_quantity` only (on-hand) | `available_quantity` 6, `availability_unit` `"portion"`, per-ingredient breakdown |
| Recipe refusal text | names ingredient + store only | names ingredient **and the dish** |

---

## 7. Work that is required outside this assignment (documented, not implemented)

These are Integration/Inventory/Accounting changes that the owner must schedule; none of them was made here.

1. **Inventory dashboard — recipe management UI.** The services (`create_stock_recipe`, `retire_stock_recipe`)
   and the API exist, but a kitchen manager still needs a screen to author BOMs with units and preview
   “portions from current stock”. Until that exists, every recipe is configured through the API/console, and
   any dish without a recipe keeps its documented behaviour (sold, hand-off parked `FAILED` for review).
   → `apps/inventory/views.py`, `frontend/dashboard/inventory.html` (out of scope).
2. **Inventory dashboard — commitment visibility.** `available_quantity` / `committed_quantity` are now
   produced by the shared service but are not yet surfaced in the stock console or on the consumption review
   queue. → `inventory.html`, `apps/inventory/serializers.py`.
3. **Accounting — POS tender fee policy.** This codebase models **no** processing fee for POS tender
   collections (only booking cancellation fees exist). If the hotel introduces one, it must be a deliberate
   ledger change (new `FinancialLine` on the collection) plus a policy setting — deliberately not invented
   here. The existing payment-reference handling (`external_reference` → ledger `external_reference`, tender
   reference as fallback) is preserved unchanged.
4. **Accounting — settlement reporting.** Reports currently read `PosOrder.settlement_status`. Now that
   `amount_paid` / `outstanding_balance` are first-class, a collections-ageing report could be built in
   `apps/reports` for the Accounting dashboard. Not required for this defect.
5. **Data hardening (recommended migration).** `PosTender.metadata` holds the idempotency key today. A
   dedicated `idempotency_key` column with a unique index would make de-duplication database-enforced instead
   of lock-enforced. It needs a migration and a maintenance window; deliberately **not** created here.
6. **`VOIDED` status.** The model defines it but no POS endpoint produces it (delivered orders are corrected
   through returns/void approvals in Finance). Left exactly as-is; deciding whether POS should expose a void
   action is an owner/business decision.
7. **Room-service settlement display.** Room-service orders settle on the guest folio; the POS shows no
   balance for them. Showing the live folio balance in the POS would need a new read-only endpoint
   (`folio_balance_queryset` already computes it) — out of scope for tender processing.

---

## 8. Remaining limitations and unverified issues

1. **Drafts are not reservations.** The figure shown (`available_quantity`) subtracts stock promised to open
   orders but no reservation row is created; a draft can still be invalidated by a concurrent delivery. This
   is the repository's existing design, is now labelled honestly in the UI, and is revalidated at submission
   and at the delivery-time deduction.
2. **Delivery is never blocked by a stock shortage.** If ingredients run out between submission and delivery,
   the order is delivered and charged while the inventory hand-off is parked `FAILED` for review. Changing
   this would block a served meal; the gap is visible in the inventory review queue.
3. **Idempotency is lock-enforced, not constraint-enforced.** Concurrent duplicates on the *same order*
   serialise on the order row, which is correct, but two *different* orders are unaffected by design. A DB
   unique index is the recommended hardening (§7.5).
4. **Verified on SQLite only.** Production runs MySQL 8. The implementation uses only portable constructs
   (`select_for_update`, `Subquery`, `Coalesce`, JSON `metadata__key` lookups), but the MySQL row-lock and
   JSON-lookup behaviour was **not** executed against a MySQL instance in this environment. The MySQL-specific
   concurrency interleavings (e.g. `REPEATABLE READ` snapshot timing around the locking read) are therefore
   *reasoned about*, not observed.
5. **Untested combinations:** multi-currency folios for room service; `PARTIALLY_REFUNDED` → tender top-up
   (refunded tenders are excluded from `amount_paid` by design, but that path has no dedicated test);
   concurrent *return* and *tender* on the same order; a tender captured while a table session is being
   closed.
6. **Frontend is verified by source-assertion tests and a syntax check, not by a real browser session.**
   No headless-browser run was performed in this environment, so modal layout on a physical phone is
   unverified (the changes use the page's existing responsive classes).
7. **The 8 pre-existing `tests-js` failures** (accounting/manifest/version.json assertions) remain untouched
   and fail identically before and after this work. They are unrelated to POS.
8. **`order.amount_paid` in the returns panel** now renders correctly, but the returns list itself is still
   fed by `PosReturnableOrderSerializer`; if a deployment adds new fields there, keep it in step with
   `PosOrderListSerializer` to avoid the same “field that never existed” bug.

---

## 9. Safe patch application and rollback

### 9.1 Apply

```bash
# from the repository root, on a clean branch
git checkout -b fix/pos-tender-stock
cd /path/to/ai_updates/pos_room_service_agent
cp -r backend frontend /path/to/j1-hotel-management-system/         # copies ONLY the 8 listed files
# or, review-then-apply:
git -C /path/to/j1-hotel-management-system apply --check PATCH.diff   # dry run
git -C /path/to/j1-hotel-management-system apply PATCH.diff
```

Then:

```bash
cd backend
DJANGO_SETTINGS_MODULE=config.settings.production python manage.py check
python manage.py test tests.test_pos_tender_balance tests.test_pos_room_service_availability \
    tests.test_pos tests.test_pos_stock_validation tests.test_pos_unopened_returns --parallel 1
python manage.py collectstatic --noinput        # pos.html is a static asset
```

* **No migration is required** (`python manage.py makemigrations --check` must report *no changes*; if it
  reports any, a file was copied from the wrong revision — stop).
* **No data backfill** is required.
* Deploy order: backend first (the added response fields are ignored by an old frontend), then the frontend.
  A new frontend against an old backend degrades gracefully: the amount box falls back to `total_amount`
  only when `outstanding_balance` is absent, and the server still refuses overpayment.

### 9.2 Rollback

```bash
git checkout -- backend/apps/pos/services/order_service.py \
                backend/apps/pos/serializers.py \
                backend/apps/pos/views.py \
                backend/apps/inventory/services/inventory_service.py \
                frontend/dashboard/pos.html \
                frontend/tests-js/test-pos-receipt-and-transfer-ui.js
rm backend/tests/test_pos_tender_balance.py backend/tests/test_pos_room_service_availability.py
git apply -R PATCH.diff        # equivalent, if you prefer a single command
```

Rollback is data-safe: no schema change and no data rewrite occurred, so reverting the code restores the
previous behaviour exactly. Tenders created while the fix was live remain valid rows with valid ledger
entries; a duplicate *cannot* have been created by the new code, and any legacy duplicate still needs the
existing Finance reconciliation workflow.
