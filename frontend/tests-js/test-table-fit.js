/* tests-js/test-table-fit.js
   Unit tests for js/table-fit.js: heading classification and the per-cell
   `cell-fit` marking that keeps amounts, dates and references on one line
   while prose columns keep wrapping. */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { makeContext, loadScript, disposeAll } = require("./harness");

function loadModule() {
  const ctx = makeContext();
  loadScript(ctx, "js/table-fit.js");
  return ctx.window.JONE_TABLE_FIT;
}

/* Minimal table-shaped object with the members fitTable() reads. */
function cellStub(text, index, colSpan = 1, classes = []) {
  const set = new Set(classes);
  return {
    textContent: text,
    colSpan,
    cellIndex: index,
    classList: {
      add(c) { set.add(c); },
      contains(c) { return set.has(c); },
    },
    _classes: set,
  };
}

function tableStub(headings, bodyRows, attrs = {}) {
  const headCells = headings.map((h, i) => cellStub(h, i, 1));
  const bodies = [{
    rows: bodyRows.map((row) => ({ cells: row.map((c, i) => cellStub(c.text, i, c.colSpan || 1, c.classes || [])) })),
  }];
  return {
    tHead: { rows: [{ cells: headCells }] },
    tBodies: bodies,
    getAttribute(name) { return attrs[name] === undefined ? null : attrs[name]; },
    _head: headCells,
    _body: bodies[0].rows,
  };
}

const has = (cell) => cell._classes.has("cell-fit");

test("atomic headings are classified as one-line values", () => {
  const { isAtomicHeading } = loadModule();
  for (const label of [
    "Amount", "Total (NGN)", "Check-in date", "Reference", "Status", "Actions",
    "Qty", "Room no.", "Last seen", "Balance due", "Created", "Phone",
    // Inventory/procurement quantity columns: these were the columns still
    // breaking mid-number before the vocabulary was widened.
    "On hand", "Counted", "Variance", "Expected", "Initiated", "Received", "Ordered",
    "Outstanding", "Lines",
  ]) {
    assert.equal(isAtomicHeading(label), true, `expected atomic: ${label}`);
  }
});

test("prose headings keep wrapping even when they contain an atomic word", () => {
  const { isAtomicHeading } = loadModule();
  for (const label of [
    "Guest name", "Room type", "Description", "Notes", "Menu item", "Staff", "Email",
    "Reason", "Last name", "Summary",
  ]) {
    assert.equal(isAtomicHeading(label), false, `expected prose: ${label}`);
  }
});

test("unknown, empty and non-string headings are not forced to one line", () => {
  const { isAtomicHeading } = loadModule();
  assert.equal(isAtomicHeading("Foo"), false);
  assert.equal(isAtomicHeading(""), false);
  assert.equal(isAtomicHeading(undefined), false);
  assert.equal(isAtomicHeading(null), false);
});

test("fitTable marks atomic columns in head and body, leaves prose columns alone", () => {
  const { fitTable } = loadModule();
  const table = tableStub(
    ["Guest name", "Amount", "Status"],
    [[{ text: "Ngozi Eze" }, { text: "₦12,500.00" }, { text: "PAID" }]],
  );
  fitTable(table);
  assert.equal(has(table._head[0]), false);
  assert.equal(has(table._head[1]), true);
  assert.equal(has(table._head[2]), true);
  const [name, amount, status] = table._body[0].cells;
  assert.equal(has(name), false);
  assert.equal(has(amount), true);
  assert.equal(has(status), true);
});

test("fitTable skips multi-column headings and body cells that own their wrapping contract", () => {
  const { fitTable } = loadModule();
  const table = tableStub(
    ["Amount", "Reference"],
    [[{ text: "₦1", classes: ["cell-nowrap"] }, { text: "REF-1", classes: ["cell-clamp"] }],
     [{ text: "₦2", colSpan: 2 }, { text: "REF-2", classes: ["cell-guest"] }]],
  );
  fitTable(table);
  const [a, b] = table._body[0].cells;
  assert.equal(has(a), false, "cell-nowrap must not be rewritten");
  assert.equal(has(b), false, "cell-clamp must not be rewritten");
  assert.equal(has(table._body[1].cells[0]), false, "colSpan cells are not single-column values");
  assert.equal(has(table._body[1].cells[1]), false, "cell-guest keeps its own contract");
});

