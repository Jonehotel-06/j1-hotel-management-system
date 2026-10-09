/* Confidential payroll UI uses the established API/ledger with no cached or duplicated records. */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { FRONTEND } = require("./harness");

test("payroll endpoints are centralized and payroll navigation is capability-scoped", () => {
  const config = fs.readFileSync(path.join(FRONTEND, "js", "config.js"), "utf8");
  const nav = fs.readFileSync(path.join(FRONTEND, "js", "dashboard.js"), "utf8");
  assert.match(config, /payrollCompensation:\s*"\/api\/admin\/staff-operations\/payroll\/compensation\/"/);
  assert.match(config, /payrollStatutoryRules:\s*"\/api\/admin\/staff-operations\/payroll\/statutory-rules\/"/);
  assert.match(config, /payrollTaxIdentities:\s*"\/api\/admin\/staff-operations\/payroll\/tax-identities\/"/);
  assert.match(config, /payrollPeriods:\s*"\/api\/admin\/staff-operations\/payroll\/periods\/"/);
  assert.match(config, /payrollPayslips:\s*"\/api\/admin\/staff-operations\/payroll\/payslips\/"/);
  assert.match(nav, /label: "Payroll"[\s\S]*capabilities: \["payroll\.view", "payroll\.manage", "payroll\.approve", "payroll\.rules\.manage", "payroll\.rules\.review"\]/);
});

test("payroll page uses private no-store reads and controlled maker-checker/ledger actions", () => {
  const page = fs.readFileSync(path.join(FRONTEND, "dashboard", "payroll.html"), "utf8");
  assert.match(page, /boot\(\["admin","manager","general_manager","accounts_manager","accountant","hr_manager"\]\)/);
  assert.match(page, /loadCapabilities\(\)/);
  assert.match(page, /cache:"no-store"/);
  assert.match(page, /API\.create\("payrollPeriods"/);
  assert.match(page, /newIdempotencyKey\("payroll-run"\)/);
  assert.match(page, /resourceAction\("payrollPeriods",reference,"submit"/);
  assert.match(page, /resourceAction\("payrollPeriods",reference,"review"/);
  assert.match(page, /resourceAction\("payrollPeriods",reference,"pay"/);
  assert.match(page, /API\.getBlob\(/);
  assert.match(page, /API\.create\("payrollCompensation"/);
  assert.match(page, /API\.create\("payrollStatutoryRules"/);
  assert.match(page, /resourceAction\("payrollStatutoryRules",reference,"review"/);
  assert.match(page, /prior_ytd/);
  assert.match(page, /API\.create\("payrollTaxIdentities"/);
  assert.match(page, /Nigeria 2026 PAYE and pension rules/);
  assert.match(page, /independent legal\/compliance reviewer/);
  assert.match(page, /benefits_in_kind/);
  assert.match(page, /<th[^>]*>Cash gross<\/th>/);
  assert.match(page, /<th>Net cash<\/th>/);
  assert.match(page, /Allowances \/ benefits \/ deductions/);
  assert.match(page, /within 30 days/);
});
