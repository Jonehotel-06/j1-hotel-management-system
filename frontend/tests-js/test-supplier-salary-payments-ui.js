/* Supplier payment marking, salary receipts, statements, occupancy exports
   and laundry UI contracts.
   Run from /frontend: node --test tests-js/test-supplier-salary-payments-ui.js */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const FRONTEND = path.resolve(__dirname, "..");
const read = (rel) => fs.readFileSync(path.join(FRONTEND, rel), "utf8");
const ACCOUNTING = read("dashboard/accounting.html");
const PAYROLL = read("dashboard/payroll.html");
const REPORTS = read("dashboard/reports.html");
const SERVICES = read("dashboard/service-requests.html");
const LAUNDRY = read("dashboard/laundry.html");
const CONFIG = read("js/config.js");

test("new endpoints are registered", () => {
  assert.match(CONFIG, /financeBalanceSheet:\s*"\/api\/admin\/finance\/reports\/balance-sheet\/"/);
  assert.match(CONFIG, /financeCashFlow:\s*"\/api\/admin\/finance\/reports\/cash-flow\/"/);
  assert.match(CONFIG, /guestLaundry:\s*"\/api\/admin\/service-requests\/laundry\/"/);
});

test("Accounting records supplier payments with an optional receipt", () => {
  assert.match(ACCOUNTING, /data-expense-settle/);
  assert.match(ACCOUNTING, /async function settleExpense\(reference\)/);
  assert.match(ACCOUNTING, /\/settle-payment\//);
  assert.match(ACCOUNTING, /name: "receipt", label: "Receipt file \(optional\)", type: "file"/);
  assert.match(ACCOUNTING, /row\.paid_at/);
  assert.match(ACCOUNTING, />Paid "/);
});

test("Accounting renders the balance sheet without a cash flow or equity section", () => {
  // BATCH 01 tasks 5 and 6. The Financial statements tab keeps the balance
  // sheet and loses the cash-flow statement entirely: section, table, caption
  // and the API call that fed it. The equity section is replaced by net
  // assets, which is identical to total equity, so the statement stays true.
  assert.match(ACCOUNTING, /id="bs-load"/);
  assert.match(ACCOUNTING, /data-tbody="bs-sheet"/);
  assert.match(ACCOUNTING, /API_ENDPOINTS\.financeBalanceSheet/);

  assert.doesNotMatch(ACCOUNTING, /data-tbody="cf-flow"/);
  assert.doesNotMatch(ACCOUNTING, /API_ENDPOINTS\.financeCashFlow/);
  assert.doesNotMatch(ACCOUNTING, /data-cf=/);
  // No heading, caption or printed section renders the statement any more.
  // (The source still *explains* the removal in comments, which is wanted.)
  assert.doesNotMatch(ACCOUNTING, /<h[23][^>]*>Cash flow/);
  assert.doesNotMatch(ACCOUNTING, /'<h2>Cash flow<\/h2>'/);
  assert.doesNotMatch(ACCOUNTING, /Balance sheet and cash flow/);

  assert.doesNotMatch(ACCOUNTING, /sheet\.equity/);
  assert.doesNotMatch(ACCOUNTING, /total_liabilities_and_equity/);
  assert.doesNotMatch(ACCOUNTING, /Opening balance offset/);
  assert.match(ACCOUNTING, /sheet\.net_assets/);
  assert.match(ACCOUNTING, /Net assets \(total assets − total liabilities\)/);
});

test("Payroll salary payment form accepts an optional receipt link or file", () => {
  assert.match(PAYROLL, /name:"receipt_url",label:"Receipt link \(optional\)"/);
  assert.match(PAYROLL, /name:"receipt",label:"Receipt file \(optional\)",type:"file"/);
  assert.match(PAYROLL, /Paid \/ Part-payment status automatically/);
  assert.match(PAYROLL, /fd\.append\("receipt",values\.receipt\)/);
});

test("Reports exports the occupancy report as Excel and PDF", () => {
  assert.match(REPORTS, /id="export-occ-xlsx"/);
  assert.match(REPORTS, /id="export-occ-pdf"/);
  assert.match(REPORTS, /function exportOccupancy\(kind\)/);
  assert.match(REPORTS, /API_ENDPOINTS\.reportsOccupancy/);
});

test("the Laundry page tracks priced items, billing and delivery", () => {
  // Laundry became its own page: it no longer shares the guest-request page.
  assert.match(LAUNDRY, /data-laundry-rows/);
  assert.match(LAUNDRY, /data-laundry-new/);
  assert.match(LAUNDRY, /Bill to folio/);
  assert.match(LAUNDRY, /Who received the laundry/);
  assert.match(LAUNDRY, /resourceAction\("guestLaundry",ref,"status",payload\)/);
  assert.match(LAUNDRY, /resourceAction\("guestLaundry",bref,"bill",\{\}\)/);
  assert.doesNotMatch(SERVICES, /data-laundry-rows/);
});
