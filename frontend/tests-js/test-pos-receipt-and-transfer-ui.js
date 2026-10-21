"use strict";
const test=require("node:test");
const assert=require("node:assert/strict");
const fs=require("node:fs");
const path=require("node:path");
const {FRONTEND}=require("./harness");
const PAGE=fs.readFileSync(path.join(FRONTEND,"dashboard","pos.html"),"utf8");
test("POS receipt action is hidden until confirmed paid and is revalidated before print",()=>{
  assert.match(PAGE,/status==="DELIVERED"&&order\.settlement_status==="PAID"/);
  assert.match(PAGE,/A paid receipt is available only after the backend confirms full payment/);
  assert.match(PAGE,/Payment is no longer confirmed; receipt printing was stopped/);
  assert.match(PAGE,/capturedAmount\+0\.0001<Number\(order\.total_amount\|\|0\)/);
  assert.match(PAGE,/paid:capturedAmount/);
  assert.doesNotMatch(PAGE,/tender\?tender\.amount:order\.total_amount/);
});
test("restaurant sessions expose only the backend-checked table transfer workflow",()=>{
  assert.match(PAGE,/data-pos-transfer-table-session/);
  assert.match(PAGE,/transferRestaurantTableSession/);
  assert.match(PAGE,/resourceAction\('posRestaurantTableSessions',reference,'transfer'/);
  assert.match(PAGE,/Occupied destinations cannot be merged/);
});
