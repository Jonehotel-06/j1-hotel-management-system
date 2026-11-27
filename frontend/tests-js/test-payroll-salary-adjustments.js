/* Salary adjustments (partial month / unpaid absence / disciplinary / other):
   the payroll page records them through the shared API layer, the server
   performs every calculation, approval is independent and never pays anyone. */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { FRONTEND, makeContext, loadScript, disposeAll } = require("./harness");

test.after(() => disposeAll());

const page = () => fs.readFileSync(path.join(FRONTEND, "dashboard", "payroll.html"), "utf8");

test("the salary-adjustment endpoint is centralized in config.js and resolves through API helpers", async () => {
  const config = fs.readFileSync(path.join(FRONTEND, "js", "config.js"), "utf8");
  assert.match(config, /payrollSalaryAdjustments:\s*"\/api\/admin\/staff-operations\/payroll\/salary-adjustments\/"/);

  const ctx = makeContext();
  loadScript(ctx, "js/config.js");
  const requests = [];
  ctx.fetch = (url, init) => {
    requests.push({ url, init });
    return Promise.resolve({
      ok: true,
      status: 200,
      headers: { get: () => "application/json" },
      json: async () => ({ success: true, data: {} }),
      text: async () => JSON.stringify({ success: true, data: {} }),
    });
  };
  ctx.window.fetch = ctx.fetch;
  loadScript(ctx, "js/api.js");
  const API = ctx.window.API;

  await API.list("payrollSalaryAdjustments", { options: "context", staff_id: 7, payroll_year: 2026, payroll_month: 3 });
  await API.post(ctx.window.APP_CONFIG.API_ENDPOINTS.payrollSalaryAdjustments + "preview/", { staff_id: 7 });
  await API.create("payrollSalaryAdjustments", { staff_id: 7, adjustment_type: "PRORATED" });
  await API.getOne("payrollSalaryAdjustments", "ADJ-2026-000001");
  await API.resourceAction("payrollSalaryAdjustments", "ADJ-2026-000001", "decision", { approved: true, decision_note: "Checked" });
  await API.resourceAction("payrollSalaryAdjustments", "ADJ-2026-000001", "reverse", { reason: "Recorded twice" });

  const urls = requests.map((r) => String(r.url));
  assert.ok(urls[0].includes("/payroll/salary-adjustments/?") && urls[0].includes("options=context") && urls[0].includes("staff_id=7"));
  assert.ok(urls[1].endsWith("/payroll/salary-adjustments/preview/"));
  assert.ok(urls[2].endsWith("/payroll/salary-adjustments/"));
  assert.ok(urls[3].endsWith("/payroll/salary-adjustments/ADJ-2026-000001/"));
  assert.ok(urls[4].endsWith("/payroll/salary-adjustments/ADJ-2026-000001/decision/"));
  assert.ok(urls[5].endsWith("/payroll/salary-adjustments/ADJ-2026-000001/reverse/"));
  assert.equal(requests[2].init.method, "POST");
  assert.equal(JSON.parse(requests[4].init.body).approved, true);
});

