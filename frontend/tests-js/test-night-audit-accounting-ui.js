/* Night audit, journals, AR aging, cash drops and expense departments:
   Accounting runs the daily reconciliation pack and posts balanced journals,
   Reports ages unpaid guest balances, and POS records safe drops.
   Run from /frontend: node --test tests-js/test-night-audit-accounting-ui.js */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const FRONTEND = path.resolve(__dirname, "..");
const read = (rel) => fs.readFileSync(path.join(FRONTEND, rel), "utf8");
const ACCOUNTING = read("dashboard/accounting.html");
const REPORTS = read("dashboard/reports.html");
const POS = read("dashboard/pos.html");
const CONFIG = read("js/config.js");

test("new finance endpoints are registered in the endpoint map", () => {
  for (const [name, url] of [
    ["financeNightAudit", "/api/admin/finance/reports/night-audit/"],
    ["financeArAging", "/api/admin/finance/reports/ar-aging/"],
    ["financeJournals", "/api/admin/finance/journals/"],
    ["financeChartOfAccounts", "/api/admin/finance/chart-of-accounts/"],
  ]) {
    assert.match(CONFIG, new RegExp(`${name}:\\s*"${url.replace(/\//g, "\\/")}"`), name);
  }
});

test("Accounting renders the night audit daily flash and checks", () => {
  assert.match(ACCOUNTING, /id="na-load"/);
  assert.match(ACCOUNTING, /data-na="adr"/);
  assert.match(ACCOUNTING, /data-na="revpar"/);
  assert.match(ACCOUNTING, /data-tbody="na-drawers"/);
  assert.match(ACCOUNTING, /data-tbody="na-unbilled"/);
  assert.match(ACCOUNTING, /API_ENDPOINTS\.financeNightAudit/);
  assert.match(ACCOUNTING, /renderNightAudit/);
});

test("Accounting posts balanced manual journals through the ledger endpoint", () => {
  assert.match(ACCOUNTING, /id="acct-journal"/);
  assert.match(ACCOUNTING, /API_ENDPOINTS\.financeChartOfAccounts/);
  assert.match(ACCOUNTING, /Journal is not balanced/);
  assert.match(ACCOUNTING, /window\.API\.create\("financeJournals", payload\)/);
});

test("Accounting expense create, rows and details carry a department", () => {
  assert.match(ACCOUNTING, /name: "department", label: "Department"/);
  assert.match(ACCOUNTING, /department: values\.department \|\| ""/);
  assert.match(ACCOUNTING, /row\.department\|\|/);
  assert.match(ACCOUNTING, /expenseDetailRow\("Department", expenseDetailText\(detail\.department\)\)/);
});

test("Reports ages receivables into buckets and exports them", () => {
  assert.match(REPORTS, /Accounts receivable aging/);
  assert.match(REPORTS, /data-ar="over_90"/);
  assert.match(REPORTS, /data-tbody="report-ar-aging"/);
  assert.match(REPORTS, /API_ENDPOINTS\.financeArAging/);
  assert.match(REPORTS, /"Accounts receivable aging"/);
  assert.match(REPORTS, /row\.department\|\|/);
});

test("POS records cash drops against the cashier's open drawer", () => {
  assert.match(POS, /data-pos-drop-cash/);
  assert.match(POS, /async function dropCash\(\)/);
  assert.match(POS, /resourceAction\("financeCashSessions",ref,"drop",values\)/);
  assert.match(POS, /dropButton\.disabled=!state\.drawer/);
});

test("Accounting and Reports export rows wrap instead of stretching buttons", () => {
  assert.match(ACCOUNTING, /display:flex;gap:0\.75rem;flex-wrap:wrap;align-items:center;/);
  assert.match(ACCOUNTING, /<span class="caption" style="flex:1 1 12rem;min-width:0;">Exports the ledger/);
  assert.match(REPORTS, /<span class="caption" style="flex:1 1 12rem;min-width:0;">Exports the ledger/);
});
