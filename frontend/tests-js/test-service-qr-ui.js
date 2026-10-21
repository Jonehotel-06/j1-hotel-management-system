/* Public room/table QR intake and controlled service-QR administration contracts. */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { makeContext, loadScript, FRONTEND, disposeAll } = require("./harness");

test.after(() => disposeAll());

test("service QR APIs are centralized and the public bearer is sent only in a no-store header", () => {
  const ctx = makeContext();
  const calls = [];
  ctx.window.fetch = async (url, options) => {
    calls.push({ url, options });
    return { status: 200, ok: true, headers: { get: () => "application/json" }, json: async () => ({ success: true, data: { ok: true } }) };
  };
  loadScript(ctx, "js/config.js");
  loadScript(ctx, "js/api.js");
  const endpoints = ctx.window.APP_CONFIG.API_ENDPOINTS;
  assert.equal(endpoints.serviceQrLinks, "/api/admin/service-qr-links/");
  assert.equal(endpoints.serviceQrContext, "/api/service-qr/context/");
  assert.equal(endpoints.serviceQrRequests, "/api/service-qr/requests/");

  return ctx.window.API.get(endpoints.serviceQrContext, {
    auth: false, cache: "no-store", headers: { "X-Service-QR-Token": "memory-only-token" },
  }).then(() => {
    assert.equal(calls[0].options.cache, "no-store");
    assert.equal(calls[0].options.headers["X-Service-QR-Token"], "memory-only-token");
    assert.equal(calls[0].options.headers.Authorization, undefined);
  });
});

test("QR guest page keeps the bearer in the fragment and memory, with no durable storage", () => {
  const page = fs.readFileSync(path.join(FRONTEND, "qr-service.html"), "utf8");
  assert.match(page, /location\.hash/);
  assert.match(page, /history\.replaceState/);
  assert.match(page, /X-Service-QR-Token/);
  assert.match(page, /auth:false/);
  assert.match(page, /cache:"no-store"/);
  assert.match(page, /newIdempotencyKey\("service-qr"\)/);
  assert.doesNotMatch(page, /localStorage|sessionStorage/);
  assert.doesNotMatch(page, /href=["'][^"']*#token=/);
});

test("service QR admin page is capability-gated and supports create, rotate, revoke, and printable SVG", () => {
  const page = fs.readFileSync(path.join(FRONTEND, "dashboard", "service-qr.html"), "utf8");
  const dashboard = fs.readFileSync(path.join(FRONTEND, "js", "dashboard.js"), "utf8");
  assert.match(page, /boot\(\["admin","manager","general_manager"\]\)/);
  assert.match(page, /createRoom/);
  assert.match(page, /createTable/);
  assert.match(page, /API\.list\("posRestaurantTables",\{active:"true",page_size:100\}\)/);
  assert.match(page, /registered_table_code/);
  assert.match(page, /resourceAction\("serviceQrLinks",reference,"rotate"/);
  assert.match(page, /resourceAction\("serviceQrLinks",reference,"revoke"/);
  assert.match(page, /new Blob\(\[svg\],\{type:"image\/svg\+xml/);
  assert.match(dashboard, /capabilities: \["service_qr\.manage"\]/);
});

test("food-and-beverage QR requests are visible in the specialist guest-request workflow", () => {
  const page = fs.readFileSync(path.join(FRONTEND, "dashboard", "service-requests.html"), "utf8");
  assert.match(page, /FOOD_BEVERAGE/);
  assert.match(page, /item\.table_number/);
  assert.match(page, /"QR"/);
  assert.match(page, /"waiter"/);
  assert.match(page, /"bartender"/);
  assert.match(page, /canCreatePosDraft/);
  assert.match(page, /data-sr-pos-draft/);
  assert.match(page, /pos\.html\?service_request=/);
});
