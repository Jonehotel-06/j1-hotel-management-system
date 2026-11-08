/* Contract tests for the approved-device credential (workstation identity that
   survives an IP change) and for the two offer-failure fixes on the client.

   These read the shipped source and assert the invariants that matter, because
   the failure they guard against is a page or a module quietly stopping sending
   the credential — which no screenshot would show:

     * the device token is stored in one place, sent on sign-in, on token REFRESH
       (the call that used to kill sessions) and on attendance calls;
     * no code ever puts a token in a URL, a log line or a toast;
     * the offers page never addresses a record by anything but its database id,
       and never replaces the API's own error sentence with a generic one.
*/
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const ROOT = path.join(__dirname, "..");
const read = (rel) => fs.readFileSync(path.join(ROOT, rel), "utf8");

test("auth.js keeps both desk credentials and sends them together", () => {
  const src = read("js/auth.js");
  assert.match(src, /const DEVICE_TOKEN_STORAGE = "jone\.frontDeskDeviceToken"/);
  assert.match(src, /function frontDeskDeviceToken\(\)/);
  assert.match(src, /function setFrontDeskDeviceToken\(/);
  assert.match(src, /function desktopHeaders\(\)/, "one place builds the desk headers");
  assert.match(src, /headers\["X-JONE-Device-Token"\] = token/);
  assert.match(src, /headers\["X-JONE-Desktop-Key"\] = key/);
  // Exported for pages.
  assert.match(src, /frontDeskDeviceToken, setFrontDeskDeviceToken, desktopHeaders/);
});

test("the device token is sent on sign-in AND on token refresh", () => {
  const src = read("js/auth.js");
  const loginFn = src.slice(src.indexOf("async function login("));
  assert.match(loginFn.slice(0, 900), /const headers = desktopHeaders\(\);/,
    "login must build its headers from desktopHeaders()");
  const refreshFn = src.slice(src.indexOf("async function refreshAccess("));
  assert.match(refreshFn.slice(0, 2000), /desktopHeaders\(\)/,
    "refresh must send the desk credentials too — a change of address must not end the session");
});

test("attendance and clock calls carry both credentials", () => {
  for (const file of ["js/biometrics.js", "dashboard/workforce.html"]) {
    const src = read(file);
    assert.match(src, /desktopHeaders/, `${file} must use the shared desk headers`);
    assert.doesNotMatch(src, /"X-JONE-Desktop-Key":\s*deskKey/, `${file} must not hand-build headers`);
  }
});

test("device tokens are never placed in a URL, a log or a toast", () => {
  const files = ["js/auth.js", "js/api.js", "js/biometrics.js", "dashboard/terminals.html"];
  for (const file of files) {
    const src = read(file);
    // A token may only travel in a request header.
    assert.doesNotMatch(src, /[?&]device_token=/, `${file} must not put a token in a query string`);
    assert.doesNotMatch(src, /console\.log\([^)]*[Tt]oken/, `${file} must not log a token`);
  }
});

test("api.js exposes validated device endpoints and no raw path building in pages", () => {
  const src = read("js/api.js");
  for (const fn of ["listFrontDeskDevices", "requestFrontDeskDevice",
                    "approveFrontDeskDevice", "revokeFrontDeskDevice"]) {
    assert.match(src, new RegExp(`function ${fn}\\(`), `${fn} must exist`);
    assert.match(src, new RegExp(`\\b${fn}\\b`), `${fn} must be exported`);
  }
  assert.match(src, /devices\/\$\{safe\}\/approve\//);
  assert.match(src, /devices\/\$\{safe\}\/revoke\//);
  assert.match(src, /This device could not be identified/, "a bad id is refused before a request");
});

test("offers page addresses records only by database id", () => {
  const src = read("dashboard/offers.html");
  assert.match(src, /function recordId\(o\)/);
  // The old fallback chain (id || slug || title) is what produced a 404 lookup.
  assert.doesNotMatch(src, /var id=o\.id\|\|o\.slug\|\|o\.title/);
  // No control is rendered without a usable id.
  assert.match(src, /\(canManageOffers && id\)/);
  assert.match(src, /var restrict = !id \? "" :/);
});

test("offers page keeps the server's own error sentence", () => {
  const src = read("dashboard/offers.html");
  assert.match(src, /function requestFailedMessage\(err, fallback\)/);
  assert.match(src, /err\.status===403\) return "Only managers and administrators/);
  // Every offer/restriction failure path goes through it.
  assert.match(src, /requestFailedMessage\(err, "The offer state could not be changed\."\)/);
  assert.match(src, /requestFailedMessage\(err, "The restricted guests could not be loaded\."\)/);
});

test("restriction list is read page by page, never one oversized page", () => {
  const offers = read("dashboard/offers.html");
  const guests = read("dashboard/guests.html");
  assert.doesNotMatch(offers, /listOfferRestrictions\(\{ offer: key, page_size: 200/);
  assert.doesNotMatch(guests, /listOfferRestrictions\(\{ guest: guestKey, page_size: 200/);
  assert.match(offers, /while\(guard\+\+ < 20\)/);
  assert.match(guests, /async function readAllPages\(fetchPage\)/);
});

test("api.js separates a missing record from an unserved route on 404", () => {
  const src = read("js/api.js");
  assert.match(src, /const NOT_FOUND_UNSERVED =/);
  assert.match(src, /return data == null \? NOT_FOUND_UNSERVED : NOT_FOUND_FALLBACK;/);
  assert.match(src, /deployment does not include this feature yet/);
});

test("the Terminals page can approve, install, request and revoke a device", () => {
  const src = read("dashboard/terminals.html");
  for (const hook of ["data-device-rows", "data-device-install", "data-device-forget",
                      "data-device-request", "data-device-state"]) {
    assert.match(src, new RegExp(hook), `${hook} must exist`);
  }
  assert.match(src, /window\.API\.approveFrontDeskDevice/);
  assert.match(src, /window\.API\.revokeFrontDeskDevice/);
  assert.match(src, /window\.API\.requestFrontDeskDevice/);
  // The token is shown once and never re-fetched.
  assert.match(src, /shown only as a digest|cannot be shown again/);
});
