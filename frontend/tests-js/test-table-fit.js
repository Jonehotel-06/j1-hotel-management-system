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
