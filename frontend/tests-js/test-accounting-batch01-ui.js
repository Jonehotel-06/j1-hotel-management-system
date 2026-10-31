/* Batch 01 — Accounting UI contracts.
 *
 * Static assertions over the shipped pages, in the style the rest of this
 * suite uses: no browser, no network, just the source the hotel deploys.
 * They pin the behaviour the owner asked for so a later edit cannot quietly
 * put a conflated figure, a hidden row or a broken export back.
 */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const FRONTEND = path.join(__dirname, "..");
const read = (...parts) => fs.readFileSync(path.join(FRONTEND, ...parts), "utf8");
const ACCOUNTING = read("dashboard", "accounting.html");
const REPORTS = read("dashboard", "reports.html");
const API = read("js", "api.js");

/* ---------------------------------------------------------------- task 1 */

test("task 1: Accounting leads with Restaurants and Bar Collections, not a conflated total", () => {
  assert.match(ACCOUNTING, /<div class="kpi-label">Restaurants and Bar Collections<\/div>/);
  assert.match(ACCOUNTING, /data-rp="restaurant-bar-collections"/);
  assert.match(ACCOUNTING, /\{ name:"restaurant-bar-collections", key:"restaurant_bar_collections" \}/);

  // The old "Recognized revenue" tile is NOT simply relabelled: it stays as a
  // separate, honestly named total so the two measures cannot be confused.
  assert.doesNotMatch(ACCOUNTING, /<div class="kpi-label">Recognized revenue<\/div>/);
  assert.match(ACCOUNTING, /<div class="kpi-label">Total revenue earned<\/div>/);
  assert.match(ACCOUNTING, /\{ name:"recognized-revenue", key:"recognized_revenue" \}/);
});

test("task 1: room service and accommodation are reported apart from the bar", () => {
  for (const measure of [
    /\{ label:"Accommodation earned \(rooms\)", key:"accommodation_revenue" \}/,
    /\{ label:"Restaurants and bar revenue earned", key:"restaurant_bar_revenue" \}/,
    /\{ label:"Room service revenue earned", key:"room_service_revenue" \}/,
    /\{ label:"Other operating income", key:"other_operating_revenue" \}/
  ]) {
    assert.match(ACCOUNTING, measure);
    assert.match(REPORTS, measure);
  }
});

/* ---------------------------------------------------------------- task 2 */

test("task 2: Total expenses is a prominent figure and a reconciled breakdown", () => {
  assert.match(ACCOUNTING, /<div class="kpi-label">Total expenses<\/div>/);
  assert.match(ACCOUNTING, /data-rp="total-expenses"/);
  assert.match(ACCOUNTING, /\{ name:"total-expenses", key:"total_expenses" \}/);
  assert.match(ACCOUNTING, /\{ label:"Total expenses", key:"total_expenses" \}/);
  // Operating expenses survive as their own line inside the detail, so the
  // new total never replaces a measure the hotel already relied on.
  assert.match(ACCOUNTING, /\{ label:"Operating expenses", key:"operating_expenses" \}/);
  assert.match(ACCOUNTING, /\{ label:"Payroll expenses", key:"payroll_expenses" \}/);
  // The printed pack carries the detail the total has to agree with.
  assert.match(ACCOUNTING, /rep\.expense_breakdown/);
  assert.match(ACCOUNTING, /Expenses by department/);
});

/* ---------------------------------------------------------------- task 3 */

test("task 3: no statutory tax figure and no row-hiding anywhere in Accounting", () => {
  // Comments documenting the removal are fine; a rendered figure is not.
  const code = ACCOUNTING.replace(/\/\*[\s\S]*?\*\//g, "").replace(/<!--[\s\S]*?-->/g, "");
  assert.doesNotMatch(code, /statutory/i);
  assert.doesNotMatch(code, /tax_accrued/);
  assert.doesNotMatch(ACCOUNTING, /atob\(/);
  assert.doesNotMatch(ACCOUNTING, /function hidden\(/);
});

/* ---------------------------------------------------------------- task 4 */

test("task 4: profit and loss separates income, expenses and collections", () => {
  assert.match(ACCOUNTING, /rep\.profit_and_loss/);
  assert.match(ACCOUNTING, /Income — revenue earned in the period/);
  assert.match(ACCOUNTING, /Expenses — posted costs for the period/);
  assert.match(ACCOUNTING, /Net result \(income − discounts − total expenses\)/);
  assert.match(ACCOUNTING, /Money received in the period — collections, not income/);
  assert.match(ACCOUNTING, /pnl\.collections_memorandum/);
  // Booking money is a collection measure on this screen, never an income one.
  assert.match(ACCOUNTING, /\{ label:"Booking payments received \(cash\)", key:"booking_payments_received" \}/);
});

/* ---------------------------------------------------------------- task 7 */

test("task 7: guest receivables show credits, deposits and a reconciliation", () => {
  assert.match(ACCOUNTING, /<h2>Guest receivables<\/h2>/);
  for (const tile of ['data-ar="gross"', 'data-ar="credits"', 'data-ar="deposits"', 'data-ar="unposted"']) {
    assert.ok(ACCOUNTING.includes(tile), `missing receivable tile ${tile}`);
  }
  // The identity that makes the report trustworthy is printed on the page.
  assert.match(ACCOUNTING, /d\.reconciliation/);
  assert.match(ACCOUNTING, /Guest balances reconcile to/);
  assert.match(ACCOUNTING, /guest balances differ from the control account/);
  // A credit balance is its own column; it is never collapsed into zero.
  assert.match(ACCOUNTING, /x\.credit_balance/);
  assert.match(ACCOUNTING, /x\.folio_reference/);
  assert.match(ACCOUNTING, /x\.rooms/);
  assert.match(ACCOUNTING, /stay_value_not_yet_charged/);
});

/* ---------------------------------------------------------------- task 8 */

test("task 8: downloadReport cannot lose start, end or export again", () => {
  // The defect: `params:{...}` was spread BEFORE `...opts`, so a caller that
  // passed its own `opts.params` replaced the merged object and the request
  // went out without a range or an export format. The server then answered
  // with JSON and the blob guard rejected it.
  const source = API.slice(API.indexOf("function downloadReport"));
  const body = source.slice(0, source.indexOf("\n  }\n") + 4);

  assert.match(body, /const \{ params: callerParams, \.\.\.rest \} = opts;/);
  assert.match(body, /query\.export = extension;/);
  // `params` must be the LAST property, after the caller's other options.
  const restAt = body.indexOf("...rest,");
  const paramsAt = body.indexOf("params: query");
  assert.ok(restAt !== -1 && paramsAt > restAt,
    "params must be applied after ...rest so it can never be overwritten");
  assert.doesNotMatch(body, /params: \{ \.\.\.\(opts\.params \|\| \{\}\), start, end, export: extension \},\s*\n\s*\.\.\.opts/);
});

test("task 8: every export button still routes through downloadReport", () => {
  for (const id of ["export-xlsx", "export-pdf", "export-occ-xlsx", "export-occ-pdf"]) {
    assert.ok(REPORTS.includes(`id="${id}"`), `missing export control ${id}`);
  }
  assert.match(REPORTS, /downloadReport/);
  assert.match(ACCOUNTING, /downloadReport/);
});
