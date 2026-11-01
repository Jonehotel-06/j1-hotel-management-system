/* Auto SKU barcodes + printable labels: new products without a typed
   barcode adopt their SKU, and staff can print chosen quantities of
   real Code 128 labels to stick on products.
   Run from /frontend: node --test tests-js/test-barcode-auto-labels-ui.js */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const FRONTEND = path.resolve(__dirname, "..");
const read = (rel) => fs.readFileSync(path.join(FRONTEND, rel), "utf8");
const POS = read("dashboard/pos.html");
const LABELS = read("dashboard/barcode-labels.html");

test("Add menu item explains the automatic SKU barcode and reports it back", () => {
  assert.match(POS, /Leave blank and the SKU automatically becomes this product's barcode/);
  assert.match(POS, /assignedBarcode/);
  assert.match(POS, /Menu management → Barcode labels/);
});

test("Menu management links to the label printing page", () => {
  /* The link lives inside the POS menu-management block AND is resolved
     through the access registry: printing labels is a stores/menu task, so a
     department the label page refuses is never offered the shortcut. */
  assert.match(POS, /<a class="btn btn-sm btn-outline" href="barcode-labels\.html" data-pos-barcode-link hidden>Print barcode labels<\/a>/);
  assert.match(POS, /canOpenPage\("barcode-labels\.html"\)/);
  assert.match(POS, /automatically use their SKU as their barcode/);
});

test("Labels page prints a chosen quantity per product", () => {
  /* Batch 01 task 01 moved the printing model into js/barcode-labels.js so the
     same code can be unit-tested. The page keeps a default "labels per
     product" box AND a per-product quantity, which is what the operator needs
     when one shelf has four units and another has thirty. */
  assert.match(LABELS, /id="label-copies"/);
  assert.match(LABELS, /id="apply-copies"/);
  assert.match(LABELS, /id="print-labels"/);
  assert.match(LABELS, /js\/barcode-labels\.js/);

  const MODEL = read("js/barcode-labels.js");
  assert.match(MODEL, /function setDefaultCopies\(state, value\)/);
  assert.match(MODEL, /function setQuantity\(state, item, value\)/);
  assert.match(MODEL, /function labelCount\(state\)/);
  /* One label object per requested copy, built from the same plan the preview
     renders — what is previewed is exactly what prints. */
  assert.match(MODEL, /for \(var i = 0; i < count; i\+\+\)/);
});

test("Labels encode the stored barcode and never fabricate a value", () => {
  const MODEL = read("js/barcode-labels.js");
  assert.match(MODEL, /return String\(item\.barcode \|\| item\.sku \|\| item\.item_sku \|\| ""\)\.trim\(\)/);
  assert.doesNotMatch(MODEL, /"SKU-" \+ item\.id/);
  assert.match(MODEL, /JONE\.barcode/);
  assert.match(MODEL, /generateBarcodeSvg\(code/);
  /* An unencodable code is reported, never replaced with a guess. */
  assert.match(MODEL, /cannot be printed as Code 128/);
});
