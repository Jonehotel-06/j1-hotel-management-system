/* tests-js/test-biometrics.js
   Capture contract: the browser client may only obtain a fingerprint template
   from (1) the department reader bridge or (2) an explicitly configured lab
   simulation. It must never invent one, and MANUAL_FALLBACK must never carry a
   probe at all.
   Run from /frontend: node --test tests-js/test-biometrics.js */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { makeContext, loadScript, disposeAll } = require("./harness");

test.after(() => disposeAll());

function makeBio(options) {
  options = options || {};
  const ctx = makeContext();
  loadScript(ctx, "js/utils.js");
  if (options.reader) ctx.window.JoneFingerprintReader = options.reader;
  if (options.mode) ctx.window.__APP_CONFIG__ = { BIOMETRIC_READER_MODE: options.mode };
  loadScript(ctx, "js/biometrics.js");
  return ctx;
}

const READER_POINTS = Array.from({ length: 12 }, (_, i) => ({
  x: 40 + i, y: 60 + i, angle: i * 10, type: i % 2 ? "bifurcation" : "ending", quality: 90,
}));

test("the module still exports the bindings the pages use", () => {
  const ctx = makeBio();
  const bio = ctx.window.JONE.biometrics;
  assert.equal(typeof bio.captureMinutiaeProbe, "function");
  assert.equal(typeof bio.requestEnrollment, "function");
  assert.equal(typeof bio.issueTemporaryWorkstationToken, "function");
  assert.equal(typeof bio.getReceptionTerminalStatus, "function");
  assert.equal(typeof bio.executeReceptionTerminalAction, "function");
  assert.equal(typeof bio.identifierPayload, "function");
});

test("identifierPayload buckets email, numeric staff id and employee code", () => {
  const ctx = makeBio();
  const classifyRaw = ctx.window.JONE.biometrics.identifierPayload;
  const classify = (v) => JSON.parse(JSON.stringify(classifyRaw(v)));  // plain object across vm realms
  assert.deepEqual(classify("  worker@jonehotel.com "), { email: "worker@jonehotel.com" });
  assert.deepEqual(classify("worker\u200b @ jonehotel.com"), { email: "worker@jonehotel.com" }, "copied formatting must not turn an exact email into a non-match");
  assert.deepEqual(classify("12"), { staff_id: 12 }, "a numeric id must travel as staff_id, not as a (never-matching) employee_code");
  assert.deepEqual(classify("STF-20261018-ABC123"), { employee_code: "STF-20261018-ABC123" });
  assert.deepEqual(classify("STF\u201320261018\u2013ABC123"), { employee_code: "STF-20261018-ABC123" }, "typographic dashes copied from a document still name the stored code");
  assert.deepEqual(classify("EMP-0012"), { employee_code: "EMP-0012" }, "the displayed EMP code travels as a code; the server cross-resolves it to the id");
  assert.deepEqual(classify(""), {});
  assert.deepEqual(classify(null), {});
  disposeAll();
});

test("with no reader and no simulation opt-in the client refuses to fabricate a fingerprint", async () => {
  const ctx = makeBio();
  await assert.rejects(
    () => ctx.window.JONE.biometrics.captureMinutiaeProbe({ seed: "worker@jonehotel.com" }),
    (err) => err.name === "FingerprintReaderError"
      && /No fingerprint reader is available/.test(err.message)
      && /supervised fallback/.test(err.message),
    "a missing reader must be reported, not papered over with synthetic minutiae"
  );
  disposeAll();
});

test("the department reader bridge supplies the probe, normalised to ISO points", async () => {
  let asked = null;
  const ctx = makeBio({ reader: { capture: async (opts) => { asked = opts; return { minutiae: READER_POINTS }; } } });
  const points = await ctx.window.JONE.biometrics.captureMinutiaeProbe({ fingerPosition: "LEFT_THUMB" });
  assert.equal(points.length, READER_POINTS.length);
  assert.deepEqual(Object.keys(points[0]).sort(), ["angle", "quality", "type", "x", "y"]);
  assert.equal(asked.fingerPosition, "LEFT_THUMB", "the requested finger reaches the reader");
  assert.ok(asked.timeout > 0, "the reader call is bounded by a timeout");
  disposeAll();
});

