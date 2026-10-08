/* Dashboard async UX: skeletons retain layout; errors/empty states replace them. */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { makeContext, loadScript, disposeAll } = require("./harness");

test.after(() => disposeAll());

function dashboard() {
  const ctx = makeContext();
  loadScript(ctx, "js/config.js");
  loadScript(ctx, "js/utils.js");
  loadScript(ctx, "js/dashboard.js");
  return ctx.window.JONE.dashboard;
}

test("table loading uses column-shaped skeleton rows instead of a spinner", () => {
  const DATA = dashboard().DATA;
  const table = { querySelector: () => ({ children: new Array(4) }) };
  const body = {
    tagName: "TBODY",
    innerHTML: "",
    closest: () => table,
  };

  DATA.loading(body);

  assert.match(body.innerHTML, /table-skeleton-row/);
  assert.match(body.innerHTML, /colspan="4"/);
  assert.doesNotMatch(body.innerHTML, /spinner/);
  assert.match(body.innerHTML, /role="status"/);
});

test("generic loading uses accessible skeleton lines instead of a spinner", () => {
  const DATA = dashboard().DATA;
  const target = { tagName: "DIV", innerHTML: "" };

  DATA.loading(target, "list");

  assert.match(target.innerHTML, /loading-skeleton/);
  assert.match(target.innerHTML, /skeleton-line/);
  assert.doesNotMatch(target.innerHTML, /spinner/);
  assert.match(target.innerHTML, /aria-live="polite"/);
});

test("legacy loading blocks are visually converted to skeletons", () => {
  const css = fs.readFileSync(path.join(__dirname, "..", "css", "components.css"), "utf8");

  assert.match(css, /\.loading-block\s*\{/);
  assert.match(css, /\.loading-block::before[\s\S]*?\.loading-block::after/);
  assert.match(css, /\.loading-block \.spinner\s*\{/);
  assert.match(css, /clip:\s*rect\(0, 0, 0, 0\)/);
});
