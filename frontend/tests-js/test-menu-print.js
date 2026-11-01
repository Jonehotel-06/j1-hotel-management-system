/* tests-js/test-menu-print.js
 *
 * Task 02 of targeted_fixes_01 — searchable / selective MENU-ITEM printing.
 *
 * These tests pin the three things the task actually asked for:
 *   1. search by item name, SKU/barcode and category;
 *   2. a SEPARATE workflow from barcode label printing (different page,
 *      different module, different output — and the label page is untouched);
 *   3. no side effects on pricing, stock or categories: the page is read-only
 *      and the module has no write path at all.
 *
 * Run from /frontend: node --test tests-js/test-menu-print.js
 */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const FRONTEND = path.resolve(__dirname, "..");
const read = (rel) => fs.readFileSync(path.join(FRONTEND, rel), "utf8");

function loadMenuPrint() {
  const sandbox = { window: { JONE: { esc: (v) => String(v == null ? "" : v) } }, console };
  vm.createContext(sandbox);
  vm.runInContext(read("js/menu-print.js"), sandbox, { filename: "menu-print.js" });
  return sandbox.window.JONE.menuPrint;
}

const PRINT = loadMenuPrint();

const ITEMS = [
  { id: 1, name: "Jollof Rice", sku: "MNU-001", barcode: "", category_name: "Rice Dishes", base_price: "2500.00", currency: "NGN", service_area: "RESTAURANT" },
  { id: 2, name: "Grilled Chicken", sku: "MNU-002", barcode: "6001111111111", category_name: "Proteins", base_price: "4500.00", currency: "NGN", service_area: "RESTAURANT" },
  { id: 3, name: "Chapman", sku: "BAR-001", barcode: "6009999999999", category_name: "Cocktails", base_price: "3000.00", currency: "NGN", service_area: "BAR" },
];

function stateWith(items) {
  const state = PRINT.createState();
  PRINT.setItems(state, items || ITEMS);
  return state;
}

test("search finds a menu item by name, SKU, barcode and category", () => {
  const state = stateWith();

  PRINT.setQuery(state, "grilled");
  assert.deepEqual([...PRINT.filtered(state).map((row) => row.name)], ["Grilled Chicken"], "name");

  PRINT.setQuery(state, "bar-001");
  assert.deepEqual([...PRINT.filtered(state).map((row) => row.name)], ["Chapman"], "SKU (case-insensitive)");

  PRINT.setQuery(state, "6001111111111");
  assert.deepEqual([...PRINT.filtered(state).map((row) => row.name)], ["Grilled Chicken"], "barcode");

  PRINT.setQuery(state, "cocktails");
  assert.deepEqual([...PRINT.filtered(state).map((row) => row.name)], ["Chapman"], "category");

  PRINT.setQuery(state, "");
  assert.equal(PRINT.filtered(state).length, 3, "empty search shows everything");
});

test("the category filter narrows results and composes with the search", () => {
  const state = stateWith();
  PRINT.setCategory(state, "Cocktails");
  assert.deepEqual([...PRINT.filtered(state).map((row) => row.name)], ["Chapman"]);

  PRINT.setQuery(state, "rice");
  assert.equal(PRINT.filtered(state).length, 0, "no rice dish is in Cocktails");

  PRINT.setCategory(state, "");
  assert.equal(PRINT.filtered(state).length, 1, "search still applies after clearing the category");

  PRINT.setQuery(state, "");
  assert.equal(PRINT.filtered(state).length, 3);
});

test("categories are derived from the loaded menu, never hardcoded", () => {
  const state = stateWith();
  assert.deepEqual([...PRINT.categories(state)], ["Cocktails", "Proteins", "Rice Dishes"]);
  PRINT.setItems(state, [{ id: 9, name: "Egusi", category_name: "Soups" }]);
  assert.deepEqual([...PRINT.categories(state)], ["Soups"]);
});

test("only selected items print", () => {
  const state = stateWith();
  PRINT.toggle(state, state.items[0], true);
  PRINT.toggle(state, state.items[2], true);
  const sheet = PRINT.sheetMarkup(state);
  assert.match(sheet, /Jollof Rice/);
  assert.match(sheet, /Chapman/);
  assert.doesNotMatch(sheet, /Grilled Chicken/);
  assert.equal(PRINT.summary(state).selected, 2);
});

test("select-all applies to the filtered list only", () => {
  const state = stateWith();
  PRINT.setCategory(state, "Cocktails");
  PRINT.selectAllFiltered(state, true);
  assert.equal(PRINT.selectedItems(state).length, 1);
  assert.equal(PRINT.selectedItems(state)[0].name, "Chapman");
  PRINT.selectAllFiltered(state, false);
  assert.equal(PRINT.selectedItems(state).length, 0);
});

