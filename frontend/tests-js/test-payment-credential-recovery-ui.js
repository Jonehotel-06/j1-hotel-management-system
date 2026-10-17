/* tests-js/test-payment-credential-recovery-ui.js */
/* The reported failure (screenshot): on the phone that had just booked, paying
   showed "This device is no longer recognised for your booking" with no way
   forward except an email.

   Three things must hold, and each is a distinct way that sentence appeared:
     1. a guest request never carries an account session, so a stale dashboard
        JWT cannot 401 the payment;
     2. a browser that created the booking keeps the creation key and uses it to
        rebuild its credential — no email, no new reservation; and
     3. if that still fails, the page says what is actually true and offers the
        emailed link as the primary action (Retry cannot help).
   Run: node --test tests-js/   (from frontend/) */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const { makeContext, loadScript } = require("./harness");

const FRONTEND = path.resolve(__dirname, "..");
const CONFIRM = fs.readFileSync(path.join(FRONTEND, "booking-confirmation.html"), "utf8");
const API = fs.readFileSync(path.join(FRONTEND, "js", "api.js"), "utf8");

function bootApi(fetchImpl) {
  const ctx = makeContext({ hostname: "www.jonehotel.test", pathname: "/booking-confirmation.html" });
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

/* ------------------------------------------------------------------- api.js */

test("a guest request never carries an account session", () => {
  const ctx = bootApi(() => jsonResponse(200, {}));
  assert.deepEqual(JSON.parse(JSON.stringify(ctx.window.API.guestAccessOpts(""))), { auth: false },
    "with no guest token the call must still refuse to attach a dashboard JWT");
  const withToken = ctx.window.API.guestAccessOpts("tok-123");
  assert.equal(withToken.auth, false);
  assert.equal(withToken.headers["X-Guest-Access-Token"], "tok-123");
});

test("the reclaim call sends the creation key and stays anonymous", async () => {
  const seen = [];
  const ctx = bootApi((url, opts) => {
    seen.push({ url, opts });
    return jsonResponse(200, { success: true, data: { guest_access_token: "fresh" } });
  });
  const res = await ctx.window.API.reclaimBookingAccess("J1-REF", "key-abc");
  assert.equal(res.data.guest_access_token, "fresh");
  assert.equal(seen.length, 1);
  assert.match(seen[0].url, /\/api\/bookings\/J1-REF\/access\/$/);
  assert.deepEqual(JSON.parse(seen[0].opts.body), { idempotency_key: "key-abc" });
  assert.equal(seen[0].opts.headers.Authorization, undefined,
    "recovery must not depend on (or be broken by) an account session");
});

/* ------------------------------------------------------------------- page */

test("the creation key is kept per booking, not thrown away on success", () => {
  assert.match(CONFIRM, /function rememberBookingIdemKey\(bookingReference\)/);
  assert.match(CONFIRM, /"jone\.booking\.idem\." \+ bookingReference/);
  assert.match(CONFIRM, /rememberBookingIdemKey\(booking\.booking_reference\);/,
    "it is stored against the booking the moment creation is confirmed");
  assert.match(CONFIRM, /function bookingIdemKeyFor\(bookingReference\)/);
});

test("paying reclaims the credential before declaring the booking unusable", () => {
  const start = CONFIRM.indexOf("async function reclaimBookingCredential()");
  const end = CONFIRM.indexOf("async function redirectToPayment()");
  assert.ok(start > 0 && end > start, "the reclaim helper is missing");
  const helper = CONFIRM.slice(start, end);
  assert.match(helper, /reclaimBookingAccess\(ref, key\)/);
  assert.match(helper, /saveGuestToken\(token, ref, ""\)/);

  const redirect = CONFIRM.slice(end, CONFIRM.indexOf("async function pay()"));
  assert.match(redirect, /await reclaimBookingCredential\(\);/);
  assert.ok(redirect.indexOf("reclaimBookingCredential") < redirect.indexOf("MISSING_BOOKING_CREDENTIAL"),
    "recovery is attempted before the failure is raised");
  assert.match(redirect, /initErr\.status === 401 \|\| initErr\.status === 404/,
    "a token rejected by the server is also retried once after recovery");
});

test("a rejected token is reclaimed once and the payment retried", () => {
  const redirect = CONFIRM.slice(
    CONFIRM.indexOf("async function redirectToPayment()"),
    CONFIRM.indexOf("async function pay()"),
  );
  assert.match(redirect, /await reclaimBookingCredential\(\)\) \{\s*payRes = await window\.API\.initPayment/);
});

test("the final message tells the truth and promotes the emailed link", () => {
  const start = CONFIRM.indexOf("} else if (err && (err.status === 401");
  const end = CONFIRM.indexOf('} else if (err && err.code === "PAYMENT_NOT_CONFIGURED")');
  assert.ok(start > 0 && end > start);
  const branch = CONFIRM.slice(start, end);

  assert.match(branch, /MISSING_BOOKING_CREDENTIAL/);
  assert.match(branch, /no longer holds the key for this booking/);
  assert.match(branch, /reservation is still saved/);
  assert.match(branch, /request a secure link below/i);
  assert.match(branch, /retry-pay/);
  assert.match(branch, /style\.display = "none"/, "Retry cannot succeed and is removed");
  assert.match(branch, /resend-access-link/);
  assert.match(branch, /btn btn-accent/, "the link is what the guest should press");
  assert.doesNotMatch(branch, /This device is no longer recognised/);
});
