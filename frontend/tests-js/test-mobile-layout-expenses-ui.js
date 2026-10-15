/* Mobile layout + Accounting/Reports expense contracts (staff screenshots):
   tables must not shred values across lines on phones, dark-mode button text
   must stay visible, installed-app content must clear the bottom nav,
   Accounting can create expenses (incl. for stocked products), and Reports
   lists expense records.
   Run from /frontend: node --test tests-js/test-mobile-layout-expenses-ui.js */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const FRONTEND = path.resolve(__dirname, "..");
const read = (rel) => fs.readFileSync(path.join(FRONTEND, rel), "utf8");
const CSS = read("css/dashboard.css");
const ACCOUNTING = read("dashboard/accounting.html");
const REPORTS = read("dashboard/reports.html");
const STAFF = read("dashboard/staff.html");

test("phone tables size to content and keep values on one line", () => {
  assert.match(CSS, /\.table-wrap \.table:not\(\.table-staff\) \{ width: max-content; min-width: 100%; \}/);
  assert.match(CSS, /\.table-wrap \.table:not\(\.table-staff\) td,\s*\.table-wrap \.table:not\(\.table-staff\) th \{ white-space: nowrap; \}/);
  // clamped prose and empty states still wrap inside bounded widths
  assert.match(CSS, /td\.cell-clamp \{ white-space: normal; max-width: 26ch; \}/);
});

test("active tab buttons pair surfaces with their ink tokens (dark-mode readable)", () => {
  assert.match(CSS, /\.op-tabs \.btn\.is-active,\s*\.payroll-tab\[aria-selected="true"\],\s*button\[data-staff-tab\]\.is-active \{[^}]*color: var\(--color-primary-ink\);/);
  for (const page of ["inventory", "procurement", "workforce"]) {
    assert.doesNotMatch(read(`dashboard/${page}.html`), /\.op-tabs \.is-active\{[^}]*color:#fff/);
  }
  assert.doesNotMatch(read("dashboard/payroll.html"), /\.payroll-tab\[aria-selected="true"\]\{[^}]*color:#fff/);
});

test("staff tabs mark the active tab for sighted and assistive users", () => {
  assert.match(STAFF, /other\.classList\.toggle\("is-active", active\)/);
  assert.match(STAFF, /other\.setAttribute\("aria-selected", active \? "true" : "false"\)/);
});

test("installed-app bottom clearance exceeds the browser clearance", () => {
  assert.match(CSS, /body\.has-bottom-nav \.dash-content \{\s*padding-bottom: calc\(5\.5rem \+ env\(safe-area-inset-bottom, 0px\)\);/);
  assert.match(CSS, /@media \(display-mode: standalone\) \{\s*\.dash \{ height: 100svh; \}/);
  assert.match(CSS, /padding-bottom: calc\(6\.5rem \+ env\(safe-area-inset-bottom, 0px\)\)/);
});

test("Accounting creates expenses and can link a stocked product or registered supplier", () => {
  assert.match(ACCOUNTING, /id="acct-create-expense"/);
  assert.match(ACCOUNTING, /async function createExpense\(\)/);
  assert.match(ACCOUNTING, /window\.API\.list\("suppliers"/);
  assert.match(ACCOUNTING, /window\.API\.list\("inventoryItems"/);
  assert.match(ACCOUNTING, /accounting_class: item \? "INVENTORY" : "OPERATING"/);
  assert.match(ACCOUNTING, /window\.API\.create\("financeExpenses", payload\)/);
  // drafts can be submitted into the existing approval workflow
  assert.match(ACCOUNTING, /data-expense-submit=/);
  assert.match(ACCOUNTING, /resourceAction\("financeExpenses", reference, "submit", \{\}\)/);
});

test("Reports lists expense records for the selected period and exports them", () => {
  assert.match(REPORTS, /data-tbody="report-expenses"/);
  assert.match(REPORTS, /function renderExpenses\(response\)/);
  assert.match(REPORTS, /API_ENDPOINTS\.financeExpenses,\{ start:dates\.from, end:dates\.to, page_size:100 \}/);
  assert.match(REPORTS, /\["Expense records"\]/);
});
