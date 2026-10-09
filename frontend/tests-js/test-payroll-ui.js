/* Confidential payroll UI uses the established API/ledger with no cached or duplicated records. */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { FRONTEND, makeContext, loadScript, disposeAll } = require("./harness");

test.after(() => disposeAll());

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

test("employee salary-payment API helpers escape nested references and keep requests centralized", async () => {
  const ctx = makeContext();
  loadScript(ctx, "js/config.js");
  const requests = [];
  ctx.fetch = (url, init) => {
    requests.push({ url, init });
    return Promise.resolve({
      status: 200,
      ok: true,
      headers: { get: (key) => key === "content-type" ? "application/json" : null },
      json: () => Promise.resolve({ success: true, data: [] }),
      text: () => Promise.resolve(JSON.stringify({ success: true, data: [] })),
    });
  };
  loadScript(ctx, "js/api.js");
  await ctx.window.API.listPayrollSalaryPayments("PAY/26", 7, { page: 2, page_size: 10 }, { cache: "no-store" });
  await ctx.window.API.recordPayrollSalaryPayment("PAY/26", 7, { amount: "25.00", idempotency_key: "salary-key-001" }, { cache: "no-store" });
  await ctx.window.API.reversePayrollSalaryPayment("PAY/26", 7, "SAL/1", { correction_reason: "Returned" }, { cache: "no-store" });
  const base = "/api/admin/staff-operations/payroll/periods/PAY%2F26/lines/7/payments/";
  assert.equal(requests[0].url.split("?")[0], base);
  assert.match(requests[0].url, /[?]page=2/);
  assert.equal(requests[0].init.method, "GET");
  assert.equal(requests[1].url, base);
  assert.equal(requests[1].init.method, "POST");
  assert.deepEqual(JSON.parse(requests[1].init.body), { amount: "25.00", idempotency_key: "salary-key-001" });
  assert.equal(requests[2].url, base + "SAL%2F1/reverse/");
  assert.equal(requests[2].init.method, "POST");
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
  assert.match(page, /PARTIALLY_PAID/);
  assert.match(page, /salary_amount_paid/);
  assert.match(page, /salary_balance/);
  assert.match(page, /recordPayrollSalaryPayment/);
  assert.match(page, /reversePayrollSalaryPayment/);
  assert.match(page, /newIdempotencyKey\("salary-payment"\)/);
  assert.match(page, /record only a payment already made/);
  assert.match(page, /Allowances \/ benefits \/ deductions/);
  assert.match(page, /within 30 days/);
});
