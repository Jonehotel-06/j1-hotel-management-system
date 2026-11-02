/* Accounting audit repair — frontend display and Reports-unchanged contracts.
   All monetary values come from the backend as Decimal strings; these tests
   prove the Accounting page renders them rather than recomputing or hiding
   them, and that the Reports page was not touched. */
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { makeContext, makeElement, disposeAll, FRONTEND } = require("./harness");

test.after(() => disposeAll());

const read = (...parts) => fs.readFileSync(path.join(FRONTEND, ...parts), "utf8");
const page = read("dashboard", "accounting.html");
const ledger = read("dashboard", "accounting-ledger.html");
const reports = read("dashboard", "reports.html");
const originalReports = fs.readFileSync(
  path.resolve(FRONTEND, "..", "..", "JONE_ORIGINAL", "frontend", "dashboard", "reports.html"), "utf8"
);

/* ---------------------------------------------------------- static markup */

test("Reports page is byte-identical to the untouched authoritative source", () => {
  assert.equal(reports, originalReports);
  assert.doesNotMatch(reports, /data-pos="gross-guest-receivables"/);
});

test("Accounting publishes separate guest and supplier balance classifications", () => {
  const cards = [
    "gross-guest-receivables", "guest-advances", "unpaid-bills", "partially-paid",
    "fully-paid", "refunded", "supplier-outstanding", "approved-unpaid",
  ];
  for (const name of cards) {
    assert.ok(page.includes(`data-pos="${name}"`), `missing balance card ${name}`);
  }
  assert.match(page, /Guest credit \/ advances/);
  assert.match(page, /Approved, not paid \(procurement\)/);
  assert.match(page, /data-tbody="x-ar-classes"/);
  assert.match(page, /<th[^>]*>Classification<\/th>/);
  // The unreliable legacy roll tiles remain withdrawn.
  for (const tile of ['data-ar="gross"', 'data-ar="credits"', 'data-ar="deposits"']) {
    assert.ok(!page.includes(tile));
  }
});

