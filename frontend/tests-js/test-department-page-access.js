/* tests-js/test-department-page-access.js

   DEPARTMENT PAGE ACCESS — a link is a promise.

   Issue #5C: "for each department, any page or table they are not meant to
   access, remove the link to that page or table or modal."

   A guard inside a page (JONE.dashboard.boot([...]) at the top of every
   dashboard HTML file) only protects the page once somebody has already been
   sent there: it bounces them to /403.html. What a department actually sees is
   decided by the LINKS — the sidebar (NAV), the bottom bar (BOTTOM_NAV), the
   dashboard quick actions, and every in-page link. Those surfaces read one
   table, PAGE_ACCESS in js/dashboard.js, through canOpenPage(href).

   That only stays true while the table and the pages agree, so this suite
   parses BOTH halves from source and fails on any drift:

     1. every dashboard page is graded by the table (and every non-public key
        in the table is a real dashboard page — no stale keys);
     2. a page that boots with an exact role array has EXACTLY that list in the
        table (this is the department rule: no implicit hierarchy between
        departments);
     3. a page that boots with a minimum role accepts every role the table
        admits for it, and the table never admits a role that page's own gate
        would refuse (never promise a screen that bounces the user);
     4. the personal surfaces (notifications, profile) use the single
        Auth.OPERATIONAL_ROLES marker — "every staff member, guests excluded";
     5. every *.html link target anywhere in the dashboard is classified, so a
        link can never silently disappear behind the fail-closed default;
     6. the sidebar and the bottom bar carry NO role lists of their own — the
        table is the only department rule, and each of their entries must pass
        navItemAllowed (registry AND gate);
     7. the resolved behaviour of canOpenPage is checked against the REAL
        js/auth.js role tables for a representative role of every department,
        including the fail-closed cases (unknown page, signed-out, guest);
     8. every gate INSIDE a page (the "may I dispatch / approve / plan?" role
        lists) is checked against the authoritative backend capability matrix
        (ROLE_CAPABILITY_CODES), in both directions — a frontend gate may not
        offer a control the server refuses, and may not refuse a department
        (the general manager, the front desk supervisor) a control the server
        grants it.

   Run from /frontend: node --test tests-js/test-department-page-access.js
   ========================================================================== */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { makeContext, loadScript, FRONTEND } = require("./harness");

const DASHBOARD_DIR = path.join(FRONTEND, "dashboard");

/* ------------------------------- fixtures -------------------------------- */

/* The shipped registry + the shipped role tables, loaded in a DOM-less
   context: js/utils.js (JONE.el/esc), js/auth.js (roles, hasRole/hasAnyRole)
   and js/dashboard.js (PAGE_ACCESS, canOpenPage, NAV, BOTTOM_NAV). */
