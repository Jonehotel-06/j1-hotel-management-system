/* tests-js/test-offers-and-payments-ui.js */
/* Frontend contract tests for the changes to offers, pagination size, the
   record-payment rule and guest booking counts.

   Everything under test is a pure function in the shipped scripts, so the real
   production code runs unmodified inside the vm harness.

   Run: node --test tests-js/test-offers-and-payments-ui.js   (from frontend/) */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { makeContext, loadScript, disposeAll } = require("./harness");

test.after(() => disposeAll());

function bootUtils() {
  const ctx = makeContext();
  loadScript(ctx, "js/config.js");
  loadScript(ctx, "js/utils.js");
  return ctx;
}

function bootDashboard() {
  const ctx = makeContext();
  loadScript(ctx, "js/config.js");
  loadScript(ctx, "js/utils.js");
  loadScript(ctx, "js/dashboard.js");
  return ctx;
}

/* ------------------------- Offer validity window ------------------------- */

test("offerPeriod shows BOTH the start and the end date", () => {
  const { JONE } = bootUtils().window;
  assert.equal(
    JONE.offerPeriod({ start_date: "2026-10-01", end_date: "2026-12-31" }, "mid"),
    "1 Oct 2026 \u2013 31 Dec 2026"
  );
});

test("offerPeriod degrades honestly when one bound is missing", () => {
  const { JONE } = bootUtils().window;
  assert.equal(JONE.offerPeriod({ start_date: "2026-10-01" }, "mid"), "From 1 Oct 2026");
  assert.equal(JONE.offerPeriod({ end_date: "2026-10-01" }, "mid"), "Until 1 Oct 2026");
  assert.equal(JONE.offerPeriod({}, "mid"), "");
  assert.equal(JONE.offerPeriod(null), "");
});

test("offerStatusNote distinguishes a scheduled offer from a live one", () => {
  const { JONE } = bootUtils().window;
  const today = "2026-09-20";
  assert.equal(JONE.offerStatusNote({ start_date: "2026-09-23" }, today), "Starts in 3 days");
  assert.equal(JONE.offerStatusNote({ start_date: "2026-09-21" }, today), "Starts tomorrow");
  assert.equal(JONE.offerStatusNote({ start_date: "2026-09-20" }, today), "Starts today");
});

test("offerStatusNote warns only when an offer is about to end", () => {
  const { JONE } = bootUtils().window;
  const today = "2026-09-20";
  const live = { start_date: "2026-09-01" };
  assert.equal(JONE.offerStatusNote({ ...live, end_date: "2026-09-20" }, today), "Ends today");
  assert.equal(JONE.offerStatusNote({ ...live, end_date: "2026-09-21" }, today), "Ends tomorrow");
  assert.equal(JONE.offerStatusNote({ ...live, end_date: "2026-09-25" }, today), "Ends in 5 days");
  // Far-off end date: nothing urgent to say.
  assert.equal(JONE.offerStatusNote({ ...live, end_date: "2026-12-31" }, today), "");
  assert.equal(JONE.offerStatusNote({ ...live, end_date: "2026-09-19" }, today), "Ended");
});

/* ------------------------------ Page size -------------------------------- */

test("dashboard page size is 10 and comes from APP_CONFIG", () => {
  const ctx = bootDashboard();
  assert.equal(ctx.window.APP_CONFIG.PAGE_SIZE, 10);
  assert.equal(ctx.window.JONE.dashboard.pageSize(), 10);
});

test("the pager derives total pages from the 10-row page size", () => {
  const { JONE } = bootDashboard().window;
  // 25 rows at 10/page = 3 pages, and the numbered window is rendered.
  const html = JONE.dashboard.paginationHTML({ count: 25, page: 1, page_size: 10, total_pages: 3 });
  assert.match(html, /Showing 1–10 of 25/);
  assert.match(html, /class="page-nums"/);
  assert.match(html, /data-page="3"/);
});

test("page numbers are always rendered (mobile hides nothing in markup)", () => {
  const { JONE } = bootDashboard().window;
  const html = JONE.dashboard.paginationHTML({ count: 137, page: 4, page_size: 10 });
  // The compact window keeps both ends plus the current page.
  assert.match(html, /data-page="1"/);
  assert.match(html, /data-page="14"/);
  assert.match(html, /aria-current="page"[^>]*aria-label="Page 4 \(current\)"/);
});

/* -------------------------- Record payment rule --------------------------- */

test("Record payment is offered only while a balance is outstanding", () => {
  const { JONE } = bootDashboard().window;
  const can = JONE.dashboard.canRecordPayment;
  assert.equal(can({ status: "CONFIRMED", amount_due: "25000.00" }), true);
  assert.equal(can({ status: "PENDING", amount_due: 1 }), true);
  assert.equal(can({ status: "CHECKED_IN", amount_due: 500 }), true);
});

test("a settled payment never offers Record payment", () => {
  const { JONE } = bootDashboard().window;
  const can = JONE.dashboard.canRecordPayment;
  assert.equal(can({ status: "CONFIRMED", amount_due: 0 }), false);
  assert.equal(can({ status: "CONFIRMED", amount_due: "0.00" }), false);
  assert.equal(can({ status: "CONFIRMED", amount_due: 5000, payment_status: "PAID" }), false);
  assert.equal(can({ status: "CONFIRMED", amount_due: -100 }), false);
});

test("a booking whose state cannot take money never offers Record payment", () => {
  const { JONE } = bootDashboard().window;
  const can = JONE.dashboard.canRecordPayment;
  for (const status of ["CANCELLED", "EXPIRED", "NO_SHOW", "CHECKED_OUT"]) {
    assert.equal(can({ status, amount_due: 50000 }), false, status + " must not offer the action");
  }
  assert.equal(can(null), false);
  assert.equal(can({}), false);
});

/* ----------------------------- normalizeList ------------------------------ */

test("normalizeList falls back to the configured page size, not a hardcoded 20", () => {
  const ctx = makeContext();
  ctx.window.APP_CONFIG = {
    API_BASE_URL: "https://api.example.test",
    PAGE_SIZE: 10,
    STORAGE: { AUTH: "jone.auth", SESSION: "jone.session", BOOKING: "jone.booking.draft" },
    API_ENDPOINTS: {},
  };
  loadScript(ctx, "js/api.js");
  const rows = Array.from({ length: 10 }, (_, i) => ({ id: i + 1 }));
  const out = ctx.window.API.normalizeList(rows, { count: 34, page: 1 });
  assert.equal(out.pageSize, 10);
  assert.equal(out.totalPages, 4);
});
