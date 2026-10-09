/* Workforce attendance, leave, scheduling, and profile UI contracts. */
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

test("workforce endpoints are centralized and preserve controlled action references", async () => {
  const ctx = makeContext();
  loadScript(ctx, "js/config.js");
  const endpoints = ctx.window.APP_CONFIG.API_ENDPOINTS;
  assert.equal(endpoints.staffProfiles, "/api/admin/staff-operations/profiles/");
  assert.equal(endpoints.shiftTemplates, "/api/admin/staff-operations/shift-templates/");
  assert.equal(endpoints.shifts, "/api/admin/staff-operations/shifts/");
  assert.equal(endpoints.attendance, "/api/admin/staff-operations/attendance/");
  assert.equal(endpoints.attendanceClock, "/api/admin/staff-operations/attendance/clock/");
  assert.equal(endpoints.leaveRequests, "/api/admin/staff-operations/leave-requests/");

  const requests = [];
  ctx.fetch = (url, init) => { requests.push({ url, init }); return response({}); };
  loadScript(ctx, "js/api.js");
  await ctx.window.API.create("attendanceClock", { action: "CLOCK_IN", idempotency_key: "clock-key" });
  await ctx.window.API.resourceAction("leaveRequests", "LVE/TEST", "review", { approved: true });
  await ctx.window.API.resourceAction("shifts", "SHF/TEST", "cancel", { reason: "Covered" });
  assert.equal(requests[0].url, "/api/admin/staff-operations/attendance/clock/");
  assert.deepEqual(JSON.parse(requests[0].init.body), { action: "CLOCK_IN", idempotency_key: "clock-key" });
  assert.equal(requests[1].url, "/api/admin/staff-operations/leave-requests/LVE%2FTEST/review/");
  assert.equal(requests[2].url, "/api/admin/staff-operations/shifts/SHF%2FTEST/cancel/");
});

test("workforce page uses bounded lists, skeleton errors, idempotency, and no polling", () => {
  const page = fs.readFileSync(path.join(FRONTEND, "dashboard", "workforce.html"), "utf8");
  assert.match(page, /__workforceAllowed/);
  assert.match(page, /\"chef\"/);
  assert.match(page, /myPayslips/);
  assert.match(page, /My salary/);
  assert.match(page, /operations\.js/);
  ["attendance", "leaveRequests", "shifts", "shiftTemplates", "staffProfiles"].forEach((resource) => {
    assert.match(page, new RegExp('API\\.list\\("' + resource + '"'));
  });
  assert.match(page, /API\.create\("attendanceClock"/);
  assert.match(page, /idempotency_key:OPS\.newIdempotencyKey\("attendance-/);
  assert.match(page, /idempotency_key=OPS\.newIdempotencyKey\("leave-request"\)/);
  assert.match(page, /idempotency_key=OPS\.newIdempotencyKey\("shift"\)/);
  assert.match(page, /DATA\.loading\(attendanceRows\)/);
  assert.match(page, /DATA\.error\(attendanceRows,/);
  assert.match(page, /page_size:JONE\.dashboard\.pageSize\(\)/);
  assert.match(page, /hotelToday\(\)/);
  assert.doesNotMatch(page, /setInterval\s*\(/);
});

test("workforce navigation serves every staff role while planning controls remain manager-only", () => {
  const nav = fs.readFileSync(path.join(FRONTEND, "js", "dashboard.js"), "utf8");
  const page = fs.readFileSync(path.join(FRONTEND, "dashboard", "workforce.html"), "utf8");
  assert.match(nav, /Workforce[\s\S]*"cashier"[\s\S]*"housekeeping"[\s\S]*"maintenance"[\s\S]*"inventory_clerk"/);
  assert.match(page, /hasAnyRole&&window\.Auth\.hasAnyRole\(\["admin","manager"\]\)/);
  assert.match(page, /data-wf-manager/);
});

test("admin staff management can create, filter, and list every supported operational role", () => {
  const page = fs.readFileSync(path.join(FRONTEND, "dashboard", "staff.html"), "utf8");
  const roles = [
    "ADMIN", "MANAGER", "GENERAL_MANAGER", "FRONT_DESK_SUPERVISOR", "RECEPTIONIST", "CASHIER",
    "RESTAURANT_MANAGER", "WAITER", "BAR_MANAGER", "BARTENDER", "KITCHEN_MANAGER", "CHEF",
    "HOUSEKEEPING_MANAGER", "HOUSEKEEPER", "HOUSEKEEPING", "MAINTENANCE_TECHNICIAN", "MAINTENANCE",
    "ACCOUNTS_MANAGER", "ACCOUNTANT", "HR_MANAGER", "SECURITY", "PROCUREMENT_OFFICER", "STOREKEEPER", "INVENTORY_CLERK",
  ];
  roles.forEach((role) => assert.match(page, new RegExp('value: "' + role + '"')));
  roles.forEach((role) => assert.match(page, new RegExp('option value="' + role + '"')));
  assert.match(page, /role__in: "ADMIN,MANAGER,GENERAL_MANAGER,FRONT_DESK_SUPERVISOR,[^"]*INVENTORY_CLERK"/);
});
