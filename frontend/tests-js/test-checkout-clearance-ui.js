/* Checkout folio/gate clearance, styled laundry dialogs, Laundry sidebar
   link and the Accounting moneyCell scope fix.
   Run from /frontend: node --test tests-js/test-checkout-clearance-ui.js */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const FRONTEND = path.resolve(__dirname, "..");
const read = (rel) => fs.readFileSync(path.join(FRONTEND, rel), "utf8");
const CHECKOUT = read("dashboard/check-out.html");
const SERVICES = read("dashboard/service-requests.html");
const LAUNDRY = read("dashboard/laundry.html");
const ACCOUNTING = read("dashboard/accounting.html");
const DASHBOARD = read("js/dashboard.js");
const ICONS = read("js/icons.js");
const API = read("js/api.js");
const THERMAL = read("js/thermal-receipt.js");

test("Accounting defines moneyCell once at page scope (statements/night-audit can use it)", () => {
  assert.equal((ACCOUNTING.match(/function moneyCell/g) || []).length, 1);
  // Defined before the load() closure that previously trapped it.
  assert.ok(ACCOUNTING.indexOf("function moneyCell") < ACCOUNTING.indexOf("function load(){"));
  assert.match(ACCOUNTING, /function bsRow\(label, amount, strong\)/);
});

