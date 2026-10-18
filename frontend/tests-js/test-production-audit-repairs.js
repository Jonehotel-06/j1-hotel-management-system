/* tests-js/test-production-audit-repairs.js */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const { makeContext, loadScript } = require("./harness");

const FRONTEND = path.resolve(__dirname, "..");
const CONFIRM = fs.readFileSync(path.join(FRONTEND, "booking-confirmation.html"), "utf8");
const MY_BOOKINGS = fs.readFileSync(path.join(FRONTEND, "my-bookings.html"), "utf8");
const DASHBOARD_JS = fs.readFileSync(path.join(FRONTEND, "js", "dashboard.js"), "utf8");
const DASH_BOOKINGS = fs.readFileSync(path.join(FRONTEND, "dashboard", "bookings.html"), "utf8");
const PROFILE = fs.readFileSync(path.join(FRONTEND, "dashboard", "profile.html"), "utf8");
const VERCEL = JSON.parse(fs.readFileSync(path.join(FRONTEND, "vercel.json"), "utf8"));

function bootApi(fetchImpl) {
  const ctx = makeContext({ hostname: "www.jonehotel.test", pathname: "/dashboard/bookings.html" });
  ctx.fetch = fetchImpl;
  ctx.window.APP_CONFIG = {
    API_BASE_URL: "",
    STORAGE: { AUTH: "jone.auth", SESSION: "jone.session", BOOKING: "jone.booking.draft" },
    API_ENDPOINTS: { bookings: "/api/admin/bookings/", payments: "/api/admin/payments/" },
  };
  loadScript(ctx, "js/api.js");
  return ctx;
}

function jsonResponse(status, body) {
  return Promise.resolve({
    status,
    ok: status >= 200 && status < 300,
    headers: { get: (k) => (String(k).toLowerCase() === "content-type" ? "application/json" : null) },
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  });
}

test("Failure A: booking-confirmation scopes tokens per booking reference and falls back to access_link_token", () => {
  assert.match(CONFIRM, /savedConfirmedMatchesRef\s*=\s*!!\([\s\S]*?savedConfirmed\.booking_reference\s*===\s*bookingRef/);
  assert.match(CONFIRM, /var\s+fallbackAccessLink\s*=\s*accessLink\s*\|\|\s*\(savedConfirmedMatchesRef\s*&&\s*savedConfirmed\.access_link_token\)\s*\|\|\s*""/);
  assert.match(CONFIRM, /window\.API\.exchangeBookingAccess\(ref,\s*linkToken\)/);
  assert.match(CONFIRM, /else if \(\(initErr && \(initErr\.status === 401 \|\| initErr\.status === 404\)\) && signedIn\(\)\)\s*\{\s*payRes = await window\.API\.initPayment\(booking\.booking_reference,\s*\{\s*auth:\s*true\s*\}\)/);
});

test("Failure A: my-bookings preserves active urlToken before exchanging access link", () => {
  assert.match(MY_BOOKINGS, /if\s*\(urlAccess\s*&&\s*!accessToken\)\s*await\s+claimEmailedLink\(\)/);
  assert.match(MY_BOOKINGS, /if\s*\(\(getErr\s*&&\s*\(getErr\.status\s*===\s*401\s*\|\|\s*getErr\.status\s*===\s*404\)\)\s*&&\s*urlAccess\s*&&\s*await\s+claimEmailedLink\(\)\)/);
});

test("Failure B: api.js friendlyMessage extracts nested guest validation errors instead of generic fallback", async () => {
  const ctx = bootApi(() =>
    jsonResponse(400, {
      success: false,
      code: "VALIDATION_ERROR",
      message: "Validation failed.",
      errors: {
        guest: {
          phone: ["Enter a valid phone number (7–20 digits)."],
        },
      },
    }),
  );

  await assert.rejects(
    () => ctx.window.API.post("/api/admin/bookings/", {}),
    (err) => {
      assert.equal(err.status, 400);
      assert.match(err.message, /Enter a valid phone number/i);
      return true;
    },
  );
});

test("Failure B: dashboard.js formModal maps nested guest.* errors to guest_* inputs and avoids duplicate toast", () => {
  assert.match(DASHBOARD_JS, /flatErrs\[k\s*\+\s*"_"\s*\+\s*subKey\]\s*=\s*val\[subKey\]/);
  assert.match(DASHBOARD_JS, /first_name:\s*"guest_first_name"/);
  assert.match(DASH_BOOKINGS, /first_name:\s*String\(v\.guest_first_name\s*\|\|\s*""\)\.trim\(\)/);
  assert.match(DASH_BOOKINGS, /phone:\s*String\(v\.guest_phone\s*\|\|\s*""\)\.trim\(\)/);
});

test("Failure C: vercel.json allows https: images in CSP and proxies /media/ to backend", () => {
  const rewrites = VERCEL.rewrites || [];
  assert.ok(
    rewrites.some((r) => r.source === "/media/(.*)" && /\/media\/\$1$/.test(r.destination)),
    "vercel.json must rewrite /media/(.*) to the backend media origin",
  );
  const cspHeader = (VERCEL.headers || [])
    .flatMap((h) => h.headers || [])
    .find((h) => h.key === "Content-Security-Policy");
  assert.ok(cspHeader, "Content-Security-Policy header must exist in vercel.json");
  assert.match(cspHeader.value, /img-src\s+'self'\s+data:\s+blob:\s+https:;/);
});

test("Failure C: profile.html falls back to /api/media/ when direct media URL fails and restores avatar fallback", () => {
  assert.match(PROFILE, /function\s+fallbackApiMediaUrl\(url\)/);
  assert.match(PROFILE, /\/api\/media\//);
  assert.match(PROFILE, /hide\("pf-avatar"\);\s*show\("pf-avatar-fb"\)/);
});
