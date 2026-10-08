/* Runtime configuration stays deployment-neutral and preserves API defaults. */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const { makeContext, loadScript } = require("./harness");

function boot(overrides) {
  const ctx = makeContext({ hostname: "hotel.example.test" });
  if (overrides !== undefined) ctx.window.__APP_CONFIG__ = overrides;
  loadScript(ctx, "js/runtime-config.js");
  loadScript(ctx, "js/config.js");
  return ctx;
}

test("runtime config defaults to same-origin API requests", () => {
  const ctx = boot();
  assert.equal(ctx.window.APP_CONFIG.API_BASE_URL, "");
  assert.equal(ctx.window.APP_CONFIG.API_ENDPOINTS.availability, "/api/rooms/availability/");
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

test("checked-in configuration ships no deployment-provider backend hostname", () => {
  const config = fs.readFileSync("js/config.js", "utf8");
  const runtime = fs.readFileSync("js/runtime-config.js", "utf8");
  assert.doesNotMatch(config + runtime, /onrender\.com|railway\.app|fly\.dev/i);
});
