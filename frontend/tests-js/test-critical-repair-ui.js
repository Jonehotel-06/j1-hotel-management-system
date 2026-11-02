/* Critical repair UI contract: each new capability has a visible, honest place
   in the staff console. Run from /frontend: node --test tests-js/ */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const FRONTEND = path.resolve(__dirname, "..");
const read = (relative) => fs.readFileSync(path.join(FRONTEND, relative), "utf8");

const config = read("js/config.js");
const accounting = read("dashboard/accounting.html");
const index = read("dashboard/index.html");
const procurement = read("dashboard/procurement.html");
const inventory = read("dashboard/inventory.html");
const serviceRequests = read("dashboard/service-requests.html");

test("the new controlled endpoint keys are additive", () => {
  assert.match(config, /inventoryManualIssues:\s*"\/api\/admin\/inventory\/manual-ingredient-issues\/"/);
  assert.match(config, /financeReconciliation:\s*"\/api\/admin\/finance\/reports\/reconciliation\/"/);
  assert.match(config, /financeExpenseHandovers:\s*"\/api\/admin\/finance\/expenses\/handover\/"/);
  assert.match(config, /inventoryIssues:\s*"\/api\/admin\/inventory\/issues\/"/);
});

test("Accounting shows reconciliation status and unposted payment exceptions", () => {
  assert.match(accounting, /Unposted Procurement payments \(exception only\)/);
  assert.match(accounting, /supplier_payments\.unposted_procurement_payments\.amount/);
  assert.match(accounting, /Reconciliation: "/);
  assert.match(accounting, /workflow exceptions/);
});

test("the owner dashboard declares one posted-ledger financial basis", () => {
  assert.match(index, /data-reconciliation-status/);
  assert.match(index, /Posted-ledger reconciliation: "/);
  assert.match(index, /d\.financial_basis\|\|d\.ledger\|\|d\.ledger_month\|\|d\.reconciliation/);
  assert.match(index, /legacy Payment\+Booking/);
  assert.doesNotMatch(index, /else if\(d\.revenue\)/);
});

test("Procurement separates payment, receipt and Accounting handover states", () => {
  assert.match(procurement, /Payment status/);
  assert.match(procurement, /Receipt status/);
  assert.match(procurement, /Accounting status/);
  assert.match(procurement, /accounting_posting_status/);
  assert.match(procurement, /finance_expense_reference/);
  assert.match(procurement, /creates one controlled Accounting handover/);
});

test("Inventory exposes the manual dish-ingredient issue workflow", () => {
  assert.match(inventory, /data-inv-tab="food-issues">Food issues/);
  assert.match(inventory, /data-inv-food-issue-rows/);
  assert.match(inventory, /data-inv-new-food-issue/);
  assert.match(inventory, /consumptionModeOptions/);
  assert.match(inventory, /MANUAL",label:"Manual Store Keeper ingredient issue/);
  assert.match(inventory, /window\.API\.create\("inventoryManualIssues"/);
  assert.match(inventory, /window\.API\.list\("inventoryManualIssues"/);
  assert.match(inventory, /Dish: '/);
  assert.match(inventory, /Batch: '/);
});

test("Guest-service details expose dishes, serving staff and payment evidence", () => {
  assert.match(serviceRequests, /function posOrderHtml\(item\)/);
  assert.match(serviceRequests, /Linked food-service order/);
  assert.match(serviceRequests, /Served by/);
  assert.match(serviceRequests, /charge_transaction_reference/);
  assert.match(serviceRequests, /folio_reference/);
  assert.match(serviceRequests, /served_by_email/);
  assert.match(serviceRequests, /Tender evidence/);
  assert.match(serviceRequests, /pos_order_details/);
});
