/* Expense details drill-down + supplier-payment visibility contract tests.
   Run from /frontend: node --test tests-js/test-expense-details-ui.js */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { makeContext, loadScript, FRONTEND, disposeAll } = require("./harness");

test.after(() => disposeAll());

const PAGE = fs.readFileSync(path.join(FRONTEND, "dashboard", "accounting.html"), "utf8");
const DASHBOARD_JS = fs.readFileSync(path.join(FRONTEND, "js", "dashboard.js"), "utf8");

function loadPageHelpers(ctx) {
  // Run the page's real helpers verbatim: the status map, the row builders and
  // metric()/metricAmount() are sliced straight out of the shipped HTML so the
  // test cannot drift from production code.
  const mapStart = PAGE.indexOf("var EXPENSE_STATUS_PAID =");
  const mapEnd = PAGE.indexOf("function expenseDetailRow(");
  const detailStart = PAGE.indexOf("function expenseDetailRow(");
  const detailEnd = PAGE.indexOf("async function openExpenseDetails(");
  const metricStart = PAGE.indexOf("function metric(report,key){");
  const metricEnd = PAGE.indexOf("function setFinanceKpisLoading(");
  for (const [name, start, end] of [
    ["status map", mapStart, mapEnd], ["detail helpers", detailStart, detailEnd], ["metric helpers", metricStart, metricEnd],
  ]) {
    assert.ok(start > 0 && end > start, `${name} not found in the shipped page`);
  }
  loadScript(ctx, "js/icons.js");
  loadScript(ctx, "js/dashboard.js");
  const pageDefaults = {};
  ctx.window.JONE.dashboard = ctx.window.JONE.dashboard || {};
  // The page renders status through the shared statusPill; keep that real
  // implementation but make it total for an empty status value.
  ctx.window.JONE.dashboard.statusPill = (value) => `<span class="pill">${ctx.window.JONE.esc(value || "\u2014")}</span>`;
  vm.runInContext(
    "var latest={financial:null};function reportCurrency(){return 'NGN';}"
    + "function financeErrorMessage(e){return (e&&e.message)||'Request failed.';}"
    + "function moneyCell(v){return JONE.formatNaira(v);}",
    ctx,
  );
  vm.runInContext(PAGE.slice(mapStart, mapEnd), ctx, { filename: "accounting.html#status-map" });
  vm.runInContext(PAGE.slice(detailStart, detailEnd), ctx, { filename: "accounting.html#detail-helpers" });
  vm.runInContext(PAGE.slice(metricStart, metricEnd), ctx, { filename: "accounting.html#metric-helpers" });
  const win = ctx.window;
  for (const name of ["expenseDetailHtml", "expenseDetailRow", "expenseDetailText", "metric", "metricAmount", "metricTransactions", "transactionLabel"]) {
    assert.equal(typeof win[name], "function", `${name} was not loaded from the page`);
  }
  return win;
}

