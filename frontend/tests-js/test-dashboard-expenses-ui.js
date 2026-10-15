/* Dashboard + Accounting expense-visibility contract tests.
   Supplier goods received in Inventory must be visible without being
   mislabelled as posted money. Run from /frontend:
   node --test tests-js/test-dashboard-expenses-ui.js */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { FRONTEND } = require("./harness");

const DASHBOARD = fs.readFileSync(path.join(FRONTEND, "dashboard", "index.html"), "utf8");
const ACCOUNTING = fs.readFileSync(path.join(FRONTEND, "dashboard", "accounting.html"), "utf8");

test("the staff dashboard renders month-to-date expense and supplier KPIs", () => {
  for (const [label, key] of [
    ["Operating expenses (month)", "month_operating_expenses"],
    ["Supplier goods received (month)", "month_goods_received"],
    ["Supplier payments (month)", "month_supplier_payments"],
    ["Outstanding payables", "outstanding_payables"],
  ]) {
    assert.ok(DASHBOARD.includes(label), `dashboard KPI missing: ${label}`);
    assert.ok(DASHBOARD.includes(`data-kpi="${key}"`), `dashboard KPI key missing: ${key}`);
  }
  // The month values come from the backend's month-to-date ledger summary,
  // not from a second frontend calculation.
  assert.match(DASHBOARD, /d\.ledger_month/);
  assert.match(DASHBOARD, /monthSupplier\.goods_receipts_value/);
  assert.match(DASHBOARD, /monthSupplier\.supplier_payments_made/);
  assert.match(DASHBOARD, /ledgerAmount\(month, "operating_expenses"\)/);
});

test("accounting surfaces supplier goods received separately from posted invoices", () => {
  assert.ok(ACCOUNTING.includes("Supplier goods received"), "accounting KPI missing goods received");
  assert.ok(ACCOUNTING.includes('data-rp="goods-received"'), "accounting KPI key missing");
  assert.match(ACCOUNTING, /Supplier goods received \(stock only — not posted\)/);
  assert.match(ACCOUNTING, /supplier_payments\.goods_receipts_value/);
  assert.match(ACCOUNTING, /supplier\.goods_receipts_count/);
  // The posted stages remain distinct measures.
  assert.match(ACCOUNTING, /Supplier invoices recognized/);
  assert.match(ACCOUNTING, /Supplier payments made/);
  assert.match(ACCOUNTING, /Outstanding supplier payables/);
});
