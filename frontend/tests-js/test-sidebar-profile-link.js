/* tests-js/test-sidebar-profile-link.js
   The signed-in identity in the dashboard sidebar is a link to the caller's OWN
   profile; sign-out stays a separate control. Covers every role (the sidebar is
   rendered from the shared shell) and both sidebar states.
   Run from /frontend: node --test tests-js/test-sidebar-profile-link.js */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { makeContext, loadScript, disposeAll, FRONTEND } = require("./harness");

function renderSidebarUser(user) {
  const ctx = makeContext({ pathname: "/dashboard/index.html" });
  loadScript(ctx, "js/utils.js");
  ctx.window.JONE = ctx.window.JONE || {};
  ctx.window.JONE.esc = (v) => String(v == null ? "" : v);
  ctx.window.JONE.initials = (name) => String(name || "").split(/\s+/).map((p) => p[0]).slice(0, 2).join("").toUpperCase();
  ctx.window.JONE.icons = { inject() {}, get() { return ""; } };
  ctx.window.Auth = { state: { user } };
  const host = ctx.document.createElement("div");
  loadScript(ctx, "js/dashboard.js");
  // The harness document has no real selector engine; point the one selector
  // renderUser() resolves at the host element so the REAL renderer runs.
  ctx.document.querySelector = (sel) => (sel === "[data-dash-user]" ? host : null);
  ctx.window.JONE.dashboard.renderUser("[data-dash-user]");
  return host.innerHTML;
}

test("the sidebar identity is a link to the signed-in user's own profile", () => {
  const html = renderSidebarUser({
    full_name: "Adaeze Okafor", email: "manager@jonehotel.com", role: "MANAGER", profile_image_url: null,
  });
  assert.match(html, /<a class="dash-user-link" href="profile\.html"/, "identity links to profile.html");
  assert.match(html, /aria-label="Open my profile \(Adaeze Okafor\)"/, "keyboard/screen-reader label");
  assert.match(html, /dash-user-name">Adaeze Okafor</, "the name is still shown");
  assert.match(html, /dash-user-role">Manager</, "the role is still shown");
  assert.match(html, /dash-avatar[^>]*>AO<|>AO</, "initials fall back when there is no photo");
  assert.doesNotMatch(html, /logout/i, "sign-out is never nested inside the profile link");
  disposeAll();
});

test("every role gets the same link (no role-specific branching)", () => {
  for (const role of ["ADMIN", "MANAGER", "GENERAL_MANAGER", "HR_MANAGER", "RECEPTIONIST", "CHEF", "HOUSEKEEPING"]) {
    const html = renderSidebarUser({ full_name: "Staff " + role, email: role.toLowerCase() + "@jonehotel.com", role });
    assert.match(html, /href="profile\.html"/, `${role} must be able to open its own profile`);
  }
  disposeAll();
});

test("a signed-in user with only an email still gets a usable link", () => {
  const html = renderSidebarUser({ email: "chef@jonehotel.com", role: "CHEF" });
  assert.match(html, /href="profile\.html"/);
  assert.match(html, /chef@jonehotel\.com/);
  assert.match(html, /aria-label="Open my profile \(chef@jonehotel\.com\)"/);
  disposeAll();
});

test("the profile link has a focus ring and survives the collapsed sidebar", () => {
  const css = fs.readFileSync(path.join(FRONTEND, "css", "dashboard.css"), "utf8");
  assert.match(css, /\.dash-user-link\s*\{[^}]*display: flex/s);
  assert.match(css, /\.dash-user-link:focus-visible\s*\{[^}]*outline:/s, "visible keyboard focus");
  assert.match(css, /\.dash-user-link:hover \.dash-user-name/, "hover state is legible on the dark sidebar");
  // Collapsed sidebar hides the name/role stack, not the avatar link.
  assert.match(css, /\[data-sidebar="collapsed"\][^{]*\.dash-user-copy\s*\{\s*display: none/);
});

test("every dashboard page that renders the sidebar also renders the link", () => {
  const dir = path.join(FRONTEND, "dashboard");
  const pages = fs.readdirSync(dir).filter((f) => f.endsWith(".html"));
  assert.ok(pages.length > 30, "sanity: the dashboard has many pages");
  for (const page of pages) {
    const html = fs.readFileSync(path.join(dir, page), "utf8");
    if (!html.includes("data-dash-user")) continue;      // receipt-details has no chrome, by design
    assert.match(html, /dash-sidebar-footer/, `${page}: sidebar footer present`);
    assert.match(html, /data-logout/, `${page}: sign-out stays a separate control`);
  }
});

test.after(() => disposeAll());