test("the shared read-only dialog host is exported and drives the drill-down", () => {
  const ctx = makeContext();
  loadScript(ctx, "js/config.js");
  loadScript(ctx, "js/utils.js");
  loadScript(ctx, "js/icons.js");
  loadScript(ctx, "js/dashboard.js");
  assert.equal(typeof ctx.window.JONE.dashboard.panel.open, "function");
  assert.equal(typeof ctx.window.JONE.dashboard.panel.close, "function");
  assert.equal(typeof ctx.window.JONE.dashboard.panel.body, "function");
  // The host keeps the one ESC/backdrop/focus contract every dialog shares.
  assert.match(DASHBOARD_JS, /function panelKeydown\(e\) \{[\s\S]{0,300}lightbox\.wrap\.classList\.contains\("open"\)/);
  assert.match(DASHBOARD_JS, /document\.addEventListener\("keydown", panelKeydown, true\)/);
  assert.match(DASHBOARD_JS, /data-panel-close/);
  assert.match(DASHBOARD_JS, /aria-modal="true"/);
  // ... and the page uses it rather than building its own modal markup.
  assert.match(PAGE, /JONE\.dashboard\.panel\.open\(title,/);
  assert.doesNotMatch(PAGE, /class="modal-backdrop"/);
});

test("the drill-down endpoint is centralized and the reference is escaped by the API helper", async () => {
  const ctx = makeContext();
  loadScript(ctx, "js/config.js");
  assert.equal(ctx.window.APP_CONFIG.API_ENDPOINTS.financeExpenseDetail, "/api/admin/finance/expenses/");
  const requests = [];
  ctx.fetch = (url) => {
    requests.push(url);
    return Promise.resolve({
      status: 200, ok: true,
      headers: { get: (k) => (k === "content-type" ? "application/json" : null) },
      json: () => Promise.resolve({ success: true, data: {} }),
      text: () => Promise.resolve("{}"),
    });
  };
  loadScript(ctx, "js/api.js");
  await ctx.window.API.getOne("financeExpenseDetail", "EXP/2026/0001");
  // The trailing slash is preserved for Django; the reference itself is
  // percent-encoded, so a slash cannot escape the route.
  assert.equal(requests[0], "/api/admin/finance/expenses/EXP%2F2026%2F0001/");
});

test("every field the brief lists is rendered, including the paid/incurred distinction", () => {
  const ctx = makeContext();
  loadScript(ctx, "js/utils.js");
  loadScript(ctx, "js/config.js");
  const win = loadPageHelpers(ctx);
  const html = win.expenseDetailHtml({
    reference: "EXP-1", status: "POSTED", category: "Supplies", accounting_class: "OPERATING",
    payment_method: "CASH", amount: "125.50", currency: "NGN", incurred_on: "2026-10-01",
    payee: "Acme Ltd", supplier_reference: "SUP-9", description: "Emergency supplies", notes: "Checked",
    receipt_url: "https://files.example/receipt.pdf", requested_by_email: "a@staff.dev",
    approved_by_email: "b@staff.dev", approved_at: "2026-10-02T09:00:00Z", approval_reference: "APR-1",
    cash_session_reference: "CSH-1", posted_by_email: "c@staff.dev", posted_at: "2026-10-02T10:00:00Z",
    financial_reference: "FIN-1", created_at: "2026-10-01T08:00:00Z",
    events: [{ type: "APPROVED", actor_email: "b@staff.dev", created_at: "2026-10-02T09:00:00Z", details: { ok: true } }],
  }, "NGN");
  for (const label of [
    "Amount", "Currency", "Status", "Category", "Accounting class", "Payment method", "Payee / supplier",
    "Supplier reference", "Incurred on", "Requested by", "Approved by", "Approved at",
    "Approval reference", "Cash session", "Posted by", "Posted at", "Financial transaction", "Created",
    "Description", "Notes", "Attachment", "Audit history",
  ]) {
    assert.ok(html.includes(`>${label}<`), `details dialog is missing: ${label}`);
  }
  // Incurred vs paid is stated explicitly, not left to the reader.
  assert.match(html, /Money has moved for this expense/);
  assert.match(html, /It cannot be paid a second time/);
  // The attachment is a real link (and it opens safely in a new tab).
  assert.match(html, /href="https:\/\/files\.example\/receipt\.pdf"/);
  assert.match(html, /rel="noopener noreferrer"/);
  // The append-only history is rendered as a timeline with actor and timestamp.
  assert.match(html, /detail-timeline/);
  assert.match(html, /b@staff\.dev/);
});

test("an expense that has not been paid says so instead of implying payment", () => {
  const ctx = makeContext();
  loadScript(ctx, "js/utils.js");
  loadScript(ctx, "js/config.js");
  const win = loadPageHelpers(ctx);
  const html = win.expenseDetailHtml({ reference: "EXP-2", status: "SUBMITTED", amount: "10.00", events: [] }, "NGN");
  assert.match(html, /no money has left the hotel for it yet/);
  assert.doesNotMatch(html, /cannot be paid a second time/);
  assert.match(html, /No audit events recorded for this expense\./);
});

test("values are HTML-escaped so a payee or note cannot inject markup", () => {
  const ctx = makeContext();
  loadScript(ctx, "js/utils.js");
  loadScript(ctx, "js/config.js");
  const win = loadPageHelpers(ctx);
  const html = win.expenseDetailHtml({
    reference: "EXP-3", status: "POSTED", amount: "5.00", payee: "<img src=x onerror=alert(1)>",
    description: "<script>alert(1)</script>", events: [],
  }, "NGN");
  assert.doesNotMatch(html, /<img src=x onerror/);
  assert.doesNotMatch(html, /<script>alert\(1\)<\/script>/);
  assert.match(html, /&lt;/);
});

test("a record with no attachment says so instead of rendering a dangling link", () => {
  const ctx = makeContext();
  loadScript(ctx, "js/utils.js");
  loadScript(ctx, "js/config.js");
  const win = loadPageHelpers(ctx);
  const html = win.expenseDetailHtml({ reference: "EXP-5", status: "DRAFT", amount: "1.00", events: [] }, "NGN");
  assert.match(html, /No attachment on this record\./);
  assert.doesNotMatch(html, /<a class="btn btn-sm btn-outline"/);
});

test("a null or missing figure renders as a dash, never as a fabricated zero", () => {
  const ctx = makeContext();
  loadScript(ctx, "js/utils.js");
  loadScript(ctx, "js/config.js");
  const win = loadPageHelpers(ctx);
  assert.equal(win.expenseDetailText(null), "\u2014");
  assert.equal(win.expenseDetailText(""), "\u2014");
  assert.equal(win.expenseDetailText(undefined), "\u2014");
  assert.equal(win.expenseDetailText("NGN"), "NGN");
  const html = win.expenseDetailHtml({ reference: "EXP-4", status: "DRAFT", amount: "0.00", events: [] }, "NGN");
  // A genuine zero still renders as zero money.
  assert.match(html, /\u20a60\.00/);
});

test("supplier-payment stages are read from the nested block with dotted paths", () => {
  const ctx = makeContext();
  loadScript(ctx, "js/config.js");
  const win = loadPageHelpers(ctx);
  const report = {
    recognized_revenue: { amount: "500.00", transactions: 2 },
    supplier_payments: {
      supplier_invoices_recognized: "250000.00",
      supplier_payments_made: "100000.00",
      outstanding_supplier_payables: "150000.00",
      cost_of_goods_sold: null,
      payable_settlement_posting_available: false,
    },
  };
  assert.equal(win.metricAmount(report, "supplier_payments.supplier_payments_made"), "100000.00");
  assert.equal(win.metricAmount(report, "supplier_payments.supplier_invoices_recognized"), "250000.00");
  // An existing top-level measure still resolves exactly as before.
  assert.equal(win.metricAmount(report, "recognized_revenue"), "500.00");
  // An absent nested measure resolves to undefined, not to a zero amount.
  assert.equal(win.metricAmount(report, "supplier_payments.never_posted"), undefined);
});

test("the accounting page distinguishes every payment stage and flags the settlement gap", () => {
  for (const label of [
    "Supplier invoices recognized", "Supplier payments made", "Operating expenses paid (cash out)",
    "Other cash disbursements", "Supplier payables raised", "Outstanding supplier payables",
  ]) {
    assert.ok(PAGE.includes(label), `payment stage not surfaced: ${label}`);
  }
  assert.match(PAGE, /Supplier payments made/);
  assert.match(PAGE, /Outstanding payables/);
  // The absent payable-settlement posting is disclosed, not hidden.
  assert.match(PAGE, /no settlement posting exists yet/);
});

test("the expense table gets a Details action that is keyboard reachable", () => {
  assert.match(PAGE, /function rowActions\(row\)/);
  assert.match(PAGE, /<button class=\\"btn btn-sm btn-outline\\" type=\\"button\\" data-expense-details=\\"/);
  assert.match(PAGE, /aria-label=\\?"View details for expense /);
  assert.match(PAGE, /event\.target\.closest\(\"\[data-expense-details\]\"\)/);
  // The empty state spans the new column count.
  assert.match(PAGE, /colspan=\\?"5\\?"/);
});

test("the details dialog never offers a second payment action", () => {
  // No pay/post/re-approve control is rendered inside the drill-down.
  const start = PAGE.indexOf("function expenseDetailHtml(");
  const end = PAGE.indexOf("async function openExpenseDetails(");
  const dialog = PAGE.slice(start, end);
  for (const forbidden of ["data-expense-post", "data-expense-review", "data-expense-pay"]) {
    assert.ok(!dialog.includes(forbidden), `dialog must not offer ${forbidden}`);
  }
  assert.match(dialog, /cannot be paid a second time/);
});
