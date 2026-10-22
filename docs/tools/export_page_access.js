/* Export the frontend navigation facts used by docs/role_permission_matrix.json.

   Run from the repository root, after the Python builder has produced the
   capability rows once:

     node docs/tools/export_page_access.js

   It writes docs/page_access_export.json containing:
     * PAGE_ACCESS        — the page → roles table (js/dashboard.js)
     * ROLES              — Auth.OPERATIONAL_ROLES
     * VISIBLE_NAV        — per role, the sidebar and bottom-bar entries that
                            navItemAllowed() actually renders once the role's
                            capabilities are loaded (i.e. the menu a signed-in
                            person sees, not the table's raw intentions).

   The scripts load through frontend/tests-js/harness.js — the same DOM-less
   context the frontend suites use — so this export fails loudly if the tables
   move instead of silently shipping stale data.
*/
"use strict";
const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..", "..");
const FRONTEND = path.join(ROOT, "frontend");
const { makeContext, loadScript } = require(path.join(FRONTEND, "tests-js", "harness"));

const ctx = makeContext();
ctx.window.API = { setTokenProvider() {}, getToken: () => null, list() {}, get() {} };
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

const dashboard = ctx.window.JONE.dashboard;
const Auth = ctx.window.Auth;
const PAGE_ACCESS = dashboard.PAGE_ACCESS;
const ROLES = Array.from(Auth.OPERATIONAL_ROLES);

let capabilityRows = {};
const matrixPath = path.join(ROOT, "docs", "role_permission_matrix.json");
if (fs.existsSync(matrixPath)) {
  const matrix = JSON.parse(fs.readFileSync(matrixPath, "utf8"));
  for (const role of matrix.roles || []) capabilityRows[role.role] = role.capabilities || [];
}

const VISIBLE_NAV = {};
for (const role of ROLES) {
  const key = String(role).toUpperCase();
  Auth.state.role = role;
  Auth.state.user = { role };
  Auth.state.capabilities = capabilityRows[key] || [];
  Auth.state.capabilitiesLoaded = Object.prototype.hasOwnProperty.call(capabilityRows, key);
  const groups = [];
  for (const group of dashboard.NAV) {
    const items = group.items.filter((item) => dashboard.navItemAllowed(item)).map((item) => item.label);
    if (items.length) groups.push({ group: group.group, items });
  }
  VISIBLE_NAV[key] = {
    groups,
    bottom: (dashboard.NAV && dashboard.BOTTOM_NAV ? dashboard.BOTTOM_NAV : [])
      .filter((item) => dashboard.navItemAllowed(item))
      .map((item) => item.label),
  };
}

const payload = {
  _meta: {
    generated: new Date().toISOString().slice(0, 10),
    source: "frontend/js/dashboard.js NAV/BOTTOM_NAV/navItemAllowed + PAGE_ACCESS + js/auth.js OPERATIONAL_ROLES",
  },
  PAGE_ACCESS,
  ROLES,
  VISIBLE_NAV,
};
fs.writeFileSync(path.join(ROOT, "docs", "page_access_export.json"), JSON.stringify(payload, null, 2) + "\n");
console.log(`pages=${Object.keys(PAGE_ACCESS).length} roles=${ROLES.length}`);
