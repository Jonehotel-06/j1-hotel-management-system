/* System-wide table layout audit (§11 of the repair brief).
   Run from /frontend: node --test tests-js/test-table-layout-audit.js

   The wrapping defect was fixed ONCE, in the shared table system:
   `css/dashboard.css` wraps prose between words and only opts atomic columns out
   with `.cell-nowrap`/`.cell-clamp`, while `js/table-fit.js` classifies columns
   from their headings and marks the atomic ones. These tests assert that every
   staff table page actually participates in that shared system, so a new page
   cannot silently reintroduce per-page overrides or a page-wide horizontal
   scroll. */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { FRONTEND } = require("./harness");

const DASHBOARD = path.join(FRONTEND, "dashboard");
const PAGES = fs.readdirSync(DASHBOARD).filter((name) => name.endsWith(".html")).sort();
const CSS = fs.readFileSync(path.join(FRONTEND, "css", "dashboard.css"), "utf8");
const TABLE_FIT = fs.readFileSync(path.join(FRONTEND, "js", "table-fit.js"), "utf8");

function tablesIn(html) {
  return (html.match(/<table\b/g) || []).length;
}

test("every page that renders a table loads the shared table-fit classifier", () => {
  const offenders = [];
  for (const name of PAGES) {
    const html = fs.readFileSync(path.join(DASHBOARD, name), "utf8");
    if (tablesIn(html) > 0 && !html.includes("js/table-fit.js")) offenders.push(name);
  }
  assert.deepEqual(offenders, [], "these pages render tables without the shared classifier");
});

test("every staff table scrolls inside a .table-wrap instead of the page", () => {
  const offenders = [];
  for (const name of PAGES) {
    const html = fs.readFileSync(path.join(DASHBOARD, name), "utf8");
    const markers = [...html.matchAll(/<table\b[^>]*>/g)];
    for (const marker of markers) {
      // The wrapper must open before the table and not be closed in between.
      const before = html.slice(0, marker.index);
      const lastWrap = before.lastIndexOf("table-wrap");
      const lastClose = before.lastIndexOf("</div>");
      if (lastWrap === -1 || lastWrap < lastClose) {
        offenders.push(`${name}:${marker.index}`);
      }
    }
  }
  assert.deepEqual(offenders, [], "these tables are not inside a .table-wrap");
});

test("the shared stylesheet wraps prose and only opts atomic cells out", () => {
  // Base rule: break between words, never mid-token.
  assert.match(CSS, /\.table td, \.table th \{ overflow-wrap: break-word; word-break: normal; \}/);
  // Opt-out is class-scoped, so it can never apply to a prose column.
  assert.match(CSS, /\.table td\.cell-nowrap, \.table th\.cell-nowrap \{\n  white-space: nowrap;/);
  assert.match(CSS, /\.table td\.cell-clamp \{/);
  // No rule anywhere turns nowrap on for every table cell.
  assert.doesNotMatch(CSS, /\.table (td|th)(, \.table (td|th))? \{[^}]*white-space: nowrap/);
  // The table container owns the horizontal scroll.
  assert.match(CSS, /\.table-wrap \{ position: relative; max-width: 100%; overflow-x: auto;/);
});

test("the page itself never scrolls sideways because of a table", () => {
  for (const name of ["audit-fixes.css", "dashboard.css", "responsive.css"]) {
    const css = fs.readFileSync(path.join(FRONTEND, "css", name), "utf8");
    // A page-wide overflow-x: auto on html/body would let a wide table push the
    // whole page sideways instead of scrolling inside its own container.
    assert.doesNotMatch(css, /(^|\})\s*(html|body)[^{]*\{[^}]*overflow-x:\s*auto/);
  }
});

test("atomic cells may still wrap their secondary caption line", () => {
  // A nowrap cell holds an atomic value; the caption under it (guest name, due
  // amount) is prose and must be allowed to wrap rather than force a wide column.
  assert.match(CSS, /\.table td\.cell-nowrap \.caption,[\s\S]{0,80}white-space: normal; overflow-wrap: break-word;/);
});

test("the classifier keeps references/dates/amounts atomic and names/notes wrapping", () => {
  const ctx = { window: {}, document: {} };
  const vm = require("node:vm");
  vm.runInNewContext(TABLE_FIT, ctx);
  const isAtomic = ctx.window.JONE_TABLE_FIT.isAtomicHeading;
  for (const heading of ["Reference", "Date", "Amount", "Qty", "Status", "Total", "Cost", "Stock value"]) {
    assert.equal(isAtomic(heading), true, `${heading} should stay on one line`);
  }
  for (const heading of ["Guest name", "Description", "Notes", "Reason", "Staff name", "Room type"]) {
    assert.equal(isAtomic(heading), false, `${heading} should be allowed to wrap`);
  }
});

test("the cells that already declare their own contract are never reclassified", () => {
  // `.cell-clamp` and `.cell-nowrap` are page-authored contracts, so the auto
  // classifier must leave them alone (otherwise a clamped prose cell would be
  // flipped back to nowrap and pushed off-screen).
  assert.match(TABLE_FIT, /var OWN_CONTRACT = \["cell-nowrap", "cell-clamp", "cell-guest"\]/);
  assert.match(TABLE_FIT, /if \(cell\.classList && cell\.classList\.contains\(OWN_CONTRACT\[i\]\)\) return true;/);
});

test("the new projection and expense tables participate in the shared system", () => {
  const inventory = fs.readFileSync(path.join(DASHBOARD, "inventory.html"), "utf8");
  const accounting = fs.readFileSync(path.join(DASHBOARD, "accounting.html"), "utf8");
  for (const html of [inventory, accounting]) {
    assert.ok(html.includes("js/table-fit.js"));
    assert.ok(html.includes('class="table-wrap'));
  }
  // Prose columns clamp, atomic ones stay on one line, numerics right-align.
  assert.match(inventory, /class="caption cell-clamp"/);
  assert.match(inventory, /class="num cell-nowrap"/);
  assert.match(accounting, /<th class="cell-clamp">Payee<\/th>/);
  assert.match(accounting, /<th class="num cell-nowrap">Amount<\/th>/);
});
