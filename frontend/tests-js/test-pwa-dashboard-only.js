/* Two installable applications, and the pages that must never install.

   CONTRACT (changed by critical_fixes_2026_11_08 — see the delivery report):
   * the PUBLIC SITE (scope "/") installs as "J-ONE Hotel Management System";
     iOS uses the apple-* meta tags + apple-touch-icon, Android uses
     /manifest.webmanifest. Public pages still register NO service worker and
     show NO install popup — only the one quiet, dismissible footer install row.
   * the STAFF DASHBOARD (scope "/dashboard/") installs as "J-ONE Staff" and is
     the only area that registers a service worker.
   * the guest portal, the bearer-link QR form and the password-reset page are
     never installable.
*/
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

// Pages that handle one-use credentials and must never become an installed app.
const NON_INSTALLABLE = new Set(["portal/login.html", "portal/index.html", "qr-service.html",
                                 "reset-password.html"]);

test("the public manifest declares the J-ONE Hotel Management System", () => {
  const manifest = JSON.parse(read("manifest.webmanifest"));
  assert.equal(manifest.name, "J-ONE Hotel Management System");
  assert.equal(manifest.short_name, "J-ONE");
  assert.equal(manifest.id, "/");
  assert.equal(manifest.start_url, "/index.html");
  assert.equal(manifest.scope, "/");
  assert.equal(manifest.display, "standalone");
  assert.equal(manifest.theme_color, "#373435");
  assert.equal(manifest.background_color, "#F8F9FA");

  const sizes = new Set(manifest.icons.map((icon) => icon.sizes));
  assert.ok(sizes.has("192x192"), "Android requires a 192px icon");
  assert.ok(sizes.has("512x512"), "Android requires a 512px icon");
  assert.ok(manifest.icons.some((icon) => icon.purpose === "maskable"));
  assert.ok(manifest.icons.some((icon) => icon.sizes === "180x180"),
    "an explicit 180x180 touch icon must be declared");
  for (const icon of manifest.icons) {
    assert.ok(fs.existsSync(path.join(FRONTEND, icon.src)), `missing PWA icon ${icon.src}`);
  }
});

test("the dashboard manifest stays scoped to the staff console", () => {
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
    assert.ok(fs.existsSync(path.join(FRONTEND, icon.src)), `missing PWA icon ${icon.src}`);
  }
});

test("every installable page carries the manifest, iOS metadata and a touch icon", () => {
  const publicPages = htmlFiles(".").filter((rel) => !rel.includes("/"));
  const dashboardPages = htmlFiles("dashboard");
  assert.ok(publicPages.length > 20);
  assert.ok(dashboardPages.length > 40);

  for (const rel of [...publicPages, ...dashboardPages]) {
    if (NON_INSTALLABLE.has(rel)) continue;
    const html = read(rel);
    const isDashboard = rel.startsWith("dashboard/");
    const manifestHref = isDashboard ? "/dashboard/manifest.webmanifest" : "/manifest.webmanifest";
    const links = html.match(/<link rel="manifest"[^>]*>/g) || [];
    assert.equal(links.length, 1, `${rel} must link its manifest exactly once`);
    assert.ok(links[0].includes(manifestHref), `${rel} must link ${manifestHref}`);

    // iOS ignores the manifest when adding to the home screen: these are the
    // tags that decide whether it installs a standalone app or a bookmark.
    assert.match(html, /<meta name="apple-mobile-web-app-capable" content="yes">/,
      `${rel} must declare apple-mobile-web-app-capable`);
    assert.match(html, /<meta name="apple-mobile-web-app-status-bar-style" content="[^"]+">/,
      `${rel} must declare an iOS status bar style`);
    assert.match(html, /<meta name="apple-mobile-web-app-title" content="[^"]+">/,
      `${rel} must declare an iOS home-screen title`);
    const icons = html.match(/<link rel="apple-touch-icon"[^>]*>/g) || [];
    assert.equal(icons.length, 1, `${rel} must declare exactly one apple-touch-icon`);
    assert.ok(icons[0].includes('sizes="180x180"'),
      `${rel} must serve the 180x180 iOS touch icon`);
    assert.ok(fs.existsSync(path.join(FRONTEND, "favicon", "apple-touch-icon-180.png")));
  }
});

