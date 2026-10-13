/* Product profitability / stock-valuation projection UI contract tests.
   Run from /frontend: node --test tests-js/test-profitability-ui.js */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { makeContext, loadScript, FRONTEND, disposeAll } = require("./harness");

test.after(() => disposeAll());

const PAGE = fs.readFileSync(path.join(FRONTEND, "dashboard", "inventory.html"), "utf8");
const CSS = fs.readFileSync(path.join(FRONTEND, "css", "dashboard.css"), "utf8");

test("the profitability endpoints are declared once and are read-only paths", () => {
  const ctx = makeContext();
  loadScript(ctx, "js/config.js");
  const endpoints = ctx.window.APP_CONFIG.API_ENDPOINTS;
  assert.equal(endpoints.inventoryProfitability, "/api/admin/inventory/profitability/");
  assert.equal(endpoints.inventoryProfitabilityFilters, "/api/admin/inventory/profitability/filters/");
});

test("the projection is labelled as a projection everywhere it can be misread", () => {
  // A reader must never mistake the projection for posted accounting balances.
  assert.match(PAGE, /Projection, not posted accounts/);
  assert.match(PAGE, /posts money or changes stock/);
  // And the payload's own label is surfaced, not invented client-side.
  assert.doesNotMatch(PAGE, /projection\s*=\s*"PROJECTION"/);
});

test("every column the brief requires is present and money columns are right-aligned", () => {
  for (const heading of [
    "Product", "Department", "Location", "Supplier", "Unit cost", "Selling price",
    "On hand", "Stock value", "Expected revenue", "Expected gross profit", "Margin",
  ]) {
    assert.ok(PAGE.includes(`>${heading}</button>`) || PAGE.includes(`>${heading}<`), `missing column: ${heading}`);
  }
  // Numeric headings carry .num so the money/quantity columns right-align.
  const numeric = PAGE.match(/<th class="num cell-nowrap[^>]*data-inv-sort=/g) || [];
  assert.ok(numeric.length >= 6, `expected >=6 right-aligned numeric columns, got ${numeric.length}`);
});

test("atomic identifier/date/money columns opt out of wrapping, prose columns clamp", () => {
  // Product code, location, unit cost, stock value stay on one line ...
  assert.match(PAGE, /<th class="cell-nowrap" data-inv-sort="sku"/);
  assert.match(PAGE, /<th class="cell-nowrap" data-inv-sort="location"/);
  // ... while the (potentially long) product name and supplier name clamp.
  assert.match(PAGE, /class="caption cell-clamp"/);
  assert.match(PAGE, /class="hide-md cell-clamp"/);
  // Money/quantity cells never wrap on the number itself.
  assert.match(PAGE, /class="num cell-nowrap"/);
});

test("the table scrolls inside its own container, never the page", () => {
  assert.match(PAGE, /class="table-wrap table-scroll"/);
  assert.match(CSS, /\.table-wrap\.table-scroll\s*\{\s*overflow-x:\s*auto/);
  assert.match(CSS, /\.table-wrap\.table-scroll \.table \{ min-width: 1080px; \}/);
  // A narrow viewport releases the minimum width instead of forcing a scroll.
  assert.match(CSS, /@media \(max-width: 720px\) \{\n  \.table-wrap\.table-scroll \.table \{ min-width: 0; \}/);
});

test("sorting is server-driven on a whitelisted key and exposes aria-sort", () => {
  assert.match(PAGE, /profitState=\{sort:"sku",direction:"asc"/);
  assert.match(PAGE, /sort:profitState\.sort,direction:profitState\.direction/);
  // Clicking the active column flips direction; a new column starts ascending.
  assert.match(PAGE, /profitState\.direction=profitState\.direction==="asc"\?"desc":"asc"/);
  assert.match(PAGE, /th\.setAttribute\("aria-sort",profitState\.direction==="asc"\?"ascending":"descending"\)/);
  // The sortable heading is a real button (keyboard reachable), not a click-only th.
  assert.match(PAGE, /<button class="th-sort" type="button" data-inv-sort-key="stock_value">/);
  assert.match(CSS, /\.table th \.th-sort:focus-visible/);
});

test("filters and pagination are bounded server-side requests", () => {
  for (const param of ["location_id", "department", "category", "supplier_id", "sellable", "low_stock", "search"]) {
    assert.ok(PAGE.includes(`params.${param}`), `filter parameter not wired: ${param}`);
  }
  assert.match(PAGE, /page:pageFor\("profitability"\),page_size:JONE\.dashboard\.pageSize\(\)/);
  assert.match(PAGE, /pager\("\[data-inv-profit-pagination\]"/);
});

test("a not-derivable figure renders as a dash and never as a fabricated zero", () => {
  const vm = require("node:vm");
  const ctx = makeContext();
  loadScript(ctx, "js/utils.js");
  // Pull the real helpers out of the shipped page and run them verbatim: no
  // retyped copies, so the test cannot drift from production code.
  const start = PAGE.indexOf("function proj(value,currency,opts)");
  const end = PAGE.indexOf("async function loadProfitabilityOptions()");
  assert.ok(start > 0 && end > start, "projection helpers not found in the shipped page");
  vm.runInContext(PAGE.slice(start, end), ctx, { filename: "inventory.html#projection-helpers" });
  const win = ctx.window;
  assert.equal(win.proj(null), "\u2014");
  assert.equal(win.proj(undefined), "\u2014");
  assert.equal(win.proj(""), "\u2014");
  assert.equal(win.proj(0), "\u20a60.00", "a real zero must still render as zero");
  assert.equal(win.proj("165000.00"), "\u20a6165,000.00");
  assert.equal(win.proj("-5000"), "\u2212\u20a65,000.00", "reversals keep their sign");
  assert.equal(win.projQty(null, {}), "\u2014");
  assert.equal(win.projQty("20.0000", { base_unit: "BOTTLE" }), "20 BOTTLE");
  assert.equal(win.projQty("2.5000", { base_unit: "L" }), "2.500 L", "measured units keep their precision");
  assert.equal(win.projPercent(null), "\u2014");
  assert.equal(win.projPercent("36.50"), "36.50%");
});

test("the cost basis and price basis behind each figure are disclosed to the reader", () => {
  // A reader can tell WHY a number is what it is: weighted-average receipts vs
  // standard cost, and how many active recipes the price came from.
  assert.match(PAGE, /Weighted-average of goods receipts/);
  assert.match(PAGE, /Standard cost \(no receipts costed yet\)/);
  assert.match(PAGE, /No cost recorded/);
  assert.match(PAGE, /From "\+row\.recipe_count\+" active recipe/);
  assert.match(PAGE, /No active recipe/);
});

test("the summary discloses coverage instead of implying a complete total", () => {
  assert.match(PAGE, /rows can be priced from an active recipe/);
  assert.match(PAGE, /are valued only/);
  assert.match(PAGE, /Figures cover the current filter, not just this page/);
});

test("the new tab is wired into the existing tab/panel/activate machinery", () => {
  assert.match(PAGE, /data-inv-tab="profitability">Profitability<\/button>/);
  assert.match(PAGE, /<section class="panel" data-inv-panel="profitability" hidden>/);
  assert.match(PAGE, /if\(tab==="profitability"\)loadProfitability\(\);/);
  assert.match(PAGE, /profitability:1\}/);
  // It reuses the shared loading/empty/error states rather than a bespoke spinner.
  assert.match(PAGE, /DATA\.loading\(profitRows,11\)/);
  assert.match(PAGE, /DATA\.empty\(profitRows,"inbox"/);
  assert.match(PAGE, /DATA\.error\(profitRows,/);
});
