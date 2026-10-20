/* Guest portal catalogue helpers: money display, filtering, search and order mapping. */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { makeContext, loadScript, FRONTEND, disposeAll } = require("./harness");

test.after(() => disposeAll());

// Values built inside the vm context are a different realm; compare plain copies.
function same(actual, expected) {
  assert.deepEqual(JSON.parse(JSON.stringify(actual)), expected);
}

function loadCatalog() {
  const ctx = makeContext();
  loadScript(ctx, "js/config.js");
  loadScript(ctx, "js/portal.js");
  loadScript(ctx, "js/portal-catalog.js");
  return ctx.window.JONE.portalCatalog;
}

const MENU = [
  {
    id: 1, name: "Mains", service_area: "RESTAURANT",
    items: [
      { id: 10, name: "Jollof rice", description: "Smoky party jollof", price: "3500.00", is_available: true, order_category: "ROOM_SERVICE" },
      { id: 11, name: "Fried plantain", description: "Sweet", price: "1200.00", is_available: false, order_category: "ROOM_SERVICE" },
    ],
  },
  {
    id: 2, name: "Cocktails", service_area: "BAR",
    items: [{ id: 20, name: "Chapman", description: "Citrus soda", price: "2500.00", is_available: true, order_category: "FOOD_BEVERAGE" }],
  },
  { id: 3, name: "Empty", service_area: "BAR", items: [] },
];

test("naira display is deterministic and never invents a price", () => {
  const c = loadCatalog();
  assert.equal(c.formatNaira("3500.00"), "\u20A63,500.00");
  assert.equal(c.formatNaira("1234567.5"), "\u20A61,234,567.50");
  assert.equal(c.formatNaira(0), "\u20A60.00");
  assert.equal(c.formatNaira(null), "Price on request");
  assert.equal(c.formatNaira(""), "Price on request");
  assert.equal(c.formatNaira("abc"), "Price on request");
});

test("menu filters by area, category and name/description search, dropping empty groups", () => {
  const c = loadCatalog();
  const all = c.filterMenu(MENU, { area: "ALL", categoryId: "ALL", query: "" });
  same(all.map((g) => g.name), ["Mains", "Cocktails"]);
  same(c.filterMenu(MENU, { area: "BAR", categoryId: "ALL", query: "" }).map((g) => g.name), ["Cocktails"]);
  same(c.filterMenu(MENU, { area: "ALL", categoryId: "1", query: "" })[0].items.map((i) => i.name),
    ["Jollof rice", "Fried plantain"]);
  same(c.filterMenu(MENU, { area: "ALL", categoryId: "ALL", query: "  CITRUS  " })[0].items.map((i) => i.name),
    ["Chapman"]);
  same(c.filterMenu(MENU, { area: "ALL", categoryId: "ALL", query: "nothing-matches" }), []);
});

test("unavailable items keep their label and no order action is implied", () => {
  const c = loadCatalog();
  const plantain = MENU[0].items[1];
  same(c.availabilityBadge(plantain), { label: "Currently unavailable", unavailable: true });
  same(c.availabilityBadge(MENU[0].items[0]), { label: "Available", unavailable: false });
});

test("ordering routes to the existing request flow with the server's department category", () => {
  const c = loadCatalog();
  same(c.orderFor(MENU[0].items[0], MENU[0]), { category: "ROOM_SERVICE", summary: "Order: Jollof rice" });
  assert.equal(c.orderFor(MENU[1].items[0], MENU[1]).category, "FOOD_BEVERAGE");
  // Without a server-provided category the restaurant/bar area still decides the route.
  assert.equal(c.orderFor({ name: "X" }, { service_area: "BAR" }).category, "FOOD_BEVERAGE");
  assert.equal(c.orderFor({ name: "X" }, { service_area: "RESTAURANT" }).category, "ROOM_SERVICE");
  assert.ok(c.orderFor({ name: "A".repeat(400) }, MENU[0]).summary.length <= 255);
});

test("service actions: requests open the request flow, facilities only show details", () => {
  const c = loadCatalog();
  same(c.serviceAction({ kind: "request", category: "HOUSEKEEPING" }),
    { kind: "request", label: "Request Service", category: "HOUSEKEEPING" });
  same(c.serviceAction({ kind: "facility" }), { kind: "details", label: "View Details", category: null });
});

test("service search matches name and description", () => {
  const c = loadCatalog();
  const services = [{ name: "Swimming pool", description: "Open 7am" }, { name: "Laundry", description: "Same day" }];
  same(c.filterServices(services, "pool").map((s) => s.name), ["Swimming pool"]);
  same(c.filterServices(services, "same day").map((s) => s.name), ["Laundry"]);
  assert.equal(c.filterServices(services, "").length, 2);
});

test("category options follow the selected area", () => {
  const c = loadCatalog();
  same(c.categoryOptions(MENU, "BAR").map((o) => o.label), ["Cocktails", "Empty"]);
  assert.equal(c.categoryOptions(MENU, "ALL").length, 3);
});

test("the catalogue module keeps the portal's no-storage, no-hardcoded-URL rules", () => {
  const source = fs.readFileSync(path.join(FRONTEND, "js", "portal-catalog.js"), "utf8");
  assert.doesNotMatch(source, /localStorage|sessionStorage|document\.cookie/);
  assert.doesNotMatch(source, /\/api\//);
  assert.doesNotMatch(source, /localhost|127\.0\.0\.1|vercel\.app|railway\.app/);
  assert.doesNotMatch(source, /innerHTML\s*=\s*[^;]*\$\{/);
});
