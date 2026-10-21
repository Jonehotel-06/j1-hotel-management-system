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

/* ---- The reported pages, audited by their own headings -------------------- */

/* Reported: "Tables data breaks to the next line" on housekeeping, laundry, the
   workstation registry, staff, POS/room service, approvals, workforce
   (schedules + leave), reports and inventory (hand-off, catalog, movement
   ledger, profitability). The shared classifier had no word for a reference
   ("Order", "Task", "Request", "Movement"), a moment ("Clocked In", "Planned
   start", "Incurred", "Submitted", "Timestamp") or a measure ("Worked", "Net",
   "Ledger transactions", "Margin"), so those columns were never marked and the
   values split mid-token. This audit pins the headings of those tables, so the
   vocabulary cannot silently lose them again. */
const REPORTED_TABLES = {
  "housekeeping.html": [
    ["Task", "Room", "Type", "Assignee", "Due", "Status", "Actions"],
  ],
  "laundry.html": [
    ["Order", "Customer", "Items", "Total", "Status", "Payment", "Actions"],
  ],
  "terminals.html": [
    ["Employee", "Department & Role", "Attendance Status", "Clocked In", "Workstation Code State", "Quick Action"],
    ["Workstation", "Department", "Status", "Last reported staff", "Last seen", "Actions"],
  ],
  "staff.html": [
    ["Staff", "Department & Role", "Status", "Last login", "Actions"],
    ["Department", "Subdomain URLs", "Job Positions", "Active / Total Staff", "Dashboard Path"],
    ["Reference", "Employee", "Category & Summary", "Status", "Action / Warning", "Approved Deduction", "Actions"],
    ["Capability Code", "Name & Category", "Description", "Granted to Role", "Toggle"],
  ],
  "pos.html": [
    ["Order", "Mode / guest", "Location", "Total", "Status", "Settlement", "Actions"],
    ["Item", "Qty", "Total"],
  ],
  "approvals.html": [
    ["Reference", "Detail", "Requested by", "Waiting since", "Amount"],
  ],
  "workforce.html": [
    ["Staff", "Business date", "Status", "Clock in", "Clock out", "Worked", "Actions"],
    ["Period", "Gross", "Deductions", "Net", "Paid", "Balance", "Actions"],
    ["Request", "Staff", "Dates", "Type", "Status", "Reviewer", "Actions"],
    ["Shift", "Staff", "Planned start", "Planned end", "Status", "Location", "Actions"],
    ["Template", "Department", "Hours", "Status", "Actions"],
    ["Staff", "Employee code", "Department", "Title", "Status", "Actions"],
  ],
  "reports.html": [
    ["Measure", "Amount", "Ledger transactions"],
    ["Status", "Count"],
    ["Booking", "Guest", "Check-in", "Balance due", "Age (days)", "Bucket"],
    ["Reference", "Incurred", "Payee", "Department", "Category", "Amount", "Status"],
  ],
  "inventory.html": [
    ["Item", "Location", "On hand", "Reorder level", "State", "Last movement"],
    ["Count", "Location", "Initiated", "Status", "Lines", "Actions"],
    ["Hand-off", "POS order", "Status", "Requested", "Resolution", "Actions"],
    ["Menu item", "Location", "Version", "Tracking", "Status", "Actions"],
    ["Item", "Unit", "Reorder", "Standard cost", "Status", "Actions"],
    ["Location", "Description", "Status", "Actions"],
    ["Movement", "Item", "Location", "Type", "Quantity", "Source", "Recorded by", "Recorded"],
    ["Item", "Expected", "Counted", "Variance"],
  ],
};

/* Headings that hold a reference, a moment, a measure or a short code: the
   value must stay on ONE line (nothing else about the column matters). */
