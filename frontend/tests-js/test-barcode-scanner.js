"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { makeContext, loadScript, disposeAll, FRONTEND } = require("./harness");

test.after(() => disposeAll());

function bootBarcode() {
  const ctx = makeContext();
  loadScript(ctx, "js/utils.js");
  loadScript(ctx, "js/barcode-scanner.js");
  return ctx;
}

test("barcode scanner module generates SVG and responds to scanner input", () => {
  const ctx = bootBarcode();
  const barcode = ctx.window.JONE.barcode;
  assert.ok(barcode, "barcode module must be defined");
  assert.equal(typeof barcode.initScanner, "function");
  assert.equal(typeof barcode.generateBarcodeSvg, "function");

  const svg = barcode.generateBarcodeSvg("SKU-10023");
  assert.match(svg, /<svg xmlns="http:\/\/www\.w3\.org\/2000\/svg"/);
  assert.match(svg, /SKU-10023/);
  assert.match(svg, /<rect/);
});

test("Code 128 encoding uses Start B, a modulo-103 checksum, and Stop", () => {
  const ctx = bootBarcode();
  const barcode = ctx.window.JONE.barcode;
  // "ABC": values B=104, A=33, B=34, C=35; checksum (104 + 33 + 68 + 105) % 103 = 1.
  assert.deepEqual(Array.from(barcode.encodeCode128("ABC")), [104, 33, 34, 35, 1, 106]);
  // Digits encode in subset B too and remain a valid Code 128 symbol.
  const values = Array.from(barcode.encodeCode128("5901234123457"));
  assert.equal(values[0], 104);
  assert.equal(values[values.length - 1], 106);
  const sum = values.slice(0, -2).reduce((acc, v, i) => acc + (i === 0 ? v : v * i), 0);
  assert.equal(values[values.length - 2], sum % 103);
  // Every emitted pattern is a real Code 128 element-width pattern summing to 11 (stop: 13).
  for (const value of values) {
    const pattern = barcode.CODE128_PATTERNS[value];
    assert.match(pattern, /^[1-4]+$/);
    const total = pattern.split("").reduce((a, c) => a + Number(c), 0);
    assert.equal(total, value === 106 ? 13 : 11);
  }
  assert.throws(() => barcode.generateBarcodeSvg("bad\u0000code"), /Code 128/);
});

test("rapid Enter-terminated burst scans once; slow typing and other inputs do not", () => {
  const ctx = bootBarcode();
  const handlers = [];
  ctx.window.addEventListener = (type, fn) => { if (type === "keydown") handlers.push(fn); };
  ctx.window.removeEventListener = (type, fn) => {
    const i = handlers.indexOf(fn); if (i >= 0) handlers.splice(i, 1);
  };
  loadScript(ctx, "js/barcode-scanner.js");
  const scans = [];
  const handle = ctx.window.JONE.barcode.initScanner({ onScan: (code) => scans.push(code), dedupeMs: 0 });
  const body = { tagName: "BODY", dataset: {} };
  const input = { tagName: "INPUT", type: "text", dataset: {}, value: "" };
  const at = (key, target, time) => handlers.forEach((fn) => fn({ key, target, preventDefault() {} }));
  let now = 1000;
  const realNow = Date.now;
  Date.now = () => now;
  try {
    // Hardware burst on the page body.
    for (const ch of "5901234123457") { at(ch, body); now += 10; }
    at("Enter", body);
    assert.deepEqual(scans, ["5901234123457"]);
    // Typing into a payment/search input is never captured as a scan.
    scans.length = 0;
    for (const ch of "123456") { at(ch, input); now += 10; }
    at("Enter", input);
    assert.deepEqual(scans, []);
    // A dedicated barcode field falls back to its value for manual entry.
    scans.length = 0;
    const barcodeInput = { tagName: "INPUT", type: "text", dataset: { barcodeTarget: "" }, value: " MANUAL-1 " };
    at("Enter", barcodeInput);
    assert.deepEqual(scans, ["MANUAL-1"]);
    assert.equal(barcodeInput.value, "", "barcode field clears after a scan");
    // destroy() removes the listener.
    handle.destroy();
    assert.equal(handlers.length, 0);
  } finally {
    Date.now = realNow;
  }
});

test("duplicate bursts from one physical scan are de-duplicated", () => {
  const ctx = bootBarcode();
  ctx.window.addEventListener = () => {};
  ctx.window.removeEventListener = () => {};
  const scans = [];
  const handle = ctx.window.JONE.barcode.initScanner({ onScan: (code) => scans.push(code) });
  // scan() applies the same dedupe window used for key bursts.
  assert.equal(handle.scan("DRINK-001"), true);
  assert.equal(handle.scan("DRINK-001"), false);
  assert.deepEqual(scans, ["DRINK-001"]);
  handle.destroy();
});

test("POS page wires scans through the server lookup and increments one cart line", () => {
  const page = fs.readFileSync(path.join(FRONTEND, "dashboard", "pos.html"), "utf8");
  assert.match(page, /id="pos-barcode" data-barcode-target/);
  assert.match(page, /data-pos-scan-status/);
  assert.match(page, /endpoint\("posBarcodeLookup"\)/);
  assert.match(page, /Product barcode not found\. Check the barcode or register the product first\./);
  assert.match(page, /existing\.quantity=Number\(existing\.quantity\)\+1/);
  assert.match(page, /scannerHandle\.destroy\(\)/);
  assert.match(page, /The product could not be verified/);
  assert.doesNotMatch(page, /itemMatch = it/);
  const config = fs.readFileSync(path.join(FRONTEND, "js", "config.js"), "utf8");
  assert.match(config, /posBarcodeLookup:\s+"\/api\/admin\/pos\/menu\/barcode-lookup\/"/);
  const labels = fs.readFileSync(path.join(FRONTEND, "dashboard", "barcode-labels.html"), "utf8");
  assert.match(labels, /Code 128/);
  assert.match(labels, /item\.barcode \|\| item\.sku/);
});

test("profile page renders the self-service employment records from the owner-scoped endpoint", () => {
  const page = fs.readFileSync(path.join(FRONTEND, "dashboard", "profile.html"), "utf8");
  for (const tab of ["overview", "employment", "salary", "attendance", "shifts", "leave", "disciplinary", "performance", "guest", "documents"]) {
    assert.match(page, new RegExp('data-ss-tab="' + tab + '"'), `missing self-service tab ${tab}`);
    assert.match(page, new RegExp('data-ss-panel="' + tab + '"'), `missing self-service panel ${tab}`);
  }
  assert.match(page, /API_ENDPOINTS\.myStaffProfile/);
  assert.match(page, /No disciplinary records available/);
  assert.match(page, /not a proven offence/);
  assert.match(page, /No payroll compensation has been configured/);
  assert.doesNotMatch(page, /myStaffProfile[^;]*staff_id/);
  const config = fs.readFileSync(path.join(FRONTEND, "js", "config.js"), "utf8");
  assert.match(config, /myStaffProfile:\s+"\/api\/admin\/staff-operations\/my-profile\/"/);
});
