/* Targeted repair (five reported bugs) — page contracts.
 *
 * These assertions pin the exact defects that were repaired, so a future edit
 * cannot quietly reintroduce them. They read the shipped pages the same way the
 * rest of this suite does: the behaviour lives in an inline block on the page,
 * so the page source IS the contract.
 */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { FRONTEND } = require("./harness");

function page(name) {
  return fs.readFileSync(path.join(FRONTEND, "dashboard", name), "utf8");
}

/* Bug 3 — Accounting From/To date inputs must respond to clicks and typing.
 *
 * They were disabled as soon as the page script ran (`fromEl.disabled=true`,
 * `disabled=!custom`) and only re-enabled by picking "Custom dates" in the
 * Period selector FIRST, so clicking the field did nothing at all. The fields
 * are now always editable and a chosen date IS the custom range. */
test("accounting From/To fields are never disabled and a chosen date selects the custom range", () => {
  const accounting = page("accounting.html");
  assert.doesNotMatch(accounting, /fromEl\.disabled\s*=\s*true/, "From is not disabled on load");
  assert.doesNotMatch(accounting, /toEl\.disabled\s*=\s*true/, "To is not disabled on load");
  assert.doesNotMatch(accounting, /fromEl\.disabled\s*=\s*!custom/, "the range no longer gates From");
  assert.doesNotMatch(accounting, /toEl\.disabled\s*=\s*!custom/, "the range no longer gates To");
  assert.match(accounting, /function selectCustomRange\(\)/, "the date picker handler exists");
  assert.match(accounting, /if\(range\.value!=="custom"\) range\.value="custom";/,
    "typing a date is what makes the range custom");
  assert.match(accounting, /fromEl\.addEventListener\("change",selectCustomRange\)/);
  assert.match(accounting, /toEl\.addEventListener\("change",selectCustomRange\)/);
  /* activeDates() reads the range first; that is unchanged and must stay so the
     API payload keeps coming from one place. */
  assert.match(accounting, /range\.value==="custom" && fromEl\.value && toEl\.value/);
});

/* Bug 1 — the personal clock controls are the signed-in user's own attendance.
 *
 * renderClockSummary() emptied [data-wf-clock-actions] whenever the attendance
 * scope selector sat on "team", so every account that can see team attendance
 * (administrator, general manager, admin manager/procurement) had no Clock out
 * control at all while the backend accepted the action. */
test("workforce clock controls stay rendered in the team scope", () => {
  const workforce = page("workforce.html");
  assert.doesNotMatch(
    workforce,
    /scopeSelect\.value==="team"\)\{summary\.textContent="Viewing team attendance"/,
    "the team scope must not blank the personal clock controls"
  );
  assert.match(workforce, /The table below lists the whole team; the controls here are your own attendance\./,
    "the scope is still explained to the operator");
  assert.match(workforce, /clockButton\("CLOCK_OUT","Clock out"/, "Clock out is still offered");
  assert.match(workforce, /function say\(base\)\{note\.textContent=base\+scopeNote;\}/,
    "the scope note is appended to every state note");
  /* The historical-date guard is a different rule and stays. */
  assert.match(workforce, /Viewing historical attendance/);
});

/* Bug 4 — a missing duration must read as "not recorded", never as zero.
 *
 * `worked_minutes` is null until the record is clocked out. `a.worked_minutes
 * || 0` printed "0" for that open record, which reads as "worked zero minutes"
 * instead of "no duration yet". */
test("staff profile never converts a null worked-minutes into zero", () => {
  const profile = page("staff-profile.html");
  assert.doesNotMatch(profile, /a\.worked_minutes\s*\|\|\s*0/,
    "null is not silently coerced to 0");
  assert.match(profile, /a\.worked_minutes == null \? "—" : a\.worked_minutes/,
    "the column states that no duration is recorded");
});

/* Bug 5 — the cart may not exceed the stock the server will actually check.
 *
 * Stock validation is per ORDER, not per line, and the picker pushes a new line
 * for the same product every time it is added. Comparing one line against the
 * available quantity let an operator assemble an unfulfillable cart and only
 * find out from the 409 when the draft was created. */
test("pos cart counts a product across all of its lines against the stock ceiling", () => {
  const pos = page("pos.html");
  assert.doesNotMatch(
    pos,
    /var stockCeiling=il\.stock_available==null\?100:Math\.min\(100,il\.stock_available\);/,
    "the increment ceiling no longer ignores the other lines"
  );
  assert.match(pos, /var incOthers=state\.cart\.reduce/, "increment counts the other lines");
  assert.match(pos, /var alreadyCarted=state\.cart\.reduce/, "adding a line counts what is already carted");
  assert.match(pos, /if\(quantity\+alreadyCarted>available\)/,
    "the add-line check compares the whole order against availability");
});
