/* tests-js/test-management-tier-gates.js */
/* Who sees the management controls, and who must not.

   Reported symptom (floor): "the manager cannot manage the offers / the guest
   discounts / the rooms screens". The audit found THREE separate causes, and
   only one of them was in the browser:

   1. js/dashboard.js and two pages used `hasRole("manager")`. That happened to
      work — Auth.hasRole() runs both sides through the retired-role alias table,
      so "manager" resolved to "general_manager" — but it is a rank comparison
      over an alias, not the exact role list the server enforces
      (MANAGER_ROLES = ADMIN, MANAGER, GENERAL_MANAGER). The gates now use
      Auth.hasAnyRole(["admin", "general_manager"]), so they cannot drift when the
      alias table changes.
   2. TWO dashboard pages (amenities.html, breakfast.html) were shipped with no
      JONE.dashboard.boot() call, no auth.js, no registry entry and an inline
      script that did not parse: no sidebar, no guard, invisible in the menu.
   3. The backend capability matrix denied the management tier the guest-amenity
      and breakfast codes the front desk holds (see
      backend/tests/test_management_tier_guest_services.py).

   This suite pins 1 and 2 in the shipped sources.

   Run: node --test tests-js/test-management-tier-gates.js   (from frontend/) */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { makeContext, loadScript, FRONTEND, disposeAll } = require("./harness");

test.after(() => disposeAll());

function boot(role) {
  const ctx = makeContext();
  loadScript(ctx, "js/config.js");
  loadScript(ctx, "js/utils.js");
  ctx.window.API = { setTokenProvider() { }, getToken() { return null; } };
  loadScript(ctx, "js/auth.js");
  loadScript(ctx, "js/dashboard.js");
  const dashboard = ctx.window.JONE.dashboard;
  if (role === null) {
    ctx.window.Auth.state.role = null;
    ctx.window.Auth.state.user = null;
  } else {
    ctx.window.Auth.state.role = role;
    ctx.window.Auth.state.user = { role: role };
  }
  return { dashboard, auth: ctx.window.Auth };
}

const MANAGEMENT_CONTROLS = ["canManageRooms"];

/* --------------------------- the tier that manages ---------------------- */

test("the management tier holds every management control", () => {
  for (const role of ["admin", "general_manager"]) {
    const { dashboard } = boot(role);
    for (const control of MANAGEMENT_CONTROLS) {
      assert.equal(dashboard[control](), true, `${role} must hold ${control}`);
    }
    assert.deepEqual(Array.from(dashboard.MANAGEMENT_ROLES), ["admin", "general_manager"]);
  }
});

test("the retired 'manager' claim still resolves to the general manager", () => {
  /* Old JWT/localStorage values may still say MANAGER; Auth normalises them, so
     the control must be there for that session too. */
  const { dashboard } = boot("manager");
  assert.equal(dashboard.canManageRooms(), true);
});

test("department roles are refused every management control", () => {
  const roles = ["receptionist", "housekeeping", "storekeeper", "accountant",
    "procurement_officer", "waiter", "chef", "kitchen_assistant", "porter", "security"];
  for (const role of roles) {
    const { dashboard } = boot(role);
    for (const control of MANAGEMENT_CONTROLS) {
      assert.equal(dashboard[control](), false, `${role} must NOT hold ${control}`);
    }
  }
});

test("a guest and a signed-out visitor hold nothing", () => {
  for (const role of ["guest", null]) {
    const { dashboard } = boot(role);
    for (const control of MANAGEMENT_CONTROLS) {
      assert.equal(dashboard[control](), false, `${role} must NOT hold ${control}`);
    }
  }
});

test("an unknown role fails closed", () => {
  const { dashboard } = boot("some_future_role");
  assert.equal(dashboard.canManageRooms(), false);
  assert.equal(dashboard.canManageStaff(), false);
});

test("staff administration stays admin-only", () => {
  assert.equal(boot("admin").dashboard.canManageStaff(), true);
  assert.equal(boot("general_manager").dashboard.canManageStaff(), false);
  assert.deepEqual(Array.from(boot("admin").dashboard.ADMIN_ROLES), ["admin"]);
});

/* ------------------- the gates name roles, not a ladder ------------------ */

