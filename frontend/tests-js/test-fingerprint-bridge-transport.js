/* tests-js/test-fingerprint-bridge-transport.js
   Pins the 2026-11-19 fingerprint-bridge repair.

   Reported console error (before repair):

     Fetch API cannot load
     https://api.jonehotels.comhttps://api.jonehotels.com/api/admin/staff-operations/
     biometrics/bridge/capture/

   Cause: js/biometrics.js built a FULL backend URL and handed it to
   window.API.post(), which prepends the API origin again, and the call was sent
   to the hosted API even though the reader is a USB device on the workstation.

   After repair:
     * API.resolveUrl() never prefixes an absolute URL (general fix in api.js);
     * the biometrics client talks to the WORKSTATION bridge
       (http://127.0.0.1:8787 by default, overridable through runtime config),
       with no Authorization header and no API origin in the path.

   Run from /frontend: node --test tests-js/test-fingerprint-bridge-transport.js */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { makeContext, loadScript, disposeAll } = require("./harness");

test.after(() => disposeAll());

const POINTS = Array.from({ length: 12 }, (_, i) => ({
  x: 40 + i, y: 60 + i, angle: i * 10, type: i % 2 ? "bifurcation" : "ending", quality: 90,
}));

function jsonResponse(payload, status) {
  return {
    ok: (status || 200) < 400,
    status: status || 200,
    text: async () => JSON.stringify(payload),
    headers: { get: () => "application/json" },
  };
}

function makeBio(options) {
  options = options || {};
  const ctx = makeContext();
  loadScript(ctx, "js/utils.js");
  if (options.runtime) ctx.window.__APP_CONFIG__ = options.runtime;
  if (options.calls) {
    ctx.window.fetch = async (url, init) => {
      options.calls.push({ url, init });
      return options.handler(url, init);
    };
  }
  loadScript(ctx, "js/biometrics.js");
  return ctx;
}

test("API.resolveUrl never prefixes a URL that already carries its own scheme", () => {
  const ctx = makeContext();
  ctx.window.__APP_CONFIG__ = { API_BASE_URL: "https://api.jonehotels.com" };
  ctx.window.APP_CONFIG = { API_BASE_URL: "https://api.jonehotels.com", STORAGE: { AUTH: "jone.auth", SESSION: "jone.session", TERMINAL: "jone.terminal" } };
  loadScript(ctx, "js/api.js");
  const resolveUrl = ctx.window.API.resolveUrl;

  assert.equal(
    resolveUrl("/api/rooms/"),
    "https://api.jonehotels.com/api/rooms/",
    "relative routes keep resolving against the API origin"
  );
  assert.equal(
    resolveUrl("https://api.jonehotels.com/api/rooms/"),
    "https://api.jonehotels.com/api/rooms/",
    "an absolute API URL is not prefixed twice"
  );
  assert.equal(
    resolveUrl("http://127.0.0.1:8787/capture"),
    "http://127.0.0.1:8787/capture",
    "the workstation bridge origin must survive untouched"
  );
  assert.equal(resolveUrl("//cdn.example.com/x.js"), "//cdn.example.com/x.js");
  assert.equal(resolveUrl("https://api.jonehotels.com/api/rooms/").includes("api.jonehotels.comhttps"), false);
  disposeAll();
});

test("the bridge address is the local workstation, not the hosted API", () => {
  // With config.js loaded (the normal desk page) the default is loopback …
  const configured = makeContext();
  loadScript(configured, "js/runtime-config.js");
  loadScript(configured, "js/config.js");
  assert.equal(configured.window.APP_CONFIG.API_BASE_URL, "https://api.jonehotels.com");
  assert.equal(configured.window.APP_CONFIG.FINGERPRINT_BRIDGE_URL, "http://127.0.0.1:8787");

  // … and it is independent of the API origin.
  const bio = makeBio();
  assert.equal(bio.window.JONE.biometrics.getBridgeUrl(), "http://127.0.0.1:8787");
  disposeAll();
});

test("config.js refuses a bridge origin that HTTPS would block as mixed content", () => {
  const ctx = makeContext();
  ctx.window.__APP_CONFIG__ = { FINGERPRINT_BRIDGE_URL: "http://192.168.1.50:8787" };
  loadScript(ctx, "js/config.js");
  assert.equal(ctx.window.APP_CONFIG.FINGERPRINT_BRIDGE_URL, "http://127.0.0.1:8787",
    "a non-loopback plain-HTTP bridge is impossible from an HTTPS page; fall back to loopback");
  assert.equal(ctx.window.APP_CONFIG.CONFIG_ERROR || "", "");

  const ok = makeContext();
  ok.window.__APP_CONFIG__ = { FINGERPRINT_BRIDGE_URL: "http://localhost:9100/" };
  loadScript(ok, "js/config.js");
  assert.equal(ok.window.APP_CONFIG.FINGERPRINT_BRIDGE_URL, "http://localhost:9100");

  const badPath = makeContext();
  badPath.window.__APP_CONFIG__ = { FINGERPRINT_BRIDGE_URL: "http://127.0.0.1:8787/capture" };
  loadScript(badPath, "js/config.js");
  assert.equal(badPath.window.APP_CONFIG.FINGERPRINT_BRIDGE_URL, "http://127.0.0.1:8787",
    "a path in the configured origin is discarded rather than silently doubling the path");
  disposeAll();
});