test("laundry dialogs are styled (no native confirm/prompt)", () => {
  assert.doesNotMatch(LAUNDRY, /window\.(confirm|prompt)\(/);
  assert.match(LAUNDRY, /Confirm laundry delivery/);
  assert.match(LAUNDRY, /JONE\.ui\.confirm\(\{title:"Bill laundry to folio\?"/);
  assert.match(LAUNDRY, /JONE\.ui\.confirm\(\{title:"Cancel this laundry order\?"/);
});

test("sidebar links Laundry to its own page with its own icon", () => {
  // Laundry used to share service-requests.html, so the sidebar showed two
  // entries opening the same page. It is now a distinct destination.
  assert.match(DASHBOARD, /label: "Laundry", href: "laundry\.html", icon: "shirt", view: "laundry"/);
  assert.match(ICONS, /shirt: '<path/);
  assert.match(LAUNDRY, /<h2>Laundry orders<\/h2>/);
  // The duplicate panel is gone from the guest-request page.
  assert.doesNotMatch(SERVICES, /data-laundry-rows/);
  assert.doesNotMatch(SERVICES, /id="laundry"/);
});

test("check-out cards offer a capability-gated folio / gate-pass view", () => {
  assert.match(CHECKOUT, /data-folio-btn=/);
  assert.match(CHECKOUT, /hasAnyCapability\(\["folio\.view"\]\)/);
  assert.match(CHECKOUT, /async function openClearance\(ref, booking\)/);
  assert.match(CHECKOUT, /API\.bookingFolioClearance\(ref\)/);
  assert.match(API, /bookingFolioClearance/);
  assert.match(API, /folio-clearance\//);
});

test("the folio states the bill for the guest's requests and blocks the gate pass", () => {
  const START = CHECKOUT.indexOf("async function openClearance(ref, booking){");
  const END = CHECKOUT.indexOf("/* The gate pass is a POS thermal receipt");
  assert.ok(START > 0 && END > START, "the clearance dialog was not found in the shipped page");
  const DIALOG = CHECKOUT.slice(START, END);

  // The figure the desk collects is the server's, and it visibly includes what
  // the guest's own requests still owe (unbilled requests are money, not a
  // footnote in a warning line).
  assert.match(DIALOG, /d\.amount_to_settle/);
  assert.match(DIALOG, /d\.unpaid_requests_total/);
  assert.match(DIALOG, /d\.unpaid_requests \|\| \[\]/);
  assert.match(DIALOG, /Requests not yet paid for/);
  // The gate pass is only offered on the server's decision.
  assert.match(DIALOG, /if \(d\.gate_pass_allowed\)/);
  assert.match(DIALOG, /d\.gate_pass_blockers \|\| \[\]/);
  assert.match(DIALOG, /Do not issue a gate pass yet\./);
  // The old client-side rule (a cleared folio plus a look at unbilled laundry)
  // cannot come back: the decision is never re-derived in the browser.
  assert.doesNotMatch(DIALOG, /if \(d\.cleared && !unbilled\.length\)/);
});

test("the desk records the money received and the folio reflects it at once", () => {
  const START = CHECKOUT.indexOf("async function recordSettlement(");
  const END = CHECKOUT.indexOf("function serverSearch(term){");
  assert.ok(START > 0 && END > START, "recordSettlement was not found in the shipped page");
  const SETTLE = CHECKOUT.slice(START, END);

  // Money is recorded against the acting staff member's own identity, with the
  // method and (for cash) the drawer the money went into.
  assert.match(SETTLE, /window\.API\.settleBookingBalance\(ref, \{/);
  assert.match(SETTLE, /provider: method/);
  assert.match(SETTLE, /include_unbilled: true/);
  assert.match(SETTLE, /cash_session_reference: method === "CASH" \? drawer : ""/);
  assert.match(SETTLE, /Cash must go into your own open drawer/);
  // The response is the authority: the dialog re-renders from the folio the
  // backend just wrote, never from browser arithmetic.
  assert.match(SETTLE, /var folio = payload\.folio \|\| \{\}/);
  assert.match(SETTLE, /renderClearance\(body, ref, folio, booking\)/);
  assert.match(SETTLE, /payload\.recorded_by/);
  // A failure must never look like a success.
  assert.match(SETTLE, /Nothing was charged/);
});

test("the settlement control is capability-gated and only offered when payable", () => {
  assert.match(CHECKOUT, /if \(d\.can_settle\) \{/);
  assert.match(CHECKOUT, /data-settle-balance=/);
  assert.match(CHECKOUT, /d\.settle_blocked_reason/);
  assert.match(API, /settleBookingBalance/);
  assert.match(API, /settle-balance\//);
});

test("the check-out card states the real outstanding figure, not just the room", () => {
  // amount_due is the STAY projection; a guest who owes for their own requests
  // must not read as "Outstanding 0" at the desk.
  assert.match(CHECKOUT, /var due = \(b\.outstanding_balance != null && b\.outstanding_balance !== ""\)/);
  assert.match(CHECKOUT, /var folioCharges = Number\(b\.folio_charges \|\| 0\)/);
  assert.match(CHECKOUT, /Settle requests &amp; print gate pass/);
  assert.match(CHECKOUT, /data-due-amount=/);
});

test("the gate pass is a POS thermal receipt listing all purchases, cleared", () => {
  assert.match(CHECKOUT, /js\/thermal-receipt\.js/);
  assert.match(CHECKOUT, /JONE\.ThermalReceipt\.print\(receipt/);
  assert.match(CHECKOUT, /mode: "GATE CLEARANCE PASS"/);
  assert.match(CHECKOUT, /ALL BILLS CLEARED — this guest is not owing the hotel\. Please allow exit at the gate\./);
  assert.match(CHECKOUT, /\(d\.charges \|\| \[\]\)\.forEach/);
  assert.match(CHECKOUT, /data-print-gate-pass/);
  // The print action only exists in the branch the SERVER allows: every unpaid
  // request and every open request blocks it (the browser no longer decides).
  assert.match(CHECKOUT, /if \(d\.gate_pass_allowed\)/);
  assert.match(CHECKOUT, /data-print-gate-pass/);
  assert.match(CHECKOUT, /Do not issue a gate pass yet/);
  assert.match(CHECKOUT, /d\.gate_pass_blockers \|\| \[\]/);
});

test("thermal receipts support an optional note without changing POS receipts", () => {
  assert.match(THERMAL, /receipt\.note/);
  assert.match(THERMAL, /Optional receipt-specific note/);
});

test("checking out with money owed offers the payment path, not only an override", () => {
  // Reported: "no button to mark it as paid during check-out for guests that
  // requested something and then makes payment". The old flow only asked
  // "check out anyway?".
  assert.match(CHECKOUT, /function settleOrOverride\(ref, amountLabel\)/);
  assert.match(CHECKOUT, /"Record payment & clear the bill"/);
  assert.match(CHECKOUT, /"Check out anyway \(balance stays owed\)"/);
  assert.match(CHECKOUT, /if \(choice === "settle"\)/);
  assert.match(CHECKOUT, /openClearance\(ref, dueBooking\)/);
  // The override is still explicit, and still says what stays owed.
  assert.match(CHECKOUT, /allow_balance_due: true/);
  assert.match(CHECKOUT, /JONE\.ui\.modal\.open\(\{\s*title: "Guest still owes money"/);
});

test("the payment taken at check-out goes through the folio, never a side route", () => {
  // One endpoint records the money (the folio settlement); nothing in this page
  // marks a bill paid locally or invents a second payment API.
  assert.doesNotMatch(CHECKOUT, /markAsPaid|mark_paid|paid_offline/);
  assert.match(CHECKOUT, /window\.API\.settleBookingBalance\(ref, \{/);
  // A request that is not on the folio yet is billed first, in the same step.
  assert.match(CHECKOUT, /include_unbilled: true/);
});
