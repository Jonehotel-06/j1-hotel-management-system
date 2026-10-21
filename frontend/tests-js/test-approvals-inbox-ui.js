/* Approvals inbox page, expense-details overflow fix, stale-bundle guard.
   Run from /frontend: node --test tests-js/test-approvals-inbox-ui.js */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const FRONTEND = path.resolve(__dirname, "..");
const read = (rel) => fs.readFileSync(path.join(FRONTEND, rel), "utf8");
const PAGE = read("dashboard/approvals.html");
const ACCOUNTING = read("dashboard/accounting.html");
const CHECKOUT = read("dashboard/check-out.html");
const DASHBOARD = read("js/dashboard.js");
const CSS = read("css/dashboard.css");
const CONFIG = read("js/config.js");

test("the approvals inbox is wired to the backend endpoint", () => {
  assert.match(CONFIG, /reportsApprovals:\s*"\/api\/admin\/reports\/approvals\/"/);
  assert.match(PAGE, /API_ENDPOINTS\.reportsApprovals/);
  assert.match(PAGE, /data-approvals/);
  assert.match(PAGE, /data-approvals-total/);
  assert.match(PAGE, /Nothing waiting in this queue/);
  assert.match(PAGE, /All caught up/);
  assert.match(PAGE, /Open queue/);
});

test("the sidebar shows Approvals with a personal pending-count badge", () => {
  assert.match(DASHBOARD, /label: "Approvals", href: "approvals\.html", icon: "checkCircle", view: "approvals"/);
  assert.match(DASHBOARD, /data-approvals-badge/);
  assert.match(DASHBOARD, /function refreshApprovalsBadge\(\)/);
  assert.match(DASHBOARD, /total_pending/);
});

test("expense audit details render as wrap-safe labelled lines, never raw JSON", () => {
  assert.doesNotMatch(ACCOUNTING, /JSON\.stringify\(event\.details\)/);
  assert.match(ACCOUNTING, /function eventDetailsHtml\(details\)/);
  assert.match(ACCOUNTING, /detail-event-details/);
  assert.match(CSS, /\.detail-timeline \.caption \{ overflow-wrap: anywhere; \}/);
  assert.match(CSS, /\.detail-event-details \.caption \{ overflow-wrap: anywhere; \}/);
});

test("the folio view degrades politely on a stale cached bundle", () => {
  assert.match(CHECKOUT, /typeof window\.API\.bookingFolioClearance !== "function"/);
  assert.match(CHECKOUT, /Update needed/);
});
