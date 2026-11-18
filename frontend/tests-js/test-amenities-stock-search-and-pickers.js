/* tests-js/test-amenities-stock-search-and-pickers.js

   Reported defects this file locks down
   -------------------------------------
   · "Failed to load stock: The database is temporarily unavailable." — the
     stock table rendered an error banner for data that had loaded.
   · Stock items were not selectable and the store-location list never appeared.
   · Stock could not be searched.

   Root cause proven here: the paginated list endpoints answer
   `{success, message, data: [...], pagination}`. `API.get` strips the envelope,
   so the payload IS an array — but the page read `payload.results` first, which
   is `undefined` for an array. Every dropdown was therefore built from an empty
   list, and the stock table was rendered from `data.balances` after an
   undefined-tolerant read that also broke the location header.

   The page scripts run unmodified against a jsdom document and a stubbed API
   that returns the exact production payload shapes.

   Run: node --test tests-js/test-amenities-stock-search-and-pickers.js
*/
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { FRONTEND } = require("./harness");

let JSDOM = null;
try {
  ({ JSDOM } = require(path.join(FRONTEND, "node_modules", "jsdom")));
} catch (_) {
  JSDOM = null;
}
const SKIP = JSDOM ? false : "jsdom is not installed (npm i jsdom --no-save)";

const PAGE = `<!DOCTYPE html><html><body class="reception-services">
<button data-tab="stock"></button><button data-tab="allocate"></button>
<button data-tab="issue"></button><button data-tab="history"></button>
<section data-panel="stock">
  <select data-amenity-shelf-select></select>
  <input data-amenity-stock-search type="search">
  <div data-amenity-stock-container></div>
</section>
<section data-panel="allocate"><form data-amenity-allocate-form>
  <select name="item_id" data-amenity-item-select></select>
  <select name="from_location_id" data-amenity-location-select></select>
  <select name="location_id" data-amenity-department-location-select></select>
  <input name="quantity"><input name="notes"></form></section>
<section data-panel="issue"><form data-amenity-issue-form>
  <select name="guest_id"></select><select name="booking_id"></select>
  <select name="room_number"></select>
  <select name="item_id" data-amenity-issue-item-select></select>
  <select name="location_id" data-amenity-department-location-select></select>
  <input name="quantity"><input name="notes"></form></section>
<section data-panel="history"><div data-amenity-history-container></div></section>
<button data-amenity-refresh></button><button data-amenity-history-refresh></button>
</body></html>`;

/* Production payload shapes, captured from the API contract. */
const LOCATIONS = [
  { id: 1, code: "MAIN", name: "Main Store", is_active: true, is_reception: false },
  { id: 2, code: "RECEPTION", name: "Reception Store", is_active: true, is_reception: true },
];
const ITEMS = [
  { id: 10, sku: "AMN-SOAP", name: "Guest Soap", base_unit: "BAR", is_active: true },
  { id: 11, sku: "AMN-TISSUE", name: "Tissue Roll", base_unit: "ROLL", is_active: true },
  { id: 12, sku: "AMN-OLD", name: "Retired Item", base_unit: "PIECE", is_active: false },
];
/* The Allocate form reads the amenity-scoped source endpoint: the issuing
   stores, the catalogue and the on-hand quantity per store. The catalogue
   endpoints (inventory/locations, inventory/items) require `inventory.manage`
   and answer with a paginated ARRAY capped at 100 rows, which is why the
   allocate dropdowns used to be empty for anyone holding only the allocate
   capability, and why `page_size=500` silently truncated. */
const SOURCE = {
  reception_location: { id: 2, code: "RECEPTION", name: "Reception Store", is_reception: true },
  locations: [
    { id: 1, code: "MAIN", name: "Main Store", is_active: true, is_reception: false },
  ],
  items: [
    { id: 10, sku: "AMN-SOAP", name: "Guest Soap", category: "Amenities", base_unit: "BAR" },
    { id: 11, sku: "AMN-TISSUE", name: "Tissue Roll", category: "Amenities", base_unit: "ROLL" },
    { id: 12, sku: "AMN-OLD", name: "Retired Item", category: "Amenities", base_unit: "PIECE" },
  ],
  stock: [
    { item_id: 10, sku: "AMN-SOAP", name: "Guest Soap", category: "Amenities", base_unit: "BAR", location_id: 1, location_code: "MAIN", location_name: "Main Store", quantity: "40.0000" },
    { item_id: 11, sku: "AMN-TISSUE", name: "Tissue Roll", category: "Amenities", base_unit: "ROLL", location_id: 1, location_code: "MAIN", location_name: "Main Store", quantity: "12.0000" },
  ],
};

