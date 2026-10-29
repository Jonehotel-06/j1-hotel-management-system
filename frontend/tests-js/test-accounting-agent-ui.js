/* Accounting agent repairs, as the page and the endpoint map must show them:

   1. the "unresolved ... checks" line names its subjects (room/billing, cash
      drawer, purchase orders) instead of lumping them together;
   2. the checks are rendered as records — object, amount, status, why it is
      flagged, next action — and can be reviewed from the page;
   3. submitted purchase orders are visible on Accounting with the payable
      position, and a payable can be raised from the page;
   4. supplier payments have their own proposed/submitted/approved/executed
      lifecycle rather than a single "mark as paid" button.

   Run from /frontend: node --test tests-js/test-accounting-agent-ui.js */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const FRONTEND = path.resolve(__dirname, "..");
const read = (rel) => fs.readFileSync(path.join(FRONTEND, rel), "utf8");
const ACCOUNTING = read("dashboard/accounting.html");
const CONFIG = read("js/config.js");

test("the new accounting endpoints are registered in the endpoint map", () => {
  for (const [name, url] of [
    ["financeReconciliationChecks", "/api/admin/finance/reconciliation/checks/"],
    ["financeProcurementPayables", "/api/admin/finance/procurement/payables/"],
    ["financePurchaseOrderPayable", "/api/admin/finance/procurement/purchase-orders/"],
    ["financeSupplierPayments", "/api/admin/finance/supplier-payments/"],
    ["procurementAccountingOrders", "/api/admin/inventory/accounting/purchase-orders/"],
  ]) {
    assert.match(CONFIG, new RegExp(`${name}:\\s*"${url.replace(/\//g, "\\/")}"`), name);
  }
});

test("the unresolved-check count names its subjects instead of lumping cash in", () => {
  assert.match(ACCOUNTING, /unresolved room or billing check/);
  assert.match(ACCOUNTING, /unresolved cash drawer check/);
  assert.match(ACCOUNTING, /summary\.unresolved_room_and_billing/);
  assert.match(ACCOUNTING, /summary\.unresolved_cash_drawer/);
  // The KPI is driven by the server's summary, not by a locally invented count.
  assert.match(ACCOUNTING, /checkSummary\.unresolved != null \? checkSummary\.unresolved/);
});

test("the checks are rendered as records with the fields an accountant needs", () => {
  assert.match(ACCOUNTING, /data-tbody="na-checks"/);
  for (const heading of ["Subject", "Check", "Record", "Amount", "Status", "Why it is flagged", "Recommended next action"]) {
    assert.ok(ACCOUNTING.includes(heading), `missing column ${heading}`);
  }
  const render = ACCOUNTING.slice(ACCOUNTING.indexOf("function renderChecks()"), ACCOUNTING.indexOf("function checksHtml()"));
  assert.match(render, /check\.why_flagged/);
  assert.match(render, /check\.recommended_action/);
  assert.match(render, /check\.amount/);
  assert.match(ACCOUNTING, /check\.object_reference/);
  // The status cell (a helper beside the renderer) is where the derived status
  // and the reviewer are shown.
  const statusCell = ACCOUNTING.slice(ACCOUNTING.indexOf("function checkStatusCell("), ACCOUNTING.indexOf("function checkObjectCell("));
  assert.match(statusCell, /check\.status/);
  assert.match(statusCell, /check\.reviewed_by/);
});

test("a check can be acknowledged, escalated or dismissed with a reason", () => {
  const review = ACCOUNTING.slice(
    ACCOUNTING.indexOf("async function reviewCheck("),
    ACCOUNTING.indexOf("function procurementPosition("),
  );
  for (const decision of ["ACKNOWLEDGED", "ESCALATED", "DISMISSED"]) {
    assert.match(review, new RegExp(decision), `missing decision ${decision}`);
  }
  assert.match(review, /financeReconciliationChecks/);
  assert.match(review, /"\/review\/"/);
  assert.match(review, /at least 5 characters/, "a review note is mandatory");
  // The page never posts an adjustment or journal to "fix" a check.
  assert.doesNotMatch(review, /financeJournals|acct-journal|ADJUSTMENT/);
});

test("submitted purchase orders are visible with the payable position", () => {
  assert.match(ACCOUNTING, /data-acc-section="purchase-orders"/);
  assert.match(ACCOUNTING, /data-tbody="acct-purchase-orders"/);
  const render = ACCOUNTING.slice(ACCOUNTING.indexOf("function renderPurchaseOrders()"), ACCOUNTING.indexOf("function loadProcurement("));
  assert.match(render, /order\.supplier_name/);
  assert.match(render, /order\.receipt_progress/);
  assert.match(render, /position\.received_value/);
  assert.match(render, /position\.payable_raised/);
  assert.match(render, /position\.payable_outstanding/);
  // The raise control is decided by the server's can_raise_payable flag.
  assert.match(ACCOUNTING, /position\.can_raise_payable/);
  assert.match(ACCOUNTING, /API_ENDPOINTS\.procurementAccountingOrders/);
  assert.match(ACCOUNTING, /Raise payable/);
});

test("a supplier payment has its own proposed/submitted/approved/executed lifecycle", () => {
  assert.match(ACCOUNTING, /data-acc-section="supplier-payments"/);
  assert.match(ACCOUNTING, /data-tbody="acct-supplier-payments"/);
  for (const marker of [
    "data-supplier-payment-propose", "data-supplier-payment-submit",
    "data-supplier-payment-approve", "data-supplier-payment-reject",
    "data-supplier-payment-execute", "data-supplier-payment-cancel",
  ]) {
    assert.ok(
      ACCOUNTING.includes(`event.target.closest ? event.target.closest("[${marker}]")`),
      `no delegated listener for ${marker}`,
    );
  }
  // Approving and executing are offered to different capabilities.
  const actions = ACCOUNTING.slice(
    ACCOUNTING.indexOf("function supplierPaymentActions("),
    ACCOUNTING.indexOf("function renderSupplierPayments("),
  );
  assert.match(actions, /CAN\.approveExpense/);
  assert.match(actions, /CAN\.manageExpenses/);
});

test("the direct settle control stays for an approver and is not shown to everyone", () => {
  // The existing route is preserved (the accounts manager keeps it)...
  assert.match(ACCOUNTING, /data-expense-settle=/);
  assert.match(ACCOUNTING, /async function settleExpense\(reference\)/);
  // ...but a user who cannot approve is offered the payment request instead.
  assert.match(ACCOUNTING, /CAN\.approveExpense[\s\S]{0,120}data-expense-settle/);
  assert.match(ACCOUNTING, /data-supplier-payment-propose/);
});
