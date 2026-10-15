/* Dashboard-only PWA installability contracts. */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { FRONTEND } = require("./harness");

function read(rel) {
  return fs.readFileSync(path.join(FRONTEND, rel), "utf8");
}

function htmlFiles(relDir) {
  return fs.readdirSync(path.join(FRONTEND, relDir), { withFileTypes: true })
    .filter((entry) => entry.isFile() && entry.name.endsWith(".html"))
    .map((entry) => path.join(relDir, entry.name))
    .sort();
}

test("the only web app manifest is scoped to the staff dashboard", () => {
  assert.equal(fs.existsSync(path.join(FRONTEND, "manifest.webmanifest")), false,
    "the old public root manifest must not remain installable/discoverable");

  const manifest = JSON.parse(read("dashboard/manifest.webmanifest"));
  assert.equal(manifest.name, "J-ONE Staff Dashboard");
  assert.equal(manifest.short_name, "J-ONE Staff");
  assert.equal(manifest.id, "/dashboard/");
  assert.equal(manifest.start_url, "/dashboard/index.html");
  assert.equal(manifest.scope, "/dashboard/");
  assert.equal(manifest.display, "standalone");
  assert.equal(manifest.shortcuts, undefined, "guest booking shortcuts do not belong in the staff app");

  const sizes = new Set(manifest.icons.map((icon) => icon.sizes));
  assert.ok(sizes.has("192x192"));
  assert.ok(sizes.has("512x512"));
  assert.ok(manifest.icons.some((icon) => icon.purpose === "maskable"));
  for (const icon of manifest.icons) {
    assert.ok(icon.src.startsWith("/favicon/"), icon.src);
    assert.ok(fs.existsSync(path.join(FRONTEND, icon.src)), `missing PWA icon ${icon.src}`);
  }
});

test("only dashboard pages link the dashboard manifest and standalone metadata", () => {
  const publicPages = htmlFiles(".");
  const portalPages = htmlFiles("portal");
  const dashboardPages = htmlFiles("dashboard");
  assert.ok(publicPages.length > 20);
  assert.ok(dashboardPages.length > 40);

  for (const rel of [...publicPages, ...portalPages]) {
    const html = read(rel);
    assert.doesNotMatch(html, /<link rel="manifest"/, `${rel} must not be installable`);
    assert.doesNotMatch(html, /apple-mobile-web-app-capable|mobile-web-app-capable|apple-mobile-web-app-title/,
      `${rel} must not advertise standalone web-app mode`);
    assert.doesNotMatch(html, /data-pwa-install/, `${rel} must not ship an install control`);
  }

  for (const rel of dashboardPages) {
    const html = read(rel);
    const links = html.match(/<link rel="manifest" href="\/dashboard\/manifest\.webmanifest">/g) || [];
    assert.equal(links.length, 1, `${rel} must link the dashboard manifest exactly once`);
    assert.match(html, /<meta name="apple-mobile-web-app-title" content="J-ONE Staff">/);
    assert.match(html, /js\/pwa\.js/, `${rel} must load the PWA layer`);
  }
});

test("pwa.js offers installation and registers the worker only under /dashboard/", () => {
  const pwa = read("js/pwa.js");
  assert.match(pwa, /var SW_SCOPE = "\/dashboard\/";/);
  assert.match(pwa, /var IS_DASHBOARD = location\.pathname === "\/dashboard"/);
  assert.match(pwa, /var INSTALL_ENABLED = IS_DASHBOARD;/);
  assert.match(pwa, /navigator\.serviceWorker\.register\(SW_URL, \{ scope: SW_SCOPE \}\)/);
  assert.match(pwa, /getRegistration\("\/"\)/);
  assert.match(pwa, /reg\.scope === rootScope/);
  assert.match(pwa, /return reg\.unregister\(\);/);
  assert.match(pwa, /if \(!INSTALL_ENABLED\) return;/);
  assert.match(pwa, /document\.querySelector\("\.dash-sidebar-footer"\)/);
  assert.match(pwa, /Install J-ONE Staff App/);
  assert.match(pwa, /onDashboardHome/);
  assert.doesNotMatch(pwa, /Install J-ONE Hotel App/);
  assert.doesNotMatch(pwa, /Faster access to rooms, offers and your stays/);
  assert.doesNotMatch(pwa, /mobile-drawer \.mobile-footer-row/);
  assert.doesNotMatch(pwa, /site-footer \.footer-bottom/);
});

test("build, service worker, and static hosting preserve the dashboard-only split", () => {
  const build = read("build.py");
  assert.match(build, /DASHBOARD_PWA_HEAD/);
  assert.match(build, /\/dashboard\/manifest\.webmanifest/);
  assert.match(build, /def _strip_pwa_head/);
  assert.match(build, /is_dashboard=path\.startswith\("dashboard\/"\)/);
  assert.doesNotMatch(build, /href="\/manifest\.webmanifest"/);

  const worker = read("sw.js");
  assert.match(worker, /"\/dashboard\/manifest\.webmanifest"/);
  assert.doesNotMatch(worker, /"\/manifest\.webmanifest"/);

  const host = JSON.parse(read("vercel.json"));
  const swRule = host.headers.find((entry) => entry.source === "/sw.js");
  assert.ok(swRule.headers.some((header) => header.key === "Service-Worker-Allowed" && header.value === "/dashboard/"));
  assert.ok(host.headers.some((entry) => entry.source === "/dashboard/manifest.webmanifest"));
  assert.equal(host.headers.some((entry) => entry.source === "/manifest.webmanifest"), false);
});
