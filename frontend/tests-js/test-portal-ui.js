/* Verified-email guest portal frontend contracts. */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { makeContext, loadScript, FRONTEND, disposeAll } = require("./harness");

test.after(() => disposeAll());

test("portal transport keeps the opaque session separate from staff JWT authorization", async () => {
  const ctx = makeContext();
  loadScript(ctx, "js/config.js");
  const calls = [];
  ctx.window.API = {
    request: (url, opts) => { calls.push({ url, opts }); return Promise.resolve({ data: {} }); },
  };
  loadScript(ctx, "js/portal.js");
  const portal = ctx.window.JONE.portal;
  assert.equal(ctx.window.APP_CONFIG.API_ENDPOINTS.portalAccessRequest, "/api/portal/auth/request/");
  assert.equal(ctx.window.APP_CONFIG.API_ENDPOINTS.portalAccessConsume, "/api/portal/auth/consume/");
  assert.equal(ctx.window.APP_CONFIG.API_ENDPOINTS.portalOverview, "/api/portal/me/");
  assert.equal(ctx.window.APP_CONFIG.API_ENDPOINTS.portalFolios, "/api/portal/folios/");
  assert.equal(ctx.window.APP_CONFIG.API_ENDPOINTS.portalRequests, "/api/portal/requests/");

  await portal.sessionRequest("portalOverview", "opaque-session-token", "GET");
  await portal.sessionPathRequest(portal.pathFor("portalRequests", "SRQ/TEST", "comments"), "opaque-session-token", "POST", { message: "Thank you" });
  assert.equal(calls[0].url, "/api/portal/me/");
  assert.equal(calls[0].opts.auth, false);
  assert.equal(calls[0].opts.headers["X-Portal-Session"], "opaque-session-token");
  assert.equal(calls[1].url, "/api/portal/requests/SRQ%2FTEST/comments/");
  assert.equal(calls[1].opts.auth, false);
  assert.deepEqual(calls[1].opts.body, { message: "Thank you" });
  assert.equal(calls[1].opts.headers["X-Portal-Session"], "opaque-session-token");
});

test("portal UI consumes a magic-link token without durable browser storage", () => {
  const transport = fs.readFileSync(path.join(FRONTEND, "js", "portal.js"), "utf8");
  const login = fs.readFileSync(path.join(FRONTEND, "portal", "login.html"), "utf8");
  const portal = fs.readFileSync(path.join(FRONTEND, "portal", "index.html"), "utf8");

  assert.match(transport, /sessionFromHash/);
  assert.match(transport, /history\.replaceState/);
  assert.doesNotMatch(transport, /localStorage|sessionStorage/);
  assert.match(login, /portal\.consumeAccess/);
  assert.match(login, /history\.replaceState/);
  assert.match(login, /sessionHash\(sessionToken\)/);
  assert.match(portal, /portal\.sessionFromHash\(\)/);
  assert.doesNotMatch(portal, /my-bookings\.html/);
  const portalApp = portal.slice(portal.indexOf("var token=JONE.portal.sessionFromHash"));
  assert.doesNotMatch(portalApp, /localStorage|sessionStorage/);
  // Reload-capable PWA/update scripts would erase this deliberately
  // memory-only session, so the release builder must keep them off portal HTML.
  assert.doesNotMatch(login, /js\/(?:pwa|update-checker|version)\.js/);
  assert.doesNotMatch(portal, /js\/(?:pwa|update-checker|version)\.js/);
});

test("portal pages are excluded from page caching and release reload controllers", () => {
  const build = fs.readFileSync(path.join(FRONTEND, "build.py"), "utf8");
  const worker = fs.readFileSync(path.join(FRONTEND, "sw.js"), "utf8");
  const staticHost = fs.readFileSync(path.join(FRONTEND, "vercel.json"), "utf8");
  assert.match(build, /PORTAL_PAGES = \["portal\/login\.html", "portal\/index\.html"\]/);
  assert.match(build, /if not is_portal:\n        raw = _ensure_pwa/);
  assert.match(worker, /url\.pathname\.startsWith\("\/portal\/"\)/);
  assert.match(worker, /protectedNavigationHandler/);
  assert.match(staticHost, /"source": "\/portal\/\(\.\*\)"/);
  assert.match(staticHost, /"Cache-Control", "value": "no-store"/);
});

test("portal dashboard consumes bounded authoritative reads, lazy statements, and controlled service actions", () => {
  const portal = fs.readFileSync(path.join(FRONTEND, "portal", "index.html"), "utf8");
  assert.match(portal, /portalOverview/);
  assert.match(portal, /portalFolios/);
  assert.match(portal, /portalRequests/);
  assert.match(portal, /in_house_stay/);
  assert.match(portal, /data-portal-postings/);
  assert.match(portal, /page_size:10/);
  assert.match(portal, /newIdempotencyKey\("portal-request"\)/);
  assert.match(portal, /sessionPathRequest/);
  assert.match(portal, /loading-skeleton/);
  assert.match(portal, /Guest portal unavailable/);
  assert.match(portal, /data-portal-postings-retry/);
  assert.match(portal, /data-portal-folio-modal-retry/);
  assert.match(portal, /data-portal-request-modal-retry/);
  assert.doesNotMatch(portal, /setInterval\s*\(/);
});
