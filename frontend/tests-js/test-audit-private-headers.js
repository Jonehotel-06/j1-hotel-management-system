"use strict";
// Cloudflare Pages redirects these HTML paths to extensionless documents. Both
// responses must be protected; this tests configuration, not a deployed CDN.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { FRONTEND } = require("./harness");

function blocks() {
  const lines = fs.readFileSync(path.join(FRONTEND, "_headers"), "utf8").split(/\r?\n/);
  const out = new Map();
  let current;
  for (const line of lines) {
    if (/^\//.test(line)) { current = line.trim(); out.set(current, new Map()); }
    else if (/^\s+[^:]+:/.test(line) && current) {
      const [name, ...value] = line.trim().split(":");
      out.get(current).set(name.toLowerCase(), value.join(":").trim().toLowerCase());
    }
  }
  return out;
}

test("private redirects AND extensionless final documents carry privacy headers", () => {
  const routes = blocks();
  for (const name of ["qr-service", "payment-verify", "booking-review", "my-bookings", "booking-confirmation", "cancellation-result", "reset-password"]) {
    for (const suffix of ["", ".html"]) {
      const route = `/${name}${suffix}`;
      assert.ok(routes.has(route), `missing ${route}`);
      assert.equal(routes.get(route).get("cache-control"), "no-store", route);
      assert.equal(routes.get(route).get("x-robots-tag"), "noindex, nofollow", route);
      assert.equal(routes.get(route).get("referrer-policy"), "no-referrer", route);
    }
  }
  // Existing generic and portal security controls are not removed or weakened.
  assert.equal(routes.get("/*").get("x-frame-options"), "deny");
  assert.equal(routes.get("/portal/*").get("referrer-policy"), "no-referrer");
});
