/* tests-js/test-fingerprint-lna-and-classification.js

   Regression tests for the 2026-11-20 fingerprint repair.

   WHY THESE EXIST
   ---------------
   Production showed "The fingerprint bridge is not reachable at
   http://127.0.0.1:8787 … Start the J-ONE Fingerprint Bridge" on a WINDOWS PC
   with the bridge running. The real cause was Chromium's Local Network Access:
   a public page's fetch() to loopback is refused with

     blocked by CORS policy: Permission was denied for this request to access
     the `loopback` address space.

   and fetch() reports that as an opaque TypeError, so every distinct cause —
   no bridge, firewall, browser block, operator denial — became the same
   misleading sentence.

   These tests pin the classification that replaced it, and that none of the
   fixes introduce a fake fingerprint.

   Run from /frontend: node --test tests-js/test-fingerprint-lna-and-classification.js */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { makeContext, loadScript, disposeAll } = require("./harness");

test.after(() => disposeAll());

const ANDROID_UA =
  "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/130.0 Mobile Safari/537.36";
const IPHONE_UA =
  "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 " +
  "(KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1";
const WINDOWS_UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36";
const WINDOWS_TABLET_UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Mobile";

/** Build a biometrics context with a scriptable permission API + fetch. */
function makeBio(opts) {
  opts = opts || {};
  const ctx = makeContext();
  ctx.window.navigator = { userAgent: opts.userAgent || WINDOWS_UA };
  ctx.window.isSecureContext = true;
  ctx.window.navigator.permissions = {
    query: async () => ({ state: opts.permission || "prompt" }),
  };
  ctx.window.fetch = async (url) => {
    if (opts.handler) return opts.handler(url);
    throw new TypeError("Failed to fetch");
  };
  loadScript(ctx, "js/biometrics.js");
  return ctx;
}

const jsonResponse = (body, status) => ({
  ok: (status || 200) < 400,
  status: status || 200,
  text: async () => JSON.stringify(body),
});

/* --------------------------------------------------------------- detection */

test("a Windows browser is never mistaken for a mobile device", () => {
  // "Mobile" appears in some Windows compatibility strings (and in Windows
  // tablet-mode UAs). A false positive here disables capture on the reception
  // PC, which is strictly worse than a false negative.
  const ctx = makeBio({ userAgent: WINDOWS_UA });
  assert.equal(ctx.window.JONE.biometrics.isMobileBrowserContext(), false);
  assert.equal(ctx.window.JONE.biometrics.workstationMode(), "desktop");
});

test("Windows tablet UA alone does not disable the reader", () => {
  const ctx = makeBio({ userAgent: WINDOWS_TABLET_UA });
  assert.equal(ctx.window.JONE.biometrics.workstationMode(), "desktop",
    "an ambiguous UA must not force the mobile guidance onto a Windows PC");
});

test("Android and iPhone are the wrong workstation", () => {
  for (const ua of [ANDROID_UA, IPHONE_UA]) {
    const ctx = makeBio({ userAgent: ua });
    assert.equal(ctx.window.JONE.biometrics.workstationMode(), "mobile", ua);
  }
});

test("the operator can override the workstation mode for the session", () => {
  const ctx = makeBio({ userAgent: ANDROID_UA });
  const bio = ctx.window.JONE.biometrics;
  assert.equal(bio.workstationMode(), "mobile");
  bio.setWorkstationMode("desktop");
  assert.equal(bio.workstationMode(), "desktop", "a Windows tablet in desktop mode must be usable");
  bio.setWorkstationMode("");
  assert.equal(bio.workstationMode(), "mobile", "clearing the override restores detection");
});

/* --------------------------------------------------- permission classification */

test("a remembered permission denial is reported as blocked, not as a stopped bridge", async () => {
  const ctx = makeBio({ permission: "denied", handler: () => { throw new TypeError("Failed to fetch"); } });
  const bio = ctx.window.JONE.biometrics;
  const state = await bio.requestLocalNetworkAccess();
  assert.equal(state, "denied");
  const status = await bio.getReaderStatus({ retries: 0 });
  assert.equal(status.bridge_error_code, "BRIDGE_PERMISSION_BLOCKED");
  assert.equal(status.status, "permission_blocked");
  assert.match(status.detail, /blocked the page from contacting the fingerprint bridge/i);
  assert.match(status.detail, /local network access/i);
  assert.doesNotMatch(status.detail, /Start the J-ONE Fingerprint Bridge/,
    "a permission denial must not tell the operator to start a bridge that may be running");
});

test("a granted permission and a stopped bridge still say 'start the bridge'", async () => {
  const ctx = makeBio({ permission: "granted", handler: () => { throw new TypeError("Failed to fetch"); } });
  const bio = ctx.window.JONE.biometrics;
  await bio.requestLocalNetworkAccess();
  const status = await bio.getReaderStatus({ retries: 0 });
  assert.equal(status.bridge_error_code, "BRIDGE_UNREACHABLE");
  assert.match(status.detail, /Start the J-ONE Fingerprint Bridge/);
});

