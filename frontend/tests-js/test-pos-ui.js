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
  assert.equal(endpoints.posRestaurantTables, "/api/admin/pos/restaurant-tables/");
  assert.equal(endpoints.posRestaurantTableSessions, "/api/admin/pos/restaurant-table-sessions/");
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
  await ctx.window.API.updatePosKitchenTicketStatus(7, "PREPARING");
  await ctx.window.API.capturePosTender("POS/REF", { method: "CARD", amount: "2500.00" });
  await ctx.window.API.openPosCashSession({ opening_float: "0.00" });
  await ctx.window.API.closePosCashSession("CS/REF", { counted_cash: "1000.00" });
  await ctx.window.API.create("posRestaurantTables", { code: "T-01", seats: 2 });
  await ctx.window.API.create("posRestaurantTableSessions", { table_id: 1, covers: 2, idempotency_key: "table-open-001" });
  await ctx.window.API.resourceAction("posRestaurantTableSessions", "RTS/REF", "close", { close_note: "Done" });

  assert.deepEqual(requests.map((request) => request.url), [
    "/api/admin/pos/orders/POS%2FREF/submit/",
    "/api/admin/pos/orders/POS%2FREF/status/",
    "/api/admin/pos/kitchen-tickets/7/status/",
    "/api/admin/pos/orders/POS%2FREF/tenders/",
    "/api/admin/pos/cash-sessions/open/",
    "/api/admin/pos/cash-sessions/CS%2FREF/close/",
    "/api/admin/pos/restaurant-tables/",
    "/api/admin/pos/restaurant-table-sessions/",
    "/api/admin/pos/restaurant-table-sessions/RTS%2FREF/close/",
  ]);
  assert.deepEqual(JSON.parse(requests[1].init.body), { status: "READY" });
  assert.deepEqual(JSON.parse(requests[2].init.body), { status: "PREPARING" });
  assert.deepEqual(JSON.parse(requests[3].init.body), { method: "CARD", amount: "2500.00" });
  assert.deepEqual(JSON.parse(requests[6].init.body), { code: "T-01", seats: 2 });
  assert.deepEqual(JSON.parse(requests[7].init.body), { table_id: 1, covers: 2, idempotency_key: "table-open-001" });
  assert.deepEqual(JSON.parse(requests[8].init.body), { close_note: "Done" });
});

test("POS page requires a bounded in-house guest search and registered session for dine-in", () => {
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
  assert.match(page, /API\.list\('posRestaurantTables',\{active:showAll\?'all':'true',page_size:100\}\)/);
  assert.match(page, /API\.update\('posRestaurantTables',table\.id/);
  assert.match(page, /API\.list\('posRestaurantTableSessions',\{status:'OPEN',page_size:100\}\)/);
  assert.match(page, /mode\(\)==="RESTAURANT"&&!state\.selectedTableSession/);
  assert.match(page, /table_session_reference:mode\(\)==="RESTAURANT"\?state\.selectedTableSession/);
  assert.match(page, /data-pos-close-table-session/);
  assert.match(page, /URLSearchParams\(window\.location\.search/);
  assert.match(page, /API\.getOne\("serviceRequests",state\.serviceRequestReference\)/);
  assert.match(page, /service_request_reference=state\.serviceRequest\.reference/);
  assert.match(page, /Nothing is sent to production or charged until staff submit and deliver it/);
  assert.match(page, /\["DRAFT","SUBMITTED","PREPARING","READY"\]\.indexOf\(status\)/);
  assert.match(page, /if\(action==="CANCELLED"\)/);
  assert.doesNotMatch(page, /setInterval\s*\(/);
  assert.match(page, /dashboard\.startLiveRefresh/);
  assert.match(page, /idempotency_key:state\.orderKey/);
  assert.match(page, /order_reference/);
});

test("operational department roles are staff and POS navigation uses resolved capabilities", () => {
  const ctx = makeContext();
  loadScript(ctx, "js/config.js");
  loadScript(ctx, "js/api.js");
  loadScript(ctx, "js/auth.js");
  const auth = ctx.window.Auth;

  ["CASHIER", "HOUSEKEEPING", "MAINTENANCE", "INVENTORY_CLERK", "WAITER", "BARTENDER", "CHEF"].forEach((role) => {
    assert.equal(auth.isStaffRole(role), true, `${role} should use staff-shell routing`);
  });
  const nav = fs.readFileSync(path.join(FRONTEND, "js", "dashboard.js"), "utf8");
  assert.match(nav, /POS & Room Service[\s\S]*capabilities: \["pos.order.manage", "restaurant.order.manage", "bar.order.manage", "kitchen.queue.view", "kitchen.ticket.manage"\]/);
  assert.match(nav, /Auth\.loadCapabilities\(\)\.then/);
  assert.equal(ctx.window.APP_CONFIG.API_ENDPOINTS.staffCapabilities, "/api/auth/capabilities/");
});

test("staff capability hints load from the authenticated endpoint and gate a navigation item", async () => {
  const requests = [];
  const ctx = bootApi(requests);
  ctx.fetch = (url, init) => {
    requests.push({ url, init });
    return jsonResponse({ role: "WAITER", capabilities: ["restaurant.order.manage"] });
  };
  loadScript(ctx, "js/utils.js");
  loadScript(ctx, "js/auth.js");
  loadScript(ctx, "js/dashboard.js");

  await ctx.window.Auth.loadCapabilities();
  assert.deepEqual(Array.from(ctx.window.Auth.state.capabilities), ["restaurant.order.manage"]);
  assert.equal(ctx.window.Auth.hasCapability("restaurant.order.manage"), true);
  assert.equal(ctx.window.Auth.hasCapability("bar.order.manage"), false);
  const posItem = ctx.window.JONE.dashboard.NAV.flatMap((group) => group.items).find((item) => item.view === "pos");
  assert.equal(ctx.window.JONE.dashboard.navItemAllowed(posItem), true);
  ctx.window.Auth.state.capabilities = ["bar.order.manage"];
  assert.equal(ctx.window.JONE.dashboard.navItemAllowed(posItem), true);
  ctx.window.Auth.state.capabilities = ["attendance.clock"];
  assert.equal(ctx.window.JONE.dashboard.navItemAllowed(posItem), false);
});
