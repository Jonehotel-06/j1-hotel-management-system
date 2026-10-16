/* Reported (Check-out → "Full booking"): a booking that has ALREADY taken the
   guest's payment still offered a plain "Cancel". Cancelling it would leave
   recorded money with nothing to refund against, so the direct cancel is closed
   once a payment has been made — the control stays visible but DISABLED, with
   the reason on hover, and the money route becomes the cancellation / refund
   workflow in Enquiries (which records the refund against the payment).

   What is NOT changed: the booking-cancel capability itself, the confirm dialog,
   and the API call it makes (API.staffCancelBooking). Only the paid case changes.

   The backend authority is untouched — this proves the screen the desk sees.

   Run from /frontend: node --test tests-js/test-booking-cancel-paid-ui.js */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const FRONTEND = path.resolve(__dirname, "..");
const read = (rel) => fs.readFileSync(path.join(FRONTEND, rel), "utf8");
const PAGE = read("dashboard/booking-details.html");
const API = read("js/api.js");

/* The page is one long inline script; the action sync is a small top-level
   function inside it, so its real shipped source can be exercised directly. */
function sliceFunction(source, header) {
  const start = source.indexOf(header);
  assert.ok(start > 0, `${header} was not found on the shipped page`);
  const end = source.indexOf("\n  }", start);
  assert.ok(end > start, `${header} has no closing brace`);
  return source.slice(start, end + 4);
}
const SYNC_ACTIONS = sliceFunction(PAGE, "function syncActions(){");

function cancelStub() {
  const el = {
    style: {},
    disabled: false,
    textContent: "Cancel",
    attrs: {},
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; },
    removeAttribute(k) { delete this.attrs[k]; },
  };
  return el;
}

function runSync(booking) {
  const cancel = cancelStub();
  const ctx = {
    book: booking,
    document: { querySelector: (sel) => (sel === "[data-bd-cancel]" ? cancel : null) },
    JONE: { formatNaira: (n) => "NGN " + Number(n).toLocaleString("en-NG", { minimumFractionDigits: 2 }) },
  };
  vm.runInNewContext(SYNC_ACTIONS + "\nsyncActions();", ctx);
  return cancel;
}

/* ---- The page contract -------------------------------------------------- */

test("the direct cancel is kept, but closed once money is recorded", () => {
  // Contract: still a real cancel for an unpaid booking, still the same API.
  assert.match(PAGE, /<button class="btn btn-sm btn-danger" data-bd-cancel>Cancel<\/button>/);
  assert.match(PAGE, /API\.staffCancelBooking\(ref\)/);
  assert.match(API, /staffCancelBooking/);
  assert.match(PAGE, /JONE\.ui\.confirm\(\{title:"Cancel this booking\?"/);
  // The paid case: disabled + reason, never silently cancelable.
  assert.match(SYNC_ACTIONS, /var paidToDate=Number\(book\.amount_paid!=null\?book\.amount_paid:0\);/);
  assert.match(SYNC_ACTIONS, /payStatus==="PAID"\|\|payStatus==="PARTIALLY_PAID"\|\|payStatus==="REFUNDED"/);
  assert.match(SYNC_ACTIONS, /cancel\.disabled=!!moneyRecorded;/);
  assert.match(SYNC_ACTIONS, /cancel\.setAttribute\("aria-disabled","true"\)/);
  assert.match(PAGE, /cancel this booking through the cancellation \/ refund workflow in Enquiries/);
});

test("the terminal booking statuses still hide the cancel entirely", () => {
  assert.match(SYNC_ACTIONS,
    /\(st==="CANCELLED"\|\|st==="CHECKED_OUT"\|\|st==="EXPIRED"\|\|st==="NO_SHOW"\) \? "none" : ""/);
});

test("a disabled cancel explains itself instead of doing nothing", () => {
  const handler = PAGE.slice(PAGE.indexOf('if(cancel) cancel.addEventListener("click"'));
  assert.ok(handler.indexOf("cancel.disabled") > -1 && handler.indexOf("cancel.disabled") < 400,
    "the click handler must refuse a disabled cancel with the visible reason");
  assert.match(handler.slice(0, 700), /JONE\.ui\.toast\(cancel\.getAttribute\("title"\)/);
  assert.match(handler.slice(0, 700), /refund workflow in Enquiries/);
});

/* ---- The behaviour the desk sees --------------------------------------- */

test("an unpaid booking keeps a working Cancel button", () => {
  const cancel = runSync({ status: "CONFIRMED", amount_paid: 0, payment_status: "UNPAID" });
  assert.equal(cancel.disabled, false, "an unpaid booking must still be cancellable");
  assert.equal(cancel.getAttribute("aria-disabled"), null);
  assert.equal(cancel.title, undefined);
  assert.equal(cancel.style.display, "");
});

test("a booking that has taken a payment shows a disabled Cancel with the reason", () => {
  const cancel = runSync({ status: "CONFIRMED", amount_paid: 2500, payment_status: "PARTIALLY_PAID" });
  assert.equal(cancel.disabled, true);
  assert.equal(cancel.getAttribute("aria-disabled"), "true");
  assert.match(cancel.title, /Payment already recorded \(NGN 2,500\.00\)/);
  assert.match(cancel.title, /cancellation \/ refund workflow in Enquiries/);
  // Visible, not hidden: the desk sees why it cannot cancel here.
  assert.equal(cancel.style.display, "");
  assert.equal(cancel.textContent, "Cancel");
});

test("a fully paid booking with no amount snapshot is still protected", () => {
  const cancel = runSync({ status: "CHECKED_IN", amount_paid: 0, payment_status: "PAID" });
  assert.equal(cancel.disabled, true);
  assert.match(cancel.title, /Payment already recorded — cancel this booking through the cancellation \/ refund workflow/);
});

test("recording a payment closes the control immediately; a refunded booking stays closed", () => {
  const afterPayment = runSync({ status: "CONFIRMED", amount_paid: "12000.00", payment_status: "PAID" });
  assert.equal(afterPayment.disabled, true);
  const refunded = runSync({ status: "CONFIRMED", amount_paid: 0, payment_status: "REFUNDED" });
  assert.equal(refunded.disabled, true);
});

test("a checked-out booking hides the control and never leaves a stale reason", () => {
  const cancel = runSync({ status: "CHECKED_OUT", amount_paid: 2500, payment_status: "PAID" });
  assert.equal(cancel.style.display, "none");
});
