/* Pack/bag stock recording, menu price-cost guard, hand-off View modal, and
   trimmed quantity contracts (staff requests).
   Run from /frontend: node --test tests-js/test-inventory-pack-price-ui.js */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const FRONTEND = path.resolve(__dirname, "..");
const page = (name) => fs.readFileSync(path.join(FRONTEND, "dashboard", name), "utf8");
const INVENTORY = page("inventory.html");
const PROCUREMENT = page("procurement.html");
const POS = page("pos.html");

test("the POS hand-off View button opens a detail modal instead of a toast", () => {
  assert.match(INVENTORY, /data-inv-consumption-view=/);
  assert.match(INVENTORY, /async function viewConsumption\(reference\)/);
  assert.match(INVENTORY, /function consumptionDetailHtml\(row\)/);
  assert.doesNotMatch(INVENTORY, /The hand-off is already resolved/);
  // The modal discloses the exactly-once rule to stop double-deduction fears.
  assert.match(INVENTORY, /will not deduct stock a second time/);
  assert.match(INVENTORY, /already processed; no stock was deducted again/);
});

test("resolved hand-offs show only View; open ones show View and Process", () => {
  assert.match(INVENTORY, /canProcess=\["PENDING","FAILED"\]\.includes\(row\.status\)/);
  assert.match(INVENTORY, /consumptionCache\[row\.reference\]=row/);
});

test("stock items record pack/bag packaging and pack + piece prices", () => {
  assert.match(INVENTORY, /name:"units_per_pack",label:"Pieces in one pack\/bag \(optional\)"/);
  assert.match(INVENTORY, /name:"pack_price",label:"Actual price per pack\/bag \(NGN, optional\)"/);
  assert.match(INVENTORY, /label:"Price per piece \\u2014 standard cost \(NGN\)"/);
  assert.match(INVENTORY, /function stockUnitOptions\(current\)/);
});

test("opening stock can be recorded as pieces, pack, or bag and converts to pieces", () => {
  assert.match(INVENTORY, /name:"recording_unit",label:"Record stock as"/);
  assert.match(INVENTORY, /values\.quantity=String\(Number\(values\.quantity\)\*perPack\)/);
  assert.match(INVENTORY, /values\.unit_cost=String\(Number\(values\.pack_price\)\/perPack\)/);
});

test("goods receipts can be recorded as pieces, pack, or bag with pack and piece prices", () => {
  assert.match(PROCUREMENT, /data-proc-receipt-mode=/);
  assert.match(PROCUREMENT, /data-proc-receipt-perpack=/);
  assert.match(PROCUREMENT, /data-proc-receipt-packprice=/);
  assert.match(PROCUREMENT, /pieces=qty\*perPack/);
});

test("quantities in counts and recipes render trimmed (20, not 20.0000)", () => {
  assert.match(INVENTORY, /qty\(line\.expected_quantity,line\)/);
  assert.match(INVENTORY, /JONE\.trimQuantityValue\(line\.counted_quantity==null\?line\.expected_quantity:line\.counted_quantity\)/);
  assert.match(INVENTORY, /qty\(line\.quantity_per_menu_unit,line\)/);
});

test("the bar menu price field warns it must cover the supplier cost", () => {
  assert.match(POS, /Must be equal to or higher than the supplier purchase cost/);
  assert.match(POS, /below the supplier cost/);
});

test("profitability labels a directly sold product's menu price basis", () => {
  assert.match(INVENTORY, /MENU_ITEM_MATCH/);
  assert.match(INVENTORY, /Menu price \(sold as stocked\)/);
});
