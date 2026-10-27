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

test("workforce navigation serves every staff role while planning controls remain management-only", () => {
  const nav = fs.readFileSync(path.join(FRONTEND, "js", "dashboard.js"), "utf8");
  const page = fs.readFileSync(path.join(FRONTEND, "dashboard", "workforce.html"), "utf8");
  assert.match(nav, /Workforce[\s\S]*"housekeeping"[\s\S]*"maintenance"[\s\S]*"inventory_clerk"/);
  assert.doesNotMatch(nav, /"cashier"/, "the retired Cashier role is not an active navigation identity");
  /* Planning (shifts, employment profiles) is the management tier: a general
     manager holds shift.manage and staff.profile.manage exactly as a manager
     does, so the tier is admin + manager + general_manager. */
  assert.match(page, /hasAnyRole&&window\.Auth\.hasAnyRole\(\["admin","manager","general_manager"\]\)/);
  assert.match(page, /data-wf-manager/);
});

test("staff assignment offers one canonical role per department and preserves legacy filters", () => {
  const page = fs.readFileSync(path.join(FRONTEND, "dashboard", "staff.html"), "utf8");
  const roleField = page.match(/name: "role", label: "Canonical role"[\s\S]*?options: \[([\s\S]*?)\]\s*\}/);
  assert.ok(roleField, "canonical role picker must exist");
  const assignable = [
    "ADMIN", "GENERAL_MANAGER", "MANAGER", "ACCOUNTANT", "PROCUREMENT_OFFICER", "CHEF", "WAITER",
    "PORTER", "RECEPTIONIST", "HOUSEKEEPING", "STOREKEEPER", "SECURITY", "BARTENDER",
    "MAINTENANCE", "HR_OFFICER",
  ];
  assignable.forEach((role) => assert.match(roleField[1], new RegExp('value: "' + role + '"')));
  const retired = [
    "FRONT_DESK_SUPERVISOR", "RESTAURANT_MANAGER", "BAR_MANAGER", "KITCHEN_MANAGER", "KITCHEN_ASSISTANT",
    "HOUSEKEEPING_MANAGER", "HOUSEKEEPER", "MAINTENANCE_TECHNICIAN", "ACCOUNTS_MANAGER", "HR_MANAGER", "INVENTORY_CLERK", "CASHIER",
  ];
  retired.forEach((role) => assert.doesNotMatch(roleField[1], new RegExp('value: "' + role + '"')));
  /* Historical records remain findable, but retired titles are not assignable. */
  assert.match(page, /role__in: "MANAGER,GENERAL_MANAGER,FRONT_DESK_SUPERVISOR,[^"]*INVENTORY_CLERK"/);
  assert.match(page, /Kitchen Assistant is recorded here as a position/);
  assert.match(roleField[1], /Kitchen · Chef/);
});
