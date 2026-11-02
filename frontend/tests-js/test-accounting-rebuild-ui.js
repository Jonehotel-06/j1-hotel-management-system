/* Focused UI tests for the additive ledger preview and safe checkout retries. */
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { makeContext, makeElement, loadScript, disposeAll, FRONTEND } = require("./harness");
test.after(() => disposeAll());
const tick = () => new Promise((resolve) => setImmediate(resolve));

function checkoutController() {
  const page = fs.readFileSync(path.join(FRONTEND, "dashboard", "check-out.html"), "utf8");
  const helpers = page.slice(page.indexOf("  function settlementStorageKey(ref)"), page.indexOf("  function renderClearance(body"));
  const handler = page.slice(page.indexOf("  async function recordSettlement("), page.indexOf("  /* The gate pass is a POS thermal receipt"));
  assert.ok(helpers.length && handler.length);
  const ctx = makeContext({pathname: "/dashboard/check-out.html"});
  ctx.window.crypto = {randomUUID: () => "00000000-1111-4222-8333-444444444444"};
  ctx.window.Auth = {state: {user: {email: "desk@example.test"}}};
  ctx.window.JONE = {ui: {toast() {}}};
  ctx.renderClearance = () => {};
  const requests = [];
  let response = () => Promise.reject({status: 0, message: "timeout"});
  ctx.window.API = {settleBookingBalance(ref, body) { requests.push({ref, body}); return response(); }};
  const fields = {
    "[data-settle-status]": makeElement("p"),
    "#co-settle-amount": makeElement("input"),
    "#co-settle-method": makeElement("select"),
    "#co-settle-drawer": makeElement("input"),
    "#co-settle-external": makeElement("input"),
    "#co-settle-notes": makeElement("input")
  };
  fields["#co-settle-amount"].value = "1500.00";
  fields["#co-settle-method"].value = "CASH";
  fields["#co-settle-drawer"].value = "DRAWER-1";
  const body = {querySelector: (selector) => fields[selector]};
  const button = makeElement("button");
  vm.runInContext(helpers + handler, ctx);
  return {ctx, body, button, fields, requests, respond(fn) { response = fn; }};
}

test("unknown checkout outcome retains the exact key and blocks changed or different-staff retries", async () => {
  const c = checkoutController();
  await c.ctx.recordSettlement(c.body, "BOOK-1", {}, {}, c.button);
  assert.equal(c.requests.length, 1);
  const first = c.requests[0].body;
  assert.match(first.idempotency_key, /^desk-/);
  assert.ok(c.ctx.sessionStorage.getItem("jone.checkout.pending.BOOK-1"));
  assert.match(c.fields["[data-settle-status]"].textContent, /outcome is unknown/i);
  c.fields["#co-settle-amount"].value = "1600.00";
  await c.ctx.recordSettlement(c.body, "BOOK-1", {}, {}, c.button);
  assert.equal(c.requests.length, 1);
  c.fields["#co-settle-amount"].value = "1500.00";
  c.ctx.Auth.state.user.email = "other@example.test";
  await c.ctx.recordSettlement(c.body, "BOOK-1", {}, {}, c.button);
  assert.equal(c.requests.length, 1);
  c.ctx.Auth.state.user.email = "desk@example.test";
  c.respond(() => Promise.resolve({data: {folio: {}, recorded_by: "desk@example.test"}}));
  await c.ctx.recordSettlement(c.body, "BOOK-1", {}, {}, c.button);
  assert.equal(c.requests.length, 2);
  assert.equal(c.requests[1].body.idempotency_key, first.idempotency_key);
  assert.equal(c.ctx.sessionStorage.getItem("jone.checkout.pending.BOOK-1"), null);
});

test("checkout requires real bank references and clears pending state for definitive rejection", async () => {
  const c = checkoutController();
  c.fields["#co-settle-method"].value = "BANK_TRANSFER";
  c.fields["#co-settle-drawer"].value = "";
  await c.ctx.recordSettlement(c.body, "BOOK-2", {}, {}, c.button);
  assert.equal(c.requests.length, 0);
  assert.match(c.fields["[data-settle-status]"].textContent, /actual POS or bank/i);
  c.fields["#co-settle-external"].value = "TRF-42";
  c.respond(() => Promise.reject({status: 400, message: "rejected"}));
  await c.ctx.recordSettlement(c.body, "BOOK-2", {}, {}, c.button);
  assert.equal(c.requests.length, 1);
  assert.equal(c.requests[0].body.external_reference, "TRF-42");
  assert.equal(c.ctx.sessionStorage.getItem("jone.checkout.pending.BOOK-2"), null);
});