test("Accounting renders server Decimal strings without browser-side arithmetic", () => {
  assert.match(page, /function setPos\(name,val\)/);
  assert.match(page, /guest\.gross_guest_receivables/);
  assert.match(page, /guest\.guest_advances/);
  assert.match(page, /supBal\.outstanding_payables/);
  assert.match(page, /approved_unpaid_procurement/);
  const setPos = page.slice(page.indexOf("function setPos(name,val){"), page.indexOf("function posAmount("));
  assert.doesNotMatch(setPos, /Number\(|parseFloat\(|Math\./);
});

test("Balance sheet renders the corrected presented rows, not a negative asset", () => {
  assert.match(page, /sheet\.presented_assets \|\| sheet\.assets/);
  assert.match(page, /sheet\.presented_liabilities \|\| sheet\.liabilities/);
  assert.match(page, /sheet\.presented_total_assets/);
  assert.match(page, /sheet\.presented_balanced/);
  assert.match(page, /Guest credit \/ advances \(liability\)/);
  assert.match(page, /Signed AR control balance \(audit only, not a receivable\)/);
  assert.match(page, /Refunded \(posted REFUND transactions\)/);
  assert.match(page, /function bsRow\(label, amount, strong\)/);
});

test("Purchase-order payables distinguish posted liability from draft documents", () => {
  assert.match(page, /position\.posted_payable_liability/);
  assert.match(page, /position\.unposted_payable_documents/);
  assert.match(page, /position\.actual_supplier_payments/);
  assert.match(page, /poPosition\.approved_unpaid_procurement/);
  assert.match(page, /approval_is_not_payment/);
  assert.match(page, /Only POSTED supplier invoices create a liability/);
});

test("Executed supplier settlements have a controlled reversal action", () => {
  assert.match(page, /data-supplier-settlement-reverse=/);
  assert.match(page, /async function reverseSupplierSettlement\(journalReference\)/);
  assert.match(page, /\/api\/admin\/finance\/supplier-settlements\//);
  assert.match(page, /reverse\/"/);
  assert.match(page, /The original payment record is preserved/);
});

test("Ledger preview exposes the new server metric keys", () => {
  for (const key of [
    "guest_receivables_control", "guest_advances_control", "guest_credit_balances",
    "unapplied_guest_deposits", "unpaid_bills", "partially_paid", "refunded_total",
    "supplier_payables_control", "supplier_prepayments",
  ]) {
    assert.ok(ledger.includes(`data-metric="${key}"`), `missing ledger metric ${key}`);
  }
  assert.match(ledger, /Debit folio balances only/);
  assert.match(ledger, /never negative AR/);
});

/* ------------------------------------------------------------ dynamic UI */

function renderFinanceWith(payload) {
  const ctx = makeContext({ pathname: "/dashboard/accounting.html" });
  const nodes = new Map();
  ctx.document.querySelectorAll = (selector) => {
    if (!selector.startsWith("[data-pos=") && !selector.startsWith("[data-rp=")) return [];
    if (!nodes.has(selector)) nodes.set(selector, makeElement("div"));
    return [nodes.get(selector)];
  };
  ctx.document.querySelector = () => null;
  ctx.financeKpis = null;
  ctx.payablesNote = null;
  ctx.financeBasis = null;
  ctx.balanceBasis = null;
  ctx.dt = null;
  ctx.ft = null;
  ctx.latest = { financial: {} };
  ctx.FINANCE_MEASURES = [];
  ctx.JONE = { esc: (v) => String(v), formatNaira: null, ui: { toast() {} } };
  const code = page.slice(
    page.indexOf("  function setKpi(name,val){"),
    page.indexOf("  function renderFinanceError(err){"),
  );
  vm.runInContext(code, ctx, { filename: "accounting.html#finance-render" });
  ctx.renderFinance(payload);
  return (name) => {
    const node = nodes.get(`[data-pos=${name}]`);
    return node ? node.textContent : null;
  };
}

test("Financial overview renders the backend's separated guest position", () => {
  const value = renderFinanceWith({
    currency: "NGN",
    recognized_revenue: { amount: "85000.00", transactions: 1 },
    collections: { amount: "100000.00", transactions: 1 },
    guest_balances: {
      as_of: "2026-11-02", basis: "posted_immutable_ledger",
      gross_guest_receivables: "0.00", guest_credit_balances: "15000.00",
      guest_advances: "15000.00", net_receivable_control: "-15000.00",
      unapplied_guest_deposits: "0.00", reconciles: true,
      unpaid_bills: { folios: 0, amount: "0.00" },
      partially_paid: { folios: 0, amount: "0.00" },
      fully_paid: { folios: 0, amount: "0.00" },
      guest_credit: { folios: 1, amount: "15000.00" },
      refunded: { folios: 0, amount: "0.00" },
      refunded_total: "0.00",
    },
    supplier_balances: {
      basis: "posted_immutable_ledger_and_posted_expense_documents",
      outstanding_payables: "60000.00", actual_supplier_payments: "90000.00",
    },
    supplier_payments: {
      outstanding_supplier_payables: "60000.00",
      supplier_payments_made: "90000.00",
      supplier_invoices_recognized: "150000.00",
      total_cash_disbursements: "90000.00",
      operating_expenses_paid: "0.00", net_payroll_paid: "0.00",
    },
  });
  assert.match(value("gross-guest-receivables"), /0\.00/);
  assert.match(value("guest-advances"), /15,000\.00/);
  assert.match(value("unpaid-bills"), /0\.00/);
  assert.match(value("partially-paid"), /0\.00/);
  assert.match(value("fully-paid"), /^0$/);
  assert.match(value("refunded"), /0\.00/);
  assert.match(value("supplier-outstanding"), /60,000\.00/);
  assert.match(value("gross-guest-receivables-sub"), /-\s*NGN\s*15,000\.00/);
});

function renderArWith(payload) {
  const ctx = makeContext({ pathname: "/dashboard/accounting.html" });
  const bodies = {
    "[data-tbody=x-ar-buckets]": makeElement("tbody"),
    "[data-tbody=x-ar-items]": makeElement("tbody"),
    "[data-tbody=x-ar-classes]": makeElement("tbody"),
  };
  ctx.$ = (selector) => bodies[selector] || null;
  ctx.empty = (cols, text) => `<tr><td colspan="${cols}">${text}</td></tr>`;
  ctx.esc = (v) => String(v == null ? "—" : v);
  ctx.money = (v) => String(v == null ? "—" : v);
  ctx.fail = () => {};
  ctx.EP = { financeArAging: "/api/admin/finance/reports/ar-aging/" };
  ctx.period = () => ({ from: "2026-11-01", to: "2026-11-02" });
  ctx.get = () => ({ then: (fn) => fn(payload) });
  const code = page.slice(
    page.indexOf("  /* Accounting audit repair: human labels"),
    page.indexOf("  /* Sections load only when opened"),
  );
  vm.runInContext(code, ctx, { filename: "accounting.html#ar-loader" });
  ctx.loaders.ar();
  return bodies;
}

test("AR worksheet labels a credit balance GUEST CREDIT, never REFUNDED", () => {
  const bodies = renderArWith({
    buckets: [], total_outstanding: "0.00",
    classification: {
      UNPAID_BILL: { folios: 0, amount: "0.00" },
      PARTIALLY_PAID: { folios: 0, amount: "0.00" },
      FULLY_PAID: { folios: 0, amount: "0.00" },
      GUEST_CREDIT: { folios: 1, amount: "15000.00" },
      REFUNDED: { folios: 0, amount: "0.00" },
      CANCELLED: { folios: 0, amount: "0.00" },
    },
    refunded_total: "0.00",
    items: [{
      booking_reference: "J1-AUDIT", guest: "Audit Guest", rooms: ["101"],
      folio_reference: "FOL-AUDIT-1", total_charges: "85000.00",
      payments_allocated: "100000.00", discounts: "0.00", refunds: "0.00",
      balance_classification: "GUEST_CREDIT", settlement_state: "REFUNDED",
      age_days: 1, balance_due: "0.00", credit_balance: "15000.00",
    }],
  });
  assert.match(bodies["[data-tbody=x-ar-classes]"].innerHTML, /Guest credit \/ advance/);
  assert.match(bodies["[data-tbody=x-ar-classes]"].innerHTML, /15,000|15000/);
  assert.match(bodies["[data-tbody=x-ar-items]"].innerHTML, /Guest credit \/ advance/);
  assert.doesNotMatch(bodies["[data-tbody=x-ar-items]"].innerHTML, /refunded/);
});

test("AR worksheet labels a posted REFUND as REFUNDED", () => {
  const bodies = renderArWith({
    buckets: [], total_outstanding: "10000.00",
    classification: {
      UNPAID_BILL: { folios: 0, amount: "0.00" },
      PARTIALLY_PAID: { folios: 0, amount: "0.00" },
      FULLY_PAID: { folios: 0, amount: "0.00" },
      GUEST_CREDIT: { folios: 0, amount: "0.00" },
      REFUNDED: { folios: 1, amount: "10000.00" },
      CANCELLED: { folios: 0, amount: "0.00" },
    },
    refunded_total: "10000.00",
    items: [{
      booking_reference: "J1-AUDIT-2", guest: "Audit Guest 2", rooms: ["102"],
      folio_reference: "FOL-AUDIT-2", total_charges: "50000.00",
      payments_allocated: "50000.00", discounts: "0.00", refunds: "10000.00",
      balance_classification: "REFUNDED", settlement_state: "UNPAID",
      age_days: 2, balance_due: "10000.00", credit_balance: "0.00",
    }],
  });
  assert.match(bodies["[data-tbody=x-ar-items]"].innerHTML, /Refunded/);
  assert.match(bodies["[data-tbody=x-ar-classes]"].innerHTML, /Refunded/);
});
