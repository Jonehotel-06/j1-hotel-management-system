/* Staff salary months and Front Desk desktop IP settings use real API fields. */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { FRONTEND } = require("./harness");

test("staff profile salary section renders current-month and monthly work/payment history", () => {
  const page = fs.readFileSync(path.join(FRONTEND, "dashboard", "profile.html"), "utf8");
  assert.match(page, /sal\.current_month_work/);
  assert.match(page, /sal\.monthly_history/);
  assert.match(page, /Current month work/);
  assert.match(page, /Months worked &amp; salary payment status/);
  assert.match(page, /badge-success/);
  assert.match(page, /badge-danger/);
  assert.match(page, /badge-warning/);
  assert.match(page, /Worked ' \+ esc\(row\.worked_days\) \+ ' \/ '/);
  assert.match(page, /Missed ' \+ esc\(row\.missed_days\) \+ ' \/ '/);
  assert.match(page, /PARTIALLY_PAID:\["badge-warning","Part payment"\]/);
  assert.match(page, /NOT_CALCULATED:\["badge-info","No approved payslip yet"\]/);
  assert.match(page, /Missed days count only scheduled shifts that have started/);
});

test("settings page exposes the Admin Front Desk desktop IP configuration", () => {
  const page = fs.readFileSync(path.join(FRONTEND, "dashboard", "settings.html"), "utf8");
  assert.match(page, /name="front_desk_desktop_ip"/);
  assert.match(page, /Front Desk desktop &amp; attendance security/);
  assert.match(page, /IP address seen by the server for this browser/);
  assert.match(page, /st-use-request-ip/);
  assert.match(page, /current\.request_ip/);
  assert.match(page, /"front_desk_desktop_ip"/);
  assert.match(page, /no code change is needed/);
});
