/* tests-js/test-theme-prepaint.js */
/* Dark-mode navigation flash regression: the stored theme must be applied
   BEFORE any content paints — a dark-mode session must never paint light. */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { makeContext, loadScript, disposeAll, FRONTEND } = require("./harness");

test.after(() => disposeAll());

function walk(dir, acc) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (["tests-js", "tests-pwa", "node_modules"].includes(entry.name)) continue;
      walk(full, acc);
    } else if (entry.name.endsWith(".html")) {
      acc.push(full);
    }
  }
  return acc;
}

const PAGES = walk(FRONTEND, []);
const SNIPPET_MARKER = "Pre-paint theme";

test("every page applies the theme in an inline head script before stylesheets load", () => {
  const offenders = [];
  for (const page of PAGES) {
    const html = fs.readFileSync(page, "utf8");
    const headEnd = html.indexOf("</head>");
    const snippetAt = html.indexOf(SNIPPET_MARKER);
    const firstCss = html.indexOf('rel="stylesheet"');
    if (headEnd === -1) continue; // component fragments have no <head>
    if (snippetAt === -1 || snippetAt > headEnd) {
      offenders.push(page + " (missing pre-paint theme script)");
    } else if (firstCss !== -1 && snippetAt > firstCss) {
      offenders.push(page + " (theme script must precede stylesheets)");
    }
  }
  assert.deepEqual(offenders, []);
});

test("the pre-paint script sets a stored dark theme with no theme.js involved", () => {
  const page = PAGES.find((p) => p.endsWith(path.join("dashboard", "pos.html")));
  const html = fs.readFileSync(page, "utf8");
  const match = html.match(/<script>\s*\/\* Pre-paint theme([\s\S]*?)<\/script>/);
  assert.ok(match, "pre-paint script extractable for execution");
  const snippetJs = match[0].replace(/^<script>/, "").replace(/<\/script>$/, "");

  // Dark preference stored the way JONE.storage stores it (JSON-encoded)…
  let ctx = makeContext();
  ctx.window.localStorage.setItem("jone.theme", JSON.stringify("dark"));
  vm.runInContext(snippetJs, ctx);
  assert.equal(ctx.window.document.documentElement.attributes["data-theme"], "dark");

  // …and a raw (non-JSON) value also works.
  ctx = makeContext();
  ctx.window.localStorage.setItem("jone.theme", "dark");
  vm.runInContext(snippetJs, ctx);
  assert.equal(ctx.window.document.documentElement.attributes["data-theme"], "dark");

  // Explicit light preference keeps light.
  ctx = makeContext();
  ctx.window.localStorage.setItem("jone.theme", JSON.stringify("light"));
  vm.runInContext(snippetJs, ctx);
  assert.equal(ctx.window.document.documentElement.attributes["data-theme"], "light");
});

test("theme.js and the pre-paint script agree on the storage key", () => {
  const themeJs = fs.readFileSync(path.join(FRONTEND, "js", "theme.js"), "utf8");
  const configJs = fs.readFileSync(path.join(FRONTEND, "js", "config.js"), "utf8");
  const page = fs.readFileSync(PAGES[0], "utf8");
  assert.match(configJs, /THEME:\s*"jone\.theme"/);
  assert.match(page, /var k="jone\.theme"/);
  assert.match(themeJs, /jone\.theme/);
});

test("navigating with a dark preference never leaves data-theme=light after init", () => {
  const ctx = makeContext();
  loadScript(ctx, "js/config.js");
  loadScript(ctx, "js/utils.js");
  loadScript(ctx, "js/theme.js");
  ctx.window.JONE.storage.set("jone.theme", "dark");
  ctx.window.JONE.theme.init();
  assert.equal(ctx.window.document.documentElement.attributes["data-theme"], "dark");
  assert.ok(!ctx.window.document.documentElement.classList.contains("preload-theme"));
});
