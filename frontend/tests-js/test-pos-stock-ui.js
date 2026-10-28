/* tests-js/test-pos-stock-ui.js
   POS + room-service stock UX contracts (static source assertions, same
   style as test-pos-create-errors.js):
     1. the product listing shows the REAL stock source (store + available
        quantity + unit) straight from the server's inventory resolver;
     2. an item can only enter the cart after a LIVE stock check — zero,
        insufficient, unconfigured-location, and check-unavailable cases are
        each refused with a specific, product-named message;
     3. the failure is fail-safe: if the stock cannot be verified, the item
        is NOT added (never assumed sufficient);
     4. cart lines carry the same stock source so the operator sees the store
        the sale will be drawn from;
     5. no store name is hardcoded — every figure and location comes from
        the API.
   Run from /frontend: node --test tests-js/test-pos-stock-ui.js */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { FRONTEND } = require("./harness");

const POS = fs.readFileSync(path.join(FRONTEND, "dashboard", "pos.html"), "utf8");
const CONFIG = fs.readFileSync(path.join(FRONTEND, "js", "config.js"), "utf8");

test("the item picker shows the live stock source under the menu item select", () => {
  assert.match(POS,
    /<div class="hint" data-pos-item-stock role="status" aria-live="polite">/,
    "a status region under the item select announces the stock source");
  assert.match(POS, /function renderItemStockHint\(\)\{/, "the hint has a renderer");
  assert.match(POS, /"Stock location: "\+stockLocationName\(stock\)\+" · Available stock: "\+available\+" "\+unitWord\(stock\.stock_unit,available\)/,
    "the hint names the location, the available quantity, and the unit");
});

test("option labels carry the store and availability, unconfigured items say so", () => {
  assert.match(POS, /function stockOptionSuffix\(stock\)\{/);
  assert.match(POS, /if\(stock\.unconfigured\)return " · stock location unavailable";/);
  assert.match(POS, /\(available>0\?" · "\+available\+" available":" · out of stock"\)/,
    "tracked items show '<store> · N available' or '<store> · out of stock'");
  assert.match(POS, /JONE\.esc\(stockOptionSuffix\(item\.stock\)\)/,
    "the suffix is appended to every item option label");
});

test("untracked kitchen items show no invented figures", () => {
  const hint = POS.slice(POS.indexOf("function renderItemStockHint"), POS.indexOf("function renderItemChoices"));
  /* The old contract was silence. Silence was the bug: an operator adding a
     product with no stock recipe and no direct-sale link had no way to see that
     the sale would never draw down a store (drafts are accepted, and the
     delivery hand-off is parked for inventory review). The hint now NAMES that
     state — still without inventing a store, a quantity or a unit. */
  assert.match(hint, /if\(!stock\)\{node\.textContent="";return;\}/,
    "a missing stock payload still renders nothing");
  assert.match(hint, /if\(unconfiguredForStock\(stock\)\)\{[\s\S]*?No stock control is configured for /,
    "an entirely unconfigured product says so in words");
  assert.match(hint, /if\(!stock\.tracked\)\{node\.textContent="Stock tracking: this product is explicitly marked as not stock-tracked in its recipe\.";return;\}/,
    "an explicit NO_STOCK recipe is reported as a decision, not as a shortage");
  const untrackedBranch = hint.slice(hint.indexOf("if(!stock.tracked)"), hint.indexOf("if(stock.unconfigured)"));
  assert.doesNotMatch(untrackedBranch, /Stock location:|Available stock:|saleable_quantity/,
    "no store, quantity or availability figure is invented for an untracked product");
});

test("unconfigured stock location is a hard, specific refusal", () => {
  assert.match(POS, /"Stock location unavailable for "\+item\.name\+": "\+\(stock\.unconfigured_reason\|\|"its inventory location is not configured"\)/,
    "the refusal names the product and the configuration problem");
});

test("add-to-cart runs a LIVE per-item stock check via the existing lookup endpoint", () => {
  assert.match(POS, /async function verifyItemStock\(item\)\{/);
  assert.match(POS, /var params=\{menu_item_id:item\.id\};/,
    "verification asks by menu item id, not by a code the operator typed");
  assert.match(POS, /window\.API\.get\(endpoint\("posBarcodeLookup"\),\{params:params,cache:"no-store"\}\)/,
    "the check hits the barcode-lookup endpoint and is never cached");
  assert.match(CONFIG, /posBarcodeLookup:\s*"[^"]*\/api\/admin\/pos\/menu\/barcode-lookup\/"/,
    "the endpoint key still points at the same URL the backend extended");
});

test("verification failure is fail-safe: the item is not added on assumption", () => {
  const verify = POS.slice(POS.indexOf("async function verifyItemStock"), POS.indexOf("function reportItemNotAdded"));
  assert.match(verify, /if\(err&&err\.status===404\)return \{ok:false,message:item\.name\+" is no longer available on the menu\./,
    "a vanished product is refused");
  assert.match(verify, /return \{ok:false,message:"We couldn't verify the stock for "\+item\.name\+"\. The item has not been added\. Please try again\."\};/,
    "a connection/verification failure refuses the add — stock is never assumed");
  assert.doesNotMatch(verify, /ok:\s*true[^}]*\}\s*catch/,
    "there is no fallback that accepts the item after a failed check");
});

test("every stock refusal is visible: toast plus inline status next to the picker", () => {
  assert.match(POS, /function reportItemNotAdded\(message\)\{/);
  assert.match(POS, /JONE\.ui\.toast\(message,"error",\{title:"Item not added"\}\);/,
    "the refusal is toasted");
  assert.match(POS, /var hint=document\.querySelector\("\[data-pos-item-stock\]"\);\s*if\(hint\)hint\.textContent=message;/s,
    "and written into the inline status right under the item select");
});

test("zero stock and insufficient stock each get the exact required message", () => {
  const addLine = POS.slice(POS.indexOf("async function addCartLine"), POS.indexOf("var scanQueue"));
  assert.match(addLine,
    /"Out of stock: "\+item\.name\+" is unavailable at "\+location\+"\. Available stock: 0 "\+unit\+"\. This item has not been added to the "\+orderNoun\(\)\+"\."/,
    "out-of-stock names the product, the store, zero, and the unit");
  assert.match(addLine,
    /"Insufficient stock: only "\+available\+" "\+unit\+" of "\+item\.name\+" are available at "\+location\+", but "\+quantity\+" were requested\. Reduce the quantity or replenish stock before adding this item\."/,
    "insufficient stock shows the available figure, the store, and what was requested");
  /* The order, not the line, is what the server validates, so the same product
     already in the cart is named as the reason when it is the reason. */
  assert.match(addLine, /var alreadyCarted=state\.cart\.reduce/,
    "the add is measured against the whole order");
  assert.match(addLine,
    /"Insufficient stock: only "\+available\+" "\+unit\+" of "\+item\.name\+" are available at "\+location\+" and "\+alreadyCarted\+" "/,
    "when the cart already holds the product, its quantity is stated");
});

test("a stock-tracked product cannot be double-added while the check is in flight", () => {
  const addLine = POS.slice(POS.indexOf("async function addCartLine"), POS.indexOf("var scanQueue"));
  assert.match(addLine, /var addLineButton=document\.getElementById\("pos-add-line"\);\s*if\(!JONE\.guardSubmit\(addLineButton\)\)return;/,
    "the add button is guarded while the async check runs");
  assert.match(addLine, /finally\{JONE\.releaseGuard\(addLineButton\);\}/,
    "the guard is always released");
});

test("cart lines remember and display their stock source", () => {
  assert.match(POS, /stock_location:stock\.tracked\?stockLocationName\(stock\):""/,
    "the line stores the location the sale will be drawn from");
  assert.match(POS, /stock_available:stock\.tracked\?Number\(stock\.saleable_quantity\|\|0\):null/,
    "and the available figure that was verified");
  assert.match(POS, (/Stock location: '\+JONE\.esc\(line\.stock_location\)/),
    "the rendered cart row shows the store");
  assert.match(POS, /' available'\)/, "with the availability");
});

test("the + button cannot push a line past the available stock", () => {
  /* The ceiling is the store's availability MINUS the same product's quantity
     on the order's other lines: the picker pushes a new line every time, and the
     server validates the whole order, so incrementing one line against the full
     availability could build an unfulfillable cart. */
  assert.match(POS, /var incOthers=state\.cart\.reduce/,
    "the + button subtracts what the other lines already hold");
  assert.match(POS, /var stockCeiling=il\.stock_available==null\?100:Math\.min\(100,Math\.max\(0,Number\(il\.stock_available\)-incOthers\)\);/,
    "untracked lines keep the old limit of 100, tracked lines are capped by the remaining stock");
  assert.match(POS, /"Insufficient stock: only "\+\(il\.stock_available==null\?"":String\(il\.stock_available\)\+" "\)\+unitWord\(il\.stock_unit,stockCeiling\)\+" of "\+il\.item_name\+" are available at "/,
    "hitting the ceiling explains itself with the real figures");
});

test("scanning enforces the same rules on the live lookup answer", () => {
  const scan = POS.slice(POS.indexOf("function addScannedItem"), POS.indexOf("var scanQueue"));
  assert.match(scan, /function addScannedItem\(item\)\{/);
  assert.match(scan, /if\(stock\.unconfigured\)\{[\s\S]*?return false;\}/,
    "an unconfigured product is refused");
  assert.match(scan,
    /"Out of stock: "\+item\.name\+" is unavailable at "\+location\+"\. Available stock: 0 "\+unit\+"\. It was not added to the "\+orderNoun\(\)\+"\."/,
    "zero stock is refused with the full message");
  assert.match(scan,
    /"Insufficient stock: only "\+available\+" "\+unit\+" of "\+item\.name\+" are available at "\+location\+", but "\+\(Number\(existing\.quantity\)\+1\)\+" were requested\. It was not added\."/,
    "a repeat scan that would exceed the store is refused");
  assert.match(scan, /setScanStatus\("Added "\+item\.name\+" · "\+money\(item\.base_price,item\.currency\)\+" · "\+location\+" · "\+available\+" "/,
    "a successful scan reports the store and availability it was allowed against");
  assert.match(POS, /var added=addScannedItem\(item\);\s*if\(added!==false\)\{/,
    "the scan handler only announces success when the item was really added");
});

test("a server stock refusal refreshes the stale figures but keeps the cart", () => {
  const handler = POS.slice(POS.indexOf('addEventListener("submit"'), POS.indexOf('document.getElementById("pos-apply")'));
  assert.match(handler, /if\(err&&err\.code==="POS_STOCK_CONFLICT"\)loadMenu\(state\.menuArea\);/,
    "a 409 POS_STOCK_CONFLICT reloads the menu so the picker shows current availability");
  assert.doesNotMatch(handler, /POS_STOCK_CONFLICT[^;]*;[^;]*state\.cart=\[\]/,
    "the conflict must not clear the cart — the operator adjusts it");
});

test("room-service wording is used in room-service mode", () => {
  assert.match(POS, /function orderNoun\(\)\{return mode\(\)==="ROOM_SERVICE"\?"room-service order":"order";\}/);
});

test("units are singular/pluralised from the server's unit, never hardcoded", () => {
  const unitHelper = POS.slice(POS.indexOf("function unitWord"), POS.indexOf("function stockLocationName"));
  assert.match(unitHelper, /if\(!token\)token="unit";/, "a missing unit falls back to 'unit'");
  assert.match(unitHelper, /Number\(quantity\)===1/, "quantity drives singular/plural");
  // The stock-display helpers must carry no store names of their own: every
  // location enters the messages through stockLocationName(stock), i.e. from
  // the API response. (The specific message tests above enforce the same for
  // the scan/add paths. Unrelated legacy copy elsewhere on the page — the
  // returns panel's prose — is out of scope for this fix.)
  const stockCode = POS.slice(POS.indexOf("Stock source display"), POS.indexOf("async function loadMenu"));
  assert.ok(stockCode.length > 500, "the stock-display code block was found");
  for (const store of ["Bar Store", "Main Store", "Kitchen Store", "bar store", "main store", "kitchen store"]) {
    assert.ok(!stockCode.includes(store), `no store name may be hardcoded in the stock-display code (found "${store}")`);
  }
});