function loadStack() {
  const ctx = makeContext();
  ctx.window.API = { setTokenProvider() { }, getToken() { return null; }, list() { }, get() { } };
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

const ctx = loadStack();
const JONE = ctx.window.JONE;
const Auth = ctx.window.Auth;
const PAGE_ACCESS = JONE.dashboard.PAGE_ACCESS;

/* Sign in as one role, exactly the way the app does: Auth.state.role. */
function asRole(role) {
  Auth.state.role = role;
  Auth.state.user = role ? { role: role } : null;
  Auth.state.capabilitiesLoaded = false;
  return JONE.dashboard;
}

const OPERATIONAL = Array.from(Auth.OPERATIONAL_ROLES);

/* Legacy rank ladder used by JONE.dashboard.boot("receptionist") — mirrors
   js/auth.js hasRole(). Department roles sit at -1 and only match when named. */
const RANK = {
  guest: 0,
  receptionist: 1, front_desk_supervisor: 1, staff: 1,
  manager: 2, general_manager: 2, admin: 3,
};
function rolesAbove(minRole) {
  const floor = RANK[minRole];
  return OPERATIONAL.filter((role) => (RANK[role] != null ? RANK[role] : -1) >= floor);
}

/* Every dashboard page, with the gate it declares for itself. */
function readPages() {
  const out = [];
  for (const file of fs.readdirSync(DASHBOARD_DIR).filter((f) => f.endsWith(".html")).sort()) {
    const source = fs.readFileSync(path.join(DASHBOARD_DIR, file), "utf-8");
    const match = source.match(/JONE\.dashboard\.boot\s*\(\s*([\s\S]*?)\s*\)\s*;/);
    out.push({
      file,
      source,
      arg: match ? match[1].trim() : null,
      roles: match && match[1].trim().startsWith("[")
        ? (match[1].match(/"([a-z0-9_]+)"/g) || []).map((r) => r.replace(/"/g, ""))
        : null,
      minRole: match && !match[1].trim().startsWith("[") && !/OPERATIONAL_ROLES/.test(match[1])
        ? (match[1].match(/"([a-z0-9_]+)"/) || [])[1]
        : null,
      everyone: !!(match && /OPERATIONAL_ROLES/.test(match[1])),
    });
  }
  return out;
}

const PAGES = readPages();
const pageByName = (name) => PAGES.filter((p) => p.file === name)[0];

function registryRoles(name) {
  const rule = PAGE_ACCESS[name];
  if (!rule || rule.public || rule.everyone) return null;
  return rule.roles ? rule.roles.slice().sort() : [];
}

/* ------------------------- the table grades every page ------------------- */

test("every dashboard page is graded by the access registry", () => {
  const ungraded = [];
  for (const page of PAGES) {
    assert.ok(page.arg !== null, `${page.file} must boot through JONE.dashboard.boot(...)`);
    if (!Object.prototype.hasOwnProperty.call(PAGE_ACCESS, page.file)) ungraded.push(page.file);
  }
  assert.deepEqual(ungraded, [],
    "these pages are in neither the registry nor the shell: " + ungraded.join(", "));
  assert.equal(PAGES.length, 43, "the dashboard has 43 pages; a new page needs a gate AND a registry entry");
});

test("the registry holds no stale page names and no empty rule", () => {
  for (const [name, rule] of Object.entries(PAGE_ACCESS)) {
    assert.ok(rule && typeof rule === "object", `${name} must have a rule object`);
    const kind = rule.public ? "public" : rule.everyone ? "everyone" : "roles";
    if (kind === "roles") {
      assert.ok(Array.isArray(rule.roles) && rule.roles.length > 0, `${name} must list at least one role`);
      for (const role of rule.roles) {
        assert.ok(OPERATIONAL.includes(role), `${name} names an unknown role: ${role}`);
      }
    }
    if (name.endsWith(".html") && !fs.existsSync(path.join(DASHBOARD_DIR, name))) {
      /* Only the two public site pages may live outside the dashboard folder. */
      assert.equal(kind, "public", `${name} is in the registry but is not a dashboard page`);
    }
    if (fs.existsSync(path.join(DASHBOARD_DIR, name))) {
      assert.notEqual(kind, "public", `${name} is a staff page and cannot be public`);
    }
  }
});

/* ------------- exact-role pages and the table must be identical ----------- */

test("a page that names its roles and the registry agree in both directions", () => {
  const problems = [];
  for (const page of PAGES) {
    if (!page.roles) continue;
    const registry = registryRoles(page.file);
    assert.ok(registry, `${page.file} names roles, so the registry must too`);
    const pageSet = page.roles.slice().sort();
    if (JSON.stringify(pageSet) !== JSON.stringify(registry)) {
      problems.push(`${page.file}\n    page:     [${pageSet.join(", ")}]\n    registry: [${registry.join(", ")}]`);
    }
  }
  assert.deepEqual(problems, [],
    "an exact-role page and PAGE_ACCESS drifted apart:\n  " + problems.join("\n  "));
});

test("a hierarchy-gated page never gets a role its own gate would refuse", () => {
  for (const page of PAGES) {
    if (!page.minRole) continue;
    const registry = registryRoles(page.file);
    assert.ok(registry, `${page.file} must be graded`);
    const allowed = rolesAbove(page.minRole);
    for (const role of registry) {
      assert.ok(allowed.includes(role),
        `${page.file} boots for "${page.minRole}"+ but the registry promises it to ${role}`);
    }
  }
});

test("personal surfaces use the one \"any staff member\" marker", () => {
  for (const page of PAGES.filter((p) => p.everyone)) {
    assert.equal(PAGE_ACCESS[page.file].everyone, true,
      `${page.file} boots for all staff, so the registry must say everyone: true`);
    assert.match(page.source, /boot\(window\.Auth \? window\.Auth\.OPERATIONAL_ROLES : null\)/,
      `${page.file} must boot from Auth.OPERATIONAL_ROLES, never a hand-written list`);
  }
  const everyone = Object.entries(PAGE_ACCESS).filter(([, rule]) => rule.everyone).map(([name]) => name).sort();
  assert.deepEqual(everyone, ["notification-details.html", "notifications.html", "profile.html"],
    "the personal surfaces are notifications, notification-details and profile");
  assert.ok(!OPERATIONAL.includes("guest"), "guests are never an operational role");
});

/* ---------------------- every link target is classified ------------------ */

test("every link target in the dashboard is classified", () => {
  const missing = {};
  for (const page of PAGES) {
    const hrefs = page.source.match(/href="([^"]*)"/g) || [];
    for (const raw of hrefs) {
      const head = raw.replace(/^href="/, "").replace(/"$/, "");
      /* The value may be a plain path, a path with ?/#, or the head of a string the page
         concatenates at runtime (booking-details.html?ref=' + encodeURIComponent(ref)).
         Only the leading path token identifies the target. */
      const token = head.match(/^[A-Za-z0-9._-]+\.html/);
      if (!token) continue;   /* an external URL, an anchor, mailto: … */
      const bare = token[0];
      if (!Object.prototype.hasOwnProperty.call(PAGE_ACCESS, bare)) {
        (missing[bare] = missing[bare] || []).push(page.file);
      }
    }
  }
  const report = Object.entries(missing).map(([t, from]) => `${t} (linked from ${from.join(", ")})`);
  assert.deepEqual(report, [],
    "an unclassified target fails CLOSED, so the link would vanish for everybody:\n  " + report.join("\n  "));
});

/* --------------------- the menus obey the same table -------------------- */

test("the sidebar and bottom bar carry no role list of their own", () => {
  const overrides = [];
  for (const group of JONE.dashboard.NAV) {
    for (const item of group.items) if (item.roles) overrides.push(item.href);
  }
  assert.deepEqual(overrides, [],
    "a menu entry with its own roles list can drift from the page it points at: " + overrides.join(", "));
  const bottom = /const BOTTOM_NAV = \[([\s\S]*?)\n  \];/.exec(
    fs.readFileSync(path.join(FRONTEND, "js", "dashboard.js"), "utf-8"))[1];
  assert.ok(!/\broles:/.test(bottom), "BOTTOM_NAV must not carry role lists either");
});

test("every menu entry names a classified page and passes the registry", () => {
  const items = [];
  for (const group of JONE.dashboard.NAV) items.push(...group.items);
  const bottom = /const BOTTOM_NAV = \[([\s\S]*?)\n  \];/.exec(
    fs.readFileSync(path.join(FRONTEND, "js", "dashboard.js"), "utf-8"))[1];
  for (const href of bottom.match(/href: "([^"]+)"/g) || []) {
    items.push({ href: href.replace(/href: "/, "").replace(/"$/, "") });
  }
  for (const item of items) {
    assert.ok(PAGE_ACCESS[item.href], `menu entry ${item.href} is not in the registry`);
  }
  /* The menu can never be wider than the pages: for every department, an entry
     survives only if the registry admits that department to its target. */
  const menu = JONE.dashboard.NAV.flatMap((g) => g.items.map((i) => i.href));
  for (const role of OPERATIONAL) {
    asRole(role);
    for (const href of menu) {
      if (JONE.dashboard.navItemAllowed({ href })) {
        assert.ok(JONE.dashboard.canOpenPage(href),
          `${role} would be shown a sidebar link the page itself refuses: ${href}`);
      }
    }
  }
  /* And the registry is the promise the sidebar keeps: staff never see the
     admin-only screens, guests see nothing at all. */
  assert.equal(asRole("housekeeper").canOpenPage("settings.html"), false);
  assert.equal(asRole("housekeeper").canOpenPage("audit-logs.html"), false);
  assert.equal(asRole("guest").canOpenPage("index.html"), false);
  assert.equal(asRole(null).canOpenPage("index.html"), false);
});

/* --------------------------- resolved behaviour ------------------------- */

test("canOpenPage resolves a page from a bare name, an href or a path", () => {
  const d = asRole("receptionist");
  assert.equal(d.canOpenPage("check-out.html"), true);
  assert.equal(d.canOpenPage("./check-out.html?room=12"), true);
  assert.equal(d.canOpenPage("../dashboard/check-out.html#folio"), true);
  assert.equal(d.canOpenPage("/dashboard/check-out.html"), true);
  assert.equal(d.pageAccessFor("check-out.html").roles.includes("receptionist"), true);
});

test("an unknown page fails closed, for every role", () => {
  for (const role of OPERATIONAL.concat(["guest"])) {
    const d = asRole(role);
    assert.equal(d.canOpenPage("brand-new-page.html"), false, `${role} must not open an unclassified page`);
    assert.equal(d.canOpenPage(""), false);
    assert.equal(d.canOpenPage(null), false);
assert.equal(d.canOpenPage(undefined), false);
  }
  /* No Auth at all (signed out): nothing is open. */
  const saved = ctx.window.Auth;
  ctx.window.Auth = undefined;
  assert.equal(JONE.dashboard.canOpenPage("index.html"), false);
  ctx.window.Auth = saved;
});

test("each department is admitted to its own screens and refused the others", () => {
  const expectations = {
    /* role:      [allowed...]                                        [refused...] */
    housekeeper: [["laundry.html", "workforce.html", "housekeeping.html", "service-requests.html", "profile.html", "notifications.html"],
      ["payments.html", "accounting.html", "payroll.html", "pos.html", "check-out.html", "bookings.html", "settings.html", "audit-logs.html", "inventory.html", "terminals.html", "staff.html"]],
    housekeeping_manager: [["laundry.html", "housekeeping.html", "workforce.html"],
      ["payments.html", "pos.html", "check-out.html", "settings.html"]],
    receptionist: [["index.html", "check-out.html", "bookings.html", "guests.html", "payments.html", "receipts.html", "workforce.html", "laundry.html"],
      ["pos.html", "accounting.html", "payroll.html", "settings.html", "audit-logs.html", "reviews.html", "staff.html", "terminals.html", "inventory.html"]],
    front_desk_supervisor: [["check-out.html", "payments.html", "workforce.html", "approvals.html"],
      ["settings.html", "audit-logs.html", "payroll.html", "pos.html"]],
    waiter: [["pos.html", "barcode-labels.html", "service-requests.html", "workforce.html"],
      ["laundry.html", "payments.html", "check-out.html", "housekeeping.html", "maintenance.html", "inventory.html", "accounting.html", "settings.html"]],
    bartender: [["pos.html", "service-requests.html", "workforce.html"],
      ["laundry.html", "barcode-labels.html", "payments.html", "check-out.html", "housekeeping.html", "maintenance.html", "inventory.html", "accounting.html", "settings.html"]],
    chef: [["pos.html", "workforce.html"],
      ["payments.html", "check-out.html", "bookings.html", "housekeeping.html", "settings.html"]],
    inventory_clerk: [["inventory.html", "procurement.html", "barcode-labels.html", "workforce.html"],
      ["pos.html", "payments.html", "check-out.html", "housekeeping.html", "settings.html"]],
    storekeeper: [["inventory.html", "procurement.html", "barcode-labels.html"],
      ["pos.html", "payments.html", "bookings.html", "settings.html"]],
    maintenance_technician: [["maintenance.html", "laundry.html", "service-requests.html", "workforce.html"],
      ["housekeeping.html", "inventory.html", "payments.html", "pos.html", "settings.html"]],
    accountant: [["payments.html", "receipts.html", "accounting.html", "payroll.html", "approvals.html", "workforce.html"],
      ["check-out.html", "bookings.html", "guests.html", "pos.html", "housekeeping.html", "laundry.html", "settings.html", "audit-logs.html", "inventory.html"]],
    accounts_manager: [["payments.html", "accounting.html", "payroll.html", "workforce.html"],
      ["check-out.html", "bookings.html", "pos.html", "barcode-labels.html", "settings.html", "audit-logs.html", "inventory.html"]],
    hr_manager: [["payroll.html", "approvals.html", "staff-profile.html", "workforce.html"],
      ["payments.html", "check-out.html", "pos.html", "inventory.html", "staff.html", "settings.html", "audit-logs.html"]],
    /* The HR department's ONE standard role (one role per department): the same
       people surfaces as the retired "HR manager" title, because the work is the
       work — and still none of the money or payroll-approval pages. */
    hr_officer: [["payroll.html", "approvals.html", "staff-profile.html", "workforce.html"],
      ["payments.html", "check-out.html", "pos.html", "inventory.html", "staff.html", "settings.html", "audit-logs.html"]],
    security: [["service-requests.html", "laundry.html", "workforce.html"],
      ["bookings.html", "check-out.html", "guests.html", "payments.html", "pos.html", "settings.html"]],
    porter: [["laundry.html", "service-requests.html", "workforce.html"],
      ["payments.html", "check-out.html", "pos.html", "inventory.html", "settings.html"]],
    /* A plain "staff" account is the generic front-desk role. */
    staff: [["index.html", "bookings.html", "guests.html", "availability.html", "enquiries.html", "profile.html"],
      ["payments.html", "accounting.html", "settings.html", "audit-logs.html", "terminals.html", "staff.html", "pos.html", "inventory.html"]],
    /* Management sees the whole shell, but still not a page it has no business
       in: the public site pages are the only thing outside the table. */
    manager: [["index.html", "bookings.html", "check-out.html", "payments.html", "accounting.html", "reports.html", "approvals.html", "staff-profile.html", "terminals.html", "workforce.html", "laundry.html", "pos.html", "inventory.html"],
      ["settings.html", "audit-logs.html", "audit-log-details.html", "reviews.html", "staff.html"]],
    general_manager: [["index.html", "reports.html", "accounting.html", "staff-profile.html", "terminals.html", "service-qr.html"],
      ["settings.html", "audit-logs.html", "reviews.html", "staff.html"]],
    admin: [["index.html", "settings.html", "audit-logs.html", "audit-log-details.html", "reviews.html", "staff.html", "payroll.html", "everything-else-listed-below"],
      []],
    guest: [[], ["index.html", "bookings.html", "check-out.html", "payments.html", "pos.html", "workforce.html", "profile.html", "notifications.html"]],
  };
  delete expectations.admin;   /* asserted separately, it may open every page */

  const mismatches = [];
  for (const [role, [allowed, refused]] of Object.entries(expectations)) {
    const d = asRole(role);
    for (const href of allowed) {
      if (!d.canOpenPage(href)) mismatches.push(`${role} should be able to open ${href}`);
    }
    for (const href of refused) {
      if (d.canOpenPage(href)) mismatches.push(`${role} must NOT be able to open ${href}`);
    }
  }
  assert.deepEqual(mismatches, [], "department access drifted from the department rule:\n  " + mismatches.join("\n  "));

  /* The administrator is the only role that may open every classified staff
     page — that is exactly what makes the other lists meaningful. */
  const d = asRole("admin");
  const shutForAdmin = Object.entries(PAGE_ACCESS)
    .filter(([name, rule]) => !rule.public && name !== "403.html" && !/operational/i.test(name))
    .filter(([name]) => !d.canOpenPage(name))
    .map(([name]) => name);
  assert.deepEqual(shutForAdmin, [], "the administrator must be able to open every staff page");
});

test("the public exit links stay reachable from inside the shell", () => {
  const d = asRole("receptionist");
  assert.equal(d.canOpenPage("../login.html"), true, "the 403 page's Sign out link");
  assert.equal(PAGE_ACCESS["policies.html"].public, true, "settings links the public policies page");
  assert.match(fs.readFileSync(path.join(DASHBOARD_DIR, "403.html"), "utf-8"), /href="\.\.\/login\.html"/);
  assert.match(fs.readFileSync(path.join(DASHBOARD_DIR, "settings.html"), "utf-8"), /href="\.\.\/policies\.html"/);
});

/* ------------- the gates inside pages must match the BACKEND ------------- */

/* ROLE_CAPABILITY_CODES (backend/apps/accounts/capabilities.py) is the
   authority for what a department may DO. A hand-written frontend role list
   silently contradicts it in both directions: it can offer a control the
   server refuses, or — the failure found in this audit — refuse a department a
   control the server grants it, because the list forgot the management tier
   (a general manager holds exactly the same capabilities as a manager, and a
   front desk supervisor everything a receptionist holds and one more). */
function backendMatrix() {
  const file = path.join(FRONTEND, "..", "backend", "apps", "accounts", "capabilities.py");
  assert.ok(fs.existsSync(file), "the backend capability matrix must be present to audit the gates");
  const source = fs.readFileSync(file, "utf-8");
  const start = source.indexOf("ROLE_CAPABILITY_CODES = {");
  assert.ok(start > -1, "ROLE_CAPABILITY_CODES is missing from capabilities.py");
  const open = source.indexOf("{", start);
  let depth = 0, close = open;
  for (let i = open; i < source.length; i++) {
    if (source[i] === "{") depth++;
    else if (source[i] === "}") { depth--; if (!depth) { close = i + 1; break; } }
  }
  const body = source.slice(open, close);
  const matrix = {};
  const entry = /"([A-Z_]+)":\s*(ALL_CAPABILITY_CODES|frozenset\(\{([^}]*)\}\))/g;
  let m;
  while ((m = entry.exec(body))) {
    matrix[m[1]] = m[2] === "ALL_CAPABILITY_CODES"
      ? { all: true, codes: [] }
      : { all: false, codes: (m[3].match(/"([^"]+)"/g) || []).map((c) => c.replace(/"/g, "")) };
  }
  assert.ok(Object.keys(matrix).length >= 20, "the capability matrix parsed as empty");
  return matrix;
}