const STOCK = {
  location: { id: 2, code: "RECEPTION", name: "Reception Store", is_reception: true },
  balances: [
    { item_id: 10, sku: "AMN-SOAP", name: "Guest Soap", base_unit: "BAR", quantity: "12.0000", is_active: true, low_stock: false, reorder_level: "2.0000" },
    { item_id: 11, sku: "AMN-TISSUE", name: "Tissue Roll", base_unit: "ROLL", quantity: "3.0000", is_active: true, low_stock: true, reorder_level: "5.0000" },
    { item_id: 13, sku: "AMN-ZERO", name: "Hand Towel", base_unit: "PIECE", quantity: "0.0000", is_active: true, low_stock: false, reorder_level: "1.0000" },
  ],
};

function boot(opts = {}) {
  const dom = new JSDOM(PAGE, {
    url: "https://staff.jonehotels.com/dashboard/amenities.html",
    runScripts: "outside-only",
  });
  const { window } = dom;
  const calls = [];
  const posted = [];
  window.API = {
    get: async (url) => {
      calls.push(url);
      let data = [];
      if (url.indexOf("/inventory/locations/") !== -1) data = LOCATIONS;
      else if (url.indexOf("/inventory/items/") !== -1) data = ITEMS;
      else if (url.indexOf("amenities/source-stock/") !== -1) data = opts.source || SOURCE;
      else if (url.indexOf("amenities/stock/") !== -1) data = opts.stock || STOCK;
      else if (url.indexOf("amenities/issues/") !== -1) data = { results: [] };
      else if (url.indexOf("reception/guests/") !== -1) data = { results: [] };
      return { status: 200, ok: true, data };
    },
    post: async (url, body) => {
      posted.push([url, body]);
      return { status: 200, ok: true, data: { created: true } };
    },
  };
  window.JONE = {
    ui: { toast: () => {}, confirm: () => Promise.resolve(true) },
    esc: (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])),
  };
  window.Auth = { state: { user: { role: "ADMIN" } } };
  window.eval(fs.readFileSync(path.join(FRONTEND, "js/reception-services.js"), "utf8"));
  window.eval(fs.readFileSync(path.join(FRONTEND, "js/amenities.js"), "utf8"));
  return { window, doc: window.document, calls, posted };
}

const settle = (ms = 300) => new Promise((resolve) => setTimeout(resolve, ms));

test("the stock table renders rows instead of an error banner", { skip: SKIP }, async () => {
  const { doc } = boot();
  await settle();
  const box = doc.querySelector("[data-amenity-stock-container]");
  assert.ok(!/Failed to load stock/i.test(box.innerHTML), "no failure banner: " + box.innerHTML);
  assert.ok(box.innerHTML.indexOf("Guest Soap") !== -1, "stock row rendered");
  assert.ok(box.innerHTML.indexOf("Reception Store") !== -1, "location header rendered");
  /* Zero-quantity rows are still listed (they are real shelf records) but must
     be marked unavailable, not offered as issuable stock. */
  assert.ok(box.innerHTML.indexOf("Unavailable") !== -1, "zero stock marked unavailable");
});

test("the allocate item dropdown is populated from the amenity source endpoint", { skip: SKIP }, async () => {
  const { doc } = boot();
  await settle();
  const select = doc.querySelector("[data-amenity-item-select]");
  const labels = Array.from(select.children).map((o) => o.textContent);
  assert.ok(labels.some((t) => t.indexOf("Guest Soap") !== -1), "in-stock item offered: " + labels.join(" | "));
  assert.ok(labels.some((t) => t.indexOf("40 BAR in stock") !== -1), "quantity on hand is stated");
  assert.ok(
    !Array.from(select.children).some((o) => o.textContent.indexOf("Retired Item") !== -1),
    "an item no store holds is not offered as allocatable stock"
  );
});

test("the store-location dropdowns list real issuing stores and exclude the Reception shelf", { skip: SKIP }, async () => {
  const { doc } = boot();
  await settle();
  const from = doc.querySelector("[data-amenity-location-select]");
  const shelf = doc.querySelector("[data-amenity-shelf-select]");
  assert.equal(from.children.length, 2, "placeholder + MAIN only");
  assert.equal(from.children[1].value, "1");
  assert.ok(from.children[1].textContent.indexOf("MAIN") !== -1);
  assert.equal(shelf.children.length, 3, "placeholder + both locations");
  const shelfLabels = Array.from(shelf.children).map((o) => o.textContent).join(" | ");
  assert.ok(shelfLabels.indexOf("(configured)") !== -1, "configured Reception store flagged");
});

