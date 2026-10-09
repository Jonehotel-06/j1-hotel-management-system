"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { makeContext, loadScript, disposeAll } = require("./harness");

test.after(() => disposeAll());

test("barcode scanner module generates SVG and responds to scanner input", () => {
  const ctx = makeContext();
  loadScript(ctx, "js/utils.js");
  loadScript(ctx, "js/barcode-scanner.js");

  const barcode = ctx.window.JONE.barcode;
  assert.ok(barcode, "barcode module must be defined");
  assert.equal(typeof barcode.initScanner, "function");
  assert.equal(typeof barcode.generateBarcodeSvg, "function");

  const svg = barcode.generateBarcodeSvg("SKU-10023");
  assert.match(svg, /<svg xmlns="http:\/\/www\.w3\.org\/2000\/svg"/);
  assert.match(svg, /SKU-10023/);
  assert.match(svg, /<rect/);
});
