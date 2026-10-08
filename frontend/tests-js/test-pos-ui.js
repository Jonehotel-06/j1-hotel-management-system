/* POS / room-service frontend contract tests.
   Run from /frontend: node --test tests-js/test-pos-ui.js */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { makeContext, loadScript, FRONTEND, disposeAll } = require("./harness");

test.after(() => disposeAll());

function jsonResponse(data) {
  return Promise.resolve({
    status: 200,
    ok: true,
    headers: { get: (key) => key === "content-type" ? "application/json" : null },
    json: () => Promise.resolve({ success: true, data }),
    text: () => Promise.resolve(JSON.stringify({ success: true, data })),
  });
}

function bootApi(requests) {
  const ctx = makeContext();
  loadScript(ctx, "js/config.js");
  ctx.fetch = (url, init) => {
    requests.push({ url, init });
    return jsonResponse({ reference: "POS-TEST" });
  };
  loadScript(ctx, "js/api.js");
  return ctx;
}

test("POS endpoints stay centralized in deployment-neutral configuration", () => {
  const ctx = makeContext();
  loadScript(ctx, "js/config.js");
  const endpoints = ctx.window.APP_CONFIG.API_ENDPOINTS;

  assert.equal(endpoints.posMenu, "/api/admin/pos/menu/");
  assert.equal(endpoints.posRoomServiceStays, "/api/admin/pos/room-service-stays/");
  assert.equal(endpoints.posOrders, "/api/admin/pos/orders/");
  assert.equal(endpoints.posKitchenTickets, "/api/admin/pos/kitchen-tickets/");
  assert.equal(endpoints.posCashSessions, "/api/admin/pos/cash-sessions/");
});

test("POS action helpers submit only server-authoritative workflow payloads", async () => {
  const requests = [];
  const ctx = bootApi(requests);

  await ctx.window.API.submitPosOrder("POS/REF", {});
  await ctx.window.API.updatePosOrderStatus("POS/REF", "READY");
  await ctx.window.API.capturePosTender("POS/REF", { method: "CARD", amount: "2500.00" });
  await ctx.window.API.openPosCashSession({ opening_float: "0.00" });
  await ctx.window.API.closePosCashSession("CS/REF", { counted_cash: "1000.00" });

  assert.deepEqual(requests.map((request) => request.url), [
    "/api/admin/pos/orders/POS%2FREF/submit/",
    "/api/admin/pos/orders/POS%2FREF/status/",
    "/api/admin/pos/orders/POS%2FREF/tenders/",
    "/api/admin/pos/cash-sessions/open/",
    "/api/admin/pos/cash-sessions/CS%2FREF/close/",
  ]);
  assert.deepEqual(JSON.parse(requests[1].init.body), { status: "READY" });
  assert.deepEqual(JSON.parse(requests[2].init.body), { method: "CARD", amount: "2500.00" });
});

test("POS page requires an explicit bounded in-house guest search and has skeleton/error paths", () => {
  const page = fs.readFileSync(path.join(FRONTEND, "dashboard", "pos.html"), "utf8");
  const findStart = page.indexOf("async function findStays()");
  const findEnd = page.indexOf("function selectStay", findStart);
  assert.notEqual(findStart, -1, "room-service search function must exist");
  assert.notEqual(findEnd, -1, "room-service search function must end before selection");
  const searchFn = page.slice(findStart, findEnd);

  assert.match(searchFn, /if\(!term\)/);
  assert.match(searchFn, /API\.list\("posRoomServiceStays",\{search:term,page_size:10\}\)/);
  assert.match(searchFn, /DATA\.loading\(stayResults,"list"\)/);
  assert.match(searchFn, /DATA\.error\(stayResults,/);
  assert.doesNotMatch(page, /setInterval\s*\(/);
  assert.match(page, /idempotency_key:state\.orderKey/);
  assert.match(page, /order_reference/);
});

test("operational staff roles are treated as staff and POS navigation is exact-role gated", () => {
  const ctx = makeContext();
  loadScript(ctx, "js/config.js");
  loadScript(ctx, "js/api.js");
  loadScript(ctx, "js/auth.js");
  const auth = ctx.window.Auth;

  ["CASHIER", "HOUSEKEEPING", "MAINTENANCE", "INVENTORY_CLERK"].forEach((role) => {
    assert.equal(auth.isStaffRole(role), true, `${role} should use staff-shell routing`);
  });
  const nav = fs.readFileSync(path.join(FRONTEND, "js", "dashboard.js"), "utf8");
  assert.match(nav, /POS & Room Service[\s\S]*roles: \["admin", "manager", "cashier"\]/);
});
