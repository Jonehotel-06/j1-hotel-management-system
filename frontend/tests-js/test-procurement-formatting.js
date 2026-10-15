/* Procurement quantity/currency formatting contract tests (§9/§10).
   Run from /frontend: node --test tests-js/test-procurement-formatting.js

   The purchase-order dialog used to print the raw four-decimal column values
   ("4.0000") and the bare unit cost ("175.00"), and computed the outstanding
   quantity with floating-point subtraction in the browser. These tests pin the
   corrected behaviour: quantities go through the shared quantity formatter with
   the product's own unit, money through the shared currency formatter, and the
   outstanding quantity comes from the server's exact decimal. */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { makeContext, loadScript, FRONTEND, disposeAll } = require("./harness");

test.after(() => disposeAll());

const PAGE = fs.readFileSync(path.join(FRONTEND, "dashboard", "procurement.html"), "utf8");

function loadHelpers(ctx) {
  const start = PAGE.indexOf("function qty(value,unit)");
  const end = PAGE.indexOf("function dateTime(value)");
  assert.ok(start > 0 && end > start, "page helpers not found in the shipped page");
  vm.runInContext(PAGE.slice(start, end), ctx, { filename: "procurement.html#helpers" });
  return ctx.window;
}

test("the page reuses the shared formatters instead of printing raw columns", () => {
  assert.match(PAGE, /function qty\(value,unit\)\{return JONE\.formatQuantity\(value,\{unit:unit\}\);\}/);
  assert.match(PAGE, /function money\(value,currency\)\{return JONE\.formatMoney\(value,\{currency:currency\|\|"NGN"\}\);\}/);
  for (const field of ["line.quantity_ordered", "line.quantity_received", "outstanding"]) {
    assert.ok(PAGE.includes("qty(" + field + ",line.base_unit)"), field + " is not formatted");
  }
  assert.ok(PAGE.includes("money(line.unit_cost)"), "the unit cost is not formatted as money");
  assert.doesNotMatch(PAGE, /toFixed\(4\)/);
  assert.doesNotMatch(PAGE, /Number\(line\.quantity_ordered\)-Number\(line\.quantity_received\)/);
});

test("the outstanding quantity comes from the server's exact decimal", () => {
  assert.match(PAGE, /line\.quantity_outstanding!=null\?line\.quantity_outstanding:line\.quantity_ordered/);
  assert.match(PAGE, /data-proc-receipt-max="'\+JONE\.esc\(remain\)\+'"/);
});

test("quantities render with the product's unit and measured precision", () => {
  const ctx = makeContext();
  loadScript(ctx, "js/utils.js");
  const win = loadHelpers(ctx);
  assert.equal(win.qty("4.0000", "BOTTLE"), "4 BOTTLE");
  assert.equal(win.qty("0.0000", "CRATE"), "0 CRATE");
  // The unit string is the product's own configured stock unit, shown as stored;
  // the PRECISION is what varies by unit, never the value.
  assert.equal(win.qty("1.2500", "KG"), "1.25 KG");
  assert.equal(win.qty("2.5000", "L"), "2.5 L");
  assert.equal(win.qty("3.5000", ""), "3.5");
  assert.equal(win.qty("0.0500", ""), "0.05");
});

test("costs render with separators and the currency's decimal precision", () => {
  const ctx = makeContext();
  loadScript(ctx, "js/utils.js");
  const win = loadHelpers(ctx);
  assert.equal(win.money("175.00"), "\u20a6175.00");
  assert.equal(win.money("250000.00"), "\u20a6250,000.00");
  assert.equal(win.money(0), "\u20a60.00");
  assert.equal(win.money("12.5"), "\u20a612.50");
  assert.equal(win.money(null), "--");
  assert.equal(win.money(""), "--");
});

test("the modified line cells keep their alignment and nowrap contract", () => {
  const numeric = PAGE.match(/<td class="num cell-nowrap">'\+JONE\.esc\((qty|money)\(/g) || [];
  assert.ok(numeric.length >= 4, "expected >=4 right-aligned, non-wrapping value cells, got " + numeric.length);
});
