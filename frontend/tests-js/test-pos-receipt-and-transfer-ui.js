"use strict";
const test=require("node:test");
const assert=require("node:assert/strict");
const fs=require("node:fs");
const path=require("node:path");
const {FRONTEND}=require("./harness");
const PAGE=fs.readFileSync(path.join(FRONTEND,"dashboard","pos.html"),"utf8");
test("POS receipt action is hidden until confirmed paid and is revalidated before print",()=>{
  /* The receipt is still refused until the money is confirmed, but the DECISION
     is now made by the server (payment_basis / receipt_printable /
     receipt_blocked_reason) rather than by re-doing the arithmetic in the
     browser: the browser cannot know whether a charge was reversed, refunded or
     settled through the folio. The old client-side comparison against
     total_amount also wrongly refused a settled order whose unopened-item
     returns had reduced the bill, and refused every room-service order. */
  assert.match(PAGE,/order\.receipt_printable/);
  assert.match(PAGE,/receipt_blocked_reason/);
  assert.match(PAGE,/A paid receipt is available only after the backend confirms full payment/);
  assert.match(PAGE,/Payment is no longer confirmed; receipt printing was stopped/);
  // Two checks: one before the width picker, one again after it, so a
  // concurrent return or reversal cannot leave a stale paid receipt printable.
  const print = PAGE.slice(PAGE.indexOf("async function printThermalReceipt"), PAGE.indexOf("async function viewOrder"));
  assert.equal((print.match(/!order\.receipt_printable/g)||[]).length, 2);
  // The detail modal offers the button only when the server allows it, and
  // otherwise shows the server's reason.
  const detail = PAGE.slice(PAGE.indexOf("async function viewOrder"), PAGE.indexOf("async function advanceOrder"));
  assert.match(detail, /if\(order\.receipt_printable\)/);
  assert.match(detail, /receiptBlockedMessage\(order\.receipt_blocked_reason\)/);
  // The browser must not re-derive "paid" from the order totals.
  assert.doesNotMatch(PAGE,/capturedAmount\+0\.0001</);
  assert.doesNotMatch(PAGE,/tender\?tender\.amount:order\.total_amount/);
  // The receipt states the basis the server gave it, never an invented method.
  assert.match(PAGE,/var isFolio=order\.payment_basis==="ROOM_FOLIO";/);
  assert.match(PAGE,/settlement_basis:isFolio/);
  assert.match(PAGE,/paid:isFolio\?net:capturedAmount/);
  assert.match(PAGE,/This is not a till receipt\./);
});

test("the printed receipt states how the charge was settled",()=>{
  const RECEIPT = fs.readFileSync(path.join(FRONTEND,"js","thermal-receipt.js"),"utf8");
  assert.match(RECEIPT, /receipt\.settlement_basis/);
  assert.match(RECEIPT, /"Settled: " \+ receipt\.settlement_basis/);
});

test("restaurant sessions expose only the backend-checked table transfer workflow",()=>{
  assert.match(PAGE,/data-pos-transfer-table-session/);
  assert.match(PAGE,/transferRestaurantTableSession/);
  assert.match(PAGE,/resourceAction\('posRestaurantTableSessions',reference,'transfer'/);
  assert.match(PAGE,/Occupied destinations cannot be merged/);
});
