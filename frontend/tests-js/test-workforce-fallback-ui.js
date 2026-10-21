/* tests-js/test-workforce-fallback-ui.js
   Supervised attendance fallback in the workforce console: managers and
   administrators get the control, ordinary staff never do, the fallback never
   carries a fabricated fingerprint probe, and the result explains that the code
   stays usable for repeat sign-ins until the employee clocks out.
   Run from /frontend: node --test tests-js/test-workforce-fallback-ui.js */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { makeContext, loadScript, disposeAll, FRONTEND } = require("./harness");

const WORKFORCE = fs.readFileSync(path.join(FRONTEND, "dashboard", "workforce.html"), "utf8");

test("the fallback control is management-only and starts hidden", () => {
  const block = WORKFORCE.match(/<div class="card card-pad" data-wf-rt-fallback[^>]*>/);
  assert.ok(block, "the fallback panel exists");
  assert.match(block[0], /\bhidden\b/, "it is hidden in the markup");
  assert.match(WORKFORCE, /fallback\.hidden=!management;/, "only management unhides it");
  assert.match(WORKFORCE, /hasAnyRole\(\["admin","manager","general_manager"\]\)/, "management is the exemption set");
});

test("the fallback needs an identifier and a substantive reason", () => {
  assert.match(WORKFORCE, /if\(!idVal&&!pickedStaffId\)\{JONE\.ui\.toast\("Select the staff member from the staff search, or enter their email, employee code or numeric staff ID\.","warning"\);return;\}/);
  assert.match(WORKFORCE, /if\(reason\.length<10\)\{JONE\.ui\.toast\("Give a reason of at least 10 characters\.","warning"\);return;\}/);
  assert.match(WORKFORCE, /payload=\{action:"MANUAL_FALLBACK",manual_override_reason:reason\}/);
  assert.match(WORKFORCE, /manual_override_reason/, "the reason travels with the request");
  assert.match(WORKFORCE, /JONE\.biometrics\.identifierPayload\(idVal\)/, "the identifier is bucketed by the shared classifier (email / staff_id / employee_code)");
  assert.match(WORKFORCE, /data-wf-rt-fallback-picker/, "the fallback offers a directory-backed staff selector");
  assert.match(WORKFORCE, /function terminalStaffPicker\(\)/, "the terminal selector uses the staff directory");
  assert.match(WORKFORCE, /resource:"staffDirectory"/, "the selector is backed by the authoritative account directory");
  assert.match(WORKFORCE, /payload\.staff_id=pickedStaffId/, "a selected staff member travels by exact account staff_id");
  assert.match(WORKFORCE, /document\.getElementById\("wf-rt-identifier"\)/, "an identifier already entered for the failed biometric attempt can be reused");
});

test("the failure path explains itself and never claims success", () => {
  assert.match(WORKFORCE, /if\(fallbackState\)fallbackState\.textContent=message;/);
  assert.match(WORKFORCE, /JONE\.ui\.toast\(message,"error"\);/);
  assert.match(WORKFORCE, /labelled?|Supervised fallback · /, "the result names the authority that authorised it");
});

test("the issued code is described as reusable until clock-out, never single-use", () => {
  assert.match(WORKFORCE, /The employee can sign in with this code as often as they need to until they clock out\./);
  assert.doesNotMatch(WORKFORCE, /Single-use; cancelled on clock-out\./);
  assert.doesNotMatch(WORKFORCE, /single-use and is cancelled the moment the employee clocks out/);
  assert.doesNotMatch(WORKFORCE, /The issued code is single-use/);
  // The fallback panel states the same lifecycle before a code is issued.
  assert.match(WORKFORCE, /stays valid for that employee's repeat sign-ins until they clock out/);
});

test("the console states whether this browser is an enrolled Receptionist Desktop", () => {
  assert.match(WORKFORCE, /data-wf-rt-desktop-state/);
  assert.match(WORKFORCE, /carries a Receptionist Desktop key/);
  assert.match(WORKFORCE, /the server will refuse to record attendance here/);
});

test("the console tells management they may record off-desk, and shows the address to register", () => {
  // Management is exempt from the desk station rule, and the address is what an
  // administrator needs in order to register a desk instead: both belong on the
  // screen where the refusal appears.
  assert.match(WORKFORCE, /As a management account you may still record attendance from here/);
  assert.match(WORKFORCE, /audited with your account/);
  assert.match(WORKFORCE, /API\.getClientAddress\(\)/);
  assert.match(WORKFORCE, /Register this address on the front desk workstation/);
});

test("MANUAL_FALLBACK never sends a synthesised fingerprint probe", async () => {
  const ctx = makeContext({ pathname: "/dashboard/workforce.html" });
  loadScript(ctx, "js/utils.js");
  ctx.window.JONE = ctx.window.JONE || {};
  ctx.window.APP_CONFIG = { API_ENDPOINTS: { receptionTerminal: "/api/admin/staff-operations/biometrics/reception-terminal/" } };
  ctx.window.Auth = { receptionistDesktopKey: () => "", state: {} };
  let sent = null;
  ctx.window.API = {
    post(_path, body) { sent = body; return Promise.resolve({ data: { status: "CODE_ISSUED" } }); },
  };
  ctx.window.navigator = ctx.window.navigator || {};
  // The console drives the lab simulation here; the capture contract itself is
  // pinned by tests-js/test-biometrics.js (no reader => refusal, never a fake probe).
  ctx.window.__APP_CONFIG__ = { BIOMETRIC_READER_MODE: "simulated" };
  loadScript(ctx, "js/biometrics.js");
  await ctx.window.JONE.biometrics.executeReceptionTerminalAction({
    action: "MANUAL_FALLBACK", email: "worker@jonehotel.com", manual_override_reason: "Reader is faulty today.",
  });
  assert.ok(sent, "the request was sent");
  assert.equal(sent.fingerprint_probe, undefined, "a fallback must not fabricate a biometric probe");
  assert.equal(sent.action, "MANUAL_FALLBACK");

  // The biometric actions still capture on a workstation that has a reader (or the
  // explicitly configured lab simulation): only then does a probe travel.
  sent = null;
  await ctx.window.JONE.biometrics.executeReceptionTerminalAction({ action: "CLOCK_IN", email: "worker@jonehotel.com" });
  assert.ok(sent && Array.isArray(sent.fingerprint_probe), "CLOCK_IN carries the captured probe");
  disposeAll();
});

test.after(() => disposeAll());
