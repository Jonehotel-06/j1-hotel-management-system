/* Reported: "Immediately a staff takes attendance through biometric terminal or
   admin issues a code to the staff, the system should automatically clock-in the
   staff then disable the clock-in button for that staff until they clock-out."

   The server side of that is proved in backend/tests/test_attendance_workstation_policy.py
   (one open attendance record per staff member, reusing an existing one and
   opening one only when there is none, a refused probe never clocking anybody
   in, and the personal clock-in refused until clock-out). This file proves the
   screens the staff member actually sees:

     * the personal Clock in control is rendered DISABLED, with the reason,
       whenever an open attendance record exists — however it was opened;
     * the button state comes from the server's own open-record query (open=true),
       not from whatever page of today's list happens to be on screen;
     * the terminal console says plainly whether this action clocked the staff
       member in or reused the open record (no invented second clock-in);
     * no polling was added: the panel re-reads attendance on tab switch, window
       focus, after a terminal action and after a refused clock-in.

   Run from /frontend: node --test tests-js/test-attendance-auto-clockin-ui.js */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { makeContext, loadScript, disposeAll } = require("./harness");

const FRONTEND = path.resolve(__dirname, "..");
const WORKFORCE = fs.readFileSync(path.join(FRONTEND, "dashboard", "workforce.html"), "utf8");
const TERMINALS = fs.readFileSync(path.join(FRONTEND, "dashboard", "terminals.html"), "utf8");

test.after(() => disposeAll());

/* Every helper this page needs is already written on one line each. */
function pageLine(page, prefix) {
  const line = page.split("\n").find((l) => l.startsWith(prefix));
  assert.ok(line, `${prefix} was not found on the shipped page`);
  return line;
}

const ACTIVE_RECORD = pageLine(WORKFORCE, "function activeRecord(");
const CLOCK_BUTTON = pageLine(WORKFORCE, "function clockButton(");
const LOAD_OPEN = pageLine(WORKFORCE, "async function loadMyOpenRecord(");
const CLOCK_SUMMARY = pageLine(WORKFORCE, "function renderClockSummary(");

const TODAY = "2026-10-16";
const OPEN_FROM_TERMINAL = {
  reference: "ATT-20261016-ABC123",
  staff: 7,
  staff_email: "housekeeping@jonehotel.com",
  business_date: TODAY,
  status: "ON_DUTY",
  clock_in_at: TODAY + "T06:58:00+01:00",
  clock_out_at: null,
};

/* ---- The page contracts ------------------------------------------------- */

