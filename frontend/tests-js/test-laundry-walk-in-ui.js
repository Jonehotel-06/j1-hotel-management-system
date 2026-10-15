/* Laundry page: walk-in customers, the settle step and the sidebar split.

   Laundry used to live inside the Guest Requests page (two sidebar entries
   opening one page) and could only be received for somebody with a stay, so a
   non-resident could not use the service at all. These tests pin the split,
   the two customer types, and the money rules the desk must follow.

   Run from /frontend: node --test tests-js/test-laundry-walk-in-ui.js */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const { makeContext, FRONTEND } = require("./harness");

const read = (rel) => fs.readFileSync(path.join(FRONTEND, rel), "utf8");
const LAUNDRY = read("dashboard/laundry.html");
const SERVICES = read("dashboard/service-requests.html");
const DASHBOARD = read("js/dashboard.js");

/* Run the page's real helpers (row building, payment state, identity capture)
   inside a vm so the assertions cannot drift from what ships. */
function loadHelpers() {
  const ctx = makeContext();
  vm.runInContext(
    "var JONE={esc:function(v){return String(v==null?'':v)"
    + ".replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\"/g,'&quot;');},"
    + "formatNaira:function(v){return 'NGN '+String(v);},"
    + "dashboard:{statusPill:function(v){return '<span class=\"pill\">'+String(v||'')+'</span>';}}};",
    ctx,
  );
  const start = LAUNDRY.indexOf("var NEXT=");
  const end = LAUNDRY.indexOf("function visible(){");
  assert.ok(start > 0 && end > start, "laundry row helpers were not found in the shipped page");
  vm.runInContext(LAUNDRY.slice(start, end), ctx, { filename: "laundry.html#row-helpers" });
  return ctx.window;
}

const walkIn = {
  reference: "LND-1", status: "RECEIVED", total_amount: "2000.00", settled: false,
  is_walk_in: true, customer_name: "Chinedu Okeke", guest_name: "Chinedu Okeke",
  guest_phone: "08030001111", payment_method: "", room_number: null,
  items: [{ description: "Shirt", quantity: 2 }],
};
const inHouse = {
  reference: "LND-2", status: "RECEIVED", total_amount: "4000.00", settled: false,
  is_walk_in: false, customer_name: "Ada Obi", guest_name: "Ada Obi",
  stay_reference: "STY-1", room_number: "701", folio_reference: null,
  items: [{ description: "Suit", quantity: 1 }],
};

// --- The sidebar split ----------------------------------------------------

test("Laundry is its own page and no longer duplicates the Guest Requests link", () => {
  assert.match(DASHBOARD, /label: "Laundry", href: "laundry\.html", icon: "shirt", view: "laundry"/);
  const guestRequests = DASHBOARD.match(/label: "Guest Requests", href: "([^"]+)"/);
  assert.ok(guestRequests, "Guest Requests entry is missing");
  assert.notEqual(guestRequests[1], "laundry.html", "the two entries must not open the same page");
});

test("the guest-request page no longer carries a laundry panel", () => {
  assert.doesNotMatch(SERVICES, /data-laundry-rows/);
  assert.doesNotMatch(SERVICES, /data-laundry-new/);
  assert.doesNotMatch(SERVICES, /id="laundry"/);
  // Its guest-request queue is untouched.
  assert.match(SERVICES, /data-sr-rows/);
});

test("every role that could reach the old panel can still reach the page", () => {
  const line = DASHBOARD.split("\n").find((l) => l.includes('label: "Laundry"'));
  assert.ok(line, "Laundry navigation entry not found");
  for (const role of ["admin", "manager", "receptionist", "housekeeper", "porter", "security", "bartender"]) {
    assert.ok(line.includes(`"${role}"`), `laundry page lost access for ${role}`);
  }
  // The page's own gate lists the same roles, so the entry and the page agree.
  const gate = LAUNDRY.match(/JONE\.dashboard\.boot\(\[([^\]]+)\]\)/);
  assert.ok(gate, "laundry page has no role gate");
  for (const role of ["admin", "manager", "receptionist", "housekeeper", "porter", "security", "bartender"]) {
    assert.ok(gate[1].includes(`"${role}"`), `laundry page gate is missing ${role}`);
  }
});

// --- Receiving for either kind of customer --------------------------------

test("the receive form offers an in-house guest and a walk-in customer", () => {
  assert.match(LAUNDRY, /data-laundry-mode="IN_HOUSE"/);
  assert.match(LAUNDRY, /data-laundry-mode="WALK_IN"/);
  assert.match(LAUNDRY, /data-laundry-name/);
  assert.match(LAUNDRY, /data-laundry-phone/);
  assert.match(LAUNDRY, /data-laundry-stay/);
});

test("receiving sends a stay for a resident and a name for a walk-in", () => {
  const field = LAUNDRY.slice(
    LAUNDRY.indexOf("function identityField(stays)"),
    LAUNDRY.indexOf("function lineField(i)"),
  );
  assert.match(field, /stay_reference:mode==="IN_HOUSE"\?\(stayEl\?stayEl\.value:""\):""/);
  assert.match(field, /customer_name:mode==="WALK_IN"\?String\(\(nameEl&&nameEl\.value\)\|\|""\)\.trim\(\):""/);
  assert.match(field, /customer_phone:mode==="WALK_IN"\?String\(\(phoneEl&&phoneEl\.value\)\|\|""\)\.trim\(\):""/);
});

