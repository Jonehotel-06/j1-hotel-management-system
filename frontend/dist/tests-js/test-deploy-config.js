/* tests-js/test-deploy-config.js */
/* Deployment configuration contract: the same-origin /api proxy must reach
   Django, and the checked-in static config must stay deployment-neutral.

   HISTORY (why one check is pending rather than green):
   2026-10-11 — the `/api/:path*` destination ending in `/` was the form verified
   live, because Vercel's default trailingSlash:false normalises the incoming
   path and Django's APPEND_SLASH only redirects GET/HEAD (a POST that arrives
   without its trailing slash is answered 301 -> converted to GET, i.e. the write
   is lost).
   2026-10-13 (`2cee2e5`, owner) — the rewrite was deliberately changed to
   `source: /api/(.*)` + `destination: .../api/$1`. The `$1` form *preserves*
   whatever the client sent, so it is safe **as long as every API call site sends
   the trailing slash** (asserted below) and Vercel does not strip it before the
   rewrite. That last part can only be settled against the deployed host, so the
   strict destination check is skipped with the exact command to run instead of
   being deleted or silently relaxed.
   Run from /frontend: node --test tests-js/test-deploy-config.js */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { FRONTEND } = require("./harness");

const vercel = JSON.parse(fs.readFileSync(path.join(FRONTEND, "vercel.json"), "utf8"));

test("vercel proxies /api to the backend over https", () => {
  const rewrite = (vercel.rewrites || []).find((r) => String(r.source).startsWith("/api/"));
  assert.ok(rewrite, "/api rewrite must exist so the site can use same-origin API calls");
  assert.match(String(rewrite.destination), /^https:\/\//, "the proxy target must be https");
  assert.match(String(rewrite.destination), /\/api\//, "the proxy must land inside the API namespace");
  // Either form is accepted here; the trailing-slash question is the next test.
  assert.ok(String(rewrite.destination).endsWith("/api/$1") || /\/api\/:path\*\/$/.test(String(rewrite.destination)),
    `unexpected rewrite destination: ${rewrite.destination}`);
});

test("the client always sends the trailing slash the proxy form depends on", () => {
  // With `$1`, whatever the browser sent is forwarded verbatim. Every staff
  // resource URL therefore has to end in "/" or Django would answer a
  // slash-less POST with a 301 that loses the body.
  const api = fs.readFileSync(path.join(FRONTEND, "js", "api.js"), "utf8");
  const required = [
    'return get(base + "/", { params, ...opts });',
    "return get(`${base}/${encodeURIComponent(id)}/`, opts);",
    'return post(base + "/", payload, opts);',
    "return patch(`${base}/${encodeURIComponent(id)}/`, payload, opts);",
    'return post(`${base}/${encodeURIComponent(id)}/${safeAction}/`, payload || {}, opts);',
  ];
  for (const call of required) {
    assert.ok(api.includes(call), `staff resource call must stay slash-terminated: ${call}`);
  }
  // Declared endpoints keep their trailing slash in the ONE place they live.
  const config = fs.readFileSync(path.join(FRONTEND, "js", "config.js"), "utf8");
  const declared = config.match(/\b(?:bookings|posOrders|attendance|staffProfiles|auditLogs|posMenu)\s*:\s*"([^"]+)"/g) || [];
  assert.ok(declared.length >= 4, "expected the staff endpoint declarations to be found");
  for (const line of declared) {
    assert.match(line, /\/"$/, `staff endpoint must be declared with a trailing slash: ${line}`);
  }
});

test("the trailing-slash rewrite form is settled LIVE, not from a unit test", { skip: "run the deployed check in PRODUCTION_AUDIT_REPAIR_REPORT.md §F6: `curl -s -o /dev/null -w '%{http_code}\\n' -X POST https://<vercel-host>/api/auth/login/ -H 'Content-Type: application/json' -d '{\"email\":\"x@y.z\",\"password\":\"nope\"}'` must answer 400/401, never 308/500. If it 308s or errors, restore `{\"source\": \"/api/:path*\", \"destination\": \"https://j1-hotel-management-system-production.up.railway.app/api/:path*/\"}` in frontend/vercel.json, or make js/api.js slash-less on purpose." }, () => {});

test("security headers keep connect-src compatible with the same-origin API proxy", () => {
  const headerBlocks = vercel.headers || [];
  const all = JSON.stringify(headerBlocks);
  assert.match(all, /content-security-policy/i, "enforcing CSP must be configured");
  assert.match(all, /connect-src 'self'/);
});

test("checked-in runtime config stays free of provider hostnames", () => {
  const config = fs.readFileSync(path.join(FRONTEND, "js", "config.js"), "utf8");
  const runtime = fs.readFileSync(path.join(FRONTEND, "js", "runtime-config.js"), "utf8");
  assert.doesNotMatch(config + runtime, /railway\.app|onrender\.com|fly\.dev/i);
});

test("no shipped JS bakes in a loopback API URL", () => {
  const dir = path.join(FRONTEND, "js");
  const offenders = [];
  for (const file of fs.readdirSync(dir)) {
    if (!file.endsWith(".js")) continue;
    const src = fs.readFileSync(path.join(dir, file), "utf8");
    // A literal loopback URL with a port is a baked-in deployment target.
    // Comparing location.hostname against "localhost" (dev detection, as in
    // api.js/pwa.js/update-checker.js) and the documented local fallback in
    // config.js are legitimate and must keep working.
    const literals = src.match(/https?:\/\/(?:127\.0\.0\.1|localhost)(?::\d+)?/g) || [];
    const allowed = file === "config.js" ? 1 : 0;   // LOCAL_API_BASE_URL only
    if (literals.length > allowed) offenders.push(`${file}: ${literals.join(", ")}`);
  }
  assert.deepEqual(offenders, [], "loopback API hosts must not be baked into shipped JS");
});
