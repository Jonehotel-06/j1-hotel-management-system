/* Inventory and procurement dashboard contract tests.
   Run from /frontend: node --test tests-js/test-inventory-ui.js */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { makeContext, loadScript, FRONTEND, disposeAll } = require("./harness");

test.after(() => disposeAll());

function response(data) {
  return Promise.resolve({
    status: 200, ok: true,
    headers: { get: (key) => key === "content-type" ? "application/json" : null },
    json: () => Promise.resolve({ success: true, data }),
    text: () => Promise.resolve(JSON.stringify({ success: true, data })),
  });
}

test("inventory and procurement endpoints are centralized and controlled actions retain references", async () => {
  const ctx = makeContext();
  loadScript(ctx, "js/config.js");
  const endpoints = ctx.window.APP_CONFIG.API_ENDPOINTS;
  assert.equal(endpoints.inventoryMenuItems, "/api/admin/inventory/menu-items/");
  assert.equal(endpoints.inventoryBalances, "/api/admin/inventory/balances/");
  assert.equal(endpoints.inventoryRecipes, "/api/admin/inventory/recipes/");
  assert.equal(endpoints.stockCounts, "/api/admin/inventory/stock-counts/");
  assert.equal(endpoints.suppliers, "/api/admin/inventory/suppliers/");
  assert.equal(endpoints.purchaseOrders, "/api/admin/inventory/purchase-orders/");

  const requests = [];
  ctx.fetch = (url, init) => { requests.push({ url, init }); return response({}); };
  loadScript(ctx, "js/api.js");
  await ctx.window.API.resourceAction("purchaseOrders", "PO/TEST", "receipts", { idempotency_key: "receipt-key" });
  await ctx.window.API.resourceAction("stockCounts", "CNT/TEST", "approve", {});
  assert.equal(requests[0].url, "/api/admin/inventory/purchase-orders/PO%2FTEST/receipts/");
  assert.deepEqual(JSON.parse(requests[0].init.body), { idempotency_key: "receipt-key" });
  assert.equal(requests[1].url, "/api/admin/inventory/stock-counts/CNT%2FTEST/approve/");
});

test("operations helpers expose bounded multi-select and line builders for authoritative inventory IDs", () => {
  const ctx = makeContext();
  loadScript(ctx, "js/config.js");
  loadScript(ctx, "js/utils.js");
  loadScript(ctx, "js/operations.js");
  const ops = ctx.window.JONE.operations;

  const multi = ops.multiSearchPicker({ name: "item_ids", label: "stock item", resource: "inventoryItems", required: true });
  assert.equal(multi.type, "custom");
  assert.equal(multi.validate(), "Select at least one stock item.");
  const lines = ops.linePicker({ name: "lines", label: "purchase-order line", resource: "inventoryItems", required: true,
    lineFields: [{ name: "quantity_ordered", required: true, min: "0.0001" }] });
  assert.equal(lines.type, "custom");
  assert.equal(lines.validate(), "Add at least one purchase-order line.");
  assert.equal(JSON.stringify(lines.getValue()), "{}");
});

test("inventory views use bounded pagination, skeleton/error states, idempotency keys, and no polling", () => {
  const inventory = fs.readFileSync(path.join(FRONTEND, "dashboard", "inventory.html"), "utf8");
  const procurement = fs.readFileSync(path.join(FRONTEND, "dashboard", "procurement.html"), "utf8");
  [inventory, procurement].forEach((page) => {
    assert.match(page, /operations\.js/);
    assert.match(page, /DATA\.loading/);
    assert.match(page, /DATA\.error/);
    assert.match(page, /page_size:JONE\.dashboard\.pageSize\(\)/);
    assert.doesNotMatch(page, /setInterval\s*\(/);
  });
  assert.match(inventory, /__inventoryAllowed/);
  assert.match(inventory, /API\.list\("inventoryBalances"/);
  assert.match(inventory, /API\.list\("stockCounts"/);
  assert.match(inventory, /API\.list\("inventoryConsumptionRequests"/);
  assert.match(inventory, /API\.list\("inventoryRecipes"/);
  assert.match(inventory, /API\.list\("inventoryMovements"/);
  assert.match(inventory, /API\.update\("inventoryItems"/);
  assert.match(inventory, /idempotency_key=OPS\.newIdempotencyKey\("stock-issue"\)/);
  assert.match(inventory, /idempotency_key=OPS\.newIdempotencyKey\("recipe"\)/);
  assert.match(procurement, /__procurementAllowed/);
  assert.match(procurement, /API\.list\("purchaseOrders"/);
  assert.match(procurement, /API\.list\("suppliers"/);
  assert.match(procurement, /idempotency_key=OPS\.newIdempotencyKey\("purchase-order"\)/);
  assert.match(procurement, /idempotency_key=OPS\.newIdempotencyKey\("goods-receipt"\)/);
});

test("navigation exposes stock and purchasing only to the inventory operational roles", () => {
  const nav = fs.readFileSync(path.join(FRONTEND, "js", "dashboard.js"), "utf8");
  assert.match(nav, /Inventory[\s\S]*capabilities: \["inventory\.manage", "inventory\.adjust\.approve"\]/);
  assert.match(nav, /Procurement[\s\S]*capabilities: \["procurement\.manage", "procurement\.approve"\]/);
  assert.match(nav, /storekeeper[\s\S]*procurement_officer/);
});
