/* tests-js/test-frontdesk-login-ui.js
   Front Desk email/password login — UI contracts for the refusal that names
   the missing enrolment step (static source assertions, same style as
   test-login-error-states.js):

   The blocking configuration trap: an administrator can fill the Settings
   "Front Desk desktop address" field (the Settings page invites it) while NO
   Front Desk workstation record exists. Every enrolment route needs that
   record, so no address and no key can authorize the desk — yet the old
   refusal just said "register this address", sending the administrator in a
   circle. The server now sends errors.no_active_front_desk_workstation and
   BOTH surfaces (login refusal, Settings page) must say which step is missing.

   Run from /frontend: node --test tests-js/test-frontdesk-login-ui.js */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { FRONTEND } = require("./harness");

const LOGIN = fs.readFileSync(path.join(FRONTEND, "login.html"), "utf8");
const SETTINGS = fs.readFileSync(path.join(FRONTEND, "dashboard", "settings.html"), "utf8");

test("the login refusal names a missing Front Desk workstation as the blocking step", () => {
  assert.match(LOGIN, /if \(meta\.no_active_front_desk_workstation\) \{/,
    "the refusal handles the server's no-workstation flag");
  assert.match(LOGIN, /No active Front Desk workstation is registered on the server yet, so no address can authorize this browser\./,
    "the message says plainly that no address can help yet");
  assert.match(LOGIN, /register the Front Desk workstation in Dashboard \\u2192 Terminals/,
    "and names the exact administrator action (Terminals page)");
});

test("the missing-workstation line is additive, not a replacement for the address advice", () => {
  const notice = LOGIN.slice(LOGIN.indexOf("function desktopRefusalNotice"), LOGIN.indexOf('window.Auth.bindLoginForm'));
  const branches = notice.match(/\} else if \(meta\./g) || [];
  assert.ok(branches.length >= 2, "the address-quality branches (unusable / mismatch) are still there");
  assert.match(notice, /if \(meta\.no_active_front_desk_workstation\) \{[\s\S]*?\}\s*if \(meta\.client_ip_is_infrastructure\) \{/,
    "the workstation line stands BEFORE and independently of the address-quality chain, so both can appear together");
});

test("the refusal still reports every address quality case it did before", () => {
  const notice = LOGIN.slice(LOGIN.indexOf("function desktopRefusalNotice"), LOGIN.indexOf('window.Auth.bindLoginForm'));
  assert.match(notice, /meta\.client_ip_is_infrastructure/);
  assert.match(notice, /meta\.configured_address_unusable/);
  assert.match(notice, /meta\.reason === "front_desk_ip_mismatch"/);
  assert.match(notice, /This browser reached the server from/);
  assert.match(notice, /6-digit code issued at the Reception Attendance Workstation/,
    "the numeric-code fallback is still offered");
});

test("the Settings page warns when no Front Desk workstation is registered", () => {
  assert.match(SETTINGS, /current\.front_desk_workstation_registered === false/,
    "the payload flag drives the warning");
  assert.match(SETTINGS, /No active Front Desk workstation is registered yet\./,
    "the warning names the missing step");
  assert.match(SETTINGS, /[Tt]he address below only authorizes sign-ins once a Front Desk workstation exists/,
    "and explains why the address field alone cannot work");
  assert.match(SETTINGS, /Dashboard \\u2192 Terminals/,
    "with the exact administrator action");
});

test("the shared sign-in flow is unchanged: email + password, one submission, independent retries", () => {
  assert.match(LOGIN, /<form id="login-form" novalidate>/);
  assert.match(LOGIN, /<input class="input" id="email" name="email" type="email" required autocomplete="email">/);
  assert.match(LOGIN, /<input class="input" id="password" name="password" type="password" required autocomplete="current-password"/);
  assert.match(LOGIN, /bindLoginForm\("#login-form", \{ onRestricted: desktopRefusalNotice \}\);/,
    "the shared binder (guard, reset, message mapping) still drives the form");
});

test("no fingerprint, attendance code, or clock-in is required to submit the password form", () => {
  const form = LOGIN.slice(LOGIN.indexOf('<form id="login-form"'), LOGIN.indexOf("</form>") + 7);
  assert.ok(form.length > 200, "the password form was found");
  for (const forbidden of ["fingerprint", "attendance", "clock", "access_code", "temporary_token"]) {
    assert.ok(!form.includes(forbidden), `the password form must not require "${forbidden}"`);
  }
  // The 6-digit code stays a SEPARATE, opt-in tab.
  assert.match(LOGIN, /id="access-code-login-form" novalidate style="display:none;"/);
});
