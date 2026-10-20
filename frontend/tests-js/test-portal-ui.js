/* Verified-email guest portal frontend contracts. */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { makeContext, loadScript, FRONTEND, disposeAll } = require("./harness");

test.after(() => disposeAll());

test("portal transport keeps the cookie session separate from staff JWT authorization", async () => {
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

  await portal.sessionRequest("portalOverview", "", "GET");
  await portal.sessionPathRequest(portal.pathFor("portalRequests", "SRQ/TEST", "comments"), "", "POST", { message: "Thank you" });
  assert.equal(calls[0].url, "/api/portal/me/");
  assert.equal(calls[0].opts.auth, false);
  assert.equal(calls[1].url, "/api/portal/requests/SRQ%2FTEST/comments/");
  assert.equal(calls[1].opts.auth, false);
  assert.deepEqual(calls[1].opts.body, { message: "Thank you" });
  // The browser attaches the HttpOnly cookie itself; page code never sends a session token.
  for (const call of calls) {
    assert.equal(call.opts.headers["X-Portal-Request"], "1");
    assert.equal(call.opts.headers["X-Portal-Session"], undefined);
    assert.equal(call.opts.headers.Authorization, undefined);
  }
  assert.equal(portal.sessionFromHash, undefined);
  assert.equal(portal.sessionHash, undefined);
});

test("portal ended-session message is shown only for the checkout code", () => {
  const ctx = makeContext();
  loadScript(ctx, "js/config.js");
  ctx.window.API = { request: () => Promise.resolve({}) };
  loadScript(ctx, "js/portal.js");
  const portal = ctx.window.JONE.portal;
  assert.equal(
    portal.sessionEndedMessage({ status: 401, code: "PORTAL_ACCESS_ENDED", message: "Portal access ended because checkout is complete." }),
    "Portal access ended because checkout is complete.",
  );
  assert.equal(portal.sessionEndedMessage({ status: 401, code: "UNAUTHORIZED", message: "invalid" }), "");
  assert.equal(portal.sessionEndedMessage(null), "");
});

test("portal UI consumes a magic link and relies on an HttpOnly cookie, not page-held tokens", () => {
  const transport = fs.readFileSync(path.join(FRONTEND, "js", "portal.js"), "utf8");
  const login = fs.readFileSync(path.join(FRONTEND, "portal", "login.html"), "utf8");
  const portal = fs.readFileSync(path.join(FRONTEND, "portal", "index.html"), "utf8");

  // The one-use link token is stripped from the address bar and never sent by Referer.
  assert.match(login, /portal\.consumeAccess/);
  assert.match(login, /history\.replaceState/);
  assert.match(login, /<meta name="referrer" content="no-referrer">/);
  assert.match(login, /window\.location\.replace\("index\.html"\)/);
  assert.doesNotMatch(login, /sessionHash|session_token/);
  // No fragment or memory session transport remains.
  assert.doesNotMatch(transport, /sessionFromHash|sessionHash|#session=|location\.hash/);
  assert.doesNotMatch(portal, /sessionFromHash|sessionHash|#session=/);
  assert.match(transport, /X-Portal-Request/);
  assert.match(portal, /JONE\.portal\.sessionEndedMessage\(err\)/);
  assert.doesNotMatch(transport, /localStorage|sessionStorage/);
  assert.doesNotMatch(portal, /my-bookings\.html/);
  const portalApp = portal.slice(portal.indexOf("var token=true"));
  assert.doesNotMatch(portalApp, /localStorage|sessionStorage/);
  // Reload-capable PWA/update scripts would erase this deliberately
  // memory-only session, so the release builder must keep them off portal HTML.
  assert.doesNotMatch(login, /js\/(?:pwa|update-checker|version)\.js/);
  assert.doesNotMatch(portal, /js\/(?:pwa|update-checker|version)\.js/);
});

test("portal and bearer-link pages avoid cacheable shells and reload controllers", () => {
  const build = fs.readFileSync(path.join(FRONTEND, "build.py"), "utf8");
  const worker = fs.readFileSync(path.join(FRONTEND, "sw.js"), "utf8");
  const staticHost = JSON.parse(fs.readFileSync(path.join(FRONTEND, "vercel.json"), "utf8"));
  const qrPage = fs.readFileSync(path.join(FRONTEND, "qr-service.html"), "utf8");
  assert.match(build, /PORTAL_PAGES = \["portal\/login\.html", "portal\/index\.html"\]/);
  assert.match(build, /QR_SERVICE_PAGES = \["qr-service\.html"\]/);
  assert.match(build, /if not is_memory_only_page:\n        raw = _ensure_pwa/);
  assert.doesNotMatch(qrPage, /js\/(?:pwa|update-checker|version)\.js|localStorage|sessionStorage/);
  assert.match(worker, /url\.pathname\.startsWith\("\/portal\/"\)/);
  assert.match(worker, /url\.pathname === "\/qr-service\.html"/);
  assert.match(worker, /protectedNavigationHandler/);
  for (const route of ["/dashboard/(.*)", "/portal/(.*)", "/qr-service.html"]) {
    const rule = staticHost.headers.find((entry) => entry.source === route);
    assert.ok(rule, `missing protected static-host header rule for ${route}`);
    assert.ok(rule.headers.some((header) => header.key === "Cache-Control" && header.value.includes("no-store")));
    assert.ok(rule.headers.some((header) => header.key === "X-Robots-Tag" && header.value === "noindex, nofollow"));
  }
  const qrRule = staticHost.headers.find((entry) => entry.source === "/qr-service.html");
  assert.ok(qrRule.headers.some((header) => header.key === "Referrer-Policy" && header.value === "no-referrer"));
});

test("portal dashboard consumes bounded authoritative reads, lazy statements, and controlled service actions", () => {
  const portal = fs.readFileSync(path.join(FRONTEND, "portal", "index.html"), "utf8");
  assert.match(portal, /portalOverview/);
  assert.match(portal, /portalFolios/);
  assert.match(portal, /portalMenu/);
  assert.match(portal, /portalServices/);
  assert.match(portal, /portal-catalog\.js/);
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