test("print columns are selectable and the price is shown from the record", () => {
  const state = stateWith();
  PRINT.toggle(state, state.items[0], true);

  PRINT.setColumn(state, "price", false);
  let card = PRINT.cardMarkup(state, state.items[0]);
  assert.doesNotMatch(card, /Price/, "price column can be switched off");

  PRINT.setColumn(state, "price", true);
  card = PRINT.cardMarkup(state, state.items[0]);
  assert.match(card, /Price/);
  assert.match(card, /2,500\.00|2500\.00/, "the real price appears, formatted");

  PRINT.setColumn(state, "code", false);
  card = PRINT.cardMarkup(state, state.items[0]);
  assert.doesNotMatch(card, /SKU MNU-001/);
});

test("an item with no SKU or barcode prints a dash, never a fabricated code", () => {
  const state = stateWith([{ id: 7, name: "Water", category_name: "Drinks", base_price: "500.00" }]);
  PRINT.toggle(state, state.items[0], true);
  const card = PRINT.cardMarkup(state, state.items[0]);
  assert.match(card, /<dt>Code<\/dt><dd>—<\/dd>/);
  assert.doesNotMatch(card, /SKU /);
});

test("the SKU is shown on its own, and the barcode separately when they differ", () => {
  const state = stateWith();
  const chicken = state.items[1];
  const card = PRINT.cardMarkup(state, chicken);
  assert.match(card, /SKU MNU-002/);
  assert.match(card, /Barcode 6001111111111/);
});

test("an empty selection renders guidance, not a blank printout", () => {
  const state = stateWith();
  assert.match(PRINT.sheetMarkup(state), /Nothing selected/);
});

test("a dead search result explains what can be searched", () => {
  const state = stateWith();
  PRINT.setQuery(state, "zzzz");
  const markup = PRINT.resultsMarkup(state);
  assert.match(markup, /Nothing matches/);
  assert.match(markup, /name, SKU, barcode or category/);
});

test("one-per-page changes the print layout class", () => {
  const state = stateWith();
  PRINT.toggle(state, state.items[0], true);
  assert.doesNotMatch(PRINT.sheetMarkup(state), /is-one-per-page/);
  state.onePerPage = true;
  assert.match(PRINT.sheetMarkup(state), /is-one-per-page/);
});

test("selection is dropped when the menu reloads without that item", () => {
  const state = stateWith();
  PRINT.toggle(state, state.items[2], true);
  assert.equal(PRINT.summary(state).selected, 1);
  PRINT.setItems(state, ITEMS.slice(0, 2));
  assert.equal(PRINT.summary(state).selected, 0, "a stale selection cannot print");
});

/* ----------------------------------------------------------------------- *
 * Separation from task 01 and freedom from side effects
 * ----------------------------------------------------------------------- */

test("menu printing is a different page, module and output from barcode labels", () => {
  const labelsPage = read("dashboard/barcode-labels.html");
  const menuPage = read("dashboard/menu-print.html");

  assert.ok(fs.existsSync(path.join(FRONTEND, "js/menu-print.js")));
  assert.ok(fs.existsSync(path.join(FRONTEND, "js/barcode-labels.js")));

  /* Each page loads its OWN module and not the other one. */
  assert.match(labelsPage, /js\/barcode-labels\.js/);
  assert.doesNotMatch(labelsPage, /js\/menu-print\.js/);
  assert.match(menuPage, /js\/menu-print\.js/);
  assert.doesNotMatch(menuPage, /js\/barcode-labels\.js/);

  /* The menu sheet does not render barcode symbology at all. */
  const model = read("js/menu-print.js");
  assert.doesNotMatch(model, /generateBarcodeSvg/, "menu sheets are not label printers");
});

test("the menu printing workflow has no write path", () => {
  const model = read("js/menu-print.js");
  for (const verb of ["POST", "PUT", "PATCH", "DELETE"]) {
    assert.doesNotMatch(model, new RegExp(`["'\`]${verb}["'\`]`), `menu printing must not ${verb}`);
  }
  assert.doesNotMatch(model, /API\.(create|update|remove|delete|patch)/, "no mutation calls");
  assert.doesNotMatch(model, /price\s*=|base_price\s*=/, "never assigns a price");
});

test("the page declares itself read-only about pricing, stock and POS", () => {
  const menuPage = read("dashboard/menu-print.html");
  assert.match(menuPage, /never changes a price, a stock level or a POS setting/);
  assert.match(menuPage, /To print barcode <em>labels<\/em>, use the Barcode Label Printing screen/);
});

test("the screen is registered in navigation and page access", () => {
  const dashboard = read("js/dashboard.js");
  assert.match(dashboard, /"menu-print\.html": \{ roles: \[[^\]]*"waiter"[^\]]*"chef"/);
  assert.match(dashboard, /href: "menu-print\.html"/);
});
