/* tests-js/test-extend-stay-flow.js */
/* Extending a stay — the rules behind the dialog, and the message the desk gets
   when the server refuses.

   Reported symptom (front desk): "The date and night in extend modal is failing.
   When a guest wants to extend the days, it's not going through." The red text
   the desk was reading was "The requested item could not be found." — which is
   NOT what the backend says. Two defects produced it:

   1. js/api.js mapped every 404 onto one sentence and its "this route is not
      served by the deployment that is answering" branch was unreachable: the
      condition tested `data == null`, but a Django HTML 404 arrives as a plain
      TEXT body, so `data` was a string. The desk was told the *booking* could
      not be found (so they retyped dates) when in fact the *endpoint* was not
      there — `POST …/extend-quote/` is answered by Django, not by the API.
   2. Pressing Confirm after a refused quote replaced the server's own sentence
      with "Choose a new check-out date…", blaming the form for a server refusal,
      and a stay the server already said cannot be extended was only discovered
      after the desk had typed a date.

   The pure decision rules are now exported and pinned here; the dialog itself is
   exercised end to end in tests-js/test-extend-stay-modal-e2e.js.

   Run: node --test tests-js/test-extend-stay-flow.js   (from frontend/) */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { makeContext, loadScript, FRONTEND, disposeAll } = require("./harness");

test.after(() => disposeAll());

function source(rel) {
  return fs.readFileSync(path.join(FRONTEND, rel), "utf8");
}

function bootDashboard() {
  const ctx = makeContext();
  loadScript(ctx, "js/config.js");
  loadScript(ctx, "js/utils.js");
  ctx.window.API = { setTokenProvider() { }, getToken() { return null; } };
  loadScript(ctx, "js/auth.js");
  loadScript(ctx, "js/dashboard.js");
  return ctx.window.JONE.dashboard;
}

function bootApi(fetchImpl) {
  const ctx = makeContext({ hostname: "www.jonehotel.test", pathname: "/index.html" });
  ctx.fetch = fetchImpl;
  ctx.window.APP_CONFIG = {
    API_BASE_URL: "https://api.example.test",
    STORAGE: { AUTH: "jone.auth", SESSION: "jone.session", BOOKING: "jone.booking.draft" },
    API_ENDPOINTS: { bookings: "/api/admin/bookings/" },
  };
  loadScript(ctx, "js/api.js");
  return ctx;
}

function jsonRes(status, body) {
  return Promise.resolve({
    status,
    ok: status >= 200 && status < 300,
    headers: { get: (k) => (String(k).toLowerCase() === "content-type" ? "application/json" : null) },
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  });
}

function htmlRes(status, html) {
  return Promise.resolve({
    status,
    ok: false,
    headers: { get: (k) => (String(k).toLowerCase() === "content-type" ? "text/html" : null) },
    json: () => Promise.reject(new Error("not json")),
    text: () => Promise.resolve(html),
  });
}

/* ------------------------- what the desk typed -> what is sent ------------- */

test("a chosen date is sent as new_check_out, on its own", () => {
  const dashboard = bootDashboard();
  const request = dashboard.extensionRequestPayload("2026-11-18", "");
  assert.equal(request.error, undefined);
  assert.equal(JSON.stringify(request.payload), '{"new_check_out":"2026-11-18"}');
  assert.equal(request.mode, "date");
});

test("a number of nights is sent as additional_nights, never as a date", () => {
  const dashboard = bootDashboard();
  const request = dashboard.extensionRequestPayload("", "2");
  assert.equal(JSON.stringify(request.payload), '{"additional_nights":2}');
  assert.equal(request.mode, "nights");
});

test("the date wins when both boxes somehow hold a value", () => {
  const dashboard = bootDashboard();
  const request = dashboard.extensionRequestPayload("2026-11-18", "2");
  assert.equal(JSON.stringify(request.payload), '{"new_check_out":"2026-11-18"}');
});

test("nonsense in the nights box is refused before it reaches the server", () => {
  const dashboard = bootDashboard();
  assert.match(dashboard.extensionRequestPayload("", "0").error, /at least one night/i);
  assert.match(dashboard.extensionRequestPayload("", "-3").error, /whole number|at least one/i);
  assert.match(dashboard.extensionRequestPayload("", "1.5").error, /whole number/i);
  assert.match(dashboard.extensionRequestPayload("", "999").error, /365/);
  assert.match(dashboard.extensionRequestPayload("", "abc").error, /whole number/i);
});

test("an empty form asks for a date or a number of nights", () => {
  const dashboard = bootDashboard();
  assert.match(dashboard.extensionRequestPayload("", "").error, /check-out date, or how many nights/i);
});

/* ------------------------------- the money -------------------------------- */

test("an empty amount means the guest paid the full extension", () => {
  const dashboard = bootDashboard();
  assert.equal(dashboard.extensionPaymentError("", "40000.00"), null);
  assert.equal(dashboard.extensionPaymentError(null, "40000.00"), null);
});

