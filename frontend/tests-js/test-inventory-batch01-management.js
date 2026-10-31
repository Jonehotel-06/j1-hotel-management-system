/* Batch 01 — inventory management, stock control and reordering (UI contracts).
   Run from /frontend: node --test tests-js/test-inventory-batch01-management.js */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const FRONTEND = path.resolve(__dirname, "..");
const INVENTORY = fs.readFileSync(path.join(FRONTEND, "dashboard", "inventory.html"), "utf8");
const PROCUREMENT = fs.readFileSync(path.join(FRONTEND, "dashboard", "procurement.html"), "utf8");

/* The pages' inline helpers are single-line minified functions; slice exactly
   one of them (brace-matched) so the assertions run the real code. */
function sliceFunction(source, name) {
  const start = source.indexOf("function " + name + "(");
  assert.notEqual(start, -1, name + " must exist");
  let depth = 0;
  for (let i = source.indexOf("{", start); i < source.length; i += 1) {
    const ch = source[i];
    if (ch === "{") depth += 1;
    else if (ch === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  throw new Error("unbalanced braces while slicing " + name);
}

function sandbox(functions, source, extra) {
  const code = functions.map((name) => sliceFunction(source, name)).join("\n") + "\n";
  const context = Object.assign({ JONE: { trimQuantityValue: (value) => String(value) }, money: (v) => String(v), date: (v) => String(v) }, extra || {});
  vm.createContext(context);
  vm.runInContext(code, context);
  return context;
}

/* ------------------------------- Task 1 ---------------------------------- */
test("stock units render as human labels, never the raw ledger token", () => {
  const ctx = sandbox(["unitLabel", "balanceUnitLabel", "qtyLabel"], INVENTORY, {
    qty: (value) => String(value),
  });
  assert.equal(ctx.unitLabel("PIECE"), "Pieces");
  assert.equal(ctx.unitLabel("KG"), "Kilograms (kg)");
  assert.equal(ctx.unitLabel("CRATE"), "Crates");
  assert.equal(ctx.unitLabel("gallon"), "Gallons");      // case-insensitive
  assert.equal(ctx.unitLabel("WEIRD_CUSTOM"), "WEIRD_CUSTOM"); // never blank
  // The API's own label wins when present (server stays authoritative).
  assert.equal(ctx.balanceUnitLabel({ base_unit: "KG", base_unit_label: "Kilograms (kg)" }), "Kilograms (kg)");
  assert.equal(ctx.balanceUnitLabel({ base_unit: "CRATE" }), "Crates");
  assert.equal(ctx.qtyLabel("12.0000", { base_unit: "PIECE" }), "12.0000 Pieces");
});

test("the add-item dialog distinguishes a base unit from an opening quantity unit", () => {
  assert.match(INVENTORY, /Count stock in \(base unit\)/);
  assert.match(INVENTORY, /Opening quantity unit/);
  assert.match(INVENTORY, /Base units in one selected unit/);
  // The opening quantity is converted server-side; the browser never multiplies.
  assert.doesNotMatch(INVENTORY, /opening_quantity=String\(Number/);
});

/* ------------------------------- Task 2 ---------------------------------- */
test("stock at hand offers an Edit action only to adjustment-capable staff", () => {
  assert.match(INVENTORY, /function canAdjustBalance\(\)\{return manager\(\);\}/);
  assert.match(INVENTORY, /if\(canAdjustBalance\(\)\)actions\+='<button class="btn btn-sm btn-outline" type="button" data-inv-balance-edit=/);
  assert.match(INVENTORY, /data-inv-balance-edit/);
});

test("the adjustment dialog demands a corrected quantity and a reason and posts the audited action", () => {
  assert.match(INVENTORY, /async function adjustBalance\(balanceId\)/);
  assert.match(INVENTORY, /label:"Corrected quantity \("/);
  assert.match(INVENTORY, /name:"reason",label:"Reason for the correction",type:"text",required:true/);
  assert.match(INVENTORY, /API\.resourceAction\("inventoryBalances",balanceId,"adjust"/);
  assert.match(INVENTORY, /newIdempotencyKey\("stock-adjust"\)/);
  // The operator is told history is not rewritten.
  assert.match(INVENTORY, /never rewritten|no past movement is rewritten/i);
});

/* ------------------------------- Task 3 ---------------------------------- */
test("the SKU field searches existing items and refuses a duplicate", () => {
  assert.match(INVENTORY, /function skuSearchField\(\)/);
  assert.match(INVENTORY, /API\.list\("inventoryItems",\{search:term,page_size:6,ordering:"sku"\}\)/);
  assert.match(INVENTORY, /fields:\[skuSearchField\(\)/);
  assert.match(INVENTORY, /function duplicateSkuMessage\(row\)/);
  const ctx = sandbox(["duplicateSkuMessage"], INVENTORY);
  const message = ctx.duplicateSkuMessage({ sku: "WATER-1", name: "Bottled water" });
  assert.match(message, /SKU WATER-1 already exists/);
  assert.match(message, /Bottled water/);
  assert.match(message, /Nothing was created/);
  assert.match(message, /Replenish stock/);
});

test("selecting an existing SKU opens replenishment instead of creating a twin", () => {
  assert.match(INVENTORY, /Replenish this item instead/);
  assert.match(INVENTORY, /data-inv-replenish-existing/);
  assert.match(INVENTORY, /async function replenishStock\(prefill\)/);
  assert.match(INVENTORY, /API\.resourceAction\("inventoryItems",selected\.id,"replenish"/);
  assert.match(INVENTORY, /newIdempotencyKey\("stock-replenish"\)/);
  // Replenishment never sends master data — only quantity/unit/cost/reference.
  assert.match(INVENTORY, /delete payload\.item_id/);
  assert.doesNotMatch(INVENTORY, /replenish[\s\S]{0,400}payload\.category/);
});

/* ------------------------------- Task 4 ---------------------------------- */
test("stock at hand can be searched without reloading the page", () => {
  assert.match(INVENTORY, /id="inv-stock-search"/);
  assert.match(INVENTORY, /Search stock at hand/);
  assert.match(INVENTORY, /if\(search\)params\.search=search;/);
  assert.match(INVENTORY, /getElementById\("inv-stock-search"\)[\s\S]{0,200}loadBalances\(1\)/);
  // Loading, empty and error states are preserved; filters survive a search.
  assert.match(INVENTORY, /DATA\.loading\(balanceRows\)/);
  assert.match(INVENTORY, /No stock matches/);
  assert.match(INVENTORY, /DATA\.error\(balanceRows/);
  assert.match(INVENTORY, /params\.reorder="true"/);
  assert.match(INVENTORY, /params\.item_type=itemType/);
});

/* ------------------------------- Task 5 ---------------------------------- */
function reorderSandbox() {
  return sandbox(
    ["trimmed", "toScaled", "orderTotalText", "reorderUnitsPerPurchaseUnit", "reorderBaseQuantity",
     "reorderBaseUnitCost", "reorderLinePayload", "reorderTotalText", "reorderReviewText",
     "reorderQuotedTotalText", "reorderPurchaseUnits",
     "baseUnitOf", "packSizeOf", "reorderUnitLabel"],
    PROCUREMENT
  );
}

test("the reorder total is computed exactly from the purchase unit", () => {
  const ctx = reorderSandbox();
  const item = { id: 7, sku: "WATER", base_unit: "PIECE", units_per_pack: "24" };
  // 3 packs at 1,200.00 per pack = 72 pieces at 50.00 each = 3,600.00
  assert.equal(ctx.reorderBaseQuantity({ reorder_quantity: "3", purchase_unit: "PACK" }, item), "72");
  assert.equal(ctx.reorderBaseUnitCost({ unit_purchase_price: "1200", purchase_unit: "PACK" }, item), "50");
  assert.equal(ctx.reorderTotalText({ reorder_quantity: "3", purchase_unit: "PACK", unit_purchase_price: "1200" }, item), "3600.00");
  // Base-unit purchase: 5 × 49.99
  assert.equal(ctx.reorderTotalText({ reorder_quantity: "5", purchase_unit: "BASE", unit_purchase_price: "49.99" }, item), "249.95");
  // A pack price that does not divide into kobo per piece is stored exactly as
  // the server stores it (1,000.00 / 24 = 41.67 per piece → 1,000.08), and the
  // review step discloses the difference from the supplier's own quote.
  assert.equal(ctx.reorderTotalText({ reorder_quantity: "1", purchase_unit: "PACK", unit_purchase_price: "1000" }, item), "1000.08");
  assert.equal(ctx.reorderQuotedTotalText({ reorder_quantity: "1", unit_purchase_price: "1000" }), "1000.00");
  const roundingReview = ctx.reorderReviewText({ reorder_quantity: "1", purchase_unit: "PACK", unit_purchase_price: "1000" }, item, {});
  assert.match(roundingReview, /supplier quote for the same quantity is 1000\.00/);
  // PACK without a declared pack size cannot be converted: no invented figure.
  const loose = { id: 8, sku: "LOOSE", base_unit: "PIECE" };
  assert.equal(ctx.reorderBaseQuantity({ reorder_quantity: "1", purchase_unit: "PACK" }, loose), null);
  assert.equal(ctx.reorderTotalText({ reorder_quantity: "1", purchase_unit: "PACK", unit_purchase_price: "10" }, loose), null);
});

test("the reorder dialog only asks for changeable fields and keeps the item details read-only", () => {
  assert.match(PROCUREMENT, /async function reorderItem\(itemId,fromLocationId\)/);
  assert.match(PROCUREMENT, /API\.inventoryReorderContext\(itemId\)/);
  for (const field of ["reorder_quantity", "purchase_unit", "unit_purchase_price", "expected_on", "notes"]) {
    assert.match(PROCUREMENT, new RegExp('name:"' + field + '"'), field + " must be enterable");
  }
  // Item identity comes from the server, never re-typed.
  assert.match(PROCUREMENT, /data-proc-reorder/);
  assert.match(PROCUREMENT, /record\.sku/);
  assert.match(PROCUREMENT, /context\.last_receipt/);
});

test("a reorder is reviewed before submission and creates only a draft", () => {
  assert.match(PROCUREMENT, /Review reorder/);
  assert.match(PROCUREMENT, /JONE\.ui\.confirm\(\{title:"Create this reorder draft\?"/);
  assert.match(PROCUREMENT, /reorderReviewText\(entered,record,context/);
  assert.match(PROCUREMENT, /API\.create\("purchaseOrders"/);
  assert.match(PROCUREMENT, /newIdempotencyKey\("purchase-order"\)/);
  const ctx = reorderSandbox();
  const item = { id: 7, sku: "WATER", name: "Bottled water", base_unit: "PIECE", units_per_pack: "24" };
  const review = ctx.reorderReviewText(
    { reorder_quantity: "3", purchase_unit: "PACK", unit_purchase_price: "1200", expected_on: "2026-11-15" },
    item, {}, { supplier: "Acme", location: "MAIN · Main store" }
  );
  assert.match(review, /Quantity: 3 pack\/bag = 72 pieces/i);
  assert.match(review, /Order total: 3600\.00/);
  assert.match(review, /Supplier: Acme/);
  assert.match(review, /Deliver to: MAIN/);
  assert.match(review, /No stock is added now/i);
  assert.match(review, /approval/i);
});

test("the inventory screen links straight into the reorder dialog", () => {
  // Batch 04: the link also carries the low-stock row's store (&location=<id>).
  assert.match(INVENTORY, /function reorderHref\(itemId,locationId\)\{var href="procurement\.html\?reorder="/);
  assert.match(INVENTORY, /esc\(reorderHref\(row\.item,row\.location\)\)/);
  assert.match(INVENTORY, /if\(canReorder\(\)\)actions\+='<a class="btn btn-sm btn-outline" href="/);
  // The auto-open reads ?reorder= from the URL (batch 04 also reads ?location=).
  assert.match(PROCUREMENT, /new URLSearchParams\(window\.location\.search\)/);
  assert.match(PROCUREMENT, /\.get\("reorder"\)/);
});
