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
  assert.doesNotMatch(SERVICES, /window\.(confirm|prompt)\(/);
  assert.match(SERVICES, /Confirm laundry delivery/);
  assert.match(SERVICES, /JONE\.ui\.confirm\(\{title:"Bill laundry to folio\?"/);
  assert.match(SERVICES, /JONE\.ui\.confirm\(\{title:"Cancel this laundry order\?"/);
});

test("sidebar links Laundry to the anchored panel with its own icon", () => {
  assert.match(DASHBOARD, /label: "Laundry", href: "service-requests\.html#laundry", icon: "shirt"/);
  assert.match(ICONS, /shirt: '<path/);
  assert.match(SERVICES, /<section class="panel" id="laundry"/);
});

test("check-out cards offer a capability-gated folio / gate-pass view", () => {
  assert.match(CHECKOUT, /data-folio-btn=/);
  assert.match(CHECKOUT, /hasAnyCapability\(\["folio\.view"\]\)/);
  assert.match(CHECKOUT, /async function openClearance\(ref\)/);
  assert.match(CHECKOUT, /API\.bookingFolioClearance\(ref\)/);
  assert.match(API, /bookingFolioClearance/);
  assert.match(API, /folio-clearance\//);
});

test("the gate pass is a POS thermal receipt listing all purchases, cleared", () => {
  assert.match(CHECKOUT, /js\/thermal-receipt\.js/);
  assert.match(CHECKOUT, /JONE\.ThermalReceipt\.print\(receipt/);
  assert.match(CHECKOUT, /mode: "GATE CLEARANCE PASS"/);
  assert.match(CHECKOUT, /ALL BILLS CLEARED — this guest is not owing the hotel\. Please allow exit at the gate\./);
  assert.match(CHECKOUT, /\(d\.charges \|\| \[\]\)\.forEach/);
  assert.match(CHECKOUT, /data-print-gate-pass/);
  // The print action only exists in the cleared branch of the modal.
  assert.match(CHECKOUT, /if \(d\.cleared && !unbilled\.length\)/);
  assert.match(CHECKOUT, /Do not issue a gate pass yet/);
  assert.match(CHECKOUT, /Outstanding balance:/);
});

test("thermal receipts support an optional note without changing POS receipts", () => {
  assert.match(THERMAL, /receipt\.note/);
  assert.match(THERMAL, /Optional receipt-specific note/);
});
