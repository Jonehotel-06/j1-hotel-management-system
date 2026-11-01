/* tests-js/test-barcode-label-print.js
 *
 * Task 01 of targeted_fixes_01 — searchable, selective barcode label printing.
 *
 * These tests exercise the real production module (frontend/js/barcode-labels.js)
 * inside the shared vm context, so they prove behaviour rather than the shape of
 * the HTML:
 *
 *   • search matches a product by NAME, SKU, BARCODE and ID;
 *   • only the ticked products print, at their own per-product quantity;
 *   • "select all matching" applies to the FILTERED result, not the whole list;
 *   • deselecting removes a product's labels from the plan entirely;
 *   • a product with no barcode prints its SKU (never a fabricated value);
 *   • the preview markup and the print plan are built from the same state.
 *
 * Run from /frontend: node --test tests-js/test-barcode-label-print.js
 */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const FRONTEND = path.resolve(__dirname, "..");

function loadLabels() {
  const sandbox = {
    window: {},
    console,
    setTimeout,
    clearTimeout,
  };
  sandbox.window.JONE = {
    esc: (value) =>
      String(value === undefined || value === null ? "" : value)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;"),
    barcode: {
      /* A stand-in for the real Code 128 renderer: it records the code it was
         asked to encode and refuses the sentinel used by these tests. */
      generateBarcodeSvg(code) {
        const value = String(code || "").trim();
        if (!value) throw new Error("empty");
        if (value === "BAD CODE") throw new Error("not encodable");
        return `<svg data-code="${value}"></svg>`;
      },
    },
  };
  vm.createContext(sandbox);
  const source = fs.readFileSync(path.join(FRONTEND, "js/barcode-labels.js"), "utf8");
  vm.runInContext(source, sandbox, { filename: "barcode-labels.js" });
  return sandbox.window.JONE.barcodeLabels;
}

const LABELS = loadLabels();

const MENU = [
  { id: 11, name: "Jollof Rice", sku: "MNU-011", barcode: "", base_price: "2500.00", category_name: "Rice" },
  { id: 12, name: "Grilled Chicken", sku: "MNU-012", barcode: "6001111111111", category_name: "Protein" },
  { id: 13, name: "Chapman", sku: "MNU-013", barcode: "6002222222222", category_name: "Bar" },
];

const STOCK = [
  { id: 41, name: "Rice 50kg", sku: "RIC-50", barcode: "6009876543210", base_unit: "BAG" },
  { id: 42, name: "Tomato Paste", sku: "TOM-01", barcode: "BAD CODE", base_unit: "TIN" },
];

function loadedState() {
  const state = LABELS.createState();
  LABELS.addItems(state, MENU.map((row) => Object.assign({}, row, { source: "menu" })));
  LABELS.addItems(state, STOCK.map((row) => Object.assign({}, row, { source: "stock" })));
  return state;
}

test("search finds a product by name, SKU, barcode and ID", () => {
  const state = loadedState();

  LABELS.setQuery(state, "grilled");
  assert.deepEqual([...LABELS.filtered(state).map((row) => row.name)], ["Grilled Chicken"], "by name");

  LABELS.setQuery(state, "RIC-50");
  assert.deepEqual([...LABELS.filtered(state).map((row) => row.name)], ["Rice 50kg"], "by SKU");

  LABELS.setQuery(state, "6009876543210");
  assert.deepEqual([...LABELS.filtered(state).map((row) => row.name)], ["Rice 50kg"], "by barcode");

  LABELS.setQuery(state, "42");
  assert.deepEqual([...LABELS.filtered(state).map((row) => row.name)], ["Tomato Paste"], "by ID");

  LABELS.setQuery(state, "");
  assert.equal(LABELS.filtered(state).length, 5, "an empty search shows everything");
});

test("search is case-insensitive and trims surrounding spaces", () => {
  const state = loadedState();
  LABELS.setQuery(state, "   chapman  ");
  assert.equal(LABELS.filtered(state).length, 1);
  LABELS.setQuery(state, "CHAPMAN");
  assert.equal(LABELS.filtered(state).length, 1);
});

