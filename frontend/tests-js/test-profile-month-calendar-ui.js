/* The "Months worked & salary payment status" block of the staff profile is
   rendered the way the occupancy calendar is: a grid of month cells, a chip
   legend, year navigation and a detail panel for a month.

   These tests run the page's OWN calendar code inside the shared stub DOM, so
   the cells are proved against the real shape of the API's monthly rows: every
   chip comes from a field the API returned, a month the API did not return
   says "No record" instead of printing zeroes, and a month with no payroll
   period says so rather than showing an empty table.

   Run from /frontend: node --test tests-js/test-profile-month-calendar-ui.js */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { makeContext, FRONTEND, disposeAll } = require("./harness");

test.after(() => disposeAll());

const PAGE = fs.readFileSync(path.join(FRONTEND, "dashboard", "profile.html"), "utf8");

/* The self-service block, from its state variable to the tab switcher: it
   holds esc/fmtDate/money/kv/emptyState/table and the month calendar. */
const SLICE_START = "  var ssData = null;";
const SLICE_END = "  function switchSsTab(tab){";

const PAID_AUGUST = {
  month: "2026-08", month_label: "August 2026", is_current_month: false,
  worked_days: 21, scheduled_days: 23, missed_days: 2, leave_days: 3,
  unscheduled_worked_days: 1, worked_minutes: 10240,
  net_pay: "185000.00", salary_amount_paid: "185000.00", salary_balance: "0.00",
  payment_status: "PAID", currency: "NGN",
  payroll_period_references: ["PR-2026-08"],
  payroll_periods: [{
    reference: "PR-2026-08", starts_on: "2026-08-01", ends_on: "2026-08-31",
    status: "PAID", payment_status: "PAID", net_pay: "185000.00",
    salary_amount_paid: "185000.00", salary_balance: "0.00",
  }],
};

const UNPAID_SEPTEMBER = {
  month: "2026-09", month_label: "September 2026", is_current_month: false,
  worked_days: 5, scheduled_days: 20, missed_days: 0, leave_days: 0,
  unscheduled_worked_days: 0, worked_minutes: 2400,
  net_pay: "0.00", salary_amount_paid: "0.00", salary_balance: "0.00",
  payment_status: "NOT_CALCULATED", currency: "NGN",
  payroll_period_references: [], payroll_periods: [],
};

function makeEl() { return { innerHTML: "", textContent: "", disabled: false, addEventListener() {} }; }

function loadCalendar(rows, thisMonth) {
  const ctx = makeContext();
  const host = makeEl();
  const yearLabel = makeEl();
  const nextBtn = makeEl();
  const closeBtn = makeEl();
  const panels = [];

  ctx.JONE = {
    esc: (v) => String(v == null ? "" : v),
    formatNaira: (v) => "NGN " + v,
    formatDate: (v) => String(v == null ? "" : v),
    formatDateTime: (v) => String(v == null ? "" : v),
    hotelTodayISO: () => thisMonth + "-16",
    icons: { inject() {}, get: () => "" },
    dashboard: {
      openPanel: (title, html) => { panels.push({ title, html }); return null; },
      closePanel() {},
    },
  };
  ctx.document.getElementById = (id) => (id === "ss-months" ? host : null);
  ctx.document.querySelector = (sel) => {
    if (sel === "[data-mo-year]") return yearLabel;
    if (sel === "[data-mo-next]") return nextBtn;
    if (sel === "[data-mo-close]") return closeBtn;
    return null;
  };

  /* The page's own element lookup, as the page defines it above this block. */
  vm.runInContext('function $(id){ return document.getElementById(id); }', ctx);

  const start = PAGE.indexOf(SLICE_START);
  const end = PAGE.indexOf(SLICE_END, start);
  assert.ok(start > 0 && end > start, "the self-service record code was not found in profile.html");
  vm.runInContext(PAGE.slice(start, end), ctx, { filename: "profile.html#records" });

  const map = {};
  for (const row of rows) map[row.month] = row;
  vm.runInContext(
    "monthlyByKey = " + JSON.stringify(map) + ";" +
    "moThisMonth = " + JSON.stringify(thisMonth) + ";" +
    "moYear = Number(moThisMonth.slice(0, 4));",
    ctx,
  );
  vm.runInContext("renderMonths();", ctx);
  return { ctx, host, yearLabel, nextBtn, panels };
}

