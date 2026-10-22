/* tests-js/test-staff-landing-access.js

   A successful sign-in must land on a page the person may actually open.

   js/auth.js defaultStaffLanding() picks the post-login page; js/dashboard.js
   PAGE_ACCESS decides which roles each page admits. The two are separate lists,
   so nothing stopped a role from being sent to a page that bounces it to
   /403.html. SECURITY did exactly that (landing index.html, which PAGE_ACCESS
   excludes it from). This suite pins the invariant for every operational role.

   Run from /frontend: node --test tests-js/test-staff-landing-access.js
*/
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { makeContext, loadScript } = require("./harness");

function loadStack() {
  const ctx = makeContext();
  ctx.window.API = { setTokenProvider() {}, getToken: () => null, list() {}, get() {} };
  ctx.window.APP_CONFIG = {
    API_ENDPOINTS: {},
    STORAGE: { THEME: "a", AUTH: "b", BOOKING: "c", SESSION: "d", TERMINAL: "e" },
  };
  loadScript(ctx, "js/utils.js");
  loadScript(ctx, "js/auth.js");
  loadScript(ctx, "js/dashboard.js");
  return ctx;
}

const ctx = loadStack();
const Auth = ctx.window.Auth;
const dashboard = ctx.window.JONE.dashboard;

test("every operational role lands on a dashboard page", () => {
  for (const role of Auth.OPERATIONAL_ROLES) {
    const landing = Auth.defaultStaffLanding(role);
    assert.match(landing, /^dashboard\/[a-z0-9-]+\.html$/, `${role} -> ${landing}`);
    assert.ok(
      Object.prototype.hasOwnProperty.call(dashboard.PAGE_ACCESS, path.basename(landing)),
      `${role} lands on an unclassified page: ${landing}`
    );
  }
});

test("the landing page admits the role it is chosen for", () => {
  const failures = [];
  for (const role of Auth.OPERATIONAL_ROLES) {
    Auth.state.role = role;
    Auth.state.user = { role };
    const landing = Auth.defaultStaffLanding(role);
    if (!dashboard.canOpenPage(landing)) failures.push(`${role} -> ${landing}`);
  }
  assert.deepEqual(failures, [], `these roles are bounced to /403.html after sign-in: ${failures.join(", ")}`);
});

test("guests never receive a staff landing", () => {
  Auth.state.role = "guest";
  Auth.state.user = { role: "guest" };
  assert.equal(Auth.isStaffRole("guest"), false);
});

test("security lands on the workforce page it is admitted to", () => {
  Auth.state.role = "security";
  Auth.state.user = { role: "security" };
  assert.equal(Auth.defaultStaffLanding("security"), "dashboard/workforce.html");
  assert.equal(dashboard.canOpenPage("dashboard/workforce.html"), true);
});