test("portal, QR and reset pages are never installable", () => {
  /* iOS lets a user add ANY page to the home screen, so "not installable" here
     means precisely: no web app manifest, no standalone display mode, and no
     install control. A plain bookmark icon tag is harmless (it only changes the
     bookmark's artwork) and stays on these pages untouched. */
  for (const rel of NON_INSTALLABLE) {
    const html = read(rel);
    assert.doesNotMatch(html, /<link rel="manifest"/, `${rel} must not be installable`);
    assert.doesNotMatch(html, /apple-mobile-web-app-capable|mobile-web-app-capable/,
      `${rel} must not advertise standalone web-app mode`);
    assert.doesNotMatch(html, /data-pwa-install/, `${rel} must not ship an install control`);
    assert.doesNotMatch(html, /js\/pwa\.js/, `${rel} must not load the install layer`);
  }
});

test("the public site shows one quiet install row and no popup", () => {
  const pwa = read("js/pwa.js");
  // iOS never fires beforeinstallprompt: the control must be offered anyway.
  assert.match(pwa, /return isIOSSafari\(\);/);
  // The public install affordance is a dismissible footer row, never a banner
  // or a modal on load.
  assert.match(pwa, /function showFooterBanner/);
  assert.match(pwa, /pwa-install-row/);
  assert.match(pwa, /markDismissed\(\)/);
  // The 6-second auto-banner stays dashboard-only.
  assert.match(pwa, /onDashboardHome/);
  assert.match(pwa, /if \(!IS_DASHBOARD\) showFooterBanner\(\);/);
});

test("pwa.js registers the worker only under /dashboard/", () => {
  const pwa = read("js/pwa.js");
  assert.match(pwa, /var SW_SCOPE = "\/dashboard\/";/);
  assert.match(pwa, /var IS_DASHBOARD = location\.pathname === "\/dashboard"/);
  assert.match(pwa, /var INSTALL_ENABLED = !IS_PASS_THROUGH_PAGE;/);
  assert.match(pwa, /navigator\.serviceWorker\.register\(SW_URL, \{ scope: SW_SCOPE \}\)/);
  // Public pages retire the legacy root registration and register nothing.
  assert.match(pwa, /getRegistration\("\/"\)/);
  assert.match(pwa, /reg\.scope === rootScope/);
  assert.match(pwa, /return reg\.unregister\(\);/);
  assert.match(pwa, /if \(!IS_DASHBOARD\) \{/);
  assert.match(pwa, /Install J-ONE Staff App/);
});

test("build, service worker and static hosting agree on both scopes", () => {
  const build = read("build.py");
  assert.match(build, /PUBLIC_PWA_HEAD/);
  assert.match(build, /DASHBOARD_PWA_HEAD/);
  assert.match(build, /\/dashboard\/manifest\.webmanifest/);
  assert.match(build, /href="\/manifest\.webmanifest"/);
  assert.match(build, /def _strip_pwa_head/);
  assert.match(build, /def _is_installable_page/);
  // Dashboard pages are the installable staff app and must be re-equipped on
  // every build (they were previously stripped and never re-added).
  assert.match(build, /is_dashboard=path\.startswith\("dashboard\/"\)/);
  assert.match(build, /page_path=path/);

  const worker = read("sw.js");
  assert.match(worker, /"\/dashboard\/manifest\.webmanifest"/);
  // The public manifest is a static file the worker leaves alone; it must not
  // be precached into the dashboard-scoped cache.
  assert.doesNotMatch(worker, /"\/manifest\.webmanifest"/);

  const host = JSON.parse(read("vercel.json"));
  const swRule = host.headers.find((entry) => entry.source === "/sw.js");
  assert.ok(swRule.headers.some((header) => header.key === "Service-Worker-Allowed" && header.value === "/dashboard/"));
  assert.ok(host.headers.some((entry) => entry.source === "/dashboard/manifest.webmanifest"));
  const publicRule = host.headers.find((entry) => entry.source === "/manifest.webmanifest");
  assert.ok(publicRule, "the public manifest needs its own MIME/cache rule");
  assert.ok(publicRule.headers.some((header) => header.key === "Content-Type"
    && header.value.startsWith("application/manifest+json")));

  const headersFile = read("_headers");
  assert.match(headersFile, /\/manifest\.webmanifest\n  Content-Type: application\/manifest\+json/);
});
