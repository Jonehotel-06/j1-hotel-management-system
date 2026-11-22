/* tests-js/test-reader-check.js

   The "Check fingerprint station" control introduced by the 2026-11-22 repair.

   Why it needs its own tests
   ==========================
   The desk pages could not report the reader at all before the first capture,
   because the browser only grants Local Network Access inside a user gesture.
   The fix is a single button that runs the WHOLE detection sequence inside
   that click. These tests pin the two things that would silently undo it:

     · the checks must all happen inside the click (no "start it, then poll");
     · the panel must never claim a state it did not observe — in particular it
       must never say "Reader ready" or "Bridge not running" without evidence.

   Run: node --test tests-js/test-reader-check.js
*/
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { FRONTEND } = require("./harness");

let JSDOM = null;
try {
  ({ JSDOM } = require(path.join(FRONTEND, "node_modules", "jsdom")));
} catch (_) {
  JSDOM = null;
}
const SKIP = JSDOM ? false : "jsdom is not installed (npm i jsdom --no-save)";
const tick = (ms) => new Promise((r) => setTimeout(r, ms || 40));

const PAGE = `<!doctype html><html><body>
  <button id="sp-btn-check-reader" type="button">Check fingerprint station</button>
  <div id="sp-reader-check" role="status" aria-live="polite"></div>
</body></html>`;

/* One fake transport for every case. `scenario` decides what the "browser"
   does; the module under test is the real js/reader-check.js, mounted exactly
   as the desk pages mount it. */
async function boot(scenario) {
  const dom = new JSDOM(PAGE, {
    url: "https://jonehotels.com/dashboard/staff-profile.html",
    runScripts: "outside-only",
  });
  const w = dom.window;
  w.__scenario = scenario;
  w.eval(fs.readFileSync(path.join(FRONTEND, "js/utils.js"), "utf8"));
  // Honest stand-in for js/biometrics.js: it records whether each call happened
  // inside a user gesture, and it performs no network work of its own beyond
  // what the scenario says a real reader station would do.
  w.eval(`
    window.__calls = [];
    window.__gesture = false;
    window.__network = 0;
    window.JONE = window.JONE || {};
    window.JONE.biometrics = {
      workstationMode() { return window.__scenario.mode || "desktop"; },
      getBridgeUrl() { return "http://127.0.0.1:8787"; },
      async describeReader(options) {
        window.__calls.push({
          call: "describeReader",
          inGesture: window.__gesture,
          requestPermission: !!(options && options.requestPermission)
        });
        if ((window.__scenario.mode || "desktop") === "mobile") return window.__scenario.report;
        window.__network += 1;
        return window.__scenario.report;
      }
    };
  `);
  w.eval(fs.readFileSync(path.join(FRONTEND, "js/reader-check.js"), "utf8"));
  w.JONE.readerCheck.mount({ button: "#sp-btn-check-reader", host: "#sp-reader-check" });
  await tick();
  return { w, $: (s) => w.document.querySelector(s) };
}

function press(c) {
  // The real browser keeps the "user activation" flag set while a click
  // handler runs; this reproduces that window.
  const button = c.$("#sp-btn-check-reader");
  button.addEventListener("click", () => { c.w.__gesture = true; }, true);
  button.click();
  c.w.__gesture = false;
}

const readyReport = { status: "connected", ready: true, mode: "desktop", bridge_reachable: true,
  detail: "Reader open and idle.", connected: true, device_serial: "4500-ABC",
  driver_installed: true, sdk_loaded: true, capture_in_progress: false,
  bridge_url: "http://127.0.0.1:8787", local_network_permission: "granted" };

test("the check exposes a labelled, keyboard-reachable button", { skip: SKIP }, async () => {
  const c = await boot({ permission: "granted", report: readyReport });
  try {
    const button = c.$("#sp-btn-check-reader");
    assert.ok(button, "the page keeps its own button");
    assert.match(button.textContent, /Check fingerprint station/i);
    assert.equal(button.getAttribute("type"), "button", "it must not submit a form");
    const panel = c.$("#sp-reader-check");
    assert.equal(panel.getAttribute("aria-live"), "polite", "state changes are announced");
  } finally { c.w.close(); }
});

test("before the first press the panel says it has not been checked", { skip: SKIP }, async () => {
  const c = await boot({ permission: "prompt", report: readyReport });
  try {
    const text = c.$("#sp-reader-check").textContent;
    assert.doesNotMatch(text, /Reader ready/i, "no unobserved success");
    assert.doesNotMatch(text, /Bridge not running/i, "no unobserved failure");
    assert.equal(c.w.__calls.length, 0, "booting the page must not touch the bridge");
    assert.equal(c.$("#sp-reader-check").getAttribute("data-state"), "not_checked");
  } finally { c.w.close(); }
});