test("a search with no match reports zero without losing the catalogue", () => {
  const state = loadedState();
  LABELS.setQuery(state, "no-such-product");
  assert.equal(LABELS.filtered(state).length, 0);
  assert.equal(state.items.length, 5);
  LABELS.setQuery(state, "");
  assert.equal(LABELS.filtered(state).length, 5);
});

test("only the selected products print, at their own per-product quantity", () => {
  const state = loadedState();
  const jollof = state.items.find((row) => row.name === "Jollof Rice");
  const rice = state.items.find((row) => row.name === "Rice 50kg");
  const chicken = state.items.find((row) => row.name === "Grilled Chicken");

  LABELS.toggle(state, jollof, true);
  LABELS.setQuantity(state, jollof, 4);
  LABELS.toggle(state, rice, true);
  LABELS.setQuantity(state, rice, 2);

  assert.equal(LABELS.selectedItems(state).length, 2);
  assert.equal(LABELS.labelCount(state), 6);

  const plan = LABELS.printPlan(state);
  assert.equal(plan.length, 6);
  assert.equal(plan.filter((entry) => entry.name === "Jollof Rice").length, 4);
  assert.equal(plan.filter((entry) => entry.name === "Rice 50kg").length, 2);
  assert.equal(plan.filter((entry) => entry.name === "Grilled Chicken").length, 0, "unselected never prints");

  /* The copy index is what the sheet prints as "Label 2 of 4". */
  const jollofEntries = plan.filter((entry) => entry.name === "Jollof Rice");
  assert.deepEqual([...jollofEntries.map((entry) => entry.index)], [1, 2, 3, 4]);
  assert.ok(jollofEntries.every((entry) => entry.of === 4));
});

test("deselecting a product removes it from the print plan immediately", () => {
  const state = loadedState();
  const chapman = state.items.find((row) => row.name === "Chapman");
  LABELS.toggle(state, chapman, true);
  LABELS.setQuantity(state, chapman, 3);
  assert.equal(LABELS.labelCount(state), 3);

  LABELS.toggle(state, chapman, false);
  assert.equal(LABELS.labelCount(state), 0);
  assert.equal(LABELS.printPlan(state).length, 0);
});

test("select-all applies to the filtered result only", () => {
  const state = loadedState();
  LABELS.setQuery(state, "rice");
  const matching = [...LABELS.filtered(state).map((row) => row.name)].sort();
  assert.deepEqual(matching, ["Jollof Rice", "Rice 50kg"]);

  LABELS.selectAllFiltered(state, true);
  assert.equal(LABELS.selectedItems(state).length, 2, "only the two rice products are selected");
  assert.ok(LABELS.selectedItems(state).every((row) => /rice/i.test(row.name)));

  /* A different search must not silently drag the earlier selection along. */
  LABELS.setQuery(state, "chapman");
  assert.equal(LABELS.summary(state).selectedInView, 0);
  assert.equal(LABELS.summary(state).selected, 2, "the rice selection is still on record");
});

test("select-all toggles between all and none for the current filter", () => {
  const state = loadedState();
  LABELS.setQuery(state, "chapman");
  LABELS.selectAllFiltered(state, true);
  assert.equal(LABELS.selectedItems(state).length, 1);
  LABELS.selectAllFiltered(state, false);
  assert.equal(LABELS.selectedItems(state).length, 0);
});

test("a product with no barcode prints its SKU and never a fabricated value", () => {
  const state = loadedState();
  const jollof = state.items.find((row) => row.name === "Jollof Rice");
  assert.equal(jollof.barcode, "");
  assert.equal(LABELS.codeOf(jollof), "MNU-011");

  LABELS.toggle(state, jollof, true);
  const plan = LABELS.printPlan(state);
  assert.equal(plan[0].code, "MNU-011");
  assert.match(LABELS.labelMarkup(plan[0]), /data-code="MNU-011"/);
});

