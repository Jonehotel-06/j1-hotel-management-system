/* The Administrator account is the hotel owner (the CEO), not an employee.

   Reported: the owner's own profile showed staff self-service records — "my
   salary, attendance, shift, leave etc." There is no employee record for the
   owner, so the page must not offer one: the staff record is never fetched,
   the staff panel is not shown for the owner, and what the owner sees is his
   own account plus the screens he runs, each resolved through the access
   registry. The staff roster must not list the owner as staff either — while
   keeping the account itself reachable.

   Run from /frontend: node --test tests-js/test-owner-ceo-profile-ui.js */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const FRONTEND = path.resolve(__dirname, "..");
const PROFILE = fs.readFileSync(path.join(FRONTEND, "dashboard", "profile.html"), "utf8");
const STAFF = fs.readFileSync(path.join(FRONTEND, "dashboard", "staff.html"), "utf8");

function slice(source, startMarker, endMarker) {
  const start = source.indexOf(startMarker);
  const end = source.indexOf(endMarker, start + 1);
  assert.ok(start > 0 && end > start, `${startMarker} not found in the shipped page`);
  return source.slice(start, end);
}

const OWNER = slice(PROFILE, "var OWNER_TILES = [", "/* ---- Staff self-service records");
const BOOT = slice(PROFILE, "/* The owner account has no staff record", "load();");

test("the owner is recognised from the signed-in session", () => {
  assert.match(PROFILE, /var isOwner = !!\(window\.Auth && window\.Auth\.hasRole && window\.Auth\.hasRole\("admin"\)\);/);
  assert.match(PROFILE, /Administrator \\u00B7 owner \(CEO\)/);
});

test("the owner never asks for, or is shown, a staff self-service record", () => {
  assert.match(PROFILE, /if \(!isOwner\) loadSelfService\(\);/);
  assert.match(BOOT, /var staffPanel = document\.querySelector\("\[data-my-profile\]"\)/);
  assert.match(BOOT, /if \(staffPanel\) staffPanel\.hidden = isOwner;/);
  /* The staff panel still exists in the page: an employee's own record is
     exactly what a staff account must keep seeing. */
  assert.match(PROFILE, /<section class="panel" data-my-profile>/);
  assert.match(PROFILE, /<section class="panel" id="pf-owner" hidden>/);
  assert.match(BOOT, /if \(ownerPanel\) ownerPanel\.hidden = false;/);
});

test("the owner console offers the screens the owner runs, through the registry", () => {
  assert.match(OWNER, /href: "staff\.html"/);
  assert.match(OWNER, /href: "settings\.html"/);
  assert.match(OWNER, /href: "accounting\.html"/);
  assert.match(OWNER, /href: "reports\.html"/);
  assert.match(OWNER, /JONE\.dashboard\.canOpenPage\(t\.href\)/,
    "a tile is only offered to an account that may open the page");
});

test("the owner console invents no employment figure", () => {
  for (const field of ["Basic salary", "Next scheduled shift", "Upcoming approved leave",
                       "Latest payslip", "Attendance this month", "Employee ID", "Pending leave requests"]) {
    assert.equal(OWNER.includes(field), false, `the owner console must not show ${field}`);
  }
  assert.match(OWNER, /\["Sign-in address", me\.email\]/);
  assert.match(OWNER, /\["Email verified", me\.email_verified \? "Yes" : "No"\]/);
  assert.match(OWNER, /\["Last sign-in", me\.last_login \? JONE\.formatDateTime\(me\.last_login\) : "No previous sign-in recorded"\]/);
  assert.match(OWNER, /Salaries, attendance, shifts and leave belong to employee records/);
});

test("the page is an owner profile for the owner and a staff profile for staff", () => {
  assert.match(BOOT, /ownerTitle\.textContent = "Owner profile";/);
  assert.match(BOOT, /document\.title = "Owner profile \| J-ONE Staff";/);
  assert.match(PROFILE, /<h2 id="pf-title">My profile<\/h2>/,
    "staff keep the ordinary wording of the same page");
});

