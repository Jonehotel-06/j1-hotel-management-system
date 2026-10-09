"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { makeContext, loadScript, disposeAll } = require("./harness");

test.after(() => disposeAll());

test("biometrics client helper provides minutiae capture and enrollment API bindings", async () => {
  const ctx = makeContext();
  loadScript(ctx, "js/utils.js");
  loadScript(ctx, "js/biometrics.js");

  const bio = ctx.window.JONE.biometrics;
  assert.ok(bio, "biometrics module must be defined");
  assert.equal(typeof bio.captureMinutiaeProbe, "function");
  assert.equal(typeof bio.requestEnrollment, "function");
  assert.equal(typeof bio.issueTemporaryWorkstationToken, "function");

  const minutiae = await bio.captureMinutiaeProbe();
  assert.ok(Array.isArray(minutiae));
  assert.ok(minutiae.length >= 10);
  assert.ok(minutiae[0].x !== undefined);
  assert.ok(minutiae[0].y !== undefined);
  assert.ok(minutiae[0].angle !== undefined);
});
