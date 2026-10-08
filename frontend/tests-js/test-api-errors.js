/* tests-js/test-api-errors.js */
/* Tests for js/api.js request error classification — the fix for the false
   "booking service unavailable" (status 0) errors.
   Run: node --test tests-js/test_api_errors.js   (from frontend/) */
"use strict";

const test = require("node:test");
const assert = require("node:assert");
const { makeContext, loadScript } = require("./harness");

function bootApi({ fetchImpl } = {}) {
  const ctx = makeContext({ hostname: "www.jonehotel.test", pathname: "/index.html" });
  ctx.fetch = fetchImpl || (() => Promise.reject(new TypeError("Failed to fetch")));
  ctx.window.APP_CONFIG = {
    API_BASE_URL: "https://api.example.test",
    STORAGE: { AUTH: "jone.auth", SESSION: "jone.session", BOOKING: "jone.booking.draft" },
    API_ENDPOINTS: { bookings: "/api/admin/bookings/", payments: "/api/admin/payments/" },
  };
  loadScript(ctx, "js/api.js");
  return ctx;
}

function res(status, body, headers = {}) {
  return Promise.resolve({
    status,
    ok: status >= 200 && status < 300,
    headers: { get: (k) => headers[k] || null },
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  });
}

test("HTTP 400 keeps status and carries field errors", async () => {
  const ctx = bootApi({
    fetchImpl: () => res(400, {
      success: false, code: "VALIDATION_ERROR",
      message: "Validation failed.",
      errors: { check_in: ["Check-in must be in the future."] },
    }, { "content-type": "application/json" }),
  });
  const err = await ctx.window.API.post("/api/bookings/", {}).catch((e) => e);
  assert.equal(err.name, "APIError");
  assert.equal(err.status, 400);
  assert.equal(err.kind, "http");
  assert.equal(err.code, "VALIDATION_ERROR");
  assert.match(err.message, /check-in must be in the future/i);
});

test("HTTP 409 room conflict keeps code for the booking flow", async () => {
  const ctx = bootApi({
    fetchImpl: () => res(409, {
      success: false, code: "ROOM_UNAVAILABLE",
      message: "No rooms of this type are available for the selected dates.",
    }, { "content-type": "application/json" }),
  });
  const err = await ctx.window.API.post("/api/bookings/", {}).catch((e) => e);
  assert.equal(err.status, 409);
  assert.equal(err.code, "ROOM_UNAVAILABLE");
});

test("timeout is classified as kind=timeout (not 'service unavailable')", async () => {
  const ctx = bootApi({
    fetchImpl: (_url, init) => new Promise((_resolve, reject) => {
      init.signal.addEventListener("abort", () => {
        const e = new Error("The operation was aborted.");
        e.name = "AbortError";
        reject(e);
      });
      // never resolves: simulates a server that never answers
    }),
  });
  const err = await ctx.window.API.post("/api/bookings/", {}, { timeout: 15 }).catch((e) => e);
  assert.equal(err.name, "APIError");
  assert.equal(err.status, 0);
  assert.equal(err.kind, "timeout");
  assert.match(err.message, /may still complete/i);
});

test("network failure (no response) is classified as kind=network", async () => {
  const ctx = bootApi({
    fetchImpl: () => Promise.reject(new TypeError("Failed to fetch")),
  });
  const err = await ctx.window.API.get("/api/rooms/").catch((e) => e);
  assert.equal(err.status, 0);
  assert.equal(err.kind, "network");
});

test("offline is classified as kind=offline", async () => {
  const ctx = bootApi({
    fetchImpl: () => Promise.reject(new TypeError("Failed to fetch")),
  });
  ctx.navigator.onLine = false;
  const err = await ctx.window.API.get("/api/rooms/").catch((e) => e);
  assert.equal(err.kind, "offline");
});

test("malformed JSON on success is classified as kind=parse", async () => {
  const ctx = bootApi({
    fetchImpl: () => Promise.resolve({
      status: 201,
      ok: true,
      headers: { get: () => "application/json" },
      json: () => Promise.reject(new SyntaxError("Unexpected token < in JSON")),
      text: () => Promise.resolve("<html>gateway error</html>"),
    }),
  });
  const err = await ctx.window.API.post("/api/bookings/", {}).catch((e) => e);
  assert.equal(err.kind, "parse");
  assert.equal(err.status, 201);
});

test("success envelope is unwrapped exactly once", async () => {
  const ctx = bootApi({
    fetchImpl: () => res(201, {
      success: true,
      message: "Booking created.",
      data: { booking_reference: "J1-20260915-TEST", total_amount: "340000.00" },
    }, { "content-type": "application/json" }),
  });
  const out = await ctx.window.API.post("/api/bookings/", {});
  assert.equal(out.status, 201);
  assert.equal(out.data.booking_reference, "J1-20260915-TEST");
  assert.equal(out.data.total_amount, "340000.00");
});

test("HTTP 500 / 502 / 503 map to distinct friendly messages", async () => {
  const cases = [
    [500, /trouble completing/i],
    [502, /temporarily unavailable/i],
    [503, /temporarily unavailable/i],
    [429, /too many requests/i],
    [401, /session has expired/i],
    [403, /don't have permission/i],
    [404, /could not be found/i],
  ];
  for (const [status, re] of cases) {
    const ctx = bootApi({
      fetchImpl: () => res(status, { success: false, code: "X", message: "" }, { "content-type": "application/json" }),
    });
    const err = await ctx.window.API.get("/api/x/").catch((e) => e);
    assert.equal(err.status, status, `status ${status}`);
    assert.equal(err.kind, "http", `kind for ${status}`);
    assert.match(err.message, re, `message for ${status}`);
  }
});


test.after(() => { require("./harness").disposeAll(); });
