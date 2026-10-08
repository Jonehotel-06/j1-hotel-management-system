/* Guest-services / housekeeping / maintenance UI integration contracts. */
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

test("operations endpoints and action paths are centralized", async () => {
  const ctx = makeContext();
  loadScript(ctx, "js/config.js");
  const endpoints = ctx.window.APP_CONFIG.API_ENDPOINTS;
  assert.equal(endpoints.serviceRequests, "/api/admin/service-requests/");
  assert.equal(endpoints.housekeepingTasks, "/api/admin/housekeeping/");
  assert.equal(endpoints.maintenanceWorkOrders, "/api/admin/maintenance/");
  assert.equal(endpoints.staffDirectory, "/api/admin/users/directory/");

  const requests = [];
  ctx.fetch = (url, init) => { requests.push({ url, init }); return response({}); };
  loadScript(ctx, "js/api.js");
  await ctx.window.API.resourceAction("maintenanceWorkOrders", "MNT/A", "outage/start", { outage_status: "MAINTENANCE" });
  assert.equal(requests[0].url, "/api/admin/maintenance/MNT%2FA/outage/start/");
  assert.deepEqual(JSON.parse(requests[0].init.body), { outage_status: "MAINTENANCE" });
});

test("shared operation helpers omit unset optional picker values and create retry keys", () => {
  const ctx = makeContext();
  loadScript(ctx, "js/config.js");
  loadScript(ctx, "js/utils.js");
  loadScript(ctx, "js/operations.js");
  const ops = ctx.window.JONE.operations;

  assert.equal(JSON.stringify(ops.omitEmpty({ room_id: "", due_at: null, summary: "Repair tap", priority: "HIGH" })),
    JSON.stringify({ summary: "Repair tap", priority: "HIGH" }));
  assert.match(ops.newIdempotencyKey("maintenance"), /^maintenance-ui-/);
  const picker = ops.searchPicker({ name: "room_id", label: "Room", resource: "rooms", required: true });
  assert.equal(picker.type, "custom");
  assert.equal(picker.validate(), "Select a room from search results.");
  assert.equal(JSON.stringify(picker.getValue()), "{}");
});

test("operational queue pages use server pagination, skeletons, retryable errors, and no polling", () => {
  const contracts = [
    ["service-requests.html", "__serviceRequestsAllowed", "serviceRequests", "guest request"],
    ["housekeeping.html", "__housekeepingAllowed", "housekeepingTasks", "housekeeping"],
    ["maintenance.html", "__maintenanceAllowed", "maintenanceWorkOrders", "maintenance"],
  ];
  contracts.forEach(([file, gate, resource, keyword]) => {
    const page = fs.readFileSync(path.join(FRONTEND, "dashboard", file), "utf8");
    assert.match(page, new RegExp(gate));
    assert.match(page, /operations\.js/);
    assert.match(page, new RegExp('API\\.list\\("' + resource + '"'));
    assert.match(page, /DATA\.loading\(rowsEl\)/);
    assert.match(page, /DATA\.error\(rowsEl,/);
    assert.match(page, /page_size:JONE\.dashboard\.pageSize\(\)/);
    assert.match(page, /resourceAction/);
    assert.doesNotMatch(page, /setInterval\s*\(/);
    assert.match(page.toLowerCase(), new RegExp(keyword));
  });
});

test("navigation shows each specialist queue only to its operational roles", () => {
  const nav = fs.readFileSync(path.join(FRONTEND, "js", "dashboard.js"), "utf8");
  assert.match(nav, /Guest Requests[\s\S]*housekeeping[\s\S]*maintenance/);
  assert.match(nav, /Housekeeping[\s\S]*roles: \["admin", "manager", "receptionist", "housekeeping"\]/);
  assert.match(nav, /Maintenance[\s\S]*roles: \["admin", "manager", "receptionist", "maintenance"\]/);
});
