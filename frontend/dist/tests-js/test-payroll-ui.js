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
  assert.match(config, /payrollStockShortages:\s*"\/api\/admin\/staff-operations\/payroll\/stock-shortages\/"/);
  assert.doesNotMatch(config, /payrollStatutoryRules|payrollTaxIdentities/);
  assert.match(config, /payrollPeriods:\s*"\/api\/admin\/staff-operations\/payroll\/periods\/"/);
  assert.match(config, /payrollPayslips:\s*"\/api\/admin\/staff-operations\/payroll\/payslips\/"/);
  assert.match(nav, /label: "Payroll"[\s\S]*capabilities: \["payroll\.view", "payroll\.manage", "payroll\.approve"\]/);
  assert.doesNotMatch(nav, /payroll\.rules\.(manage|review)/);
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

test("payroll page uses simplified salary terms and controlled maker-checker/ledger actions", () => {
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
  assert.match(page, /Government tax and pension calculations are not part of this payroll system/);
  assert.doesNotMatch(page, /payrollStatutoryRules|payrollTaxIdentities/);
  assert.doesNotMatch(page, /pension_applicable|minimum_wage_applicable|prior_ytd|benefits_in_kind|tax_claims/);
  assert.doesNotMatch(page, /Statutory rules|Tax IDs|PAYE withheld|Employer pension/);
  assert.match(page, /<th class="hide-sm">Gross<\/th>/);
  assert.match(page, /<th>Net<\/th>/);
  assert.match(page, /PARTIALLY_PAID/);
  assert.match(page, /salary_amount_paid/);
  assert.match(page, /salary_balance/);
  assert.match(page, /recordPayrollSalaryPayment/);
  assert.match(page, /reversePayrollSalaryPayment/);
  assert.match(page, /newIdempotencyKey\("salary-payment"\)/);
  assert.match(page, /record only a payment already made/);
  assert.match(page, /Allowances \/ deductions/);
});

test("stock shortages require an Admin role decision and never deduct when recorded", () => {
  const page = fs.readFileSync(path.join(FRONTEND, "dashboard", "payroll.html"), "utf8");
  assert.match(page, /data-payroll-tab="shortages"/);
  assert.match(page, /Department stock shortages/);
  assert.match(page, /Recording a case does not deduct anyone's pay/);
  assert.match(page, /Only an account with the <strong>Admin<\/strong> role can approve it/);
  assert.match(page, /isAdmin=String\(\(me&&me\.role\)\|\|""\)\.toLowerCase\(\)==="admin"/);
  assert.match(page, /row\.status==="PENDING_ADMIN_APPROVAL"&&isAdmin/);
  assert.match(page, /API\.create\("payrollStockShortages"/);
  assert.match(page, /API\.getOne\("payrollStockShortages"/);
  assert.match(page, /resourceAction\("payrollStockShortages",reference,"decision"/);
  assert.match(page, /Approve — divide equally across all department staff/);
  assert.match(page, /Reject — make no payroll deduction/);
  assert.match(page, /No payroll deduction has been made yet/);
});
