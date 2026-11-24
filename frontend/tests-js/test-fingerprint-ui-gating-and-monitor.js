/* tests-js/test-fingerprint-ui-gating-and-monitor.js
   Verifies required UI state labels, capture button readiness gating,
   live monitor transitions without page reload, and capture lifecycle states.
   Run from /frontend: node --test tests-js/test-fingerprint-ui-gating-and-monitor.js */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { makeContext, loadScript, disposeAll, FRONTEND } = require("./harness");

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

test("all required user-facing UI labels match hardware/bridge states", () => {
  const ctx = makeContext();
  loadScript(ctx, "js/utils.js");
  loadScript(ctx, "js/biometrics.js");
  const bio = ctx.window.JONE.biometrics;

  assert.equal(
    bio.uiLabelForReport({ mode: "desktop", bridge_reachable: true, status: "not_connected", ready: false }),
    "Fingerprint reader not connected"
  );
  assert.equal(
    bio.uiLabelForReport({ mode: "desktop", bridge_reachable: true, status: "usb_detected", ready: false }),
    "Reader detected"
  );
  assert.equal(
    bio.uiLabelForReport({ mode: "desktop", bridge_reachable: true, status: "initializing", ready: false }),
    "Initializing fingerprint reader"
  );
  assert.equal(
    bio.uiLabelForReport({ mode: "desktop", bridge_reachable: true, status: "driver_missing", ready: false }),
    "Driver/interface unavailable"
  );
  assert.equal(
    bio.uiLabelForReport({ mode: "desktop", bridge_reachable: true, status: "service_unavailable", ready: false }),
    "Biometric service unavailable"
  );
  assert.equal(
    bio.uiLabelForReport({ mode: "desktop", bridge_reachable: false, status: "not_connected", ready: false }),
    "Biometric service unavailable"
  );
  assert.equal(
    bio.uiLabelForReport({ mode: "desktop", bridge_reachable: true, status: "connected", ready: true }),
    "Fingerprint reader ready"
  );
  assert.equal(
    bio.uiLabelForReport({ mode: "desktop", bridge_reachable: true, status: "busy", ready: false }),
    "Reading fingerprint"
  );
  assert.equal(
    bio.uiLabelForReport({ mode: "desktop", bridge_reachable: true, status: "reconnecting", ready: false }),
    "Reconnecting to fingerprint reader"
  );
  assert.equal(bio.UI_LABELS.capture_success, "Fingerprint captured successfully");
  disposeAll();
});

test("isCaptureReady only allows capture when reader is ready (unless simulated or in-page SDK)", () => {
  const ctx = makeContext();
  loadScript(ctx, "js/utils.js");
  loadScript(ctx, "js/biometrics.js");
  const bio = ctx.window.JONE.biometrics;

  assert.equal(bio.isCaptureReady(null), false);
  assert.equal(bio.isCaptureReady({ ready: false, status: "usb_detected" }), false);
  assert.equal(bio.isCaptureReady({ ready: false, status: "initializing" }), false);
  assert.equal(bio.isCaptureReady({ ready: false, status: "not_connected" }), false);
  assert.equal(bio.isCaptureReady({ ready: true, status: "connected" }), true);

  const simCtx = makeContext();
  simCtx.window.__APP_CONFIG__ = { BIOMETRIC_READER_MODE: "simulated" };
  loadScript(simCtx, "js/utils.js");
  loadScript(simCtx, "js/biometrics.js");
  assert.equal(simCtx.window.JONE.biometrics.isCaptureReady(null), true);
  disposeAll();
});

test("captureMinutiaeProbe emits capturing -> capture_success and capturing -> capture_failed", async () => {
  const events = [];
  const ctx = makeContext();
  loadScript(ctx, "js/utils.js");
  ctx.window.navigator.permissions = { query: async () => ({ state: "granted" }) };
  ctx.window.fetch = async () => jsonResponse({ success: true, minutiae: POINTS, count: POINTS.length });
  loadScript(ctx, "js/biometrics.js");

  const pts = await ctx.window.JONE.biometrics.captureMinutiaeProbe({
    onCaptureState: (s) => events.push(s),
  });
  assert.equal(pts.length, POINTS.length);
  assert.equal(events[0].state, "capturing");
  assert.equal(events[0].label, "Reading fingerprint");
  assert.equal(events[1].state, "capture_success");
  assert.equal(events[1].label, "Fingerprint captured successfully");

  const failEvents = [];
  ctx.window.fetch = async () => jsonResponse({ success: false, error: "READER_POOR_QUALITY", message: "Print was too faint." }, 422);
  await assert.rejects(
    () => ctx.window.JONE.biometrics.captureMinutiaeProbe({
      onCaptureState: (s) => failEvents.push(s),
    }),
    /Print was too faint/
  );
  assert.equal(failEvents[0].state, "capturing");
  assert.equal(failEvents[1].state, "capture_failed");
  assert.match(failEvents[1].detail, /too faint/i);
  disposeAll();
});

test("startReaderMonitor tracks live USB plug/init/ready/unplug without page refresh", async () => {
  let step = 0;
  const states = [
    { status: "not_connected", connected: false, detail: "No reader." },
    { status: "usb_detected", connected: false, usb_detected: true, detail: "USB detected." },
    { status: "connected", connected: true, device_serial: "4500-LIVE", detail: "Ready." },
    { status: "not_connected", connected: false, detail: "Unplugged." },
  ];
  const observed = [];
  const ctx = makeContext();
  loadScript(ctx, "js/utils.js");
  ctx.window.navigator.permissions = { query: async () => ({ state: "granted" }) };
  ctx.window.fetch = async (url) => {
    assert.ok(url.endsWith("/status"));
    return jsonResponse(states[Math.min(step, states.length - 1)]);
  };
  loadScript(ctx, "js/biometrics.js");

  const mon = ctx.window.JONE.biometrics.startReaderMonitor({
    autoInitialize: false,
    intervalMs: 1000,
    onStatus: (r) => observed.push({ status: r.status, ready: r.ready, label: r.ui_label }),
  });

  await new Promise((r) => setTimeout(r, 30));
  step = 1; await mon.refreshNow();
  step = 2; await mon.refreshNow();
  step = 3; await mon.refreshNow();
  mon.stop();

  assert.deepEqual(observed, [
    { status: "not_connected", ready: false, label: "Fingerprint reader not connected" },
    { status: "usb_detected", ready: false, label: "Reader detected" },
    { status: "connected", ready: true, label: "Fingerprint reader ready" },
    { status: "not_connected", ready: false, label: "Fingerprint reader not connected" },
  ]);
  disposeAll();
});

test("desk HTML pages gate capture buttons on reader readiness and load updated biometrics", () => {
  for (const rel of ["dashboard/workforce.html", "dashboard/terminals.html", "dashboard/staff-profile.html"]) {
    const html = fs.readFileSync(path.join(FRONTEND, rel), "utf8");
    assert.match(html, /biometrics\.js\?v=1\.1\.36/, rel + " must load bumped biometrics.js");
    assert.match(html, /startReaderMonitor|isCaptureReady/, rel + " must wire live monitor and capture gating");
  }
});
