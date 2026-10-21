/* tests-js/test-booking-access-links.js */
/* The reported failure: POST /api/payments/initialize/ answered 404 for a real,
   payable booking because the browser held no guest credential for it.

   Two things must stay true after this work:
     1. the emailed link can always be turned into a working credential — on any
        device — through POST /api/bookings/<ref>/access/;
     2. the page never sends a payment request it knows can only 404, and never
        leaves the guest without a way to ask for the link.
   Run: node --test tests-js/   (from frontend/) */
"use strict";

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const { makeContext, loadScript } = require("./harness");

const FRONTEND = path.resolve(__dirname, "..");
const CONFIRM = fs.readFileSync(path.join(FRONTEND, "booking-confirmation.html"), "utf8");
const MY_BOOKINGS = fs.readFileSync(path.join(FRONTEND, "my-bookings.html"), "utf8");

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

test("exchangeBookingAccess posts the signed value to the booking access route", async () => {
  const calls = [];
  const ctx = bootApi((url, opts) => {
    calls.push({ url, opts });
    return jsonResponse(200, { success: true, data: { guest_access_token: "tok-1" } });
  });
  const res = await ctx.window.API.exchangeBookingAccess("J1-20261017-ABCDEF12", "signed value");
  assert.equal(res.data.guest_access_token, "tok-1");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "/api/bookings/J1-20261017-ABCDEF12/access/");
  assert.equal(calls[0].opts.method, "POST");
  assert.equal(JSON.parse(calls[0].opts.body).access, "signed value");
});

test("the exchange never sends a stored login: the link IS the credential", async () => {
  const ctx = bootApi(() => jsonResponse(200, { success: true, data: {} }));
  ctx.window.sessionStorage.setItem("jone.session", JSON.stringify({ access: "jwt-123" }));
  let sent = null;
  ctx.fetch = (url, opts) => { sent = opts; return jsonResponse(200, { success: true, data: {} }); };
  await ctx.window.API.exchangeBookingAccess("J1-X", "sig");
  assert.equal(sent.headers.Authorization, undefined);
});

test("resendBookingAccessLink posts to the resend route with no body payload", async () => {
  const calls = [];
  const ctx = bootApi((url, opts) => {
    calls.push({ url, opts });
    return jsonResponse(200, { success: true, data: { sent: true } });
  });
  const res = await ctx.window.API.resendBookingAccessLink("J1-20261017-ABCDEF12");
  assert.equal(res.data.sent, true);
  assert.equal(calls[0].url, "/api/bookings/J1-20261017-ABCDEF12/access-link/");
  assert.equal(calls[0].opts.method, "POST");
});

test("the confirmation page exchanges an emailed ?access= value before loading", () => {
  assert.match(CONFIRM, /var accessLink = p\.get\("access"\)/);
  assert.match(CONFIRM, /exchangeBookingAccess\(bookingRef, accessLink\)/);
  // The signed value must not linger in the address bar / history.
  assert.match(CONFIRM, /clean\.delete\("access"\)/);
  assert.match(CONFIRM, /history\.replaceState/);
});

test("a device with its own token for this booking keeps it (no needless rotation)", () => {
  assert.match(CONFIRM, /function bookingScopedToken\(\)/);
  assert.match(CONFIRM, /accessLink && bookingRef && !bookingScopedToken\(\)/);
});

test("the credential is resolved at call time, not only at page load", () => {
  assert.match(CONFIRM, /function currentGuestToken\(\)/);
  assert.match(CONFIRM, /function guestOpts\(\) \{ return window\.API\.guestAccessOpts\(currentGuestToken\(\)\); \}/);
});

test("the page refuses to send a request that can only come back 404", () => {
  assert.match(CONFIRM, /MISSING_BOOKING_CREDENTIAL/);
  assert.match(CONFIRM, /if \(!currentGuestToken\(\) && !signedIn\(\)\)/);
});

test("the failure screen offers a fresh emailed link instead of a dead end", () => {
  assert.match(CONFIRM, /id="resend-access-link"/);
  assert.match(CONFIRM, /function offerAccessLink\(ref\)/);
  assert.match(CONFIRM, /offerAccessLink\(bookingRef\)/);
  // and it says the same thing whether or not the booking exists
  assert.match(CONFIRM, /If that booking exists, a secure link is on its way/);
});

test("my-bookings claims the emailed access value — it is the link target", () => {
  assert.match(MY_BOOKINGS, /urlParams\.get\("access"\)/);
  assert.match(MY_BOOKINGS, /exchangeBookingAccess\(urlRef, urlAccess\)/);
  // The token is remembered per booking as well as globally.
  assert.match(MY_BOOKINGS, /jone\.guest_access_token\.\" \+ ref/);
});

test("my-bookings no longer spins forever when it has a reference but no credential", () => {
  assert.match(MY_BOOKINGS, /if \(accessRef && accessToken\) \{ load\(\); return; \}/);
  assert.match(MY_BOOKINGS, /data-resend-access/);
});
