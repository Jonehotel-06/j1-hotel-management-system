/* tests-js/test-audit-topbar.js
   The Audit Logs page uses the shared dashboard shell, and the shared top bar
   cannot be painted transparent by a browser that supports backdrop-filter but
   not color-mix (the failure that made the bar look missing on this page, whose
   content sits flush under it).
   Run from /frontend: node --test tests-js/test-audit-topbar.js */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { FRONTEND } = require("./harness");

const DASH_CSS = fs.readFileSync(path.join(FRONTEND, "css", "dashboard.css"), "utf8");
const PUBLIC_CSS = fs.readFileSync(path.join(FRONTEND, "css", "public.css"), "utf8");
const OPERATIONS_CSS = fs.readFileSync(path.join(FRONTEND, "css", "operations.css"), "utf8");
const AUDIT = fs.readFileSync(path.join(FRONTEND, "dashboard", "audit-logs.html"), "utf8");

function stripComments(css) {
  return css.replace(/\/\*[\s\S]*?\*\//g, "");
}

function rule(css, selector) {
  const clean = stripComments(css);
  const start = clean.indexOf(selector);
  assert.notEqual(start, -1, `missing rule: ${selector}`);
  const end = clean.indexOf("}", start);
  return clean.slice(start, end);
}

test("the top bar declares an opaque background BEFORE the translucent one", () => {
  // The main rule (the @supports fallback further down has no color-mix()).
  const topbar = rule(DASH_CSS, ".dash-topbar {\n  display: flex");
  const opaque = topbar.indexOf("background: var(--color-surface);");
  const translucent = topbar.indexOf("color-mix(");
  assert.notEqual(opaque, -1, "an opaque fallback must exist");
  assert.notEqual(translucent, -1, "the translucent treatment stays for modern browsers");
  assert.ok(opaque < translucent,
    "the opaque declaration must come first: otherwise a browser without color-mix() " +
    "drops the translucent one and paints NO background at all");
  // The existing backdrop-filter fallback is untouched.
  assert.match(DASH_CSS, /@supports not \(\(backdrop-filter: blur\(10px\)\) or \(-webkit-backdrop-filter: blur\(10px\)\)\)/);
});

test("the public site header gets the same protection", () => {
  const header = rule(PUBLIC_CSS, ".site-header {");
  const opaque = header.indexOf("background: var(--color-background);");
  const translucent = header.indexOf("color-mix(");
  assert.notEqual(opaque, -1);
  assert.ok(opaque < translucent, "opaque fallback first");
});

test("no dashboard rule defines a colour-function border without an opaque fallback", () => {
  const rule_ = rule(OPERATIONS_CSS, "border: 1px solid var(--color-border);");
  assert.ok(rule_.includes("color-mix("), "the emphasis colour is still applied as an enhancement");
});

test("the audit page uses the shared shell contract every other page uses", () => {
  for (const hook of [
    'class="dash-topbar"', "dash-menu-toggle", "data-dash-title", "data-topbar-actions",
    "theme-toggle", "data-dash-nav", "data-dash-user", "data-logout",
  ]) {
    assert.ok(AUDIT.includes(hook), `audit-logs.html is missing ${hook}`);
  }
  assert.match(AUDIT, /<h1>Audit Logs<\/h1>/, "the page title is present");
  assert.match(AUDIT, /<p>Immutable record of staff actions<\/p>/, "the shared title block keeps its subtitle");
  assert.doesNotMatch(AUDIT, /<header[^>]*style=/, "no page-level inline styling of the top bar");
  assert.doesNotMatch(AUDIT, /dash-topbar[^>]*display\s*:\s*none/, "the bar is never hidden on this page");
  assert.ok(AUDIT.includes("js/dashboard.js"), "the shared shell script bootstraps the page");
});

test("the audit page loads the same shared stylesheet as the rest of the console", () => {
  const staff = fs.readFileSync(path.join(FRONTEND, "dashboard", "staff.html"), "utf8");
  const sheets = (html) => (html.match(/href="\.\.\/css\/[^"]+"/g) || []).join(",");
  assert.equal(sheets(AUDIT), sheets(staff), "identical stylesheet set to every other console page");
  assert.match(AUDIT, /css\/main\.css/);
});
