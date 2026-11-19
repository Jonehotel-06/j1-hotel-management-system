/* tests-js/test-fingerprint-workstation-context.js
   127.0.0.1 is the device running the browser. On a phone it is the phone,
   so a loopback capture can never reach the reader on the reception PC —
   the page must say that instead of "start the bridge on this workstation".
   Run from /frontend: node --test tests-js/test-fingerprint-workstation-context.js */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { makeContext, loadScript, disposeAll } = require("./harness");

test.after(() => disposeAll());

const ANDROID_UA =
  "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/130.0 Mobile Safari/537.36";

function makeBio(userAgent) {
  const ctx = makeContext();
  ctx.window.navigator = { userAgent };
  ctx.window.fetch = async () => {
    throw new Error("fetch must not be called for a mobile context");
  };
  loadScript(ctx, "js/biometrics.js");
  return ctx;
}

test("Android context is recognised as the wrong workstation", async () => {
  const ctx = makeBio(ANDROID_UA);
  const bio = ctx.window.JONE.biometrics;
  assert.equal(bio.isMobileBrowserContext(), true);
  const status = await bio.getReaderStatus();
  assert.equal(status.connected, false);
  assert.equal(status.bridge_reachable, false);
  assert.equal(status.bridge_error_code, "WRONG_WORKSTATION");
  assert.match(status.detail, /reception Windows workstation/);
  await assert.rejects(
    () => bio.captureMinutiaeProbe({ fingerPosition: "RIGHT_INDEX" }),
    (err) => err.code === "WRONG_WORKSTATION" && /reception PC/.test(err.message)
  );
});

test("desktop context still uses the bridge transport", async () => {
  const ctx = makeBio("Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130.0");
  assert.equal(ctx.window.JONE.biometrics.isMobileBrowserContext(), false);
});