test("an unencodable code is reported, not printed as a guess", () => {
  const state = loadedState();
  const tomato = state.items.find((row) => row.name === "Tomato Paste");
  LABELS.toggle(state, tomato, true);
  const markup = LABELS.labelMarkup(LABELS.printPlan(state)[0]);
  assert.match(markup, /cannot be printed as Code 128/);
  assert.doesNotMatch(markup, /<svg/);
});

test("the preview sheet and the print plan are the same labels", () => {
  const state = loadedState();
  LABELS.selectAllFiltered(state, true);
  LABELS.setQuantity(state, state.items[0], 2);
  const plan = LABELS.printPlan(state);
  const sheet = LABELS.sheetMarkup(state);
  const rendered = (sheet.match(/data-label-key=/g) || []).length;
  assert.equal(rendered, plan.length, "the sheet renders exactly one card per planned label");
  assert.equal(rendered, LABELS.labelCount(state));
});

test("quantities are clamped to a sane printable range", () => {
  const state = loadedState();
  const item = state.items[0];
  LABELS.setQuantity(state, item, 0);       // below the minimum
  assert.equal(LABELS.quantityFor(state, item), 1);
  LABELS.setQuantity(state, item, 99999);   // above the maximum
  assert.equal(LABELS.quantityFor(state, item), LABELS.MAX_LABELS_PER_PRODUCT);
  LABELS.setQuantity(state, item, "7");
  assert.equal(LABELS.quantityFor(state, item), 7, "a typed value is honoured");
});

test("the default label count seeds products that have no typed quantity", () => {
  const state = loadedState();
  LABELS.setDefaultCopies(state, 3);
  LABELS.selectAllFiltered(state, true);
  assert.equal(LABELS.labelCount(state), state.items.length * 3);
});

test("reloading a catalogue replaces rows instead of duplicating them", () => {
  const state = loadedState();
  const before = state.items.length;
  LABELS.addItems(state, MENU.map((row) => Object.assign({}, row, { source: "menu" })));
  assert.equal(state.items.length, before, "a product loaded twice is still one row");
});

test("selection is dropped for products that are no longer loaded", () => {
  const state = loadedState();
  const chapman = state.items.find((row) => row.name === "Chapman");
  LABELS.toggle(state, chapman, true);
  LABELS.setQuantity(state, chapman, 2);
  assert.equal(LABELS.labelCount(state), 2);

  LABELS.setItems(state, MENU.filter((row) => row.name !== "Chapman")
    .map((row) => Object.assign({}, row, { source: "menu" })));
  assert.equal(LABELS.labelCount(state), 0, "a stale key can never print a phantom label");
});

test("an empty selection renders the guidance state, not a blank sheet", () => {
  const state = loadedState();
  const markup = LABELS.sheetMarkup(state);
  assert.match(markup, /No labels selected/);
});

test("an empty search result explains what can be searched", () => {
  const state = loadedState();
  LABELS.setQuery(state, "zzzz");
  const markup = LABELS.resultsMarkup(state);
  assert.match(markup, /Nothing matches/);
  assert.match(markup, /name, SKU, barcode or ID/);
});

test("results markup exposes the hooks the page binds to", () => {
  const state = loadedState();
  const markup = LABELS.resultsMarkup(state);
  assert.match(markup, /data-label-row=/);
  assert.match(markup, /data-label-select=/);
  assert.match(markup, /data-label-qty=/);
});

test("summary counts describe what will print", () => {
  const state = loadedState();
  LABELS.setQuery(state, "rice");
  LABELS.selectAllFiltered(state, true);
  LABELS.setQuantity(state, LABELS.filtered(state)[0], 2);
  const info = LABELS.summary(state);
  assert.equal(info.matching, 2);
  assert.equal(info.total, 5);
  assert.equal(info.selected, 2);
  assert.equal(info.labels, 3);
  assert.equal(info.allViewSelected, true);
});
