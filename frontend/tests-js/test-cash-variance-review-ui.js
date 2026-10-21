/* Cash-drawer variance decision UI (Accounting).

   Reported: "no button to approve cash collected in accounting". A material
   variance on drawer close puts the session in PENDING_REVIEW; the API could
   already decide it (POST /api/admin/finance/cash-sessions/<ref>/variance-review/)
   but no page offered the button, so the drawer could never close and the
   counted cash stayed unreconciled.

   These tests pin the control, its capability gate, the exact endpoint it uses
   and the evidence a rejection must carry — so the gap cannot reopen and no
   second, invented cash route can appear.

   Run from /frontend: node --test tests-js/test-cash-variance-review-ui.js */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const FRONTEND = path.resolve(__dirname, "..");
const ACCOUNTING = fs.readFileSync(path.join(FRONTEND, "dashboard", "accounting.html"), "utf8");

function slice(startMarker, endMarker) {
  const start = ACCOUNTING.indexOf(startMarker);
  const end = ACCOUNTING.indexOf(endMarker, start + 1);
  assert.ok(start > 0 && end > start, `${startMarker} not found in the shipped page`);
  return ACCOUNTING.slice(start, end);
}

const CELL = slice("function cashVarianceCell(row){", "function renderCash(){");
const RENDER = slice("function renderCash(){", "/* The approvals section, decided by capability.");
const REVIEW = slice("async function reviewCashVariance(reference){", "document.addEventListener(\"click\"");
const APPROVALS = slice("function approvalActions(row){", "function renderApprovals(){");

test("the cash table renders the drawer decision, and only for a session awaiting one", () => {
  assert.match(CELL, /status!=="PENDING_REVIEW"[\s\S]*?caption[\s\S]*?—/);
  assert.match(CELL, /data-cash-variance=/);
  // An open or closed drawer offers no decision control.
  assert.doesNotMatch(CELL, /status==="OPEN"[\s\S]{0,80}data-cash-variance/);
});

test("only a reviewer is offered the button; everyone else is told why it is not theirs", () => {
  assert.match(CELL, /if\(!CAN\.reviewDrawer\) return '<span class="caption">Manager review required<\/span>'/);
  assert.match(ACCOUNTING, /reviewDrawer:window\.Auth\.hasCapability\("payment\.refund\.approve"\)/);
  // Capability arrival re-renders the table without a page reload.
  assert.match(ACCOUNTING, /if\(changed\)\{ renderExpenses\(\); renderApprovals\(\); renderCash\(\); refreshApprovalsSection\(\); \}/);
});

test("the table keeps the authoritative drawer columns and adds the decision column", () => {
  for (const header of ["Reference", "Cashier", "Expected", "Counted", "Variance", "Status"]) {
    assert.match(ACCOUNTING, new RegExp(`<th[^>]*>${header}</th>`), `cash table lost the ${header} column`);
  }
  assert.match(RENDER, /cell-nowrap\\">"\+JONE\.esc\(row\.status/);
  assert.match(RENDER, /cashVarianceCell\(row\)/);
});

test("the decision goes to the controlled cash-session route, not a new one", () => {
  assert.match(REVIEW, /window\.API\.resourceAction\("financeCashSessions",reference,"variance-review",\{/);
  assert.match(REVIEW, /approved:decision\.approved, review_note:decision\.review_note/);
  // The browser never posts an adjustment or a journal to "fix" a variance.
  assert.doesNotMatch(REVIEW, /financeJournals|acct-journal|ADJUSTMENT/);
  // And the drawer is re-read from the server afterwards.
  assert.match(REVIEW, /load\(\);/);
});

test("a rejection has to say why, because the reason is the audit evidence", () => {
  assert.match(REVIEW, /if\(!approved&&!note\) throw new Error\("A rejected variance needs a reason\."\)/);
  assert.match(REVIEW, /maxLength:2000/);
});

test("both outcomes are stated plainly after recording", () => {
  assert.match(REVIEW, /the variance is accepted/);
  assert.match(REVIEW, /the variance was not accepted/);
});

test("the approvals list points at the button that now exists", () => {
  assert.match(APPROVALS, /type==="CASH_VARIANCE"/);
  assert.match(APPROVALS, /href="#cash-management">Review in Cash management<\/a>/);
  assert.match(ACCOUNTING, /id="cash-management"/);
  // Still no inline decision control on the approvals row for a variance.
  assert.doesNotMatch(APPROVALS, /CASH_VARIANCE[\s\S]{0,200}data-approval-approve/);
});
