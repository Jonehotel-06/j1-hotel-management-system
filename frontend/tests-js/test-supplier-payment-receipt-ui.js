/* Supplier transfer-receipt flow: file the proof of a payment that has already
   left the bank, and say plainly which expenses still need a payment recorded.
   Run from /frontend: node --test tests-js/test-supplier-payment-receipt-ui.js */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { makeContext, loadScript, FRONTEND, disposeAll } = require("./harness");

test.after(() => disposeAll());

const PAGE = fs.readFileSync(path.join(FRONTEND, "dashboard", "accounting.html"), "utf8");

function loadDetailHelpers(ctx) {
  const mapStart = PAGE.indexOf("var EXPENSE_STATUS_PAID =");
  const detailStart = PAGE.indexOf("function expenseDetailRow(");
  const detailEnd = PAGE.indexOf("async function openExpenseDetails(");
  assert.ok(mapStart > 0 && detailStart > mapStart && detailEnd > detailStart, "detail helpers not found");
  loadScript(ctx, "js/icons.js");
  loadScript(ctx, "js/dashboard.js");
  ctx.window.JONE.dashboard.statusPill = (value) => `<span class="pill">${ctx.window.JONE.esc(value || "\u2014")}</span>`;
  vm.runInContext("function moneyCell(v){return JONE.formatNaira(v);}", ctx);
  vm.runInContext(PAGE.slice(mapStart, detailEnd), ctx, { filename: "accounting.html#detail-helpers" });
  return ctx.window;
}

test("a posted expense that will be paid later is sent to the settlement step", () => {
  // On-account and unpaid: the payable has to be released once, by the payment.
  assert.match(PAGE, /data-expense-settle/);
  assert.match(PAGE, /row\.payment_method === "PAYABLE" && !row\.paid_at/);
  assert.match(PAGE, /Record supplier payment/);
});

test("a settled or already-paid expense can still have its transfer receipt filed", () => {
  // Money that left at posting (bank / cash / card) or an already-settled
  // payable: no posting is left, but the proof can be attached.
  assert.match(PAGE, /data-expense-receipt/);
  assert.match(PAGE, /async function attachPaymentEvidence\(reference\)/);
  assert.match(PAGE, /"\/payment-evidence\/"/);
  // The evidence controls are on the expense tables, not inside the read-only
  // drill-down: the drill-down still offers no payment action of its own.
  const dialog = PAGE.slice(PAGE.indexOf("function expenseDetailHtml("), PAGE.indexOf("async function openExpenseDetails("));
  for (const forbidden of ["data-expense-receipt", "data-expense-settle", "data-expense-post"]) {
    assert.ok(!dialog.includes(forbidden), `the read-only dialog must not offer ${forbidden}`);
  }
});

test("the receipt dialog can take a file, a link, a reference and the date paid", () => {
  const start = PAGE.indexOf("async function attachPaymentEvidence(reference)");
  const end = PAGE.indexOf("/* ---------------- Financial statements", start);
  const body = PAGE.slice(start, end);
  assert.ok(body.length > 0, "attachPaymentEvidence not found");
  assert.match(body, /name: "receipt"/);
  assert.match(body, /name: "receipt_url"/);
  assert.match(body, /name: "payment_reference"/);
  assert.match(body, /name: "paid_on"/);
  assert.match(body, /multipart: true/);
  // Something must be on the record, and the page says so instead of posting an
  // empty record.
  assert.match(body, /Attach the receipt file, paste a receipt link, or enter the transfer reference\./);
  // It never claims to have moved money.
  assert.match(body, /No new posting was made/);
});

test("the create dialog separates money already out from money still owed", () => {
  assert.match(PAGE, /Bank transfer — already paid \(money left the bank\)/);
  assert.match(PAGE, /On account \/ not paid yet \(waiting to be paid\)/);
  assert.match(PAGE, /Supplier accounts and payables, where you record the payment and file the transfer receipt/);
  // The supplier's own document can be attached at creation, through the
  // existing receipt_url field the API already accepts.
  assert.match(PAGE, /name: "receipt_url", label: "Supplier invoice \/ receipt link \(optional\)"/);
  assert.match(PAGE, /if \(values\.receipt_url\) payload\.receipt_url = String\(values\.receipt_url\)\.trim\(\);/);
});

test("the payables section explains the two-step flow in the backend's own words", () => {
  assert.match(PAGE, /data-payables-note/);
  assert.match(PAGE, /supplier\.payable_settlement_posting_note/);
  assert.match(PAGE, /payablesNote\.textContent = supplier\.payable_settlement_posting_note|payablesNote\.textContent=supplier\.payable_settlement_posting_note/);
  assert.match(PAGE, /Outstanding now: /);
});

test("the drill-down shows every attachment and never implies an unpaid bank expense", () => {
  const ctx = makeContext();
  loadScript(ctx, "js/utils.js");
  loadScript(ctx, "js/config.js");
  const win = loadDetailHelpers(ctx);

  const bankExpense = win.expenseDetailHtml({
    reference: "EXP-9", status: "POSTED", amount: "50000.00", currency: "NGN",
    payment_method: "BANK_TRANSFER", receipt_url: "https://supplier.example/invoice.pdf",
    payment_receipt_file_url: "/media/finance_receipts/2026/10/slip.png",
    payment_receipt_url: "https://bank.example.com/receipt/ABC", payment_reference: "TRF-99887766",
    events: [],
  }, "NGN");
  assert.match(bankExpense, /Supplier document/);
  assert.match(bankExpense, /href="https:\/\/supplier\.example\/invoice\.pdf"/);
  assert.match(bankExpense, /Payment receipt file/);
  assert.match(bankExpense, /href="\/media\/finance_receipts\/2026\/10\/slip\.png"/);
  assert.match(bankExpense, /Payment receipt link/);
  // A bank-transfer expense is settled by the ledger at posting: the dialog says
  // so instead of leaving the reader to guess from a dash.
  assert.match(bankExpense, /Paid at posting — BANK_TRANSFER/);
  assert.match(bankExpense, /no separate supplier payment to record/);

  const onAccount = win.expenseDetailHtml({
    reference: "EXP-10", status: "POSTED", amount: "1000.00", currency: "NGN",
    payment_method: "PAYABLE", events: [],
  }, "NGN");
  assert.match(onAccount, /Not yet paid — on account/);
  assert.doesNotMatch(onAccount, /Paid at posting/);

  const paid = win.expenseDetailHtml({
    reference: "EXP-11", status: "POSTED", amount: "1000.00", currency: "NGN",
    payment_method: "PAYABLE", paid_at: "2026-10-14T09:00:00Z", paid_by_email: "acct@staff.dev", events: [],
  }, "NGN");
  assert.match(paid, /Paid /);
  assert.match(paid, /acct@staff\.dev/);
  assert.match(paid, /No attachment on this record\./);

  // With nothing attached at all the dialog still says there is nothing.
  const bare = win.expenseDetailHtml({ reference: "EXP-12", status: "DRAFT", amount: "5.00", events: [] }, "NGN");
  assert.match(bare, /No attachment on this record\./);
});
