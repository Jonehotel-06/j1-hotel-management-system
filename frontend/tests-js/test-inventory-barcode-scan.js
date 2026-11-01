/* tests-js/test-inventory-barcode-scan.js
 *
 * Task 03 of targeted_fixes_01 — USB HID barcode scanning on the INVENTORY
 * screen (the POS terminal's scanner workflow already existed and is untouched).
 *
 * What these tests pin, and why:
 *
 *   • the stock page has a dedicated barcode field that accepts BOTH terminators
 *     a USB HID scanner can be configured with (Enter and Tab);
 *   • each scan becomes exactly ONE server lookup — scans are serialised, so a
 *     burst cannot race or double-post;
 *   • a repeated code inside the guard window does not fire again;
 *   • scanning is a LOOKUP: the page never posts a movement, adjustment,
 *     receipt, issue or count from the scan handler;
 *   • "not found" is surfaced clearly, with the scanned code in the message.
 */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const FRONTEND = path.resolve(__dirname, "..");
const read = (rel) => fs.readFileSync(path.join(FRONTEND, rel), "utf8");
const INVENTORY = read("dashboard/inventory.html");

/* The scan handler lives in one inline script block; isolate it so assertions
   are about the scanner, not about unrelated inventory code. */
const scanBlock = (() => {
  const start = INVENTORY.indexOf("Task 03 — USB HID barcode scanning");
  assert.ok(start > 0, "the inventory scan handler must be present");
  return INVENTORY.slice(start, INVENTORY.indexOf("applyRecordingControls();", start));
})();

test("the stock screen has a scanner field wired to the barcode target", () => {
  assert.match(INVENTORY, /id="inv-barcode"[^>]*data-barcode-target/);
  assert.match(INVENTORY, /data-inv-scan-status/);
  assert.match(INVENTORY, /data-inv-scan-result/);
  assert.match(INVENTORY, /data-inv-scan-clear/);
});

test("the field accepts both Enter and Tab terminators", () => {
  assert.match(scanBlock, /event\.key==="Enter"\|\|event\.key==="Tab"/);
  /* Enter inside the barcode field must never submit or reload the page. */
  assert.match(scanBlock, /event\.preventDefault\(\)/);
});

test("a scan performs exactly one server lookup per code", () => {
  assert.match(scanBlock, /window\.API\.get\(lookupUrl,\{params:\{barcode:scanned\}/);
  assert.match(scanBlock, /inventoryBarcodeLookup/);
  /* Serialised through a promise chain: the second scan waits for the first. */
  assert.match(scanBlock, /scanQueue=scanQueue\.then/);
  assert.match(scanBlock, /return scanQueue;/);
});

test("a keyboard burst still resolves through the shared scanner module", () => {
  assert.match(scanBlock, /JONE\.barcode\.initScanner\(\{onScan:function\(code\)\{handleStockScan\(code\);\}\}\)/);
  /* ...and the listener is torn down when the page goes away. */
  assert.match(scanBlock, /pagehide.*destroy|destroy\(\).*pagehide/s);
  assert.match(scanBlock, /invScannerHandle\.destroy\(\)/);
});

test("a repeated code inside the guard window does not re-fire", () => {
  assert.match(scanBlock, /scanned===lastScanCode&&now-lastScanAt<3000/);
  assert.match(scanBlock, /Already showing/);
});

test("the scan handler never posts a stock movement", () => {
  /* Nothing in the scan block may create a movement: no POST/PATCH to any
     stock endpoint, and no call into the recording services. */
  for (const verb of ["API.post", "API.create", "API.update", "API.patch"]) {
    assert.ok(!scanBlock.includes(verb), `the scan handler must not call ${verb}`);
  }
  for (const endpoint of [
    "inventoryOpening", "inventoryIssues", "inventoryTransfers",
    "inventoryReturns", "inventoryStockCounts", "inventoryBalanceAdjust",
  ]) {
    assert.ok(!scanBlock.includes(endpoint), `the scan handler must not touch ${endpoint}`);
  }
  /* The only request it makes is a GET of the read-only lookup. */
  assert.equal((scanBlock.match(/API\.get\(/g) || []).length, 1);
});

test("the screen states that a scan records nothing", () => {
  assert.match(INVENTORY, /A scan only <strong>identifies<\/strong> the item/);
  assert.match(INVENTORY, /never records stock, a receipt or an adjustment/);
  assert.match(scanBlock, /This is a lookup only — nothing has been recorded\./);
  assert.match(scanBlock, /Nothing recorded — choose an action to change stock\./);
});

test("an unknown barcode produces a clear not-found message naming the code", () => {
  assert.match(scanBlock, /status===404\?"No stock item matches the barcode "\+scanned/);
  assert.match(scanBlock, /Check the code, or add the item to the catalogue first\./);
  /* The message is announced and shown, not swallowed. */
  assert.match(scanBlock, /setScanStatus\(message,"error"\)/);
  assert.match(scanBlock, /JONE\.ui\.toast\(message,"error"\)/);
});

test("a permission refusal is explained rather than shown as a generic error", () => {
  assert.match(scanBlock, /status===403\?"You do not have permission to look up stock items\."/);
});

test("the result panel shows identity and balances, never an action", () => {
  assert.match(scanBlock, /Identified by /);
  assert.match(scanBlock, /Total on hand/);
  assert.match(scanBlock, /payload\.locations/);
  /* No "record"/"adjust"/"issue" button is produced by the scan result. */
  assert.doesNotMatch(scanBlock, /<button[^>]*>(?:Record|Adjust|Issue|Receive)/);
});

test("the POS scanner workflow is unchanged by this work", () => {
  const pos = read("dashboard/pos.html");
  assert.match(pos, /id="pos-barcode" data-barcode-target/);
  assert.match(pos, /endpoint\("posBarcodeLookup"\)/);
  assert.match(pos, /Product barcode not found\./);
  /* The stock lookup is a different endpoint, so the two never share state. */
  assert.match(read("js/config.js"), /posBarcodeLookup:\s+"\/api\/admin\/pos\/menu\/barcode-lookup\/"/);
  assert.match(read("js/config.js"), /inventoryBarcodeLookup:\s+"\/api\/admin\/inventory\/items\/barcode-lookup\/"/);
});
