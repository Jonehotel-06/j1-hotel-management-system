/* Focused evidence for the critical-repair browser surfaces added after the
   baseline. These checks exercise the real dashboard HTML and the real page
   scripts; no production data or network calls are used. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("jsdom");

const root = path.resolve(__dirname, "..");
const read = (relative) => fs.readFileSync(path.join(root, relative), "utf8");
const html = (name) => read(`dashboard/${name}.html`);
const js = (name) => read(`js/${name}.js`);

test("inventory exposes configurable units, manual stock-out and kitchen issues", () => {
  const inventory = html("inventory");
  for (const marker of [
    'data-inv-tab="units"', 'data-inv-panel="units"', 'data-inv-new-unit',
    'data-inv-tab="manual-stock-out"', 'data-inv-panel="manual-stock-out"', 'data-inv-new-stock-out',
    'data-inv-tab="kitchen-issues"', 'data-inv-panel="kitchen-issues"', 'data-inv-new-kitchen-issue',
    'data-inv-stock-out-rows', 'data-inv-kitchen-issue-rows', 'data-inv-unit-rows',
  ]) assert.match(inventory, new RegExp(marker.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
  assert.match(inventory, /API\.list\(\"inventoryUnits\"/);
  assert.match(inventory, /endpoint:\"inventoryManualStockOut\"/);
  assert.match(inventory, /endpoint:\"inventoryKitchenIssues\"/);
  assert.match(inventory, /API\.list\(cfg\.endpoint/);
  assert.match(inventory, /API\.create\(\"inventoryUnits\"/);
  assert.match(inventory, /is_reception/);
  assert.match(inventory, /Configured Reception store/);
  assert.match(inventory, /DEFAULT_STOCK_UNITS/);
  assert.match(inventory, /configuredStockUnits\.length\?configuredStockUnits/);
});

test("endpoint map owns every new browser-facing resource", () => {
  const config = js("config");
  for (const key of [
    "inventoryUnits", "inventoryManualStockOut", "inventoryKitchenIssues",
    "minimartCategories", "minimartAdminProducts",
  ]) assert.match(config, new RegExp(`${key}\\s*:`));
});

test("Mini Mart catalogue management boots against the real admin endpoint map", async () => {
  const dom = new JSDOM(html("minimart"), {
    url: "https://hotel.test/dashboard/minimart.html",
    runScripts: "outside-only",
  });
  const w = dom.window;
  const calls = [];
  w.JONE = {
    esc: (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])),
    ui: { toast() {} },
  };
  w.Auth = { state: { user: { role: "ADMIN" } } };
  const envelope = (data) => ({ success: true, data });
  const rows = {
    inventoryUnits: [{ id: 1, code: "BOTTLE", label: "Bottle", is_active: true }],
    inventoryLocations: [{ id: 7, code: "MAIN", name: "Main Store", is_active: true }],
    minimartCategories: [{ id: 3, name: "Drinks", description: "Cold", is_active: true, product_count: 1 }],
    minimartAdminProducts: [{ id: 9, name: "Test Water", sku: "TW", selling_price: "150.00", status: "ACTIVE", is_active: true }],
  };
  w.API = {
    list: async (name, params) => {
      calls.push({ method: "list", name, params });
      return envelope({ results: rows[name] || [], pagination: null });
    },
    create: async (name, data) => {
      calls.push({ method: "create", name, data });
      return envelope({ id: 99, ...data });
    },
    update: async (name, id, data) => {
      calls.push({ method: "update", name, id, data });
      return envelope({ id, ...data });
    },
  };
  try {
    w.eval(js("minimart-admin"));
    await new Promise((resolve) => setTimeout(resolve, 30));
    const doc = w.document;
    assert.equal(doc.querySelector("[data-mart-admin]").hidden, false);
    assert.match(doc.querySelector("#mart-admin-base-unit").innerHTML, /BOTTLE/);
    assert.match(doc.querySelector("#mart-admin-location").innerHTML, /MAIN/);
    assert.match(doc.querySelector("[data-mart-admin-categories]").textContent, /Drinks/);
    assert.match(doc.querySelector("[data-mart-admin-products]").textContent, /Test Water/);
    assert.ok(calls.some((c) => c.name === "minimartCategories" && c.method === "list"));
    assert.ok(calls.some((c) => c.name === "minimartAdminProducts" && c.method === "list"));

    const form = doc.querySelector("[data-mart-admin-category-form]");
    form.elements.name.value = "Snacks";
    form.elements.description.value = "Shelf-stable";
    form.dispatchEvent(new w.Event("submit", { bubbles: true, cancelable: true }));
    await new Promise((resolve) => setTimeout(resolve, 30));
    const created = calls.find((c) => c.method === "create" && c.name === "minimartCategories");
    assert.ok(created, "category form must call the configured create endpoint");
    assert.equal(created.data.name, "Snacks");
    assert.equal(created.data.is_active, true);
  } finally {
    w.close();
  }
});

test("reception catalogue search is server-side and exposes active categories", () => {
  const script = js("minimart");
  assert.match(script, /query\.set\('search',term\)/);
  assert.match(script, /query\.set\(String\(category\)\.match\(\/\^\\d\+\$\/\)\?'category_id':'category',category\)/);
  assert.match(script, /data\.categories/);
  assert.match(script, /p\.description \|\| ''/);
  assert.doesNotMatch(script, /products\.filter\(p=>.*category.*term\)\s*;\s*$/m);
});

test("amenities and breakfast use the configured department store and optional stock ledger", () => {
  const amenitiesHtml = html("amenities"), amenitiesJs = js("amenities");
  assert.match(amenitiesHtml, /data-amenity-shelf-select/);
  assert.match(amenitiesHtml, /data-amenity-department-location-select/);
  assert.match(amenitiesJs, /loc\.is_reception/);
  assert.match(amenitiesJs, /withLocation\("\/api\/admin\/service-requests\/reception\/amenities\/stock\/"\)/);
  assert.match(amenitiesJs, /payload\.location_id = parseInt\(locationId\)/);
  assert.match(amenitiesJs, /Use configured Reception store/);

  const breakfastHtml = html("breakfast"), breakfastJs = js("breakfast");
  for (const marker of [
    "data-breakfast-stock-item", "data-breakfast-stock-location", "data-breakfast-stock-unit",
    "name=\"stock_quantity\"", "name=\"stock_conversion_factor\"",
  ]) assert.match(breakfastHtml, new RegExp(marker.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
  assert.match(breakfastJs, /API\.list\('inventoryItems'/);
  assert.match(breakfastJs, /payload\.items=\[line\]/);
  assert.match(breakfastJs, /payload\.location_id=loc/);
  assert.match(breakfastJs, /stock_movement_count/);
  assert.match(breakfastHtml, /no POS order, folio charge or second revenue\/expense posting/);
});
