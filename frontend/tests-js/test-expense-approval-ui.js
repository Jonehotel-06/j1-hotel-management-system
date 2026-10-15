/* Controlled-expense decision UI contract (Accounting).
   The expense workflow is submit -> approve -> post -> pay. Approve and post
   existed only on the API, so a submitted expense sat on the Accounting screen
   with no way forward for the administrator who had to decide it. These tests
   pin the decision controls, their capability gating and the endpoint each
   decision calls, so the gap cannot silently reopen.

   Run from /frontend: node --test tests-js/test-expense-approval-ui.js */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const FRONTEND = path.resolve(__dirname, "..");
const ACCOUNTING = fs.readFileSync(path.join(FRONTEND, "dashboard", "accounting.html"), "utf8");

/* The row-action builder and the approvals builder are the two surfaces that
   offer a decision. Both are sliced out of the shipped page so these
   assertions cannot drift from production code. */
function slice(startMarker, endMarker) {
  const start = ACCOUNTING.indexOf(startMarker);
  const end = ACCOUNTING.indexOf(endMarker, start + 1);
  assert.ok(start > 0 && end > start, `${startMarker} not found in the shipped page`);
  return ACCOUNTING.slice(start, end);
}

const ROW_ACTIONS = slice("function rowActions(row) {", "/* One delegated listener");
const APPROVAL_ACTIONS = slice("function approvalActions(row){", "function renderApprovals(){");

test("a submitted expense offers approve and reject, gated on the approval capability", () => {
  assert.match(ROW_ACTIONS, /row\.status === "SUBMITTED" && CAN\.approveExpense/);
  assert.match(ROW_ACTIONS, /data-expense-approve=/);
  assert.match(ROW_ACTIONS, /data-expense-reject=/);
  // A viewer without expense.approve is offered no decision control at all.
  assert.doesNotMatch(ROW_ACTIONS, /row\.status === "SUBMITTED" && \(true|1\)/);
});

test("an approved expense can be posted to the ledger from the row", () => {
  assert.match(ROW_ACTIONS, /row\.status === "APPROVED"/);
  assert.match(ROW_ACTIONS, /data-expense-post=/);
  // Posting is never offered for a record that is not approved.
  assert.doesNotMatch(ROW_ACTIONS, /data-expense-post[\s\S]{0,80}row\.status === "DRAFT"/);
});

test("the draft and settlement steps still behave exactly as before", () => {
  assert.match(ROW_ACTIONS, /row\.status === "DRAFT"[\s\S]*?data-expense-submit=/);
  assert.match(ROW_ACTIONS, /row\.status === "POSTED" && row\.payment_method === "PAYABLE" && !row\.paid_at/);
  assert.match(ROW_ACTIONS, /data-expense-settle=/);
});

test("approving and rejecting call the expense review endpoint with an explicit decision", () => {
  const review = slice("async function reviewExpense(reference, approved)", "/* Posting an approved expense");
  assert.match(review, /window\.API\.resourceAction\("financeExpenses", reference, "review", \{/);
  assert.match(review, /approved: !!approved, review_note: reviewNote/);
});

test("a rejection demands a reason because the reason is the audit evidence", () => {
  const review = slice("async function reviewExpense(reference, approved)", "/* Posting an approved expense");
  assert.match(review, /label: "Why is this expense rejected\?"/);
  assert.match(review, /required: true/);
  assert.match(review, /if \(!reviewNote\) throw new Error\("A rejection reason is required\."\)/);
});

test("posting is an explicit money movement and calls the post action", () => {
  const post = slice("async function postExpense(reference)", "/* Registry approvals");
  assert.match(post, /window\.API\.resourceAction\("financeExpenses", reference, "post", \{\}\)/);
  assert.match(post, /This cannot be undone/);
});

test("an EXPENSE approval is decided with its expense reference, not the approval reference", () => {
  assert.match(APPROVAL_ACTIONS, /type==="EXPENSE"/);
  assert.match(APPROVAL_ACTIONS, /var expenseRef=JONE\.esc\(row\.source_reference\|\|""\)/);
  // The decision targets the expense the approval is about.
  assert.ok(APPROVAL_ACTIONS.includes('data-expense-approve=\\""+expenseRef'));
  assert.ok(APPROVAL_ACTIONS.includes('data-expense-reject=\\""+expenseRef'));
});

test("a cash variance is never routed through the approvals endpoint from this table", () => {
  assert.match(APPROVAL_ACTIONS, /type==="CASH_VARIANCE"/);
  assert.match(APPROVAL_ACTIONS, /Review with the cash drawer/);
  // The variance branch returns before any decision button is composed, so a
  // cash variance can never be decided here (the backend refuses it too).
  const varianceBranch = APPROVAL_ACTIONS.slice(
    APPROVAL_ACTIONS.indexOf('type==="CASH_VARIANCE"'),
    APPROVAL_ACTIONS.indexOf('if(type==="EXPENSE")'),
  );
  assert.doesNotMatch(varianceBranch, /data-approval-approve|data-approval-reject/);
});

test("other registry approvals use the approval's own reference and the refunds capability", () => {
  const tail = APPROVAL_ACTIONS.slice(APPROVAL_ACTIONS.indexOf('if(!CAN.refunds)'));
  assert.ok(tail.includes('data-approval-approve=\\""+reference'));
  assert.ok(tail.includes('data-approval-reject=\\""+reference'));
  const generic = slice("async function reviewApproval(reference, approved)", "/* ---------------- Supplier payment settlement");
  assert.match(generic, /window\.API\.resourceAction\("financeApprovals", reference, "review", \{/);
});

test("decision buttons fail closed until the server-resolved capabilities arrive", () => {
  assert.match(ACCOUNTING, /var CAN = \{ approveExpense:false, refunds:false \};/);
  assert.match(ACCOUNTING, /window\.Auth\.hasCapability\("expense\.approve"\)/);
  assert.match(ACCOUNTING, /window\.Auth\.hasCapability\("payment\.refund\.approve"\)/);
  // The tables re-render when the capability list lands, so a reviewer does
  // not have to reload the page to see their own decision buttons.
  assert.match(ACCOUNTING, /if\(changed\)\{ renderExpenses\(\); renderApprovals\(\); \}/);
  assert.match(ACCOUNTING, /\n  capabilityReady\(\);/);
});

test("the approvals table renders an actions column sized to the new cell", () => {
  assert.match(ACCOUNTING, /<th class="cell-nowrap"><span class="sr-only">Actions<\/span><\/th><\/tr><\/thead>\s*<tbody data-tbody="acct-approvals">/);
  assert.match(ACCOUNTING, /colspan=\\"6\\"><div class=\\"empty-state\\"><h3>No pending approvals/);
});

test("every decision control is wired through a delegated listener", () => {
  // Rows are re-rendered on every load, so each control needs the delegated
  // pattern the page already uses for Submit/Details rather than a per-row
  // listener that would leak and go stale.
  for (const attr of [
    "data-expense-approve", "data-expense-reject", "data-expense-post",
    "data-approval-approve", "data-approval-reject", "data-approval-expense",
  ]) {
    assert.ok(
      ACCOUNTING.includes(`event.target.closest ? event.target.closest("[${attr}]")`),
      `no delegated listener for ${attr}`,
    );
  }
});