test("the form refuses an unnamed walk-in and an unselected guest", () => {
  const field = LAUNDRY.slice(
    LAUNDRY.indexOf("function identityField(stays)"),
    LAUNDRY.indexOf("function lineField(i)"),
  );
  assert.match(field, /if\(mode==="IN_HOUSE"&&!v\.stay_reference\) return "Choose the in-house guest/);
  assert.match(field, /if\(mode==="WALK_IN"&&!v\.customer_name\) return "Enter the walk-in customer's name/);
});

test("the receive payload only sends the identity fields that apply", () => {
  const submit = LAUNDRY.slice(LAUNDRY.indexOf("async function receive()"), LAUNDRY.indexOf("/* ------------------ Settle"));
  assert.match(submit, /if\(values\.stay_reference\) payload\.stay_reference=values\.stay_reference;/);
  assert.match(submit, /if\(values\.customer_name\) payload\.customer_name=values\.customer_name;/);
});

// --- What each row offers -------------------------------------------------

test("a walk-in order is offered a desk payment, never a folio charge", () => {
  const win = loadHelpers();
  const html = win.rowActions(walkIn);
  assert.match(html, /data-laundry-settle="LND-1"/);
  assert.match(html, />Record payment</);
  assert.doesNotMatch(html, /data-laundry-bill/, "a walk-in has no folio to charge");
});

test("an in-house order is offered a folio charge, never a desk payment", () => {
  const win = loadHelpers();
  const html = win.rowActions(inHouse);
  assert.match(html, /data-laundry-bill="LND-2"/);
  assert.match(html, />Bill to folio</);
  assert.doesNotMatch(html, /data-laundry-settle/, "in-house laundry settles through the folio");
});

test("money is never taken twice: a settled order offers no payment control", () => {
  const win = loadHelpers();
  const settled = Object.assign({}, walkIn, { settled: true, payment_method: "CASH" });
  const html = win.rowActions(settled);
  assert.doesNotMatch(html, /data-laundry-settle/);
  assert.doesNotMatch(html, /data-laundry-bill/);
  // But it still moves through delivery.
  assert.match(html, /data-laundry-target="IN_PROGRESS"/);
});

test("a cancelled order offers no payment control at all", () => {
  const win = loadHelpers();
  const html = win.rowActions(Object.assign({}, walkIn, { status: "CANCELLED" }));
  assert.doesNotMatch(html, /data-laundry-settle/);
  assert.doesNotMatch(html, /data-laundry-bill/);
});

test("the payment column tells the two settlement routes apart", () => {
  const win = loadHelpers();
  assert.match(win.paymentCell(walkIn), /Not settled/);
  const paid = Object.assign({}, walkIn, { settled: true, payment_method: "BANK_TRANSFER" });
  assert.match(win.paymentCell(paid), /Paid/);
  assert.match(win.paymentCell(paid), /BANK TRANSFER/);
  const billed = Object.assign({}, inHouse, { settled: true, folio_reference: "FOL-9" });
  assert.match(win.paymentCell(billed), /Billed/);
  assert.match(win.paymentCell(billed), /FOL-9/);
  assert.match(win.paymentCell(Object.assign({}, walkIn, { status: "CANCELLED" })), /Cancelled/);
});

test("every customer row names somebody, walk-in or resident", () => {
  const win = loadHelpers();
  assert.match(win.customerCell(walkIn), /Chinedu Okeke/);
  assert.match(win.customerCell(walkIn), /Walk-in/);
  assert.match(win.customerCell(inHouse), /Ada Obi/);
  assert.match(win.customerCell(inHouse), /Room 701/);
  // A nameless legacy record still renders as a person, not a blank cell.
  assert.match(win.customerCell({ is_walk_in: true }), /Walk-in customer/);
});

// --- The settle step ------------------------------------------------------

test("recording a payment calls the settle action with the chosen method", () => {
  const settle = LAUNDRY.slice(LAUNDRY.indexOf("async function settle(order)"), LAUNDRY.indexOf("rowsEl.addEventListener"));
  assert.match(settle, /window\.API\.resourceAction\("guestLaundry",order\.reference,"settle",\{/);
  for (const method of ["CASH", "BANK_TRANSFER", "CARD"]) {
    assert.ok(settle.includes(`value:"${method}"`), `settle form is missing ${method}`);
  }
});

test("cash is refused without a drawer reference, mirroring the server rule", () => {
  const settle = LAUNDRY.slice(LAUNDRY.indexOf("async function settle(order)"), LAUNDRY.indexOf("rowsEl.addEventListener"));
  assert.match(settle, /if\(method==="CASH"&&!session\) throw new Error\("Cash payments require your open cash-session reference\."\)/);
  assert.match(settle, /if\(method!=="CASH"&&session\) throw new Error\("Remove the cash-session reference for a bank or card payment\."\)/);
});

test("the settle action is delegated so re-rendered rows keep working", () => {
  assert.match(LAUNDRY, /event\.target\.closest\("\[data-laundry-settle\]"\)/);
  assert.match(LAUNDRY, /event\.target\.closest\("\[data-laundry-bill\]"\)/);
  assert.match(LAUNDRY, /event\.target\.closest\("\[data-laundry-status\]"\)/);
});

// --- Filters --------------------------------------------------------------

test("the list can be filtered to walk-in customers and to unsettled orders", () => {
  assert.match(LAUNDRY, /<option value="WALK_IN">Walk-in customers<\/option>/);
  assert.match(LAUNDRY, /<option value="IN_HOUSE">In-house guests<\/option>/);
  assert.match(LAUNDRY, /<option value="UNSETTLED">Not settled<\/option>/);
  assert.match(LAUNDRY, /if\(who==="WALK_IN"&&!order\.is_walk_in\) return false;/);
  assert.match(LAUNDRY, /if\(pay==="UNSETTLED"&&order\.settled\) return false;/);
});
