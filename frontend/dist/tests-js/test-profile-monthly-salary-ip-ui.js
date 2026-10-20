/* Staff salary months and Front Desk desktop IP settings use real API fields. */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { FRONTEND } = require("./harness");

test("staff profile renders the months worked as an occupancy-style month calendar", () => {
  const page = fs.readFileSync(path.join(FRONTEND, "dashboard", "profile.html"), "utf8");
  assert.match(page, /sal\.current_month_work/);
  assert.match(page, /sal\.monthly_history/);
  assert.match(page, /Current month work/);
  assert.match(page, /Months worked &amp; salary payment status/);
  assert.match(page, /badge-success/);
  assert.match(page, /badge-danger/);
  assert.match(page, /badge-warning/);
  /* The months are rendered the way the occupancy calendar is rendered: a grid
     of cells, a chip legend, year navigation and a detail panel per cell. Every
     chip is built from the API's own monthly fields — a month the API did not
     return says "No record" instead of printing zeroes. */
  assert.match(page, /monthlyByKey\[row\.month\] = row/);
  assert.match(page, /esc\(row\.worked_days\) \+ " worked<\/span>"/);
  assert.match(page, /esc\(row\.leave_days\) \+ " leave<\/span>"/);
  assert.match(page, /esc\(row\.missed_days\) \+ " missed<\/span>"/);
  assert.match(page, /<span class="mo-empty">No record<\/span>/);
  assert.match(page, /class="mo-grid" id="ss-months" role="grid"/);
  assert.match(page, /class="mo-legend"/);
  assert.match(page, /data-mo-prev/);
  assert.match(page, /data-mo-next/);
  assert.match(page, /data-mo-current>This year<\/button>/);
  assert.match(page, /openMonth\(month\.getAttribute\("data-mo-month"\)\)/);
  assert.match(page, /"Months worked — " \+ row\.month_label/);
  assert.match(page, /row\.payroll_periods \|\| \[\]/);
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