test("an incomplete or errored read is reported, never sent as a fingerprint", async () => {
  const tooFew = makeBio({ reader: { capture: async () => ({ minutiae: READER_POINTS.slice(0, 3) }) } });
  await assert.rejects(() => tooFew.window.JONE.biometrics.captureMinutiaeProbe(),
    (err) => err.name === "FingerprintReaderError" && /incomplete capture/.test(err.message));
  disposeAll();

  const cancelled = makeBio({ reader: { capture: async () => ({ cancelled: true }) } });
  await assert.rejects(() => cancelled.window.JONE.biometrics.captureMinutiaeProbe(),
    (err) => err.name === "FingerprintReaderError" && /cancelled/.test(err.message));
  disposeAll();

  const broken = makeBio({ reader: { capture: async () => { throw new Error("device unplugged"); } } });
  await assert.rejects(() => broken.window.JONE.biometrics.captureMinutiaeProbe(),
    (err) => err.name === "FingerprintReaderError" && /device unplugged/.test(err.message));
  disposeAll();
});

test("the lab simulation is opt-in, announced, and stable for a seed", async () => {
  const ctx = makeBio({ mode: "simulated" });
  const warnings = [];
  ctx.window.console = { warn: (msg) => warnings.push(String(msg)) };
  const first = await ctx.window.JONE.biometrics.captureMinutiaeProbe({ seed: "worker@jonehotel.com" });
  const second = await ctx.window.JONE.biometrics.captureMinutiaeProbe({ seed: "worker@jonehotel.com" });
  assert.ok(Array.isArray(first) && first.length >= 10);
  assert.ok(first[0].x !== undefined && first[0].y !== undefined && first[0].angle !== undefined);
  assert.deepEqual(first, second, "a simulation must be reproducible inside the demo");
  assert.ok(warnings.join(" ").includes("simulated"), "the demo warns that it is simulating");
  disposeAll();
});

test("MANUAL_FALLBACK is sent without a probe; other actions demand a capture", async () => {
  const sent = [];
  const ctx = makeBio({ mode: "simulated" });
  ctx.window.APP_CONFIG = { API_ENDPOINTS: { receptionTerminal: "/api/admin/staff-operations/biometrics/reception-terminal/" } };
  ctx.window.Auth = { receptionistDesktopKey: () => "" };
  ctx.window.API = { post: async (path, body) => { sent.push(body); return { data: { status: "OK" } }; } };

  await ctx.window.JONE.biometrics.executeReceptionTerminalAction({
    action: "MANUAL_FALLBACK", email: "worker@jonehotel.com", manual_override_reason: "reader faulty today",
  });
  assert.equal(sent[0].fingerprint_probe, undefined, "an override must never carry a fabricated probe");

  await ctx.window.JONE.biometrics.executeReceptionTerminalAction({ action: "CLOCK_IN", email: "worker@jonehotel.com" });
  assert.ok(Array.isArray(sent[1].fingerprint_probe), "a capture action carries the captured template");
  disposeAll();

  const noReader = makeBio();
  noReader.window.APP_CONFIG = { API_ENDPOINTS: { receptionTerminal: "/api/admin/staff-operations/biometrics/reception-terminal/" } };
  noReader.window.Auth = { receptionistDesktopKey: () => "" };
  let called = false;
  noReader.window.API = { post: async () => { called = true; return { data: {} }; } };
  await assert.rejects(
    () => noReader.window.JONE.biometrics.executeReceptionTerminalAction({ action: "CLOCK_IN", email: "worker@jonehotel.com" }),
    (err) => err.name === "FingerprintReaderError"
  );
  assert.equal(called, false, "nothing is sent when no fingerprint could be captured");
  disposeAll();
});