test("fitTable honours data-table-fit=off and tolerates bad input", () => {
  const { fitTable } = loadModule();
  const table = tableStub(["Amount"], [[{ text: "₦5" }]], { "data-table-fit": "off" });
  fitTable(table);
  assert.equal(has(table._head[0]), false);
  assert.doesNotThrow(() => fitTable(null));
  assert.doesNotThrow(() => fitTable({}));
});

test("fitAll is a no-op without a document (for example in a worker)", () => {
  const { fitAll } = loadModule();
  assert.equal(fitAll(null), 0);
});

test.after(() => disposeAll());

/* ---- Reported tables (housekeeping, laundry, workstations, staff, POS, ----
   approvals, workforce schedules/leave, reports, inventory) -----------------

   These are the exact column headings of the tables reported as "data breaks to
   the next line". Each one holds a reference, a moment, a measure or a short
   code — never prose — so it must be classified atomic and keep its value on
   one line. */
test("the reported tables' reference/date/measure/code columns stay on one line", () => {
  const { isAtomicHeading } = loadModule();
  const headings = [
    // housekeeping task queue
    "Task", "Room", "Due", "Status", "Actions",
    // laundry orders
    "Order", "Total", "Payment",
    // reception terminal + workstation registry
    "Attendance Status", "Clocked In", "Workstation Code State", "Quick Action",
    "Workstation", "Last seen",
    // staff page (roster meta line, departments, deductions, capabilities)
    "Department & Role", "Last login", "Reference", "Approved Deduction",
    "Capability Code", "Dashboard Path", "Subdomain URLs",
    // POS / room service
    "Settlement", "Qty",
    // approvals queue
    "Waiting since", "Amount",
    // workforce: attendance, payslips, leave, schedules, templates, profiles
    "Business date", "Clock in", "Clock out", "Worked", "Period", "Gross",
    "Deductions", "Net", "Paid", "Balance", "Dates", "Shift", "Planned start",
    "Planned end", "Hours", "Employee code", "Request", "Template",
    // reports
    "Ledger transactions", "Count", "Check-in", "Balance due", "Age (days)",
    "Bucket", "Incurred", "Category",
    // inventory: hand-off, catalog, movement ledger, profitability, counts
    "On hand", "Reorder level", "State", "Last movement", "Count", "Initiated",
    "Lines", "Hand-off", "POS order", "Requested", "Version", "Tracking",
    "Unit", "Reorder", "Standard cost", "Movement", "Quantity", "Recorded",
    "Unit cost", "Selling price", "Stock value", "Expected revenue",
    "Expected gross profit", "Margin", "Expected", "Counted", "Variance",
    "Unit price", "Line total", "Payment method",
  ];
  for (const label of headings) {
    assert.equal(isAtomicHeading(label), true, `expected atomic (one line): ${label}`);
  }
});

test("widening the vocabulary did not freeze the prose columns that contain those words", () => {
  const { isAtomicHeading } = loadModule();
  for (const label of [
    "Category & Allegation", "Employee Response", "Areas to Improve",
    "Components per unit", "Job Positions", "Description", "Detail",
    "Reason / Review Note", "Requested by", "Resolution", "Active / Total Staff",
    "Staff", "Guest name", "Room type", "Item", "Menu item", "Location",
    "Supplier", "Payee", "Assignee", "Measure", "Title & Description",
    "Late details", "Notes",
  ]) {
    assert.equal(isAtomicHeading(label), false, `expected prose (wraps): ${label}`);
  }
  /* The exception that proves the rule: a case REFERENCE plus its department is
     an atomic pair, so it keeps its line. */
  assert.equal(isAtomicHeading("Case / department"), true);
});
