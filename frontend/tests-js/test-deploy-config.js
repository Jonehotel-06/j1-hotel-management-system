/* tests-js/test-deploy-config.js */
/* Deployment configuration contract: the same-origin /api proxy must actually
   reach Django without a trailing-slash redirect loop, and the checked-in
   static config must stay deployment-neutral. */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { FRONTEND } = require("./harness");

const vercel = JSON.parse(fs.readFileSync(path.join(FRONTEND, "vercel.json"), "utf8"));

test("vercel proxies /api to the backend with the trailing slash restored", () => {
  const rewrite = (vercel.rewrites || []).find((r) => String(r.source).startsWith("/api/"));
  assert.ok(rewrite, "/api rewrite must exist so the site can use same-origin API calls");
  // Vercel's trailingSlash:false redirects strip the slash; Django's
  // APPEND_SLASH re-adds it — the destination must re-add it too or every
  // proxied API call 308-loops (verified live 2026-10-11).
  assert.match(String(rewrite.destination), /\/api\/:path\*\/$/,
    "destination must end in '/api/:path*/' (slash restored before Django)");
  assert.match(String(rewrite.destination), /^https:\/\//);
});

test("security headers keep connect-src compatible with the same-origin API proxy", () => {
  const headerBlocks = vercel.headers || [];
  const all = JSON.stringify(headerBlocks);
  assert.match(all, /content-security-policy/i, "enforcing CSP must be configured");
  // With /api proxied same-origin, connect-src 'self' is correct and strict.
  assert.match(all, /connect-src 'self'/);
});

test("checked-in runtime config stays free of provider hostnames", () => {
  const config = fs.readFileSync(path.join(FRONTEND, "js", "config.js"), "utf8");
  const runtime = fs.readFileSync(path.join(FRONTEND, "js", "runtime-config.js"), "utf8");
  assert.doesNotMatch(config + runtime, /railway\.app|onrender\.com|fly\.dev/i);
});