const MUST_STAY_ON_ONE_LINE = /^(Task|Room|Due|Status|Actions|Order|Total|Payment|Employee|Department & Role|Attendance Status|Clocked In|Workstation Code State|Quick Action|Workstation|Last seen|Department|Subdomain URLs|Dashboard Path|Last login|Reference|Employee|Approved Deduction|Capability Code|Settlement|Qty|Waiting since|Amount|Business date|Clock in|Clock out|Worked|Period|Gross|Deductions|Net|Paid|Balance|Dates|Shift|Planned start|Planned end|Hours|Employee code|Request|Template|Ledger transactions|Count|Check-in|Balance due|Age \(days\)|Bucket|Incurred|Category|Booking|Source|Action \/ Warning|On hand|Reorder level|State|Last movement|Initiated|Lines|Hand-off|POS order|Requested|Version|Tracking|Unit|Reorder|Standard cost|Movement|Quantity|Recorded|Expected|Counted|Variance)$/;

test("every heading of the reported tables is either on one line or knowingly prose", () => {
  const ctx = { window: {}, document: {} };
  const vm = require("node:vm");
  vm.runInNewContext(TABLE_FIT, ctx);
  const isAtomic = ctx.window.JONE_TABLE_FIT.isAtomicHeading;

  const shouldWrap = [];
  const shouldNot = [];
  for (const [page, tables] of Object.entries(REPORTED_TABLES)) {
    for (const headings of tables) {
      for (const heading of headings) {
        const expected = MUST_STAY_ON_ONE_LINE.test(heading);
        if (expected !== isAtomic(heading)) {
          (expected ? shouldNot : shouldWrap).push(`${page}: ${heading}`);
        }
      }
    }
  }
  assert.deepEqual(shouldNot, [],
    "these columns hold a reference/date/measure/code but are still allowed to break mid-value");
  assert.deepEqual(shouldWrap, [],
    "these prose columns were frozen onto one line by the widened vocabulary");
});

test("an atomic column may still wrap the prose caption the page marked as one", () => {
  // The value stays on one line; a `.caption` line underneath it (a task
  // summary, a leave reason) wraps between words. Pages use this on exactly the
  // columns where the second line is free text.
  assert.match(CSS, /\.table td\.cell-fit \.caption \{ white-space: normal; overflow-wrap: break-word; \}/);
  const housekeeping = fs.readFileSync(path.join(DASHBOARD, "housekeeping.html"), "utf8");
  assert.match(housekeeping, /<small class="caption">'\+JONE\.esc\(task\.summary\)/,
    "the housekeeping task summary is prose under an atomic reference");
  const workflow = fs.readFileSync(path.join(DASHBOARD, "workforce.html"), "utf8");
  assert.match(workflow, /<small class="caption">'\+JONE\.esc\(row\.reason/);
});

test("an atomic value under a prose column stays on one line (.cell-sub)", () => {
  // "Item" / "Location" / "Product" carry an identifier AND prose, so the whole
  // cell cannot be frozen: pages mark just the identifier with .cell-sub.
  assert.match(CSS, /\.table td \.cell-sub \{ white-space: nowrap; overflow-wrap: normal; word-break: normal; \}/);
  // The attribute may be written plainly (markup) or escaped inside a
  // double-quoted JS string (laundry.html) — both render the same HTML.
  const marked = ["inventory.html", "pos.html", "laundry.html", "staff.html", "workforce.html"];
  for (const page of marked) {
    const html = fs.readFileSync(path.join(DASHBOARD, page), "utf8");
    assert.match(html, /class=\\?"cell-sub\\?"/, `${page} must mark its atomic sub-values`);
  }
});

test("an action cluster in a table cell is one line on desktop", () => {
  // Buttons folded into stacked lines on the reported pages (four actions in a
  // squeezed column). The cluster now stays on one line and the table scrolls.
  assert.match(CSS, /\.table:not\(\.table-staff\) td \.row-actions \{ flex-wrap: nowrap; \}/);
  // The card-restacked staff roster keeps wrapping: its columns are fixed.
  assert.match(CSS, /\.table-staff \{ table-layout: fixed; \}/);
});

test("status pills and badges never fold inside themselves", () => {
  const components = fs.readFileSync(path.join(FRONTEND, "css", "components.css"), "utf8");
  assert.match(CSS, /\.status-pill \{[\s\S]{0,120}white-space: nowrap;/);
  assert.match(components, /\.badge \{[\s\S]{0,400}?white-space: nowrap;/);
});