function read(rel) {
  return fs.readFileSync(path.join(FRONTEND, rel), "utf8");
}

test("no management gate in the shell is expressed as a rank", () => {
  const source = read("js/dashboard.js");
  assert.doesNotMatch(source, /canManageRooms\(\)\s*\{\s*return hasRole\(/,
    "canManageRooms must not use the seniority ladder");
  assert.doesNotMatch(source, /canManageStaff\(\)\s*\{\s*return hasRole\(/,
    "canManageStaff must not use the seniority ladder");
  assert.match(read("dashboard/offers.html"),
    /canManageOffers = JONE\.dashboard\.hasAnyRole\(\["admin", "general_manager"\]\)/);
  assert.match(read("dashboard/guests.html"),
    /canManageDiscounts = JONE\.dashboard\.hasAnyRole\(\["admin", "general_manager"\]\)/);
});

test("no shipped page still asks hasRole(\"manager\")", () => {
  const pages = fs.readdirSync(path.join(FRONTEND, "dashboard"))
    .filter((f) => f.endsWith(".html"))
    .map((f) => path.join("dashboard", f))
    .concat(["js/dashboard.js"]);
  const offenders = pages.filter((rel) => /hasRole\(\s*"manager"\s*\)/.test(read(rel)));
  assert.deepEqual(offenders, [], "these still use the retired 'manager' rank: " + offenders.join(", "));
});

/* ------------- the two pages that were shipped as orphans ---------------- */

test("every dashboard page boots the shell and loads the auth script", () => {
  const files = fs.readdirSync(path.join(FRONTEND, "dashboard")).filter((f) => f.endsWith(".html"));
  const problems = [];
  for (const file of files) {
    const html = read(path.join("dashboard", file));
    if (!/JONE\.dashboard\.boot\s*\(/.test(html)) problems.push(`${file}: no JONE.dashboard.boot(...)`);
    if (!/js\/auth\.js/.test(html)) problems.push(`${file}: no js/auth.js`);
    if (!/js\/runtime-config\.js/.test(html)) problems.push(`${file}: no js/runtime-config.js`);
  }
  assert.deepEqual(problems, [], "dashboard pages missing their shell wiring:\n  " + problems.join("\n  "));
});

test("amenities and breakfast are registered, gated and reachable", () => {
  const { dashboard } = boot("receptionist");
  assert.equal(dashboard.canOpenPage("amenities.html"), true, "the front desk issues amenities");
  assert.equal(dashboard.canOpenPage("breakfast.html"), true, "the front desk records breakfast");
  const gm = boot("general_manager").dashboard;
  assert.equal(gm.canOpenPage("amenities.html"), true, "management covers the station");
  assert.equal(gm.canOpenPage("breakfast.html"), true);
  const keeper = boot("storekeeper").dashboard;
  assert.equal(keeper.canOpenPage("amenities.html"), true, "the store keeper allocates to Reception");
  assert.equal(keeper.canOpenPage("breakfast.html"), false, "but does not record breakfast");
  const waiter = boot("waiter").dashboard;
  assert.equal(waiter.canOpenPage("amenities.html"), false);
  assert.equal(waiter.canOpenPage("breakfast.html"), false);

  /* Both pages carry the same boot list their registry entry names. */
  for (const [file, roles] of [
    ["amenities.html", ["admin", "general_manager", "receptionist", "storekeeper"]],
    ["breakfast.html", ["admin", "general_manager", "receptionist"]],
  ]) {
    const html = read(path.join("dashboard", file));
    const match = html.match(/JONE\.dashboard\.boot\(\[([^\]]*)\]\)/);
    assert.ok(match, `${file} must boot through JONE.dashboard.boot([...])`);
    const shipped = (match[1].match(/"[a-z0-9_]+"/g) || []).map((r) => r.replace(/"/g, "")).sort();
    assert.deepEqual(shipped, roles.slice().sort(), `${file} boot list disagrees with the registry`);
  }
});

test("the sidebar entries for amenities and breakfast pass the registry gate", () => {
  const nav = boot("receptionist").dashboard.NAV.flatMap((group) => group.items.map((i) => i.href));
  assert.ok(nav.includes("amenities.html"));
  assert.ok(nav.includes("breakfast.html"));
});
