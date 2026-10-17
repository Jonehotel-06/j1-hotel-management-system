/* Procurement editing, quantity-type and receipt-upload contracts.
   Run from /frontend: node --test tests-js/test-procurement-editing-ui.js

   Three staff requests are pinned here:

   * an Edit action that exists ONLY before approval (and stops being rendered
     the moment the order is approved);
   * a purchase-order line that states which unit the typed quantity is in
     (pieces vs pack/bag) and converts it to pieces exactly once;
   * an approval step that can file the supplier invoice / payment receipt.

   The server enforces the same boundaries (apps/inventory/services), so these
   tests check the page agrees with it rather than trusting the browser. */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { FRONTEND } = require("./harness");

const PAGE = fs.readFileSync(path.join(FRONTEND, "dashboard", "procurement.html"), "utf8");
const ACCOUNTING = fs.readFileSync(path.join(FRONTEND, "dashboard", "accounting.html"), "utf8");

function sliceFunction(name) {
  const start = PAGE.indexOf("function " + name + "(");
  assert.ok(start > 0, name + " is missing from the shipped page");
  // Balanced-brace scan from the function's opening brace.
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

function loadConversions() {
  const ctx = { Math, String, Number, Error };
  vm.createContext(ctx);
  vm.runInContext(sliceFunction("trimmed"), ctx);
  vm.runInContext(sliceFunction("linesToBaseUnits"), ctx);
  return ctx;
}

/* ------------------------------- Edit action ------------------------------ */

test("Edit is offered only while the order is still a proposal", () => {
  assert.match(PAGE, /function canEdit\(status\)\{return status==="DRAFT"\|\|status==="SUBMITTED";\}/);
  assert.match(PAGE, /canEdit\(row\.status\)\?'<button class="btn btn-sm btn-outline" type="button" data-proc-order-edit=/,
    "the list row must not render an Edit button once approval has happened");
  assert.match(PAGE, /if\(canEdit\(order\.status\)\)action\("Edit","btn-outline"/,
    "the detail modal shows Edit under exactly the same rule");
});

test("editing goes through the controlled PATCH endpoint, not a second create", () => {
  const edit = sliceFunction("editOrder");
  assert.match(edit, /window\.API\.update\("purchaseOrders",reference,payload\)/);
  // An emptied optional field is an edit too, so it is sent explicitly.
  assert.match(edit, /payload\.notes=values\.notes\|\|""/);
  assert.match(edit, /payload\.lines=linesToBaseUnits\(values\.lines\)/);
  // The client refuses a stale dialog too, and resyncs the list when it does.
  assert.match(edit, /already been approved and can no longer be edited/);
  assert.match(edit, /loadOrders\(\)/);
});

test("the edit dialog starts from the stored order instead of an empty form", () => {
  assert.match(PAGE, /function initialLinesFor\(order\)/);
  assert.match(PAGE, /quantity_ordered:JONE\.trimQuantityValue\(String\(line\.quantity_ordered\)\)/);
  assert.match(PAGE, /expected_on:order\.expected_on\|\|""/);
});

/* ----------------------------- Quantity types ----------------------------- */

test("each purchase-order line states the unit its quantity is typed in", () => {
  assert.match(PAGE, /name:"quantity_unit",label:"Quantity type",type:"select"/);
  assert.match(PAGE, /label:"Pieces — counted in "\+unit/);
  assert.match(PAGE, /label:"Pack \/ bag — holds "/);
  assert.match(PAGE, /name:"pieces_per_pack",label:"Pieces in one pack\/bag"/);
});

test("a pack quantity is converted to pieces exactly once", () => {
  const ctx = loadConversions();
  const converted = ctx.linesToBaseUnits([
    { record: { id: 7, sku: "RICE" }, quantity_unit: "PIECE", quantity_ordered: "4", unit_cost: "1500" },
    { record: { id: 8, sku: "WATER" }, quantity_unit: "PACK", pieces_per_pack: "24", quantity_ordered: "2", unit_cost: "2400" },
  ]);
  // The objects come from another vm realm, so compare their JSON form.
  assert.equal(JSON.stringify(converted[0]), JSON.stringify({ item_id: 7, quantity_ordered: "4", unit_cost: "1500", notes: "" }));
  // 2 packs of 24 = 48 pieces, and the pack price becomes a per-piece price.
  assert.equal(JSON.stringify(converted[1]), JSON.stringify({ item_id: 8, quantity_ordered: "48", unit_cost: "100", notes: "" }));
});

test("a pack line without its pack size is refused instead of guessed", () => {
  const ctx = loadConversions();
  assert.throws(
    () => ctx.linesToBaseUnits([{ record: { id: 9, sku: "OIL" }, quantity_unit: "PACK", quantity_ordered: "1", unit_cost: "100" }]),
    /how many pieces are in one pack\/bag/,
  );
});

/* --------------------------- Approval evidence ---------------------------- */

test("approving captures the payment reference and the invoice/receipt file", () => {
  const approve = sliceFunction("approveOrder");
  assert.match(approve, /multipart:true/);
  assert.match(approve, /name:"payment_reference"/);
  assert.match(approve, /name:"approval_receipt",label:"Supplier invoice \/ payment receipt/);
  assert.match(approve, /resourceAction\("purchaseOrders",order\.reference,"approve",fd\)/);
});

test("a receipt can also be filed after approval, and replaces the old one", () => {
  const upload = sliceFunction("uploadApprovalReceipt");
  assert.match(upload, /resourceAction\("purchaseOrders",reference,"approval-receipt",fd\)/);
  assert.match(upload, /Uploading replaces the file already attached/);
  assert.match(PAGE, /if\(canAttachReceipt\(order\.status\)&&manager\(\)\)action\("Upload invoice \/ receipt"/);
});

test("the detail view links the stored invoice and shows the payment reference", () => {
  assert.match(PAGE, /function receiptLink\(order\)/);
  assert.match(PAGE, /Open supplier invoice \/ payment receipt/);
  assert.match(PAGE, /order\.payment_reference\?'<div><dt>Payment reference<\/dt>/);
});

/* --------------------------- Receipts & wrapping -------------------------- */

test("the receipt grid posts pieces, never pack counts", () => {
  const receipt = sliceFunction("receiptField");
  assert.match(receipt, /pieces=qty\*perPack/);
  assert.match(receipt, /<option value="PIECE">Pieces \(/);
  // The dialog previews what will actually be posted to the ledger.
  assert.match(receipt, /data-proc-receipt-preview/);
  assert.match(receipt, /Posts to stock/);
});

test("procurement tables freeze their atomic columns and scroll as a block", () => {
  assert.match(PAGE, /<td class="cell-nowrap"><strong>'\+JONE\.esc\(row\.reference\)/);
  assert.match(PAGE, /<td class="cell-nowrap"><div class="row-actions">/);
  assert.match(PAGE, /<td class="hide-sm num cell-nowrap">/);
  assert.match(PAGE, /<table class="table table-receipt">/);
  const css = fs.readFileSync(path.join(FRONTEND, "css", "dashboard.css"), "utf8");
  assert.match(css, /\.table-receipt th, \.table-receipt td \{ white-space: nowrap;/);
});

/* -------------------------------- Expenses -------------------------------- */

test("an expense can carry an optional invoice/receipt file", () => {
  assert.match(ACCOUNTING, /name: "receipt_file", label: "Supplier invoice \/ receipt file \(optional\)", type: "file"/);
  assert.match(ACCOUNTING, /multipart: true/);
  assert.match(ACCOUNTING, /body\.append\("receipt_file", values\.receipt_file/);
  // The file is optional: the plain JSON path stays for expenses without one.
  assert.match(ACCOUNTING, /await window\.API\.create\("financeExpenses", payload\);/);
  assert.match(ACCOUNTING, /detail\.receipt_file_url/);
});