test("the staff roster never lists the owner as staff", () => {
  const params = STAFF.match(/role__in: "([^"]+)"/);
  assert.ok(params, "the roster query lists roles");
  assert.equal(params[1].split(",").includes("ADMIN"), false,
    "the owner is not a roster row");
  assert.doesNotMatch(STAFF, /<option value="ADMIN">/, "and cannot be filtered for either");
  assert.match(STAFF, /The Administrator is the hotel owner, not a staff member/);
  /* The account still exists, so it stays reachable through one labelled
     control rather than by being listed as an employee. */
  assert.match(STAFF, /<button class="btn btn-sm btn-outline" type="button" data-owner-account hidden>/);
  assert.match(STAFF, /API\.list\("users", \{ role: "ADMIN", page_size: 5 \}\)/);
  assert.match(STAFF, /btn\.setAttribute\("data-edit", String\(owner\.id\)\)/);
  assert.match(STAFF, /Owner account: ' \+ JONE\.esc\(owner\.full_name \|\| owner\.email\)/);
});

/* ---- The console as it actually renders (the page's own code) ----------- */

const vm = require("node:vm");
const { makeContext, disposeAll } = require("./harness");

test.after(() => disposeAll());

function loadOwnerConsole(allowed) {
  const ctx = makeContext();
  const body = { innerHTML: "", addEventListener() {} };
  ctx.JONE = {
    esc: (v) => String(v == null ? "" : v),
    formatDate: () => "1 May 2024",
    formatDateTime: () => "1 Oct 2026, 09:00",
    icons: { inject() {}, get: () => "" },
    dashboard: { canOpenPage: (href) => allowed.includes(href) },
  };
  ctx.document.getElementById = (id) => (id === "pf-owner-body" ? body : null);
  vm.runInContext('function $(id){ return document.getElementById(id); }', ctx);
  /* The owner console uses the same fact grid as the staff record, so the
     record slice (which defines kv) is loaded first, then the console. */
  const recStart = PROFILE.indexOf("  var ssData = null;");
  const recEnd = PROFILE.indexOf("  function switchSsTab(tab){", recStart);
  const ownStart = PROFILE.indexOf("  var OWNER_TILES = [");
  const ownEnd = PROFILE.indexOf('  $("pf-form").addEventListener("submit"', ownStart);
  assert.ok(recStart > 0 && recEnd > recStart, "the self-service record code was not found");
  assert.ok(ownStart > 0 && ownEnd > ownStart, "the owner console code was not found");
  vm.runInContext(PROFILE.slice(recStart, recEnd), ctx, { filename: "profile.html#records" });
  vm.runInContext(PROFILE.slice(ownStart, ownEnd), ctx, { filename: "profile.html#owner" });
  vm.runInContext('me = { full_name: "Ada Owner", first_name: "Ada", last_name: "Owner", email: "owner@hotel.dev",'
    + ' email_verified: true, phone: "0803 000 0000", last_login: "2026-10-16T09:00:00Z",'
    + ' date_joined: "2024-05-01T09:00:00Z", role: "ADMIN" };'
    + " renderOwner();", ctx);
  return body.innerHTML;
}

test("the owner console renders the account and the screens the owner runs", () => {
  const html = loadOwnerConsole(["staff.html", "settings.html", "accounting.html", "reports.html"]);
  for (const href of ["staff.html", "settings.html", "accounting.html", "reports.html"]) {
    assert.ok(html.includes('href="' + href + '"'), `the owner console offers ${href}`);
  }
  assert.match(html, /owner@hotel\.dev/);
  assert.match(html, /Administrator — owner \(CEO\)/);
  assert.match(html, /No previous sign-in recorded|1 Oct 2026, 09:00/);
});

test("the rendered owner console carries no staff figure at all", () => {
  const html = loadOwnerConsole(["staff.html", "settings.html", "accounting.html", "reports.html"]);
  for (const marker of ["₦", "NGN", "Basic salary", "Payslip", "Shift", "Leave balance",
                        "Attendance this month", "Employee ID"]) {
    assert.equal(html.includes(marker), false, `the owner console rendered ${marker}`);
  }
});

test("a console with no permitted screen says so instead of showing an empty box", () => {
  const html = loadOwnerConsole([]);
  assert.match(html, /No owner screens available/);
  assert.equal(html.includes('href="accounting.html"'), false);
});
