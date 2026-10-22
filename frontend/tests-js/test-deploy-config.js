/* tests-js/test-deploy-config.js */
/* Deployment configuration contract:
   - Production static frontend on Cloudflare (jonehotels.com / www.jonehotels.com)
     calls https://api.jonehotels.com directly over HTTPS via js/runtime-config.js.
   - No obsolete Vercel or Cloudflare Worker /api rewrite remains active.
   - CSP permits connections strictly to 'self' and https://api.jonehotels.com.
   - Every API client endpoint preserves Django's trailing-slash contract.
   Run from /frontend: node --test tests-js/test-deploy-config.js */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { FRONTEND } = require("./harness");

const vercel = JSON.parse(fs.readFileSync(path.join(FRONTEND, "vercel.json"), "utf8"));
const cloudflareHeaders = fs.readFileSync(path.join(FRONTEND, "_headers"), "utf8");

test("no obsolete production API rewrite remains active in static hosting configs", () => {
  assert.equal(vercel.rewrites, undefined, "vercel.json must not retain stale /api or /media rewrites");
  const rawWrangler = fs.readFileSync(path.join(FRONTEND, "wrangler.jsonc"), "utf8");
  const cleanWrangler = rawWrangler.replace(/^\s*\/\/.*$/gm, "");
  const wrangler = JSON.parse(cleanWrangler);
  assert.equal(wrangler.main, undefined, "wrangler.jsonc must deploy pure static assets without a competing Worker proxy");
  assert.equal(fs.existsSync(path.join(FRONTEND, "worker", "index.js")), false, "obsolete worker/index.js proxy must not remain");
});

test("the client always sends the trailing slash Django routes require", () => {
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
  const config = fs.readFileSync(path.join(FRONTEND, "js", "config.js"), "utf8");
  const declared = config.match(/\b(?:bookings|posOrders|attendance|staffProfiles|auditLogs|posMenu)\s*:\s*"([^"]+)"/g) || [];
  assert.ok(declared.length >= 4, "expected the staff endpoint declarations to be found");
  for (const line of declared) {
    assert.match(line, /\/"$/, `staff endpoint must be declared with a trailing slash: ${line}`);
  }
});

test("security headers configure strict CSP allowing https://api.jonehotels.com on Cloudflare and static hosts", () => {
  assert.match(cloudflareHeaders, /Content-Security-Policy:/i);
  assert.match(cloudflareHeaders, /connect-src 'self' https:\/\/api\.jonehotels\.com;/);
  assert.doesNotMatch(cloudflareHeaders, /Content-Security-Policy-Report-Only:/i);

  const headerBlocks = vercel.headers || [];
  const all = JSON.stringify(headerBlocks);
  assert.match(all, /content-security-policy/i, "enforcing CSP must be configured");
  assert.match(all, /connect-src 'self' https:\/\/api\.jonehotels\.com;/);
});

test("checked-in runtime config stays free of third-party PaaS hostnames", () => {
  const config = fs.readFileSync(path.join(FRONTEND, "js", "config.js"), "utf8");
  const runtime = fs.readFileSync(path.join(FRONTEND, "js", "runtime-config.js"), "utf8");
  assert.doesNotMatch(config + runtime, /railway\.app|onrender\.com|fly\.dev|vercel\.app|herokuapp\.com|code\.run/i);
});

test("no shipped JS bakes in a loopback API URL outside config.js local fallback", () => {
  const dir = path.join(FRONTEND, "js");
  const offenders = [];
  for (const file of fs.readdirSync(dir)) {
    if (!file.endsWith(".js")) continue;
    const src = fs.readFileSync(path.join(dir, file), "utf8");
    const literals = src.match(/https?:\/\/(?:127\.0\.0\.1|localhost)(?::\d+)?/g) || [];
    const allowed = file === "config.js" ? 1 : 0;   // LOCAL_API_BASE_URL only
    if (literals.length > allowed) offenders.push(`${file}: ${literals.join(", ")}`);
  }
  assert.deepEqual(offenders, [], "loopback API hosts must not be baked into shipped JS");
});