test("only items with stock on the shelf are issuable", { skip: SKIP }, async () => {
  const { doc } = boot();
  await settle();
  const select = doc.querySelector("[data-amenity-issue-item-select]");
  assert.equal(select.children.length, 3, "placeholder + two items with quantity > 0");
  const labels = Array.from(select.children).map((o) => o.textContent).join(" | ");
  assert.ok(labels.indexOf("Guest Soap") !== -1);
  assert.ok(labels.indexOf("Hand Towel") === -1, "zero-quantity item not offered");
});

test("stock search filters by name, SKU and is whitespace/case tolerant", { skip: SKIP }, async () => {
  const { doc, window } = boot();
  await settle();
  const search = doc.querySelector("[data-amenity-stock-search]");
  const box = doc.querySelector("[data-amenity-stock-container]");

  search.value = "  towel  ";
  search.dispatchEvent(new window.Event("input"));
  await settle(300);
  assert.ok(box.innerHTML.indexOf("Hand Towel") !== -1, "lowercase substring, padded search matches");
  assert.ok(box.innerHTML.indexOf("Guest Soap") === -1, "non-matching rows removed");

  search.value = "amn-soap";
  search.dispatchEvent(new window.Event("input"));
  await settle(300);
  assert.ok(box.innerHTML.indexOf("Guest Soap") !== -1, "SKU search works");
  assert.ok(box.innerHTML.indexOf("Tissue Roll") === -1);

  search.value = "zzz-nothing";
  search.dispatchEvent(new window.Event("input"));
  await settle(300);
  assert.ok(/No stock items match/.test(box.innerHTML), "explicit empty state");
});

test("search is debounced and cleared without a page reload", { skip: SKIP }, async () => {
  const { doc, window, calls } = boot();
  await settle();
  calls.length = 0;
  const search = doc.querySelector("[data-amenity-stock-search]");
  const box = doc.querySelector("[data-amenity-stock-container]");

  for (const ch of "soap".split("")) {
    search.value += ch;
    search.dispatchEvent(new window.Event("input"));
    await settle(30);
  }
  await settle(400);
  assert.equal(calls.length, 0, "client-side filtering issues no API traffic");

  search.value = "zzzz";
  search.dispatchEvent(new window.Event("input"));
  await settle(300);
  const clear = box.querySelector("[data-amenity-stock-clear]");
  assert.ok(clear, "clear control offered in the empty state");
  clear.click();
  await settle(100);
  assert.equal(search.value, "", "input cleared");
  assert.ok(box.innerHTML.indexOf("Guest Soap") !== -1, "full list restored");
});

test("an unconfigured department store explains itself instead of failing", { skip: SKIP }, async () => {
  const { doc } = boot({ stock: { location: null, balances: [] } });
  await settle();
  const box = doc.querySelector("[data-amenity-stock-container]");
  assert.ok(/No department store is configured/i.test(box.innerHTML), box.innerHTML);
  assert.ok(!/Failed to load stock/i.test(box.innerHTML));
});

test("issuing an amenity posts the selected item, quantity and shelf", { skip: SKIP }, async () => {
  const { doc, window, posted } = boot();
  await settle();
  const form = doc.querySelector("[data-amenity-issue-form]");
  form.elements.guest_id.innerHTML = '<option value="7">Ada Obi</option>';
  form.elements.guest_id.value = "7";
  const itemSelect = doc.querySelector("[data-amenity-issue-item-select]");
  itemSelect.value = "10";
  form.elements.quantity.value = "2";
  form.elements.notes.value = "";
  form.elements.room_number.value = "204";
  const shelf = doc.querySelector("[data-amenity-department-location-select]");
  if (shelf) shelf.value = "2";
  form.dispatchEvent(new window.Event("submit", { cancelable: true, bubbles: true }));
  await settle(200);
  const call = posted.find((entry) => entry[0].indexOf("amenities/issue/") !== -1);
  assert.ok(call, "issue endpoint called");
  assert.equal(call[1].item_id, 10);
  assert.equal(call[1].guest_id, 7);
  assert.equal(call[1].quantity, "2");
  assert.ok(call[1].idempotency_key, "an idempotency key accompanies the write");
});
