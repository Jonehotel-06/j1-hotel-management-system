/* tests-js/test-formatting.js */
/* Shared money and quantity formatting (§9, §10).

   The defect these lock down: monetary columns rendered without thousands
   separators, and inventory quantities rendered as the raw four-decimal column
   value ("+20.0000") for products that are counted in whole units. */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { makeContext, loadScript, disposeAll } = require("./harness");

test.after(() => disposeAll());

function boot() {
  const ctx = makeContext();
  loadScript(ctx, "js/utils.js");
  return ctx.window.JONE;
}

/* --------------------------------- money --------------------------------- */

test("money uses thousands separators and two decimal places", () => {
  const J = boot();
  assert.equal(J.formatNaira(165000), "\u20a6165,000.00");
  assert.equal(J.formatNaira("165000.00"), "\u20a6165,000.00");
  assert.equal(J.formatNaira(1250000), "\u20a61,250,000.00");
  assert.equal(J.formatNaira(85000), "\u20a685,000.00");
  assert.equal(J.formatNaira(0), "\u20a60.00");
  assert.equal(J.formatNaira(999), "\u20a6999.00");
  assert.equal(J.formatNaira(1000), "\u20a61,000.00");
});

test("money preserves decimals that are actually present", () => {
  const J = boot();
  assert.equal(J.formatNaira("165000.5"), "\u20a6165,000.50");
  assert.equal(J.formatNaira("165000.55"), "\u20a6165,000.55");
  assert.equal(J.formatNaira("0.01"), "\u20a60.01");
});

test("money keeps negative amounts, refunds and reversals signed", () => {
  const J = boot();
  assert.equal(J.formatNaira(-5000), "\u2212\u20a65,000.00");
  assert.equal(J.formatNaira("-5000.00"), "\u2212\u20a65,000.00");
  assert.equal(J.formatNaira(0 - 1), "\u2212\u20a61.00");
});

test("money shows a placeholder rather than a misleading zero", () => {
  const J = boot();
  assert.equal(J.formatNaira(null), "--");
  assert.equal(J.formatNaira(undefined), "--");
  assert.equal(J.formatNaira(""), "--");
  assert.equal(J.formatNaira("not a number"), "--");
  assert.equal(J.formatNaira(NaN), "--");
});

test("money honours the requested currency and precision", () => {
  const J = boot();
  assert.equal(J.formatMoney(1234.5, { currency: "USD" }), "$1,234.50");
  assert.equal(J.formatMoney(1234.5, { currency: "NGN", decimals: 0 }), "\u20a61,235");
  // Badge-friendly compact mode drops the cents, never the separators.
  assert.equal(J.formatNaira(5000, { compact: true }), "\u20a65,000");
});

test("money formats numbers, not strings that already contain a symbol", () => {
  const J = boot();
  // A pre-symbolised string cannot be parsed as a number, so it is rejected
  // rather than double-prefixed.
  assert.equal(J.formatNaira("\u20a6165,000"), "--");
});

/* ------------------------------- quantities ------------------------------ */

test("whole-unit products show no decimals", () => {
  const J = boot();
  assert.equal(J.formatQuantity("20.0000", { unit: "BOTTLE" }), "20 BOTTLE");
  assert.equal(J.formatQuantity("12.0000", { unit: "PACK" }), "12 PACK");
  assert.equal(J.formatQuantity("3", { unit: "PIECE" }), "3 PIECE");
  assert.equal(J.formatQuantity("0.0000", { unit: "CRATE" }), "0 CRATE");
  assert.equal(J.formatStockQuantity("20.0000", { base_unit: "BOTTLE" }), "20 BOTTLE");
});

test("measured products keep their configured precision", () => {
  const J = boot();
  // 1.250 must not become 1, and must not lose its significant zeroes.
  assert.equal(J.formatQuantity("1.250", { unit: "KG" }), "1.250 KG");
  assert.equal(J.formatQuantity("2.500", { unit: "L" }), "2.500 L");
  assert.equal(J.formatQuantity("2.5", { unit: "LITRE" }), "2.500 LITRE");
  assert.equal(J.formatQuantity("0.1500", { unit: "L" }), "0.150 L");
});

test("an explicit precision overrides the unit default", () => {
  const J = boot();
  assert.equal(J.formatQuantity("20.0000", { unit: "BOTTLE", precision: 2 }), "20.00 BOTTLE");
  assert.equal(J.formatQuantity("2.5", { unit: "L", precision: 1 }), "2.5 L");
  assert.equal(J.formatQuantity("1.25", { unit: "KG", precision: 0 }), "1 KG");
});

test("an unknown unit is trimmed, never rounded away", () => {
  const J = boot();
  assert.equal(J.formatQuantity("20.0000", { unit: "BAG-CUSTOM" }), "20 BAG-CUSTOM");
  assert.equal(J.formatQuantity("1.250", { unit: "BAG-CUSTOM" }), "1.25 BAG-CUSTOM");
  assert.equal(J.formatQuantity("1.2500", {}), "1.25");
});

test("movement signs read clearly: +20 received, -4 issued", () => {
  const J = boot();
  assert.equal(J.formatQuantity("20.0000", { unit: "BOTTLE", signed: true }), "+20 BOTTLE");
  assert.equal(J.formatQuantity("-4.0000", { unit: "BOTTLE", signed: true }), "\u22124 BOTTLE");
  assert.equal(J.formatQuantity("-0.1500", { unit: "L", signed: true }), "\u22120.150 L");
  assert.equal(J.formatQuantity("0.0000", { unit: "BOTTLE", signed: true }), "0 BOTTLE");
});

test("the unit label can be suppressed and the raw value is never modified", () => {
  const J = boot();
  assert.equal(J.formatQuantity("2.500", { unit: "L", showUnit: false }), "2.500");
  const raw = "2.5000";
  J.formatQuantity(raw, { unit: "L" });
  assert.equal(raw, "2.5000", "formatting must never mutate its input");
});

test("quantities reject non-numeric input instead of printing NaN", () => {
  const J = boot();
  assert.equal(J.formatQuantity(null, { unit: "BOTTLE" }), "--");
  assert.equal(J.formatQuantity(undefined, { unit: "BOTTLE" }), "--");
  assert.equal(J.formatQuantity("", { unit: "BOTTLE" }), "--");
});

test("quantity precision is resolvable per unit for table cells", () => {
  const J = boot();
  assert.equal(J.quantityPrecisionFor("BOTTLE"), 0);
  assert.equal(J.quantityPrecisionFor("L"), 3);
  assert.equal(J.quantityPrecisionFor("KG"), 3);
  assert.equal(J.quantityPrecisionFor("mystery"), null);
});

test("large integer quantities still group correctly", () => {
  const J = boot();
  assert.equal(J.formatQuantity("12500.0000", { unit: "PIECE" }), "12,500 PIECE");
  assert.equal(J.formatNaira(1250000000), "\u20a61,250,000,000.00");
});