test("ledger preview does not request financial data when the page guard fails", () => {
  const ctx = makeContext({pathname: "/dashboard/accounting-ledger.html"});
  let calls = 0;
  ctx.JONE = {ui: {initChrome() {}}, dashboard: {boot: () => false}};
  ctx.__ledgerAllowed = false;
  ctx.API = {get() { calls += 1; }};
  loadScript(ctx, "js/accounting-rebuild.js");
  assert.equal(calls, 0);
});

test("ledger preview displays server-provided balances and warnings", async () => {
  const ctx = makeContext({pathname: "/dashboard/accounting-ledger.html"});
  const ids = ["ledger-start", "ledger-end", "ledger-report-type", "ledger-account", "ledger-account-wrap",
    "ledger-report-status", "ledger-table-body", "ledger-table-head", "ledger-report-totals", "ledger-cards",
    "ledger-warning", "ledger-prev", "ledger-next", "ledger-page-label", "ledger-apply", "ledger-load-report"];
  const nodes = Object.fromEntries(ids.map((id) => ["#" + id, makeElement("div")]));
  Object.values(nodes).forEach((el) => { el.replaceChildren = function () { this.children = []; }; });
  nodes["#ledger-report-type"].value = "profit-loss";
  nodes["#ledger-account"].value = "ASSET.CASH_ON_HAND";
  const metric = makeElement("strong"); metric.dataset.metric = "guest_advances_control";
  ctx.document.querySelector = (selector) => nodes[selector] || null;
  ctx.document.querySelectorAll = (selector) => selector === "[data-metric]" ? [metric] : [];
  const create = ctx.document.createElement.bind(ctx.document);
  ctx.document.createElement = (tag) => { const el = create(tag); el.replaceChildren = function(){ this.children = []; }; return el; };
  ctx.document.createTextNode = (text) => ({textContent: text});
  ctx.JONE = {ui: {initChrome() {}}, dashboard: {boot: () => true}};
  ctx.__ledgerAllowed = true;
  const requests = [];
  ctx.API = {get(url, options) {
    requests.push({url, options});
    if (url.endsWith("accounts/")) return Promise.resolve({data: {accounts: [{code:"ASSET.CASH_ON_HAND", label:"Cash"}]}});
    if (url.endsWith("overview/")) return Promise.resolve({data: {guest_advances_control:"15000.00", warnings:["PREVIEW ONLY"]}});
    return Promise.resolve({data: {records: [{code:"REVENUE.FOOD_BEVERAGE", account:"Food", amount:"2500.00"}], totals: {net_profit:"2500.00"}, warnings:["Reconciling"]}});
  }};
  loadScript(ctx, "js/accounting-rebuild.js");
  await tick(); await tick();
  assert.match(metric.textContent, /15,000\.00/);
  assert.match(nodes["#ledger-warning"].textContent, /PREVIEW ONLY/);
  assert.equal(requests.filter((r) => r.url.endsWith("overview/")).length, 1);
  assert.equal(nodes["#ledger-table-body"].children.length, 1);
});


test("supplier proposal exposes partial amounts without claiming the whole bill is paid", () => {
  const page = fs.readFileSync(path.join(FRONTEND, "dashboard", "accounting.html"), "utf8");
  const proposal = page.slice(page.indexOf("async function proposeSupplierPayment("), page.indexOf("async function submitSupplierPaymentRequest("));
  assert.match(proposal, /name: "amount"/);
  assert.match(proposal, /amount: values\.amount \|\| null/);
  assert.match(proposal, /idempotency_key: proposalKey/);
  const ctx = makeContext();
  loadScript(ctx, "js/utils.js"); loadScript(ctx, "js/config.js"); loadScript(ctx, "js/icons.js"); loadScript(ctx, "js/dashboard.js");
  ctx.JONE.dashboard.statusPill = (value) => String(value || "");
  vm.runInContext("function moneyCell(v){return JONE.formatNaira(v);}", ctx);
  vm.runInContext(page.slice(page.indexOf("function expenseDetailRow("), page.indexOf("function load(){")), ctx);
  vm.runInContext(page.slice(page.indexOf("var EXPENSE_STATUS_PAID ="), page.indexOf("async function openExpenseDetails(")), ctx);
  const html = ctx.expenseDetailHtml({reference:"EXP-1", status:"POSTED", payment_method:"PAYABLE", amount:"200000.00",
    paid_amount:"100000.00", outstanding_amount:"100000.00", payment_records:[{paid_on:"2026-11-01", amount:"100000.00", method:"BANK_TRANSFER", payment_reference:"TRF-1"}], events:[]}, "NGN");
  assert.match(html, /Partially paid — balance remains on account/);
  assert.match(html, /Paid so far/);
  assert.match(html, /Outstanding/);
  assert.match(html, /TRF-1/);
  assert.doesNotMatch(html, /It cannot be paid a second time/);
});
