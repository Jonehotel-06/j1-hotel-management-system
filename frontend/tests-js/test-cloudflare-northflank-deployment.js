/* Comprehensive frontend deployment readiness suite for Cloudflare (jonehotels.com)
   -> Northflank (https://api.jonehotels.com). */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const { FRONTEND, makeContext, loadScript } = require("./harness");

test("1. Both jonehotels.com and www.jonehotels.com resolve API calls to https://api.jonehotels.com without double slashes", async () => {
  for (const host of ["jonehotels.com", "www.jonehotels.com"]) {
    const calls = [];
    const ctx = makeContext({ hostname: host, protocol: "https:" });
    ctx.fetch = (url, init) => {
      calls.push({ url: String(url), init: init || {} });
      return Promise.resolve({
        ok: true,
        status: 200,
        headers: { get: () => "application/json" },
        json: () => Promise.resolve({ success: true, data: {} }),
        text: () => Promise.resolve("{}"),
      });
    };
    loadScript(ctx, "js/runtime-config.js");
    loadScript(ctx, "js/config.js");
    loadScript(ctx, "js/api.js");
    loadScript(ctx, "js/portal.js");

    assert.equal(ctx.window.APP_CONFIG.API_BASE_URL, "https://api.jonehotels.com");
    await ctx.window.API.get("/api/rooms/");
    await ctx.window.JONE.portal.sessionRequest("portalOverview", null, "GET");
    await ctx.window.JONE.portal.requestAccess("guest@example.com");

    assert.equal(calls[0].url, "https://api.jonehotels.com/api/rooms/");
    assert.equal(calls[0].init.credentials, undefined, "non-portal JWT/public calls do not attach cookies");

    assert.equal(calls[1].url, "https://api.jonehotels.com/api/portal/me/");
    assert.equal(calls[1].init.credentials, "include", "portal calls must include HttpOnly session cookie across subdomains");
    assert.equal(calls[1].init.headers["X-Portal-Request"], "1", "portal calls send X-Portal-Request CSRF guard");

    assert.equal(calls[2].url, "https://api.jonehotels.com/api/portal/auth/request/");
    assert.equal(calls[2].init.credentials, "include", "portal auth consume/request accepts Set-Cookie across subdomains");
  }
});

test("2. Cloudflare _headers sets Cache-Control: no-store on runtime-config.js and token-bearing pages with Referrer-Policy: no-referrer", () => {
  const headers = fs.readFileSync(path.join(FRONTEND, "_headers"), "utf8");
  for (const route of [
    "/js/runtime-config.js",
    "/version.json",
    "/dashboard/*",
    "/portal/*",
    "/qr-service.html",
    "/payment-verify.html",
    "/booking-confirmation.html",
    "/cancellation-result.html",
  ]) {
    assert.ok(headers.includes(`\n${route}\n`), `missing Cloudflare _headers block for ${route}`);
  }
  assert.match(headers, /\/js\/runtime-config\.js\s+Cache-Control:\s*no-store/);
  assert.match(headers, /X-Frame-Options:\s*DENY/);
  assert.match(headers, /X-Content-Type-Options:\s*nosniff/);
  assert.match(headers, /frame-ancestors 'none'/);
  assert.match(headers, /form-action 'self' https:\/\/checkout\.paystack\.com/);
});

test("3. All 72 deployed HTML pages load runtime-config.js before config.js and match version.json", () => {
  const expectedVersion = JSON.parse(fs.readFileSync(path.join(FRONTEND, "version.json"), "utf8")).version;
  const htmlFiles = [];
  for (const sub of ["", "dashboard", "portal"]) {
    const dir = path.join(FRONTEND, sub);
    for (const entry of fs.readdirSync(dir)) {
      if (entry.endsWith(".html")) htmlFiles.push(path.join(sub, entry));
    }
  }
  assert.equal(htmlFiles.length, 72, `expected 72 HTML pages, found ${htmlFiles.length}`);

  for (const rel of htmlFiles) {
    const content = fs.readFileSync(path.join(FRONTEND, rel), "utf8");
    const srcs = [...content.matchAll(/<script[^>]+src="([^"]+)"/g)].map((m) => m[1]);
    const cfgIdx = srcs.findIndex((s) => /(^|\/)config\.js(\?|$)/.test(s));
    if (cfgIdx !== -1) {
      const rtIdx = srcs.findIndex((s) => /runtime-config\.js(\?|$)/.test(s));
      assert.ok(rtIdx !== -1 && rtIdx < cfgIdx, `${rel} must load runtime-config.js before config.js`);
    }
    for (const s of srcs) {
      if (!s.startsWith("http")) {
        assert.ok(s.endsWith(`?v=${expectedVersion}`), `${rel} has unstamped script ${s} (expected ?v=${expectedVersion})`);
      }
    }
  }
});

test("4. No secrets, private keys, or stale hosting domains exist anywhere in shipped frontend assets", () => {
  const ignoreDirs = new Set(["tests-js", "tests-pwa", "__pycache__"]);
  const secretPatterns = [
    /\bsk_live_[A-Za-z0-9]+/,
    /\bsk_test_[A-Za-z0-9]+/,
    /\bxkeysib-[A-Za-z0-9-]+/,
    /\bSG\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/,
    /railway\.app|onrender\.com|fly\.dev|herokuapp\.com|code\.run/i,
  ];
  function walk(dir) {
    for (const name of fs.readdirSync(dir, { withFileTypes: true })) {
      if (ignoreDirs.has(name.name)) continue;
      const full = path.join(dir, name.name);
      if (name.isDirectory()) walk(full);
      else if (/\.(html|js|css|json|jsonc|xml|txt|webmanifest)$/i.test(name.name)) {
        const txt = fs.readFileSync(full, "utf8");
        for (const re of secretPatterns) {
          assert.doesNotMatch(txt, re, `forbidden pattern ${re} in ${path.relative(FRONTEND, full)}`);
        }
      }
    }
  }
  walk(FRONTEND);
});

test("5. build.py rejects non-HTTPS --api-base-url values cleanly", () => {
  assert.throws(() => {
    execFileSync("python3", ["build.py", "--api-base-url", "http://insecure.example.com"], {
      cwd: FRONTEND,
      stdio: "pipe",
    });
  }, /Invalid JONE_API_BASE_URL/);
});
