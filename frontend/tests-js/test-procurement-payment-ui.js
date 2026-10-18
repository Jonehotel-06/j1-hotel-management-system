/* Supplier-payment recording on the procurement screen.
   Run from /frontend: node --test tests-js/test-procurement-payment-ui.js

   Until this existed there was nowhere to record that money had
   actually been paid to a supplier: the approval receipt proved what
   was filed when the order was authorised, not the payment itself.
   These tests pin the page contract; the server enforces the same
   rules (apps/inventory/services/inventory_service.py). */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { FRONTEND } = require("./harness");

const PAGE = fs.readFileSync(path.join(FRONTEND, "dashboard", "procurement.html"), "utf8");

function sliceFunction(name) {
  const start = PAGE.indexOf("function " + name + "(");
  assert.ok(start > 0, name + " is missing from the shipped page");
  const open = PAGE.indexOf("{", start);
  let depth = 0;
  for (let i = open; i < PAGE.length; i += 1) {
    if (PAGE[i] === "{") depth += 1;
    else if (PAGE[i] === "}") {
      depth -= 1;
      if (depth === 0) return PAGE.slice(start, i + 1);
    }
  }
  throw new Error("unbalanced braces in " + name);
}

/* ------------------------------ Screen state ----------------------------- */

test("a paid order is a first-class status on the screen", () => {
  assert.match(PAGE, /<option value="PAID">Paid<\/option>/);
  assert.match(PAGE, /function canPay\(status\)\{return \["APPROVED","ORDERED","PARTIALLY_RECEIVED","RECEIVED"\].indexOf\(status\)!==-1;\}/);
  // Cancelled and unauthorised orders can never be paid.
  const canPay = sliceFunction("canPay");
  assert.ok(!canPay.includes("DRAFT") && !canPay.includes("CANCELLED") && !canPay.includes("PAID"));
});

test("the order detail states the money, not just the goods", () => {
  assert.match(PAGE, /<dt>Order total<\/dt><dd class="num">'\+JONE\.esc\(money\(order\.total_amount,order\.currency\)\)/);
  assert.match(PAGE, /<dt>Paid to supplier<\/dt>/);
  assert.match(PAGE, /<dt>Outstanding<\/dt>/);
  assert.match(PAGE, /order\.status==="PAID"\?'<div><dt>Paid at<\/dt><dd>'/);
});

/* ------------------------------- Recording ------------------------------- */

test("recording a payment sends amount, method, reference, date and evidence", () => {
  const pay = sliceFunction("recordPayment");
  assert.match(pay, /title:"Record supplier payment · "/);
  assert.match(pay, /multipart:true/);
  assert.match(pay, /name:"amount",label:"Amount paid/);
  assert.match(pay, /name:"method",label:"How was it paid",type:"select",required:true/);
  assert.match(pay, /value:"BANK_TRANSFER".*value:"CARD".*value:"CHEQUE".*value:"CASH"/s);
  assert.match(pay, /name:"reference",label:"Payment reference"/);
  assert.match(pay, /name:"paid_on",label:"Date paid",type:"date",required:true/);
  assert.match(pay, /name:"evidence_file",label:"Supplier invoice \/ payment receipt",type:"file"/);
  assert.match(pay, /resourceAction\("purchaseOrders",reference,"pay",fd\)/);
  // The amount starts at the outstanding balance: a full settlement
  // is the one-field case.
  assert.match(pay, /value:String\(outstanding\|\|""\)/);
});

test("the dialog refuses an overpayment and a reference-less transfer locally", () => {
  const pay = sliceFunction("recordPayment");
  assert.match(pay, /throw new Error\("Enter a payment amount greater than zero\."\)/);
  assert.match(pay, /cannot exceed the outstanding balance/);
  assert.match(pay, /values\.method!=="CASH"&&!\(values\.reference\|\|""\)\.trim\(\)/);
});

test("only a manager may record a payment, and only while the order is unpaid", () => {
  assert.match(PAGE, /if\(canPay\(order\.status\)&&manager\(\)\)action\("Record supplier payment","btn-accent"/);
});

/* ------------------------------- Evidence -------------------------------- */

test("every payment made is listed with its own receipt or invoice", () => {
  const history = sliceFunction("paymentHistoryHtml");
  assert.match(history, /Payments to this supplier \('\+payments\.length\+'\)/);
  assert.match(history, /JONE\.esc\(money\(payment\.amount,order\.currency\)\)/);
  assert.match(history, /payment\.evidence_url\?'<a href="'\+JONE\.esc\(payment\.evidence_url\)/);
  assert.match(history, /Receipt \/ invoice/);
  assert.match(history, /payment\.uploaded_by_email/);
  // It renders in the order detail, under the approval receipt.
  assert.match(PAGE, /receiptLink\(order\)\+paymentHistoryHtml\(order\)/);
});

test("a payment without evidence still shows, honestly as none", () => {
  const history = sliceFunction("paymentHistoryHtml");
  assert.match(history, /\(payment\.evidence_url\?'<a href=/);
  assert.match(history, /:"—"/);
});
