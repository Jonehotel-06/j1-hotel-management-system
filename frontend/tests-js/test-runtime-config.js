/* Runtime configuration points non-local deployments cleanly at https://api.jonehotels.com
   while keeping local development pointed at http://127.0.0.1:8000 and preserving overrides. */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const { makeContext, loadScript } = require("./harness");

function boot(overrides, hostname = "jonehotels.com", protocol = "https:") {
  const ctx = makeContext({ hostname, protocol });
  if (overrides !== undefined) ctx.window.__APP_CONFIG__ = overrides;
  loadScript(ctx, "js/runtime-config.js");
  loadScript(ctx, "js/config.js");
  return ctx;
}

test("runtime config resolves deployed origins to https://api.jonehotels.com", () => {
  const ctx = boot();
  assert.equal(ctx.window.APP_CONFIG.API_BASE_URL, "https://api.jonehotels.com");
  assert.equal(ctx.window.APP_CONFIG.CONFIG_ERROR, "");
  assert.equal(ctx.window.APP_CONFIG.API_ENDPOINTS.availability, "/api/rooms/availability/");
});

test("runtime config resolves localhost development to http://127.0.0.1:8000", () => {
  const ctx = boot(undefined, "localhost", "http:");
  assert.equal(ctx.window.APP_CONFIG.API_BASE_URL, "http://127.0.0.1:8000");
  assert.equal(ctx.window.APP_CONFIG.CONFIG_ERROR, "");
});

test("a public configured API origin is normalized and endpoint overrides are additive", () => {
  const ctx = boot({
    API_BASE_URL: " https://api.example.test/ ",
    PAGE_SIZE: 25,
    API_ENDPOINTS: { availability: "/api/v2/availability/" },
  });

  assert.equal(ctx.window.APP_CONFIG.API_BASE_URL, "https://api.example.test");
  assert.equal(ctx.window.APP_CONFIG.PAGE_SIZE, 25);
  assert.equal(ctx.window.APP_CONFIG.API_ENDPOINTS.availability, "/api/v2/availability/");
  assert.equal(ctx.window.APP_CONFIG.API_ENDPOINTS.bookings, "/api/admin/bookings/");
});

test("invalid or insecure HTTP API_BASE_URL on HTTPS emits an explicit diagnostic", () => {
  const missingScheme = boot({ API_BASE_URL: "api.jonehotels.com" }, "jonehotels.com", "https:");
  assert.equal(missingScheme.window.APP_CONFIG.API_BASE_URL, "");
  assert.match(missingScheme.window.APP_CONFIG.CONFIG_ERROR, /Invalid API_BASE_URL/);

  const insecureHttp = boot({ API_BASE_URL: "http://api.jonehotels.com" }, "jonehotels.com", "https:");
  assert.equal(insecureHttp.window.APP_CONFIG.API_BASE_URL, "");
  assert.match(insecureHttp.window.APP_CONFIG.CONFIG_ERROR, /Insecure HTTP API_BASE_URL/);
});

test("checked-in configuration ships no third-party PaaS backend hostname", () => {
  const config = fs.readFileSync("js/config.js", "utf8");
  const runtime = fs.readFileSync("js/runtime-config.js", "utf8");
  assert.doesNotMatch(config + runtime, /onrender\.com|railway\.app|fly\.dev|vercel\.app|herokuapp\.com/i);
});