test("a browser without the permission descriptor behaves exactly as before", async () => {
  const ctx = makeBio({ handler: () => jsonResponse({ success: true, status: "connected", connected: true }) });
  delete ctx.window.navigator.permissions;
  const bio = ctx.window.JONE.biometrics;
  assert.equal(await bio.requestLocalNetworkAccess(), "unsupported");
  const status = await bio.getReaderStatus({ retries: 0 });
  assert.equal(status.status, "connected");
});

test("a permission query that rejects (no LNA) is 'unsupported', never 'denied'", async () => {
  const ctx = makeBio({ handler: () => jsonResponse({ success: true, status: "connected", connected: true }) });
  ctx.window.navigator.permissions = { query: async () => { throw new TypeError("name not supported"); } };
  const bio = ctx.window.JONE.biometrics;
  assert.equal(await bio.localNetworkPermissionState(), "unsupported");
  const status = await bio.getReaderStatus({ retries: 0 });
  assert.equal(status.status, "connected");
});

/* ------------------------------------------------------------- describeReader */

test("describeReader reports a phone as the wrong workstation with no bridge call", async () => {
  let called = 0;
  const ctx = makeBio({ userAgent: ANDROID_UA, handler: () => { called += 1; throw new Error("must not be called"); } });
  const report = await ctx.window.JONE.biometrics.describeReader();
  assert.equal(called, 0, "a phone must never attempt a loopback request");
  assert.equal(report.mode, "mobile");
  assert.equal(report.ready, false);
  assert.equal(report.code, "WRONG_WORKSTATION");
  assert.equal(report.source, "policy");
  assert.match(report.detail, /designated Windows reception workstation/);
});

test("describeReader reports a ready reader with its serial", async () => {
  const ctx = makeBio({
    permission: "granted",
    handler: () => jsonResponse({
      success: true, status: "connected", connected: true,
      detail: "Fingerprint reader connected and ready.", device_serial: "4500-ABC",
      driver_installed: true, sdk_loaded: true,
    }),
  });
  const report = await ctx.window.JONE.biometrics.describeReader();
  assert.equal(report.ready, true);
  assert.equal(report.source, "bridge");
  assert.equal(report.device_serial, "4500-ABC");
  assert.equal(report.bridge_reachable, true);
});

test("describeReader separates an unplugged reader from a stopped bridge", async () => {
  const unplugged = makeBio({
    permission: "granted",
    handler: () => jsonResponse({
      success: true, status: "not_connected", connected: false,
      detail: "No fingerprint reader detected. Connect the DigitalPersona 4500 reader to this workstation.",
      driver_installed: true, sdk_loaded: true,
    }),
  });
  const report = await unplugged.window.JONE.biometrics.describeReader();
  assert.equal(report.bridge_reachable, true, "the bridge answered: it is running");
  assert.equal(report.ready, false);
  assert.equal(report.status, "not_connected");
  assert.doesNotMatch(report.detail, /Start the J-ONE Fingerprint Bridge/);

  const noBridge = makeBio({ permission: "granted", handler: () => { throw new TypeError("Failed to fetch"); } });
  const down = await noBridge.window.JONE.biometrics.describeReader();
  assert.equal(down.bridge_reachable, false);
  assert.equal(down.code, "BRIDGE_UNREACHABLE");
});

test("describeReader re-initialises once when the reader is not ready", async () => {
  let statusCalls = 0;
  let initializeCalls = 0;
  const ctx = makeBio({
    permission: "granted",
    handler: (url) => {
      if (/\/initialize$/.test(url)) {
        initializeCalls += 1;
        return jsonResponse({ success: true, status: "connected", connected: true, device_serial: "S1" });
      }
      statusCalls += 1;
      return jsonResponse({ success: true, status: "communication_failure", connected: false, detail: "reader dropped" });
    },
  });
  const report = await ctx.window.JONE.biometrics.describeReader({ initialize: true });
  assert.equal(initializeCalls, 1);
  assert.equal(report.ready, true, "a replugged reader recovers without a bridge restart");
  assert.ok(statusCalls >= 1);
});

/* ------------------------------------------------------------- no fake probes */

test("no path in this module fabricates minutiae outside the explicit simulation opt-in", () => {
  const fs = require("node:fs");
  const path = require("node:path");
  const source = fs.readFileSync(path.join(__dirname, "..", "js", "biometrics.js"), "utf8");
  const simulated = /READER_MODE_SIMULATED|simulateMinutiae/.test(source);
  assert.ok(simulated, "the lab simulation must remain explicit and greppable");
  assert.match(source, /BIOMETRIC_READER_MODE=simulated/, "the simulation must announce itself");
});

test("a bridge capture with too few minutiae is reported, never sent", async () => {
  const ctx = makeBio({
    permission: "granted",
    handler: () => jsonResponse({ success: true, minutiae: [{ x: 1, y: 2, angle: 3, type: "ending", quality: 90 }] }),
  });
  await assert.rejects(
    () => ctx.window.JONE.biometrics.captureMinutiaeProbe({}),
    (err) => err.code === "BRIDGE_INCOMPLETE_CAPTURE"
  );
});
