/* Regression suite for retiring standalone Cashier and merging its work into
   the existing Restaurants WAITER role, plus removing Laundry from the
   Restaurants dashboard.

   Run from /frontend: node --test tests-js/test-cashier-restaurants-consolidation.js
*/
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { makeContext, loadScript, FRONTEND, disposeAll } = require("./harness");

test.after(() => disposeAll());

function read(relative) {
  return fs.readFileSync(path.join(FRONTEND, relative), "utf8");
}

function loadAuthStack() {
  const ctx = makeContext();
  loadScript(ctx, "js/config.js");
  loadScript(ctx, "js/utils.js");
  loadScript(ctx, "js/api.js");
  loadScript(ctx, "js/auth.js");
  return ctx;
}

function loadDashboardStack() {
  const ctx = makeContext();
  ctx.window.API = {
    setTokenProvider() {},
    getToken() { return null; },
    list() {},
    get() {},
  };
  ctx.window.APP_CONFIG = {
    API_ENDPOINTS: {},
    STORAGE: {
      THEME: "jone.theme",
      AUTH: "jone.auth",
      BOOKING: "jone.booking.draft",
      SESSION: "jone.session",
      TERMINAL: "jone.terminal.reference",
    },
  };
  loadScript(ctx, "js/utils.js");
  loadScript(ctx, "js/auth.js");
  loadScript(ctx, "js/dashboard.js");
  return ctx;
}

test("department portals no longer admit a cashier role and Waiter keeps settlement portals", () => {
  const ctx = makeContext();
  loadScript(ctx, "js/config.js");
  const portals = ctx.window.APP_CONFIG.DEPARTMENT_SUBDOMAINS;
  for (const [name, portal] of Object.entries(portals)) {
    assert.equal(portal.allowedRoles.includes("cashier"), false, `${name} must not allow the retired role`);
  }
  assert.equal(portals.restaurant.allowedRoles.includes("waiter"), true);
  assert.equal(portals.bar.allowedRoles.includes("waiter"), true, "bar-order settlement remains available to Restaurants");
});

test("stored CASHIER sessions normalize to WAITER and no active Cashier identity is listed", () => {
  const auth = loadAuthStack().window.Auth;
  assert.equal(auth.OPERATIONAL_ROLES.includes("cashier"), false);
  assert.equal(auth.isStaffRole("CASHIER"), true);
  assert.equal(auth.defaultStaffLanding("CASHIER"), "dashboard/pos.html");
  assert.equal(auth.isAuthorizedForSubdomain({ role: "CASHIER" }, { allowedRoles: ["waiter"] }), true);
});

test("active page registry has no Cashier role and Waiter inherits the cashier workspaces", () => {
  const dashboard = loadDashboardStack().window.JONE.dashboard;
  for (const [page, rule] of Object.entries(dashboard.PAGE_ACCESS)) {
    if (Array.isArray(rule.roles)) {
      assert.equal(rule.roles.includes("cashier"), false, `${page} still names the retired role`);
    }
  }
  assert.equal(dashboard.PAGE_ACCESS["pos.html"].roles.includes("waiter"), true);
  assert.equal(dashboard.PAGE_ACCESS["barcode-labels.html"].roles.includes("waiter"), true);
  assert.equal(dashboard.PAGE_ACCESS["workforce.html"].roles.includes("waiter"), true);
});

test("staff assignment and transfer forms no longer offer Cashier department or role", () => {
  const staff = read("dashboard/staff.html");
  const profile = read("dashboard/staff-profile.html");
  for (const source of [staff, profile]) {
    assert.doesNotMatch(source, /value:\s*"CASHIER"/);
    assert.doesNotMatch(source, /label:\s*"Cashier(?:\s*·\s*Cashier)?"/);
  }
  assert.doesNotMatch(staff, /<option value="CASHIER">Cashiers<\/option>/);
  assert.match(staff, /POS Cashier is a designation under the Restaurant Waiter role/);
});

test("POS cashier controls keep the existing endpoints and admit Waiter before capabilities load", () => {
  const pos = read("dashboard/pos.html");
  assert.doesNotMatch(pos, /boot\(\[[^\]]*"cashier"/);
  assert.match(pos, /canOpenCash\(\)\{return hasCapability\("cash_session\.open",\[[^\]]*"waiter"/);
  assert.match(pos, /canCloseCash\(\)\{return hasCapability\("cash_session\.close",\[[^\]]*"waiter"/);
  assert.match(pos, /canRefundReturns\(\)\{return hasCapability\("pos\.return\.refund",\[[^\]]*"waiter"/);
  assert.match(pos, /canTender\(\)\{return hasCapability\("payment\.capture",\[[^\]]*"waiter"/);
  assert.match(pos, /window\.API\.openPosCashSession/);
  assert.match(pos, /window\.API\.closePosCashSession/);
  assert.match(pos, /window\.API\.capturePosTender/);
});

test("Laundry keeps its own page and authorized roles, but excludes restaurant roles", () => {
  const laundry = read("dashboard/laundry.html");
  const dashboard = loadDashboardStack().window.JONE.dashboard;
  const boot = laundry.match(/JONE\.dashboard\.boot\(\[([^\]]*)\]/);
  assert.ok(boot, "Laundry still has its own page gate");
  const roles = (boot[1].match(/"([a-z0-9_]+)"/g) || []).map((r) => r.replace(/"/g, ""));
  for (const forbidden of ["waiter", "bartender"]) {
    assert.equal(roles.includes(forbidden), false, `${forbidden} must not open the Laundry page`);
    assert.equal(dashboard.PAGE_ACCESS["laundry.html"].roles.includes(forbidden), false);
  }
  for (const allowed of ["receptionist", "housekeeper", "porter", "security"]) {
    assert.equal(roles.includes(allowed), true, `${allowed} keeps Laundry access`);
    assert.equal(dashboard.PAGE_ACCESS["laundry.html"].roles.includes(allowed), true);
  }
  assert.match(laundry, /data-laundry-rows/);
  assert.match(laundry, /resourceAction\("guestLaundry"/);
});
