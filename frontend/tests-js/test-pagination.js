/* tests-js/test-pagination.js */
/* Tests for the shared dashboard list pagination:
   - js/api.js normalizeList() totalPages derivation
   - js/dashboard.js paginationHTML() (the ONE reusable pager markup builder)

   The backend paginates every list (DRF StandardPagination); these tests pin
   the frontend contract around it: correct range line, a compact numbered
   window, disabled bounds, and no markup for single-page results.

   Run: node --test tests-js/test-pagination.js   (from frontend/) */
"use strict";

const test = require("node:test");
const assert = require("node:assert");
const { makeContext, loadScript, disposeAll } = require("./harness");

test.after(() => disposeAll());

/* ------------------------------- normalizeList --------------------------- */
function bootApi() {
  const ctx = makeContext({ hostname: "www.jonehotel.test", pathname: "/index.html" });
  ctx.window.APP_CONFIG = {
    API_BASE_URL: "https://api.example.test",
    STORAGE: { AUTH: "jone.auth", SESSION: "jone.session", BOOKING: "jone.booking.draft" },
    API_ENDPOINTS: {},
  };
  loadScript(ctx, "js/api.js");
  return ctx;
}

test("normalizeList exposes totalPages from the contract pagination block", () => {
  const ctx = bootApi();
  const norm = ctx.window.API.normalizeList(
    [1, 2, 3],
    { count: 45, page: 1, page_size: 20, total_pages: 3, next: "x", previous: null }
  );
  assert.equal(norm.count, 45);
  assert.equal(norm.page, 1);
  assert.equal(norm.pageSize, 20);
  assert.equal(norm.totalPages, 3);
  assert.equal(norm.items.length, 3);
});

test("normalizeList derives totalPages when the block omits it", () => {
  const ctx = bootApi();
  const norm = ctx.window.API.normalizeList([], { count: 7, page: 1, page_size: 5 });
  assert.equal(norm.totalPages, 2);
  const none = ctx.window.API.normalizeList(null, null);
  assert.equal(none.totalPages, 1);
  assert.equal(none.items.length, 0);
});

/* ------------------------------ paginationHTML --------------------------- */
function bootDashboard() {
  const ctx = makeContext({ hostname: "www.jonehotel.test", pathname: "/dashboard/bookings.html" });
  ctx.window.APP_CONFIG = { API_BASE_URL: "https://api.example.test", STORAGE: {}, API_ENDPOINTS: {} };
  loadScript(ctx, "js/utils.js");
  loadScript(ctx, "js/dashboard.js");
  return ctx;
}

const PGMETA = { count: 137, page: 2, page_size: 20, total_pages: 7 };

test("pager shows the result range for the requested page", () => {
  const ctx = bootDashboard();
  const html = ctx.window.JONE.dashboard.paginationHTML(PGMETA);
  assert.match(html, /Showing 21–40 of 137/);
  // Current page carries aria-current and is disabled (it's a position, not a link).
  assert.match(html, /aria-current="page"[^>]*aria-label="Page 2 \(current\)"/);
  // Prev is enabled on a middle page, disabled on page 1.
  assert.match(html, /data-page="1" aria-label="Previous page"(?! disabled)/);
  const first = ctx.window.JONE.dashboard.paginationHTML({ ...PGMETA, page: 1, next: null });
  assert.match(first, /data-page="0" aria-label="Previous page" disabled/);
});

test("pager attaches a role/aria-live range cell within one control", () => {
  const ctx = bootDashboard();
  const html = ctx.window.JONE.dashboard.paginationHTML(PGMETA);
  // Exposes an aria-live region for screen readers
  assert.match(html, /aria-live="polite"/);
  // Every actionable element is a labelled button (keyboard/touch operable).
  const buttons = html.match(/<button/g) || [];
  assert.ok(buttons.length >= 2);
  assert.match(html, /aria-label="Next page"/);
});

test("numbered window compacts long ranges with ellipses at both ends", () => {
  const ctx = bootDashboard();
  const html = ctx.window.JONE.dashboard.paginationHTML({ count: 300, page: 8, page_size: 20, total_pages: 15 });
  assert.match(html, />8<\/button>/);
  assert.match(html, />1<\/button>/);
  assert.match(html, />15<\/button>/);
  assert.match(html, /page-ellipsis/);
  // requested window: 1 … 7 8 9 … 15
  for (const n of [7, 8, 9, 15]) {
    assert.ok(html.includes('data-page="' + n + '"'), "page " + n + " rendered");
  }
});

test("no controls for a single page or empty result", () => {
  const ctx = bootDashboard();
  assert.equal(ctx.window.JONE.dashboard.paginationHTML({ count: 0, page: 1, page_size: 20, total_pages: 1 }), "");
  assert.equal(ctx.window.JONE.dashboard.paginationHTML({ count: 12, page: 1, page_size: 20, total_pages: 1 }), "");
  assert.equal(ctx.window.JONE.dashboard.paginationHTML(null), "");
});

test("accepts the normalizeList() shape too (additive normalisation)", () => {
  const ctx = bootDashboard();
  const html = ctx.window.JONE.dashboard.paginationHTML({ count: 41, page: 3, pageSize: 20, totalPages: 3 });
  assert.match(html, /Showing 41–41 of 41/);
  assert.match(html, /aria-label="Next page" disabled/);
});

test("unpaged meta without count still renders Page x of y", () => {
  const ctx = bootDashboard();
  const html = ctx.window.JONE.dashboard.paginationHTML({ page: 2, page_size: 20, total_pages: 3 });
  assert.match(html, /Page 2 of 3/);
});