test("Record Adjusted Salary is a payroll-manage action with a server-calculated preview and explicit confirmation", () => {
  const html = page();
  assert.match(html, /data-adjustment-new[^>]*hidden>Record Adjusted Salary<\/button>/);
  assert.match(html, /data-payroll-tab="adjustments"/);
  assert.match(html, /async function recordAdjustment\(\)\{\s*if\(!canManage\)return;/);
  // Staff are chosen from the payroll-eligible directory, not an arbitrary user search.
  assert.match(html, /resource:"payrollCompensation",params:\{options:"staff"\}/);
  // The browser never computes pay: context + preview come from the server.
  assert.match(html, /options:"context",staff_id:Number\(picked\.staff_id\|\|picked\.id\)/);
  assert.match(html, /API_ENDPOINTS\.payrollSalaryAdjustments\+"preview\/"/);
  assert.doesNotMatch(html, /monthly_salary\s*[\*\/]\s*days_worked/);
  assert.doesNotMatch(html, /daily_rate\s*\*\s*(days_worked|absence_days)/);
  // Every adjustment type the server supports is offered, with the month basis options.
  for (const type of ["PRORATED", "ABSENCE", "DISCIPLINARY", "OTHER"]) assert.match(html, new RegExp('value:"' + type + '"'));
  for (const basis of ["CALENDAR_DAYS", "FIXED_30", "SCHEDULED_DAYS"]) assert.match(html, new RegExp('value:"' + basis + '"'));
  // The summary block shows the required lines.
  for (const label of ["Normal Monthly Salary", "Prorated Salary", "Disciplinary Deduction", "Additional Payment", "Final Payable Salary"]) {
    assert.ok(html.includes(label), label + " is shown in the calculation summary");
  }
  // Confirmation + justification are mandatory before recording.
  assert.match(html, /name:"confirm",label:"I have checked the calculation summary above/);
  assert.match(html, /if\(!values\.confirm\)throw new Error/);
  assert.match(html, /name:"violation_category"/);
  assert.match(html, /name:"incident_date",label:"Incident date",type:"date",max:today\(\)/);
  assert.match(html, /name:"evidence_reference"/);
  // Idempotent create through the shared API layer.
  assert.match(html, /OPS\.newIdempotencyKey\("salary-adjustment"\)/);
  assert.match(html, /payload\.idempotency_key=key;await window\.API\.create\("payrollSalaryAdjustments",payload/);
  assert.match(html, /Nothing has been paid/);
});

test("approval of a salary adjustment is independent, explicit, and separate from payment", () => {
  const html = page();
  assert.match(html, /row\.status==="PENDING_APPROVAL"&&canApprove&&!owner/);
  assert.match(html, /async function decideAdjustment\(reference\)\{if\(!canApprove\)return;/);
  assert.match(html, /resourceAction\("payrollSalaryAdjustments",reference,"decision",\{approved:values\.approved==="true",decision_note:values\.decision_note\.trim\(\)\}/);
  assert.match(html, /Approval records your independent decision only\. It does not pay the staff member/);
  assert.match(html, /resourceAction\("payrollSalaryAdjustments",reference,"submit"/);
  assert.match(html, /resourceAction\("payrollSalaryAdjustments",reference,"reverse",\{reason:values\.reason\.trim\(\)\}/);
  assert.match(html, /Approved leave is never treated as an unpaid absence/);
  // Detail view surfaces the audit trail and the month summary from the server.
  assert.match(html, /API\.getOne\("payrollSalaryAdjustments",reference/);
  assert.match(html, /adjSummaryTable\(item\.month_summary,cur\)/);
  assert.match(html, /\(item\.events\|\|\[\]\)\.map/);
  // No client-side status flips: statuses only come from server rows.
  assert.doesNotMatch(html, /row\.status="APPROVED"/);
  assert.doesNotMatch(html, /item\.status="PAID"/);
});

test("salary-adjustment history filters are server-side query params and capability-gated", () => {
  const html = page();
  for (const id of ["adj-search", "adj-department", "adj-month", "adj-year", "adj-type", "adj-status"]) {
    assert.ok(html.includes('id="' + id + '"'), id + " filter control exists");
  }
  assert.match(html, /if\(search\)params\.search=search;if\(department\)params\.department=department;if\(month\)params\.payroll_month=month;if\(year\)params\.payroll_year=year;if\(type\)params\.adjustment_type=type;if\(status\)params\.status=status;/);
  assert.match(html, /async function loadAdjustments\(page\)\{if\(!canView\)return;/);
  assert.match(html, /API\.list\("payrollSalaryAdjustments",adjQuery\(\),\{cache:"no-store"\}\)/);
  assert.match(html, /document\.querySelector\('\[data-payroll-tab="adjustments"\]'\)\.hidden=!canView/);
  assert.match(html, /document\.querySelectorAll\("\[data-adjustment-new\]"\)\.forEach\(function\(button\)\{button\.hidden=!canManage;\}\)/);
});

test("all payroll inline scripts still parse with the salary-adjustment block", () => {
  const html = page();
  let count = 0;
  for (const match of html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)) {
    if (match[1].trim()) { count += 1; assert.doesNotThrow(() => new Function(match[1])); }
  }
  assert.ok(count >= 1);
});