const MATRIX = backendMatrix();
const upper = (role) => role.toUpperCase();
const capabilityHolders = (capability) => OPERATIONAL.filter((role) => {
  const entry = MATRIX[upper(role)];
  return entry && (entry.all || entry.codes.includes(capability));
});
const capsOf = (role) => {
  const entry = MATRIX[upper(role)];
  return entry && entry.all ? ALL_CODES() : new Set((entry && entry.codes) || []);
};
function ALL_CODES() {
  const all = new Set();
  for (const entry of Object.values(MATRIX)) entry.codes.forEach((c) => all.add(c));
  return all;
}
/* The role list a page's inline gate falls back to before the capability read
   has landed — read straight out of the shipped page. */
function fallbackList(file, marker) {
  const source = pageByName(file.split("/").pop()).source;
  const at = source.indexOf(marker);
  assert.ok(at > -1, `${file} no longer contains ${marker}`);
  const m = source.slice(at).match(/hasAnyRole\(\[([^\]]*)\]\)/);
  assert.ok(m, `${file}: ${marker} has no role fallback`);
  return (m[1].match(/"([a-z0-9_]+)"/g) || []).map((r) => r.replace(/"/g, ""));
}

test("every in-page gate is the capability it implements, in both directions", () => {
  const audit = [
    { file: "dashboard/inventory.html", marker: "hasCapability(\"inventory.adjust.approve\")", capability: "inventory.adjust.approve" },
    { file: "dashboard/procurement.html", marker: "hasCapability(\"procurement.approve\")", capability: "procurement.approve" },
    { file: "dashboard/housekeeping.html", marker: "hasCapability(\"housekeeping.task.assign\")", capability: "housekeeping.task.assign" },
    { file: "dashboard/maintenance.html", marker: "hasCapability(\"maintenance.work_order.assign\")", capability: "maintenance.work_order.assign" },
    { file: "dashboard/service-requests.html", marker: "hasCapability(\"guest_request.assign\")", capability: "guest_request.assign" },
  ];
  const problems = [];
  for (const item of audit) {
    const audience = OPERATIONAL.filter((role) => { asRole(role); return JONE.dashboard.canOpenPage(item.file.split("/").pop()); });
    const expected = capabilityHolders(item.capability).filter((r) => audience.includes(r)).sort();
    const actual = fallbackList(item.file, item.marker).sort();
    if (JSON.stringify(expected) !== JSON.stringify(actual)) {
      problems.push(`${item.file} (${item.capability})\n    expected: [${expected.join(", ")}]\n    shipped:  [${actual.join(", ")}]`);
    }
  }
  assert.deepEqual(problems, [],
    "an in-page gate disagrees with the backend capability matrix:\n  " + problems.join("\n  "));
});

test("the POS-draft gate is the composite capability the server enforces", () => {
  const audience = OPERATIONAL.filter((role) => { asRole(role); return JONE.dashboard.canOpenPage("service-requests.html"); });
  const canDraft = OPERATIONAL.filter((role) =>
    capabilityHolders("guest_request.manage").includes(role) &&
    (capabilityHolders("restaurant.order.manage").includes(role) ||
      capabilityHolders("bar.order.manage").includes(role)));
  const expected = canDraft.filter((r) => audience.includes(r)).sort();
  const shipped = fallbackList("dashboard/service-requests.html", 'hasCapability("guest_request.manage")').sort();
  assert.deepEqual(Array.from(shipped), Array.from(expected),
    "the POS-draft fallback must be exactly the roles that hold guest_request.manage AND a food/bar order capability");
});

test("no frontend gate forgets the management or senior tier of a named role", () => {
  /* Equality of capability sets is what makes these implications true. If the
     matrix ever separates them, this test fails and tells you which pages to
     revisit — the whole defect class was "a listing that forgot the tier". */
  const pairs = [
    ["manager", "general_manager"],
    ["receptionist", "front_desk_supervisor"],
    ["housekeeper", "housekeeping_manager"],
  ];
  const missing = [];
  for (const [junior, senior] of pairs) {
    const a = capsOf(junior), b = capsOf(senior);
    const superset = [...a].every((code) => b.has(code));
    assert.ok(superset, `${senior} no longer holds everything ${junior} holds — re-derive the page gates`);
    const files = fs.readdirSync(DASHBOARD_DIR).filter((f) => f.endsWith(".html"))
      .concat(["../js/dashboard.js"]);
    for (const file of files) {
      const source = fs.readFileSync(path.join(DASHBOARD_DIR, file), "utf-8");
      for (const list of source.match(/hasAnyRole\(\[[^\]]*\]\)/g) || []) {
        const roles = (list.match(/"([a-z0-9_]+)"/g) || []).map((r) => r.replace(/"/g, ""));
        if (roles.includes(junior) && !roles.includes(senior)) {
          missing.push(`${file}: [${roles.join(", ")}] names ${junior} but not ${senior}`);
        }
      }
    }
  }
  assert.deepEqual(missing, [], "a gate denies the senior tier:\n  " + missing.join("\n  "));
});

test("a page's audience is closed under its senior tier", () => {
  /* The rule that caught the real gaps: whoever holds a superset of an admitted
     role's capabilities must be admitted too — otherwise the front desk
     supervisor cannot open the housekeeping board they are allowed to dispatch
     on, and the general manager cannot open the maintenance board, because the
     page's own gate forgot them while the server granted them everything the
     admitted role has. */
  const pairs = [["manager", "general_manager"], ["receptionist", "front_desk_supervisor"],
    ["housekeeper", "housekeeping_manager"]];
  const problems = [];
  for (const [name, rule] of Object.entries(PAGE_ACCESS)) {
    if (!rule.roles) continue;
    for (const [junior, senior] of pairs) {
      if (rule.roles.includes(junior) && !rule.roles.includes(senior)) {
        problems.push(`${name} admits ${junior} but not ${senior}`);
      }
    }
  }
  assert.deepEqual(problems, [],
    "a page gate omits the senior tier of a role it admits:\n  " + problems.join("\n  "));
});

test("workforce planning is the management tier, not two of its three roles", () => {
  const workforce = pageByName("workforce.html").source;
  assert.match(workforce, /isManager=[\s\S]{0,140}\["admin","manager","general_manager"\]/,
    "shift/profile planning must include the general manager");
  assert.match(workforce, /if\(\(tab==="shifts"\|\|tab==="profiles"\)&&!isManager\)return;/);
});

/* ----------------- the in-page surfaces that used to leak --------------- */

test("the dashboard quick actions are built from the registry", () => {
  const page = pageByName("index.html").source;
  assert.match(page, /<div class="quick-actions" data-quick-actions><\/div>/,
    "the quick-action row is empty in the markup — nothing is hard-coded");
  const markup = page.split(/<script/)[0];
  assert.ok(!/class="quick-action"/.test(markup),
    "and no quick-action anchor is baked into the markup either");
  assert.match(page, /var allowed = links\.filter\(function\(link\)\{/, "the allowed set comes from the registry");
  assert.match(page, /JONE\.dashboard\.canOpenPage\(link\.href\)/);
  assert.match(page, /data-icon/);
});

test("the staff-profile and workforce cross-links are gated", () => {
  const profile = pageByName("profile.html").source;
  assert.match(profile, /data-ss-workforce-link/, "the leave tab's link is marked");
  assert.match(profile, /canOpenPage\("workforce\.html"\)/, "and resolved through the registry");

  /* The HR/management employment-profile page used to be unreachable: the only
     entry that led towards staff data was the admin-only staff directory. */
  assert.equal(PAGE_ACCESS["staff-profile.html"].roles.includes("hr_manager"), true);
  const nav = JONE.dashboard.NAV.flatMap((g) => g.items.map((i) => i.href));
  assert.ok(nav.includes("staff-profile.html"), "Staff Profiles is a sidebar entry");
  assert.ok(nav.includes("staff.html"), "and the admin staff directory keeps its own entry");
  assert.equal(asRole("hr_manager").canOpenPage("staff-profile.html"), true);
  assert.equal(asRole("hr_manager").canOpenPage("staff.html"), false, "which HR is not promised");
});

test("the Reception Biometric Terminal is front desk + management only", () => {
  const workforce = pageByName("workforce.html").source;
  /* The page itself admits every operational role, so the tab is gated in the
     page: the registry alone cannot express a per-tab rule. */
  assert.equal(PAGE_ACCESS["workforce.html"].roles.length, OPERATIONAL.length,
    "workforce.html is the personal attendance console for every department");
  const tab = workforce.match(/<button[^>]*data-wf-tab="reception-terminal"[^>]*>/)[0];
  assert.match(tab, /\bhidden\b/, "the terminal tab starts hidden");
  assert.match(tab, /data-wf-desk-terminal/, "and carries the marker the gate resolves");
  assert.match(workforce,
    /deskTerminal=[\s\S]{0,120}\["admin","manager","general_manager","receptionist","front_desk_supervisor"\]/,
    "only management and the front desk may see it");
  assert.match(workforce, /document\.querySelectorAll\("\[data-wf-desk-terminal\]"\)\.forEach\(function\(el\)\{el\.hidden=!deskTerminal;\}\)/);
  assert.match(workforce, /if\(tab==="reception-terminal"&&!deskTerminal\)return;/,
    "activating the panel by any other route is refused too");
});

test("the POS and booking links inside other pages respect the registry", () => {
  const pos = pageByName("pos.html").source;
  assert.match(pos, /data-pos-barcode-link hidden/, "the barcode link starts hidden");
  assert.match(pos, /canOpenPage\("barcode-labels\.html"\)/);

  const requests = pageByName("service-requests.html").source;
  assert.match(requests, /canOpenPage\("pos\.html"\)/, "the POS draft needs the POS page");
  assert.match(requests, /JONE\.ui\.toast\("Your department cannot open the POS screen\.","error"\)/);

  for (const file of ["payments.html", "receipts.html", "receipt-details.html"]) {
    const source = pageByName(file).source;
    assert.match(source, /canOpenPage\("booking-details\.html"\)/,
      `${file} must resolve booking links through the registry`);
  }
});

test("every cross-department link resolves its target through the registry", () => {
  /* A link from one department's page to another page is only safe while it is
     resolved through canOpenPage: the reader's own department decides whether
     they see it. Each pair below is therefore required to carry that call. */
  const pairs = [
    ["profile.html", "workforce.html"],
    ["pos.html", "barcode-labels.html"],
    ["service-requests.html", "pos.html"],
    ["payments.html", "booking-details.html"],
    ["receipts.html", "booking-details.html"],
    ["receipt-details.html", "booking-details.html"],
  ];
  const missing = [];
  for (const [from, to] of pairs) {
    const source = pageByName(from).source;
    if (!source.includes(`canOpenPage("${to}")`)) missing.push(`${from} → ${to}`);
  }
  assert.deepEqual(missing, [], "these links are shown without asking the registry: " + missing.join(", "));

  /* And the resolved check the pages perform really is the registry's answer:
     a stores clerk may open the label page from the sidebar while the POS menu
     link inside pos.html never appears — pos.html itself is closed to them. */
  assert.equal(asRole("inventory_clerk").canOpenPage("barcode-labels.html"), true);
  assert.equal(asRole("inventory_clerk").canOpenPage("pos.html"), false);
  assert.equal(asRole("waiter").canOpenPage("pos.html"), true);
  assert.equal(asRole("waiter").canOpenPage("check-out.html"), false);
});

test("Laundry stays available to its own staff but is no longer a Restaurants link", () => {
  const laundryItem = JONE.dashboard.NAV.flatMap((group) => group.items)
    .find((item) => item.href === "laundry.html");
  assert.ok(laundryItem, "Laundry remains a real navigation entry for authorized staff");
  for (const role of ["waiter", "bartender"]) {
    asRole(role);
    Auth.state.capabilitiesLoaded = true;
    Auth.state.capabilities = ["guest_request.manage", "guest_request.assign"];
    assert.equal(JONE.dashboard.canOpenPage("laundry.html"), false, `${role} must not open Laundry`);
    assert.equal(JONE.dashboard.navItemAllowed(laundryItem), false, `${role} must not be shown the Laundry link`);
    assert.equal(JONE.dashboard.canOpenPage("service-requests.html"), true, `${role} keeps Guest Requests`);
    assert.equal(JONE.dashboard.canOpenPage("pos.html"), true, `${role} keeps the POS workspace`);
  }
  for (const role of ["receptionist", "housekeeper", "porter", "security"]) {
    asRole(role);
    assert.equal(JONE.dashboard.canOpenPage("laundry.html"), true, `${role} keeps authorized Laundry access`);
  }
});