test("a deployment can move the bridge port through runtime config", () => {
  const ctx = makeBio();
  ctx.window.APP_CONFIG = { FINGERPRINT_BRIDGE_URL: "http://127.0.0.1:9100" };
  assert.equal(ctx.window.JONE.biometrics.getBridgeUrl(), "http://127.0.0.1:9100");
  disposeAll();
});

test("capture posts JSON to the bridge and sends no credential with it", async () => {
  const calls = [];
  const ctx = makeBio({
    calls,
    handler: () => jsonResponse({ success: true, minutiae: POINTS, count: POINTS.length }),
  });
  const points = await ctx.window.JONE.biometrics.captureMinutiaeProbe({ fingerPosition: "LEFT_THUMB", timeout: 15000 });
  assert.equal(points.length, POINTS.length);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "http://127.0.0.1:8787/capture");
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.credentials, "omit", "the bridge holds no credential; none may be sent");
  assert.equal(calls[0].init.headers.Authorization, undefined);
  assert.doesNotMatch(calls[0].url, /api\.jonehotels\.com/, "the hosted API must never be asked about a local USB device");
  assert.equal(JSON.parse(calls[0].init.body).finger_position, "LEFT_THUMB");
  disposeAll();
});

test("a reader that was replugged is recovered by re-initialising once, then capturing", async () => {
  const calls = [];
  let attempt = 0;
  const ctx = makeBio({
    calls,
    handler: (url) => {
      if (url.endsWith("/capture")) {
        attempt += 1;
        return attempt === 1
          ? jsonResponse({ success: false, error: "READER_NOT_CONNECTED", message: "No fingerprint reader detected." })
          : jsonResponse({ success: true, minutiae: POINTS, count: POINTS.length });
      }
      if (url.endsWith("/initialize")) return jsonResponse({ success: true, status: "connected", connected: true });
      return jsonResponse({ success: true, status: "connected", connected: true });
    },
  });
  const points = await ctx.window.JONE.biometrics.captureMinutiaeProbe();
  assert.equal(points.length, POINTS.length);
  assert.deepEqual(calls.map((c) => c.url.replace("http://127.0.0.1:8787", "")),
    ["/capture", "/initialize", "/capture"],
    "one bounded recovery attempt, never an unbounded retry loop");
  disposeAll();
});

test("a busy reader is reported with its own code, not a fabricated capture", async () => {
  const ctx = makeBio({
    calls: [],
    handler: () => jsonResponse({ success: false, error: "READER_BUSY", message: "The fingerprint reader is busy." }),
  });
  await assert.rejects(
    () => ctx.window.JONE.biometrics.captureMinutiaeProbe(),
    (err) => err.name === "FingerprintReaderError" && /busy/i.test(err.message)
  );
  disposeAll();
});

test("status polling reports an unreachable bridge honestly and does not throw", async () => {
  const ctx = makeBio({ calls: [], handler: () => { throw new TypeError("Failed to fetch"); } });
  const status = await ctx.window.JONE.biometrics.getReaderStatus({ retries: 0 });
  assert.equal(status.connected, false);
  assert.equal(status.status, "not_connected");
  assert.equal(status.bridge_reachable, false);
  assert.match(status.detail, /fingerprint bridge is not reachable at http:\/\/127\.0\.0\.1:8787/);
  disposeAll();
});

test("status polling reads the bridge's own device state", async () => {
  const ctx = makeBio({
    calls: [],
    handler: () => jsonResponse({
      success: true, status: "connected", connected: true,
      detail: "Fingerprint reader connected and ready.", device_serial: "4500-ABC",
    }),
  });
  const status = await ctx.window.JONE.biometrics.getReaderStatus();
  assert.equal(status.status, "connected");
  assert.equal(status.connected, true);
  assert.equal(status.device_serial, "4500-ABC");
  assert.equal(status.bridge_reachable, true);
  disposeAll();
});

test("initialisation is a POST to the bridge and returns the resulting status", async () => {
  const calls = [];
  const ctx = makeBio({
    calls,
    handler: () => jsonResponse({ success: true, status: "connected", connected: true, device_serial: "4500-XYZ" }),
  });
  const status = await ctx.window.JONE.biometrics.initializeReader();
  assert.equal(calls[0].url, "http://127.0.0.1:8787/initialize");
  assert.equal(calls[0].init.method, "POST");
  assert.equal(status.connected, true);
  assert.equal(status.device_serial, "4500-XYZ");
  disposeAll();
});
