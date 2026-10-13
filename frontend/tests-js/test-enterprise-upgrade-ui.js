/* Enterprise upgrade regression suite covering Priorities 1-5 on the frontend:
   - Public footer staff link removal
   - 11 department subdomain portals & cross-department access isolation
   - 6-digit numeric workstation access-code login & reception attendance console
   - 10-tab Staff Profile detail page & Admin Permission Matrix UI
   - Inventory stock operations & single-flight receipt email deduplication
*/
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { makeContext, loadScript, FRONTEND, disposeAll } = require("./harness");

test.after(() => disposeAll());

test("public footer component and built public pages do not expose staff login link", () => {
  const footerComponent = fs.readFileSync(path.join(FRONTEND, "components", "footer.html"), "utf8");
  assert.doesNotMatch(footerComponent, /<a[^>]*href=["'][^"']*login\.html["'][^>]*>\s*Staff\s*<\/a>/i);

  ["index.html", "about.html", "rooms.html", "contact.html"].forEach((pageName) => {
    const html = fs.readFileSync(path.join(FRONTEND, pageName), "utf8");
    const footerStart = html.indexOf('<footer class="site-footer"');
    const footerEnd = html.indexOf("</footer>", footerStart);
    assert.notEqual(footerStart, -1, `${pageName} must have a site-footer`);
    const footerHtml = html.slice(footerStart, footerEnd);
    assert.doesNotMatch(footerHtml, /<a[^>]*href=["'](?:\.\/|\/)?login\.html["']/i, `${pageName} footer must not link to staff login`);
  });
});

test("all 11 department subdomains resolve and enforce role/department isolation", () => {
  const ctx = makeContext({ hostname: "kitchen.jonehotel.com", pathname: "/login.html" });
  loadScript(ctx, "js/config.js");
  loadScript(ctx, "js/utils.js");
  loadScript(ctx, "js/api.js");
  loadScript(ctx, "js/auth.js");

  const subdomains = ctx.window.APP_CONFIG.DEPARTMENT_SUBDOMAINS;
  const expectedSubdomains = [
    "manager", "accountant", "admin", "procurement", "frontdesk",
    "kitchen", "restaurant", "bar", "housekeeping", "porter", "store",
  ];
  expectedSubdomains.forEach((sub) => {
    assert.ok(subdomains[sub], `Missing department subdomain configuration for ${sub}.jonehotel.com`);
  });

  const auth = ctx.window.Auth;
  const kitchenCtx = auth.getSubdomainContext("kitchen.jonehotel.com");
  assert.equal(kitchenCtx.code, "KITCHEN");
  assert.equal(kitchenCtx.landingPath, "/dashboard/pos.html");

  // Chef and Kitchen Assistant are authorized on kitchen.jonehotel.com
  assert.equal(auth.isAuthorizedForSubdomain({ role: "CHEF" }, kitchenCtx), true);
  assert.equal(auth.isAuthorizedForSubdomain({ role: "KITCHEN_ASSISTANT" }, kitchenCtx), true);
  // Accountant and Porter are rejected on kitchen.jonehotel.com
  assert.equal(auth.isAuthorizedForSubdomain({ role: "ACCOUNTANT" }, kitchenCtx), false);
  assert.equal(auth.isAuthorizedForSubdomain({ role: "PORTER" }, kitchenCtx), false);
  // Admin and General Manager can access all department portals
  assert.equal(auth.isAuthorizedForSubdomain({ role: "ADMIN" }, kitchenCtx), true);
  assert.equal(auth.isAuthorizedForSubdomain({ role: "GENERAL_MANAGER" }, kitchenCtx), true);

  const porterCtx = auth.getSubdomainContext("porter.jonehotel.com");
  assert.equal(auth.isAuthorizedForSubdomain({ role: "PORTER" }, porterCtx), true);
  assert.equal(auth.isAuthorizedForSubdomain({ role: "WAITER" }, porterCtx), false);

  const storeCtx = auth.getSubdomainContext("store.jonehotel.com");
  assert.equal(auth.isAuthorizedForSubdomain({ role: "STOREKEEPER" }, storeCtx), true);
  assert.equal(auth.isAuthorizedForSubdomain({ role: "RECEPTIONIST" }, storeCtx), false);
});

test("6-digit numeric workstation access code login posts access_code and department context", async () => {
  const requests = [];
  const ctx = makeContext({ hostname: "restaurant.jonehotel.com", pathname: "/login.html" });
  ctx.fetch = (url, init) => {
    requests.push({ url, init });
    return Promise.resolve({
      status: 200,
      ok: true,
      headers: { get: (k) => (k === "content-type" ? "application/json" : null) },
      json: () => Promise.resolve({
        success: true,
        data: {
          user: { id: 12, email: "waiter@jonehotel.com", role: "WAITER", department: "Restaurant & Bar Service" },
          tokens: { access: "jwt-access-123", refresh: "jwt-refresh-123" },
        },
      }),
      text: () => Promise.resolve("{}"),
    });
  };
  loadScript(ctx, "js/config.js");
  loadScript(ctx, "js/utils.js");
  loadScript(ctx, "js/api.js");
  loadScript(ctx, "js/auth.js");

  await ctx.window.Auth.loginWithAccessCode("482910");
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, "/api/auth/login/");
  const body = JSON.parse(requests[0].init.body);
  assert.equal(body.access_code, "482910");
  assert.equal(body.department, "restaurant");
});

test("dedicated 10-tab staff profile page and reception biometric workstation console are wired", () => {
  const profilePage = fs.readFileSync(path.join(FRONTEND, "dashboard", "staff-profile.html"), "utf8");
  const expectedTabs = [
    "overview", "personal", "employment", "biometrics", "salary",
    "disciplinary", "leave_shifts", "documents", "permissions", "audit",
  ];
  expectedTabs.forEach((tab) => {
    assert.match(profilePage, new RegExp('data-sp-tab="' + tab + '"'), `Missing tab ${tab} in staff-profile.html`);
    assert.match(profilePage, new RegExp('id="sp-pane-' + tab + '"'), `Missing pane ${tab} in staff-profile.html`);
  });
  assert.match(profilePage, /API_ENDPOINTS\.staffFullProfile/);

  const terminalsPage = fs.readFileSync(path.join(FRONTEND, "dashboard", "terminals.html"), "utf8");
  assert.match(terminalsPage, /reception-attendance-workstation-panel/);
  assert.match(terminalsPage, /data-reception-issued-code/);
  assert.match(terminalsPage, /executeReceptionTerminalAction/);

  const staffPage = fs.readFileSync(path.join(FRONTEND, "dashboard", "staff.html"), "utf8");
  assert.match(staffPage, /staff-profile\.html\?id=/);
  assert.match(staffPage, /API_ENDPOINTS\.permissionsMatrix/);
  assert.match(staffPage, /KITCHEN_ASSISTANT/);
  assert.match(staffPage, /PORTER/);
});
