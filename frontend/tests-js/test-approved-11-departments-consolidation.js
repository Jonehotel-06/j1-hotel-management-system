/* Regression suite verifying the Approved 11 Departments & 11 Staff Roles
   consolidation across Auth, Department Portals, Page Access, and UI Forms,
   while preserving Security, General Manager, Accountant, and Front-Desk
   sign-in integrity.

   Run from /frontend: node --test tests-js/test-approved-11-departments-consolidation.js
*/
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { makeContext, loadScript, FRONTEND, disposeAll } = require("./harness");

test.after(() => disposeAll());

const APPROVED_STAFF_ROLES = [
  "general_manager",
  "accountant",
  "procurement_officer",
  "chef",
  "kitchen_assistant",
  "waiter",
  "porter",
  "receptionist",
  "housekeeping",
  "storekeeper",
  "security",
];

const RETIRED_ROLES = [
  "manager",
  "front_desk_supervisor",
  "staff",
  "restaurant_manager",
  "bar_manager",
  "bartender",
  "cashier",
  "kitchen_manager",
  "housekeeping_manager",
  "housekeeper",
  "maintenance",
  "maintenance_technician",
  "accounts_manager",
  "hr_manager",
  "hr_officer",
  "inventory_clerk",
];

const APPROVED_DEPARTMENT_LABELS = [
  "General Manager",
  "Accountant",
  "Admin Manager/Procurement",
  "Chefs",
  "Kitchen Assistant",
  "Waiters/Waitresses",
  "Porters",
  "Receptionist",
  "Housekeepers",
  "Store Keeper",
  "Security",
];

function read(relative) {
  return fs.readFileSync(path.join(FRONTEND, relative), "utf8");
}

function loadFullStack() {
  const ctx = makeContext();
  loadScript(ctx, "js/config.js");
  loadScript(ctx, "js/utils.js");
  loadScript(ctx, "js/api.js");
  loadScript(ctx, "js/auth.js");
  loadScript(ctx, "js/dashboard.js");
  return ctx;
}

test("Auth.ROLES and Auth.OPERATIONAL_ROLES contain ONLY the 11 approved staff roles plus admin/guest", () => {
  const auth = loadFullStack().window.Auth;
  const roleValues = Array.from(Object.values(auth.ROLES)).sort();
  const expectedAll = ["admin", "guest", ...APPROVED_STAFF_ROLES].sort();
  assert.deepEqual(roleValues, expectedAll);

  const operational = Array.from(auth.OPERATIONAL_ROLES).sort();
  const expectedOperational = ["admin", ...APPROVED_STAFF_ROLES].sort();
  assert.deepEqual(operational, expectedOperational);
  assert.equal(operational.length, 12);

  for (const retired of RETIRED_ROLES) {
    assert.equal(auth.OPERATIONAL_ROLES.includes(retired), false, `${retired} must not be in OPERATIONAL_ROLES`);
  }
});

test("all 16 retired stored roles normalize cleanly to their consolidated approved role at the boundary", () => {
  const auth = loadFullStack().window.Auth;
  const expectedLandings = {
    MANAGER: "dashboard/index.html",
    FRONT_DESK_SUPERVISOR: "dashboard/index.html",
    STAFF: "dashboard/index.html",
    RESTAURANT_MANAGER: "dashboard/pos.html",
    BAR_MANAGER: "dashboard/pos.html",
    BARTENDER: "dashboard/pos.html",
    CASHIER: "dashboard/pos.html",
    KITCHEN_MANAGER: "dashboard/pos.html",
    HOUSEKEEPING_MANAGER: "dashboard/housekeeping.html",
    HOUSEKEEPER: "dashboard/housekeeping.html",
    MAINTENANCE: "dashboard/housekeeping.html",
    MAINTENANCE_TECHNICIAN: "dashboard/housekeeping.html",
    ACCOUNTS_MANAGER: "dashboard/payments.html",
    HR_MANAGER: "dashboard/procurement.html",
    HR_OFFICER: "dashboard/procurement.html",
    INVENTORY_CLERK: "dashboard/inventory.html",
  };
  for (const [legacyCode, expectedLanding] of Object.entries(expectedLandings)) {
    assert.equal(auth.isStaffRole(legacyCode), true, `${legacyCode} session should remain recognized as staff`);
    assert.equal(auth.defaultStaffLanding(legacyCode), expectedLanding, `${legacyCode} landing mismatch`);
  }
});

test("DEPARTMENT_SUBDOMAINS and PAGE_ACCESS expose zero retired roles while preserving Security, GM, and Accountant", () => {
  const ctx = loadFullStack();
  const portals = ctx.window.APP_CONFIG.DEPARTMENT_SUBDOMAINS;
  const pageAccess = ctx.window.JONE.dashboard.PAGE_ACCESS;

  for (const [name, portal] of Object.entries(portals)) {
    for (const retired of RETIRED_ROLES) {
      assert.equal(portal.allowedRoles.includes(retired), false, `Portal ${name} still lists retired role ${retired}`);
    }
  }
  assert.ok(portals.security, "Dedicated security portal must exist");
  assert.equal(portals.security.label, "Security");
  assert.equal(portals.security.allowedRoles.includes("security"), true);
  assert.ok(portals["kitchen-assistant"], "Dedicated kitchen-assistant portal must exist");
  assert.equal(portals["kitchen-assistant"].label, "Kitchen Assistant");

  for (const [page, rule] of Object.entries(pageAccess)) {
    if (Array.isArray(rule.roles)) {
      for (const retired of RETIRED_ROLES) {
        assert.equal(rule.roles.includes(retired), false, `PAGE_ACCESS[${page}] still lists retired role ${retired}`);
      }
    }
  }
});

test("staff creation, transfer, workstation, and expense forms offer ONLY the 11 approved departments and roles", () => {
  const staff = read("dashboard/staff.html");
  const staffProfile = read("dashboard/staff-profile.html");
  const terminals = read("dashboard/terminals.html");
  const accounting = read("dashboard/accounting.html");
  const dashboardJs = read("js/dashboard.js");

  for (const label of APPROVED_DEPARTMENT_LABELS) {
    assert.ok(staff.includes(`label: "${label}"`), `staff.html missing ${label}`);
    assert.ok(staffProfile.includes(`label: "${label}"`), `staff-profile.html missing ${label}`);
    assert.ok(terminals.includes(`label:"${label}"`), `terminals.html missing ${label}`);
    assert.ok(accounting.includes(`label: "${label}"`), `accounting.html missing ${label}`);
    assert.ok(dashboardJs.includes(`label: "${label}"`), `dashboard.js editStaffModal missing ${label}`);
  }

  for (const retiredUpper of ["BAR", "MAINTENANCE", "HR", "CASHIER", "BARTENDER", "HR_OFFICER", "INVENTORY_CLERK"]) {
    assert.doesNotMatch(staff, new RegExp(`value:\\s*"${retiredUpper}"`));
    assert.doesNotMatch(staffProfile, new RegExp(`value:\\s*"${retiredUpper}"`));
    assert.doesNotMatch(terminals, new RegExp(`value:"${retiredUpper}"`));
  }
});
