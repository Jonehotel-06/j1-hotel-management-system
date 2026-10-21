/* Automatic lateness deductions use centralized payroll APIs and an Admin-only policy. */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { FRONTEND } = require("./harness");

test("lateness endpoints are centralized in the frontend endpoint map", () => {
  const config = fs.readFileSync(path.join(FRONTEND, "js", "config.js"), "utf8");
  assert.match(config, /payrollLatenessPolicy:\s*"\/api\/admin\/staff-operations\/payroll\/lateness-policy\/"/);
  assert.match(config, /payrollLatenessDeductions:\s*"\/api\/admin\/staff-operations\/payroll\/lateness-deductions\/"/);
});

test("payroll page exposes the lateness policy and revocation workflow", () => {
  const page = fs.readFileSync(path.join(FRONTEND, "dashboard", "payroll.html"), "utf8");
  assert.match(page, /data-payroll-tab="lateness"/);
  assert.match(page, /data-payroll-panel="lateness"/);
  assert.match(page, /Automatic lateness deductions/);
  assert.match(page, /monthly basic salary plus housing and transport allowances/);
  assert.match(page, /Arrivals inside the grace period are never deducted/);
  assert.match(page, /API\.get\(window\.APP_CONFIG\.API_ENDPOINTS\.payrollLatenessPolicy/);
  assert.match(page, /API\.patch\(window\.APP_CONFIG\.API_ENDPOINTS\.payrollLatenessPolicy/);
  assert.match(page, /API\.list\("payrollLatenessDeductions"/);
  assert.match(page, /data-lateness-policy[^>]*hidden/);
  assert.match(page, /document\.querySelector\("\[data-lateness-policy\]"\)\.hidden=!isAdmin/);
  assert.match(page, /Only an Admin can configure the lateness deduction policy/);
  assert.match(page, /resourceAction\("payrollLatenessDeductions",reference,"request-revocation"/);
  assert.match(page, /Staff member's tangible reason/);
  assert.match(page, /resourceAction\("payrollLatenessDeductions",reference,"decision"/);
  assert.match(page, /row\.status==="REVOCATION_REQUESTED"\)buttons\+=/);
  assert.match(page, /adds the same amount back on a later payroll run/);
});

test("employee profile shows only the signed-in staff member's lateness records", () => {
  const page = fs.readFileSync(path.join(FRONTEND, "dashboard", "profile.html"), "utf8");
  assert.match(page, /sal\.lateness_deductions/);
  assert.match(page, /Automatic lateness deductions/);
  assert.match(page, /No automatic lateness deduction has been recorded for you/);
});