test("every month of the year gets a cell, and a month with no record says so", () => {
  const { host } = loadCalendar([PAID_AUGUST, UNPAID_SEPTEMBER], "2026-10");
  const cells = host.innerHTML.split('class="mo-cell').length - 1;
  assert.equal(cells, 12, "one cell per month of the year");
  /* Ten of the twelve months have no attendance, roster or payslip record:
     they are placeholders, never a row of zeroes presented as facts. */
  assert.equal((host.innerHTML.match(/No record/g) || []).length, 10);
  assert.equal(host.innerHTML.includes('data-mo-month="2026-07"'), false,
    "a month with no record is not openable");
  assert.equal(host.innerHTML.includes('data-mo-month="2026-08"'), true,
    "a month with a record is");
});

test("a month cell carries its own worked, leave and missed days, never zeroes", () => {
  const { host } = loadCalendar([PAID_AUGUST, UNPAID_SEPTEMBER], "2026-10");
  assert.match(host.innerHTML, /21 worked/);
  assert.match(host.innerHTML, /3 leave/);
  assert.match(host.innerHTML, /2 missed/);
  assert.match(host.innerHTML, /NGN 185000\.00/);
  assert.match(host.innerHTML, /badge-success">Paid</);
  assert.match(host.innerHTML, /PR-2026-08/);
  /* September was worked but never ran through payroll: its zero leave and zero
     missed days are not printed as chips, and the salary status says plainly
     that no payslip exists yet. */
  const sept = host.innerHTML.slice(host.innerHTML.indexOf('data-mo-month="2026-09"'));
  const chips = sept.slice(sept.indexOf('class="mo-chips"'), sept.indexOf("</button>"));
  assert.match(chips, /5 worked/);
  assert.equal(/0 leave|0 missed/.test(chips), false, "a zero count is not a chip");
  assert.match(sept, /No approved payslip yet/);
});

test("the current month is marked and the year is labelled", () => {
  const { host, yearLabel } = loadCalendar([PAID_AUGUST], "2026-10");
  assert.equal(yearLabel.textContent, "2026");
  assert.equal((host.innerHTML.match(/is-current/g) || []).length, 1,
    "exactly the month being lived in is marked");
  assert.match(host.innerHTML, /class="mo-cell is-current"/);
});

test("the grid navigates by year and cannot run past the current one", () => {
  const { ctx, yearLabel, nextBtn, host } = loadCalendar([PAID_AUGUST], "2026-10");
  assert.equal(nextBtn.disabled, true, "there is nothing ahead to show");
  vm.runInContext("moYear = 2025; renderMonths();", ctx);
  assert.equal(yearLabel.textContent, "2025");
  assert.equal(nextBtn.disabled, false, "the current year is now ahead again");
  assert.equal((host.innerHTML.match(/No record/g) || []).length, 12,
    "a year the API returned nothing for is empty, not invented");
});

test("opening a month shows its own payroll periods", () => {
  const { ctx, panels } = loadCalendar([PAID_AUGUST, UNPAID_SEPTEMBER], "2026-10");
  vm.runInContext('openMonth("2026-08");', ctx);
  assert.equal(panels.length, 1, "a month with a record opens a detail panel");
  assert.match(panels[0].title, /August 2026/);
  assert.match(panels[0].html, /Payroll periods in this month/);
  assert.match(panels[0].html, /PR-2026-08/);
  assert.match(panels[0].html, /21/, "the month's own day counts are shown");
  assert.match(panels[0].html, /Missed days count only scheduled shifts that have started/);

  vm.runInContext('openMonth("2026-09");', ctx);
  assert.equal(panels.length, 2, "a worked month without payroll still opens");
  assert.match(panels[1].html, /No payroll period covers this month/);

  vm.runInContext('openMonth("2026-03");', ctx);
  assert.equal(panels.length, 2, "a month with no record opens nothing at all");
});
