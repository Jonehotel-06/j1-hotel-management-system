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

test("Accounting renders balance sheet and cash flow statements", () => {
  assert.match(ACCOUNTING, /id="bs-load"/);
  assert.match(ACCOUNTING, /data-tbody="bs-sheet"/);
  assert.match(ACCOUNTING, /data-tbody="cf-flow"/);
  assert.match(ACCOUNTING, /API_ENDPOINTS\.financeBalanceSheet/);
  assert.match(ACCOUNTING, /API_ENDPOINTS\.financeCashFlow/);
  assert.match(ACCOUNTING, /Opening balance offset/);
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

test("Guest Requests tracks laundry with priced items, billing and delivery", () => {
  assert.match(SERVICES, /data-laundry-rows/);
  assert.match(SERVICES, /data-laundry-new/);
  assert.match(SERVICES, /Bill to folio/);
  assert.match(SERVICES, /Who received the laundry/);
  assert.match(SERVICES, /resourceAction\("guestLaundry",ref,"status",payload\)/);
  assert.match(SERVICES, /resourceAction\("guestLaundry",bref,"bill",\{\}\)/);
});