test("one press runs permission, status and rendering inside the gesture", { skip: SKIP }, async () => {
  const c = await boot({ permission: "granted", report: readyReport });
  try {
    press(c);
    await tick(80);
    assert.equal(c.w.__calls.length, 1, "exactly one detection sequence ran");
    assert.equal(c.w.__calls[0].call, "describeReader");
    assert.equal(c.w.__calls[0].inGesture, true,
      "the whole sequence must run inside the click, where the browser shows its prompt");
    assert.equal(c.w.__calls[0].requestPermission, true,
      "the check must ask for local network access, not read around it");;
    const panel = c.$("#sp-reader-check");
    assert.match(panel.textContent, /Reader ready/i);
    assert.match(panel.textContent, /4500-ABC/, "the reader serial is reported");
    assert.equal(panel.getAttribute("data-state"), "ready");
  } finally { c.w.close(); }
});

test("a second press while a check runs does not start a second check", { skip: SKIP }, async () => {
  const c = await boot({ permission: "granted", report: readyReport });
  try {
    const button = c.$("#sp-btn-check-reader");
    button.click();
    const disabledWhileRunning = button.disabled;
    button.click();
    await tick(100);
    assert.ok(disabledWhileRunning, "the button is disabled while the check runs");
    assert.equal(c.w.__calls.length, 1, "only one detection sequence ran");
    assert.equal(button.disabled, false, "the button is usable again afterwards");
  } finally { c.w.close(); }
});

test("a refused permission is explained, and is not called a missing bridge", { skip: SKIP }, async () => {
  const c = await boot({
    permission: "denied",
    report: { status: "permission_blocked", ready: false, mode: "desktop", bridge_reachable: false,
      detail: "The browser blocked local network access for this site.",
      driver_installed: null, sdk_loaded: null } });
  try {
    press(c);
    await tick(80);
    const panel = c.$("#sp-reader-check");
    assert.match(panel.textContent, /local network access/i);
    assert.doesNotMatch(panel.textContent, /not running/i);
    assert.equal(panel.getAttribute("data-state"), "permission_blocked");
  } finally { c.w.close(); }
});

test("a stopped bridge is reported as stopped only when the read failed", { skip: SKIP }, async () => {
  const c = await boot({
    permission: "granted",
    report: { status: "not_connected", ready: false, mode: "desktop", bridge_reachable: false,
      detail: "The fingerprint bridge is not reachable at http://127.0.0.1:8787.",
      driver_installed: null, sdk_loaded: null } });
  try {
    press(c);
    await tick(80);
    const panel = c.$("#sp-reader-check");
    assert.match(panel.textContent, /bridge/i);
    assert.equal(panel.getAttribute("data-state"), "bridge_unreachable");
  } finally { c.w.close(); }
});

test("a reader that is unplugged is told apart from a stopped bridge", { skip: SKIP }, async () => {
  const c = await boot({
    permission: "granted",
    report: { status: "not_connected", ready: false, mode: "desktop", bridge_reachable: true,
      detail: "No fingerprint reader was found. Check the USB cable.",
      driver_installed: true, sdk_loaded: true } });
  try {
    press(c);
    await tick(80);
    const panel = c.$("#sp-reader-check");
    assert.equal(panel.getAttribute("data-state"), "reader_not_ready");
    assert.doesNotMatch(panel.textContent, /bridge not running/i);
  } finally { c.w.close(); }
});

test("a stopped DigitalPersona service is named as a service, not a driver", { skip: SKIP }, async () => {
  const c = await boot({
    permission: "granted",
    report: { status: "service_unavailable", ready: false, mode: "desktop", bridge_reachable: true,
      detail: "The DigitalPersona driver is installed but its service did not start.",
      driver_installed: true, sdk_loaded: false } });
  try {
    press(c);
    await tick(80);
    const text = c.$("#sp-reader-check").textContent;
    assert.match(text, /service/i);
    assert.match(text, /installed/i, "the operator is told the driver IS installed");
  } finally { c.w.close(); }
});

test("a capture already holding the reader is refused, not double-read", { skip: SKIP }, async () => {
  const c = await boot({ permission: "granted", report: readyReport });
  try {
    c.w.__JONE_READER_BUSY__ = () => true;   // a capture is running on Workforce
    press(c);
    await tick(80);
    const panel = c.$("#sp-reader-check");
    assert.equal(c.w.__calls.length, 0, "no second detection sequence was started");
    assert.equal(panel.getAttribute("data-state"), "busy");
    assert.match(panel.textContent, /already using the reader/i);
    assert.doesNotMatch(panel.textContent, /Reader ready/i, "the panel must not claim ready");
    c.w.__JONE_READER_BUSY__ = () => false;  // the capture finished
    press(c);
    await tick(80);
    assert.equal(panel.getAttribute("data-state"), "ready");
  } finally { c.w.close(); }
});

test("a phone is told why, and no capture is attempted", { skip: SKIP }, async () => {
  const c = await boot({ mode: "mobile",
    report: { status: "wrong_workstation", ready: false, mode: "mobile", bridge_reachable: false,
      detail: "This page is open on a phone. 127.0.0.1 on a phone is the phone itself, so it " +
        "cannot reach the reader on the reception workstation.",
      driver_installed: null, sdk_loaded: null } });
  try {
    press(c);
    await tick(80);
    const panel = c.$("#sp-reader-check");
    assert.equal(panel.getAttribute("data-state"), "wrong_workstation");
    assert.equal(c.w.__network, 0, "a phone must not ask for the reader at all");
    assert.match(panel.textContent, /phone|workstation/i);
  } finally { c.w.close(); }
});