test("the desk cannot record more money than the extension costs", () => {
  const dashboard = bootDashboard();
  assert.match(dashboard.extensionPaymentError("50000", "40000.00"), /cannot pay more/i);
  assert.equal(dashboard.extensionPaymentError("40000", "40000.00"), null, "exactly the cost is fine");
  assert.equal(dashboard.extensionPaymentError("20000", "40000.00"), null, "a part payment is allowed");
  assert.match(dashboard.extensionPaymentError("-1", "40000.00"), /valid amount/i);
  assert.match(dashboard.extensionPaymentError("abc", "40000.00"), /valid amount/i);
});

/* --------------------- the server already said it can't ------------------- */

test("a stay the server refuses up front keeps its own reason", () => {
  const dashboard = bootDashboard();
  assert.equal(dashboard.extensionNotExtendable(null), "");
  assert.equal(dashboard.extensionNotExtendable({ can_extend: true }), "");
  assert.equal(
    dashboard.extensionNotExtendable({ can_extend: false, can_extend_reason: "This booking has no physical room assigned yet." }),
    "This booking has no physical room assigned yet."
  );
  assert.match(dashboard.extensionNotExtendable({ can_extend: false }), /cannot be extended/i);
});

test("the dialog greys out its inputs when the stay cannot be extended", () => {
  const js = source("js/dashboard.js");
  assert.match(js, /extensionNotExtendable\(history\)/, "the history answer must be acted on");
  assert.match(js, /blockedReason = notExtendable/, "and remembered for later attempts");
  const at = js.indexOf("blockedReason = notExtendable");
  assert.match(js.slice(at, at + 500), /el\.disabled = true/, "inputs are disabled, not just annotated");
});

test("Confirm repeats the server's own refusal instead of blaming an empty form", () => {
  const js = source("js/dashboard.js");
  assert.match(js, /lastQuoteError = msg/, "the refusal is remembered");
  assert.match(js, /showError\(blockedReason \|\| lastQuoteError \|\|/, "and quoted back on submit");
});

/* ------------------- the message when the route is not there -------------- */

test("a 404 that never reached the API says so, instead of 'item not found'", async () => {
  const ctx = bootApi(() => htmlRes(404, "<!doctype html><title>Not Found</title>"));
  const err = await ctx.window.API.post("/api/admin/bookings/J1-1/extend-quote/", {}).catch((e) => e);
  assert.equal(err.status, 404);
  assert.match(err.message, /does not recognise this request/i);
  assert.match(err.message, /deployment does not include this feature/i);
  assert.ok(
    !/requested item could not be found/i.test(err.message),
    "the misleading sentence the desk reported must be gone"
  );
});

test("a 404 from a real view keeps the sentence the backend authored", async () => {
  const ctx = bootApi(() => jsonRes(404, {
    success: false, code: "RESOURCE_NOT_FOUND", message: "This booking no longer exists.",
  }));
  const err = await ctx.window.API.post("/api/admin/bookings/J1-1/extend-quote/", {}).catch((e) => e);
  assert.equal(err.message, "This booking no longer exists.");
});

test("a JSON 404 with no sentence still falls back to 'could not be found'", async () => {
  const ctx = bootApi(() => jsonRes(404, { success: false, code: "RESOURCE_NOT_FOUND", message: "" }));
  const err = await ctx.window.API.post("/api/admin/bookings/J1-1/extend-quote/", {}).catch((e) => e);
  assert.match(err.message, /could not be found/i);
});

test("an empty 404 body is treated as an unserved route too", async () => {
  const ctx = bootApi(() => htmlRes(404, ""));
  const err = await ctx.window.API.post("/api/admin/bookings/J1-1/extend-quote/", {}).catch((e) => e);
  assert.match(err.message, /does not recognise this request/i);
});

/* ---------------- the two halves must agree about the URL ----------------- */

test("the endpoints the browser calls are registered by the backend", () => {
  const api = source("js/api.js");
  for (const action of ["extend-quote", "extend-stay"]) {
    assert.match(api, new RegExp(`bookingAction\\("${action}"`), `api.js must call ${action}`);
  }
  assert.match(api, /\/extensions\//, "the history call must be /extensions/");

  const urls = path.resolve(FRONTEND, "../backend/apps/bookings/urls_admin.py");
  if (!fs.existsSync(urls)) {
    /* The frontend can be checked out on its own; say so instead of passing. */
    console.log("SKIP: backend/apps/bookings/urls_admin.py not present — cannot cross-check routes");
    return;
  }
  const conf = fs.readFileSync(urls, "utf8");
  for (const route of ['"<str:lookup>/extend-quote/"', '"<str:lookup>/extend-stay/"', '"<str:lookup>/extensions/"']) {
    assert.ok(conf.includes(route), `backend must register ${route}`);
  }
});

test("the check-in screen offers Extend on every in-house row", () => {
  const html = source("dashboard/check-in.html");
  assert.match(html, /data-extend-btn/, "the button exists");
  assert.match(html, /JONE\.dashboard\.extendStayModal\(/, "and opens the shared dialog");
  assert.match(html, /await JONE\.dashboard\.extendStayModal\(exBooking,\{\}\)/, "with the in-house row");
});
