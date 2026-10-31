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
  assert.match(page, /boot\(\["admin","general_manager","accountant"\]\)/);
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
  assert.match(page, /Approve explicit allocations below/);
  assert.match(page, /label:"Reject"/);
  assert.match(page, /no pay deducted/);
});

test("compensation editing creates a controlled effective-dated replacement and never rewrites history", () => {
  const page = fs.readFileSync(path.join(FRONTEND, "dashboard", "payroll.html"), "utf8");
  assert.match(page, /data-comp-edit/);
  assert.match(page, /async function editCompensation/);
  assert.match(page, /Save new effective-dated terms/);
  assert.match(page, /staff_id:Number\(row\.staff\)/);
  assert.match(page, /effective date \(today or later\)/i);
  assert.match(page, /history is append-only/);
  assert.match(page, /Previously submitted, approved or paid payroll remains unchanged/);
  assert.match(page, /if\(!canManage\)return/);
});

test("submitted payroll exposes an approval/rejection review action only to an independent approver", () => {
  const page = fs.readFileSync(path.join(FRONTEND, "dashboard", "payroll.html"), "utf8");
  assert.match(page, /row\.status==="SUBMITTED"&&canApprove&&!owner/);
  assert.match(page, /">Approve<\/button>/);
  assert.match(page, /resourceAction\("payrollPeriods",reference,"review"/);
  assert.match(page, /approved:values\.approved==="true"/);
  assert.match(page, /status==="APPROVED"&&canManage/);
});

 test("missing-stock UI uses named stock and verified allocations, not estimated browser pricing", () => {
 const page = fs.readFileSync(path.join(FRONTEND, "dashboard", "payroll.html"), "utf8");
 assert.match(page, /stock_count_line_id:row.id/);
 assert.match(page, /discovered_at:at/);
 assert.match(page, /receipt-weighted purchase cost per base unit/);
 assert.match(page, /responsibility_type/);
 assert.match(page, /allocations:eligible.filter/);
 assert.match(page, /rate_per_minute:values.rate_per_minute/);
 assert.doesNotMatch(page, /resource:"staffDirectory"/);
 });

test("all payroll inline scripts parse", () => {
 const html = fs.readFileSync(path.join(FRONTEND, "dashboard", "payroll.html"), "utf8");
 for (const match of html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)) {
   if (match[1].trim()) assert.doesNotThrow(() => new Function(match[1]));
 }
});

test("multi-department and remote stock pagination are payroll-local", () => {
 const html = fs.readFileSync(path.join(FRONTEND, "dashboard", "payroll.html"), "utf8");
 assert.match(html, /name:"department_ids"[\s\S]*type:"checks"/);
 assert.match(html, /department_ids:context.department_ids/);
 assert.match(html, /search:search.value.trim\(\)/);
 assert.match(html, /page:page,page_size:25/);
 assert.match(html, /version!==requestVersion/);
 assert.match(html, /Next stock page/);
 assert.match(html, /excluded_employee_count/);
 assert.match(html, /financial totals retain the original ledger amounts/);
});

test("missing-stock wizard submits multiple departments and searches remote pages without stale IDs", async () => {
  const html = fs.readFileSync(path.join(FRONTEND, "dashboard", "payroll.html"), "utf8");
  const code = html.slice(html.indexOf("async function addStockShortageCase()"), html.indexOf("function parseComponents("));
  const requests = [], creations = [];
  function element() { return { children: [], listeners: {}, disabled: false, value: "", innerHTML: "", textContent: "",
    appendChild(child) { this.children.push(child); return child; },
    addEventListener(name, handler) { this.listeners[name] = handler; } }; }
  const select = element(), search = element(), panel = element();
  select.value = "1";
  panel.querySelector = (query) => query.includes("stock_line") ? select : search;
  const stock = (id) => ({ id, name: "Item " + id, location: "Store", quantity: "3", currency: "NGN", unit_cost: "1200.00", amount: "3600.00", stock_count_reference: "COUNT-" + id });
  const API = {
    async list(resource, params) {
      requests.push({ resource, params });
      if (resource === "departments") return { data: [{ id: 1, name: "Bar" }, { id: 2, name: "Store" }] };
      if (params.options === "staff") return { data: [{ staff_id: 11, name: "Staff A", department: "Bar" }, { staff_id: 22, name: "Staff B", department: "Store" }] };
      return { data: [stock(params.search ? 104 : params.page === 2 ? 26 : 1)], pagination: { has_next: !params.search && params.page !== 2 } };
    },
    async create(resource, payload) { creations.push(payload); }
  };
  let step = 0;
  const JONE = { dashboard: { async formModal(options) {
    if (++step === 1) {
      await options.onSubmit({ department_ids: ["1", "2"], discovery_date: "2026-10-30", discovery_time: "10:30", responsibility_type: "STAFF" });
      return true;
    }
    options.onRender(panel);
    const controls = panel.children[1];
    controls.children[2].listeners.click();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(requests.at(-1).params.page, 2);
    search.value = "Item 104";
    search.listeners.input();
    await new Promise(resolve => setTimeout(resolve, 340));
    assert.equal(requests.at(-1).params.search, "Item 104");
    assert.equal(select.children.at(-1).value, "104");
    await assert.rejects(() => options.onSubmit({ stock_line: "1" }), /Select a valid/);
    await options.onSubmit({ stock_line: "104", effective_date: "2026-10-30", notes: "Reviewed incident", staff_11: "yes", staff_22: "yes" });
    return true;
  } } };
  const fn = new Function("window", "OPS", "JONE", "today", "loadStockShortages", "document", code + ";return addStockShortageCase;")(
    { API }, { listItems: response => response.data }, JONE, () => "2026-10-30", () => {}, { createElement: element });
  await fn();
  assert.deepEqual(creations[0].department_ids, [1, 2]);
  assert.deepEqual(creations[0].staff_ids, [11, 22]);
  assert.equal(creations[0].stock_count_line_id, 104);
  assert.equal(creations[0].stock_count_reference, "COUNT-104");
  assert.equal(creations[0].discovered_at, "2026-10-30T10:30");
  assert.equal(creations[0].total_amount, undefined); // Backend owns valuation.
});
