"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { makeContext, loadScript, disposeAll } = require("./harness");
test.after(() => disposeAll());
test("procurement sees inventory in payroll's old position; other menus retain their order", () => {
  const ctx = makeContext();
  ctx.window.API = { setTokenProvider() {}, getToken() { return null; } };
  ctx.window.APP_CONFIG = { API_ENDPOINTS: {}, STORAGE: { AUTH: "jone.auth" } };
  loadScript(ctx, "js/utils.js"); loadScript(ctx, "js/auth.js"); loadScript(ctx, "js/dashboard.js");
  const auth = ctx.window.Auth, dash = ctx.window.JONE.dashboard;
  function items(role) { auth.state.role = role; auth.state.user = {role}; auth.state.capabilitiesLoaded = false; return Array.from(dash.NAV.flatMap(g => g.items).filter(dash.navItemAllowed), i => i.href); }
  const proc = items("procurement_officer");
  assert.equal(proc.filter(x => x === "inventory.html").length, 1);
  assert(proc.includes("procurement.html")); assert(!proc.includes("payroll.html"));
  assert(proc.indexOf("procurement.html") < proc.indexOf("inventory.html"));
  assert(dash.canOpenPage("inventory.html")); assert(!dash.canOpenPage("payroll.html"));
  const store = items("storekeeper"); assert(store.indexOf("inventory.html") < store.indexOf("procurement.html"));
  for (const role of ["admin", "general_manager", "accountant"]) assert(items(role).includes("payroll.html"), role);
});
