/* tests-js/test-payment-api-base-url.js */
/* The reported incident reached the payment endpoint at
      http://127.0.0.1:8000/api/payments/initialize/
   while the rest of the site kept working. That URL is the LOCAL DEVELOPMENT
   fallback in js/config.js, so this file pins the rules that keep it from
   ever being used by a deployed site — and pins that the payment endpoint
   travels on exactly the same configured origin as every other API call.

   Run: node --test tests-js/test-payment-api-base-url.js   (from frontend/) */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const { makeContext, loadScript } = require("./harness");

const FRONTEND = path.resolve(__dirname, "..");

/* Boot the real config.js + api.js for a given host, capture the URL the
   API client would request, and return both. */
function boot({ hostname, runtimeConfig }) {
  const ctx = makeContext({ hostname, pathname: "/booking-confirmation.html" });
  if (runtimeConfig !== undefined) ctx.window.__APP_CONFIG__ = runtimeConfig;
  loadScript(ctx, "js/runtime-config.js");
  loadScript(ctx, "js/config.js");

  const requested = [];
  ctx.fetch = (url) => {
    requested.push(String(url));
    return Promise.resolve({
      status: 201,
      ok: true,
      headers: { get: () => "application/json" },
      json: () => Promise.resolve({
        success: true,
        data: {
          reference: "J1P-TEST-1",
          booking_reference: "J1-20261018-ABCDEF12",
          authorization_url: "https://checkout.paystack.com/test",
          amount: "50000.00",
          currency: "NGN",
        },
      }),
      text: () => Promise.resolve(""),
    });
  };
  loadScript(ctx, "js/api.js");
  return { ctx, requested };
}

test("a deployed site calls the payment endpoint on its own origin, never on 127.0.0.1", async () => {
  const { ctx, requested } = boot({ hostname: "www.jonehotel.test" });
  assert.equal(ctx.window.APP_CONFIG.API_BASE_URL, "", "a public host defaults to same-origin");

  await ctx.window.API.initPayment("J1-20261018-ABCDEF12");

  assert.equal(requested.length, 1);
  assert.equal(requested[0], "/api/payments/initialize/");
  assert.doesNotMatch(requested[0], /127\.0\.0\.1|localhost/,
    "a deployed site must never fall back to the local dev API");
});

test("the localhost fallback is only ever reachable from a local dev host", async () => {
  const { ctx, requested } = boot({ hostname: "127.0.0.1" });
  assert.equal(ctx.window.APP_CONFIG.API_BASE_URL, "http://127.0.0.1:8000");

  await ctx.window.API.initPayment("J1-20261018-ABCDEF12");

  assert.equal(requested[0], "http://127.0.0.1:8000/api/payments/initialize/");
});

test("a deployment-configured API origin is used verbatim for the payment endpoint", async () => {
  const { ctx, requested } = boot({
    hostname: "www.jonehotel.test",
    runtimeConfig: { API_BASE_URL: "https://api.example.test/" },
  });
  assert.equal(ctx.window.APP_CONFIG.API_BASE_URL, "https://api.example.test");

  await ctx.window.API.initPayment("J1-20261018-ABCDEF12");

  assert.equal(requested[0], "https://api.example.test/api/payments/initialize/");
});

test("the payment endpoint uses the same base as every other API endpoint", async () => {
  const { ctx, requested } = boot({
    hostname: "www.jonehotel.test",
    runtimeConfig: { API_BASE_URL: "https://api.example.test" },
  });

  await ctx.window.API.initPayment("J1-20261018-ABCDEF12");
  await ctx.window.API.get("/api/rooms/");

  assert.equal(requested[0], "https://api.example.test/api/payments/initialize/");
  assert.equal(requested[1], "https://api.example.test/api/rooms/");
  assert.equal(
    requested[0].replace("/api/payments/initialize/", ""),
    requested[1].replace("/api/rooms/", ""),
    "the payment endpoint must not carry a base URL of its own"
  );
});

test("the payment path keeps the trailing slash Django's route requires", () => {
  const source = fs.readFileSync(path.join(FRONTEND, "js", "api.js"), "utf8");
  assert.match(source, /post\("\/api\/payments\/initialize\/"/,
    "a missing trailing slash turns the call into a 301/308 redirect");
  assert.match(source, /\/api\/payments\/verify\/\$\{encodeURIComponent\(/);
});

test("no shipped frontend file hardcodes a deployment API host for the payment flow", () => {
  const config = fs.readFileSync(path.join(FRONTEND, "js", "config.js"), "utf8");
  const runtime = fs.readFileSync(path.join(FRONTEND, "js", "runtime-config.js"), "utf8");
  const api = fs.readFileSync(path.join(FRONTEND, "js", "api.js"), "utf8");
  const combined = config + runtime + api;

  // 1. No hosting-provider hostname may be committed to the frontend.
  assert.doesNotMatch(combined, /railway\.app|onrender\.com|fly\.dev|vercel\.app|herokuapp\.com/i,
    "a deployment-specific API host must never be committed to the frontend");

  // 2. The only executable host in config.js is the documented local-dev
  //    fallback; a public host resolves to same-origin instead.
  const executableHosts = (config.match(/["']https?:\/\/[^"']+["']/g) || [])
    .map((h) => h.replace(/["']/g, ""));
  assert.deepEqual(executableHosts, ["http://127.0.0.1:8000"],
    "config.js may only name the local dev API");

  // 3. The shipped runtime config defaults to same-origin, so a deployment
  //    that forgets to set API_BASE_URL still talks to its own backend.
  assert.match(runtime, /API_BASE_URL:\s*""/,
    "runtime-config.js must ship an empty (same-origin) API base");
});