test("the personal clock state is read from the server's open-record query", () => {
  assert.match(LOAD_OPEN, /API\.list\("attendance",\{open:"true",staff_id:me\.id,page_size:1\}\)/,
    "the open record must be asked for explicitly, not inferred from a filtered page");
  assert.match(ACTIVE_RECORD, /if\(state\.myOpenRecord\)return state\.myOpenRecord;/,
    "the open record wins over the visible list: the terminal may be a different session");
  assert.match(WORKFORCE, /state\.attendanceItems=items;renderClockSummary\(\);loadMyOpenRecord\(\);/);
  assert.match(WORKFORCE, /var state=\{tab:"attendance",[\s\S]{0,200}myOpenRecord:null/);
});

test("the Clock in button is rendered disabled with its reason while a record is open", () => {
  assert.match(CLOCK_BUTTON, /if\(reason\)\{button\.disabled=true;button\.setAttribute\("aria-disabled","true"\);button\.title=reason;/,
    "a blocked Clock in must be disabled for real, not just styled");
  assert.match(CLOCK_BUTTON, /button\.dataset\.wfClockReason=reason;/);
  assert.match(CLOCK_BUTTON, /why\.setAttribute\("data-wf-clock-reason",""\)/);
  assert.match(CLOCK_BUTTON, /why\.style\.flexBasis="100%"/,
    "the reason sits on its own line under the buttons, not squeezed between them");
  assert.match(CLOCK_BUTTON, /why\.textContent=reason;/,
    "the reason must be readable, not only a tooltip");
  /* Both record states of the summary block offer the disabled control; an
     enabled Clock in appears only when no record is open. */
  const enabled = CLOCK_SUMMARY.match(/clockButton\("CLOCK_IN","Clock in","",actions\)/g) || [];
  const disabled = CLOCK_SUMMARY.match(/clockButton\("CLOCK_IN","Clock in","btn-outline",actions,blocked\)/g) || [];
  assert.equal(enabled.length, 1, "exactly one enabled Clock in — the not-clocked-in state");
  assert.equal(disabled.length, 2, "on duty and on break both disable Clock in");
  assert.match(CLOCK_SUMMARY, /Clock in is disabled: an attendance record is already open/);
  assert.match(CLOCK_SUMMARY, /Clock in stays disabled until you clock out/);
  assert.match(CLOCK_SUMMARY, /a 6-digit code issued for you, opens this same record and disables Clock in/);
});

test("the panel re-reads attendance on return to the screen, without polling", () => {
  assert.match(WORKFORCE, /function refreshAttendanceWhenVisible\(\)\{if\(state\.tab!=="attendance"\|\|document\.hidden\)return;loadAttendance\(\);\}/);
  assert.match(WORKFORCE, /document\.addEventListener\("visibilitychange",refreshAttendanceWhenVisible\);/);
  assert.match(WORKFORCE, /window\.addEventListener\("focus",refreshAttendanceWhenVisible\);/);
  assert.match(WORKFORCE, /if\(tab==="attendance"\)loadAttendance\(\);/,
    "opening the attendance tab reloads too");
  assert.doesNotMatch(WORKFORCE, /setInterval\s*\(/,
    "this page polls nothing — the refresh is event driven");
});

test("a refused clock-in is explained and immediately re-reads the real state", () => {
  assert.match(WORKFORCE, /JONE\.ui\.toast\(\(err&&err\.message\)\|\|"The attendance action couldn't be recorded\.","error"\);loadAttendance\(\);/,
    "the refusal is shown (never a silent no-op) and the button state is corrected");
});

test("the terminal console reports what the action did to attendance", () => {
  assert.match(TERMINALS, /var attendanceNote=JONE\.biometrics\.attendanceNote\(res\);/);
  assert.match(TERMINALS, /data-rt-attendance-note/);
  assert.match(WORKFORCE, /data-wf-rt-attendance-note/);
  assert.doesNotMatch(TERMINALS, /Clocked in now|Already clocked in/,
    "the wording lives in one module, not copied per page");
  assert.doesNotMatch(WORKFORCE, /Clocked in now|Already clocked in/);
});

test("the shared note distinguishes a clock-in from a reused record, and stays silent otherwise", () => {
  const ctx = makeContext();
  loadScript(ctx, "js/utils.js");
  loadScript(ctx, "js/biometrics.js");
  const note = ctx.window.JONE.biometrics.attendanceNote;
  assert.equal(typeof note, "function");

  const clockedInNow = note({ action: "CLOCK_IN", created: true, status: "CLOCKED_IN" });
  assert.match(clockedInNow, /Clocked in now/);
  assert.match(clockedInNow, /Clock in stays disabled until they clock out/);

  const reused = note({ action: "ISSUE_CODE", created: false, status: "CODE_ISSUED" });
  assert.match(reused, /Already clocked in/);
  assert.match(reused, /no second record was created/);

  assert.match(note({ action: "MANUAL_FALLBACK", attendance_created: true }), /Clocked in now/);
  assert.match(note({ action: "MANUAL_FALLBACK", attendance_created: false }), /Already clocked in/);

  assert.equal(note({ action: "VERIFY_STATUS", status: "VERIFIED" }), "",
    "a status check invents no attendance statement");
  assert.equal(note({ action: "CLOCK_OUT", status: "CLOCKED_OUT" }), "");
  assert.equal(note(undefined), "");
});

/* ---- The page code as it actually runs ---------------------------------- */

function clockContext() {
  const ctx = makeContext();
  const today = { value: TODAY };
  const scope = { value: "mine" };
  ctx.state = { tab: "attendance", attendanceItems: [], myOpenRecord: null };
  ctx.me = { id: 7, email: "housekeeping@jonehotel.com" };
  ctx.isManager = false;
  ctx.hotelToday = () => TODAY;
  ctx.date = (v) => (v ? String(v) : "—");
  ctx.dateTime = (v) => (v ? String(v) : "—");
  ctx.OPS = { listItems: (response) => (response && response.data) || [] };
  ctx.window.JONE = { esc: (v) => String(v == null ? "" : v) };
  ctx.document.getElementById = (id) => (id === "wf-attendance-date" ? today : id === "wf-attendance-scope" ? scope : null);
  vm.runInContext(`${ACTIVE_RECORD}\n${CLOCK_BUTTON}\n${LOAD_OPEN}\n${CLOCK_SUMMARY}`, ctx, { filename: "workforce.html#clock" });
  return ctx;
}

function renderSummary(record, list) {
  const ctx = clockContext();
  ctx.state.myOpenRecord = record;
  ctx.state.attendanceItems = list || [];
  const summary = ctx.document.createElement("div");
  const note = ctx.document.createElement("div");
  const actions = ctx.document.createElement("div");
  ctx.document.querySelector = (selector) => {
    if (selector === "[data-wf-attendance-summary]") return summary;
    if (selector === "[data-wf-attendance-summary-note]") return note;
    if (selector === "[data-wf-clock-actions]") return actions;
    return null;
  };
  ctx.renderClockSummary();
  const buttons = actions.children.filter((el) => el.tagName === "BUTTON");
  const captions = actions.children.filter((el) => el.getAttribute("data-wf-clock-reason") === "");
  return {
    summary: summary.textContent,
    note: note.textContent,
    buttons,
    captions,
    clockIn: buttons.filter((b) => b.dataset.wfClock === "CLOCK_IN")[0] || null,
  };
}

test("a terminal clock-in disables the Clock in button in the staff member's own panel", () => {
  const open = renderSummary(OPEN_FROM_TERMINAL);
  assert.equal(open.summary, "You are on duty");
  assert.ok(open.clockIn, "the control is still on screen — it is disabled, not hidden");
  assert.equal(open.clockIn.disabled, true, "Clock in must be disabled while the record is open");
  assert.equal(open.clockIn.getAttribute("aria-disabled"), "true");
  assert.match(open.clockIn.title, /already open/);
  assert.equal(open.captions.length, 1, "the disabled reason is visible text");
  assert.match(open.captions[0].textContent, /Clock out before clocking in again/);
  assert.match(open.note, /Clock in stays disabled until you clock out/);
  const enabled = open.buttons.filter((b) => b.dataset.wfClock === "CLOCK_IN" && !b.disabled);
  assert.equal(enabled.length, 0, "there is no enabled Clock in next to an open record");
  assert.deepEqual(
    open.buttons.filter((b) => b.dataset.wfClock !== "CLOCK_IN").map((b) => b.dataset.wfClock),
    ["BREAK_STARTED", "CLOCK_OUT"],
    "break and clock-out stay reachable — clocking out is what re-enables Clock in"
  );
});

test("while on break Clock in stays disabled and only ending the break is offered", () => {
  const open = renderSummary(Object.assign({}, OPEN_FROM_TERMINAL, {
    status: "ON_BREAK",
    active_break_started_at: TODAY + "T11:00:00+01:00",
  }));
  assert.equal(open.summary, "You are on break");
  assert.equal(open.clockIn.disabled, true);
  assert.deepEqual(open.buttons.filter((b) => b.dataset.wfClock !== "CLOCK_IN").map((b) => b.dataset.wfClock), ["BREAK_ENDED"]);
  assert.match(open.note, /Clock in stays disabled until you clock out/);
});

test("with no open record the Clock in button is offered and enabled", () => {
  const closed = renderSummary(null, []);
  assert.equal(closed.summary, "You are not clocked in");
  assert.equal(closed.clockIn.disabled, false);
  assert.equal(closed.captions.length, 0);
  assert.match(closed.note, /opens this same record and disables Clock in until you clock out/);
});

test("a record on a page that is not on screen still disables the button", () => {
  /* The visible list is a date-filtered page: the open record must not depend
     on it, otherwise a stale list would offer a second clock-in. */
  const open = renderSummary(OPEN_FROM_TERMINAL, []);
  assert.equal(open.clockIn.disabled, true);
  const completed = renderSummary(null, [Object.assign({}, OPEN_FROM_TERMINAL, {
    status: "COMPLETED", clock_out_at: TODAY + "T15:00:00+01:00",
  })]);
  assert.equal(completed.clockIn.disabled, false, "a completed record re-enables Clock in");
});

test("the open-record query is issued with the server filter and drives the panel", async () => {
  const ctx = clockContext();
  const requests = [];
  let rendered = 0;
  ctx.renderClockSummary = () => { rendered += 1; };
  ctx.window.API = {
    list: (resource, params) => {
      requests.push({ resource, params });
      return Promise.resolve({ data: [OPEN_FROM_TERMINAL], pagination: { count: 1 } });
    },
  };
  await ctx.loadMyOpenRecord();
  assert.equal(requests.length, 1, "one bounded request, not a scan");
  assert.equal(requests[0].resource, "attendance");
  assert.equal(requests[0].params.open, "true");
  assert.equal(requests[0].params.staff_id, 7, "the caller's own record, not the whole team's");
  assert.equal(requests[0].params.page_size, 1);
  assert.equal(ctx.state.myOpenRecord.reference, "ATT-20261016-ABC123");
  assert.equal(rendered, 1, "the panel re-renders from the server's answer");

  /* A failed probe of the query must never leave a stale disabled button. */
  ctx.window.API = { list: () => Promise.reject(new Error("offline")) };
  await ctx.loadMyOpenRecord();
  assert.equal(ctx.state.myOpenRecord, null);
  assert.equal(rendered, 2);
});

test("Clock in renders disabled only with a reason (never a silent no-op)", () => {
  const ctx = makeContext();
  vm.runInContext(CLOCK_BUTTON + "\nglobalThis.__clockButton = clockButton;", ctx, { filename: "workforce.html#clockButton" });

  const open = ctx.document.createElement("div");
  ctx.__clockButton("CLOCK_IN", "Clock in", "", open);
  assert.equal(open.children.length, 1);
  assert.equal(open.children[0].disabled, false);
  assert.equal(open.children[0].getAttribute("aria-disabled"), null);
  assert.equal(open.children[0].title, undefined, "no reason, no title annotation");

  const blocked = ctx.document.createElement("div");
  ctx.__clockButton("CLOCK_IN", "Clock in", "btn-outline", blocked, "An attendance record is already open.");
  const button = blocked.children[0];
  assert.equal(button.disabled, true);
  assert.equal(button.getAttribute("aria-disabled"), "true");
  assert.equal(button.title, "An attendance record is already open.",
    "title = reason (the stub keeps the property; a browser mirrors it to the attribute)");
  /* dataset.* is the DOM property whose attribute is data-wf-clock-reason —
     the stub stores the property without mirroring the attribute. */
  assert.equal(button.dataset.wfClockReason, "An attendance record is already open.");
  assert.equal(button.dataset.wfClock, "CLOCK_IN");
  assert.equal(blocked.children[1].textContent, "An attendance record is already open.");
});
