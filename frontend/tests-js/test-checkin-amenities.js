/* tests-js/test-checkin-amenities.js */
/* Recording the basic amenities handed to a guest AT CHECK-IN.

   Reported symptom (front desk): "There is nowhere to record that tissue,
   brush, paste and soap has been given to the guest checking in."

   The audit found the feature was *not* missing — it was unreachable, for three
   separate reasons, and this suite pins all three:

   1. THE GUEST PICKER WAS EMPTY. js/amenities.js asked for
      GET /api/bookings/guests/?page_size=500. There is no such route: it is
      swallowed by the guest booking detail route as <lookup> = "guests", so the
      request fails, the .catch() swallowed the error and the required "Guest *"
      <select> kept a single "— Select —" option. The Issue form could therefore
      never be submitted. js/breakfast.js had the identical bug, which is why the
      breakfast issuance pickers were empty too. The staff list is
      /api/admin/guests/.
   2. NOTHING ON THE ARRIVAL PATH ASKED. dashboard/check-in.html checked the
      guest in and stopped. The toiletries changed hands at that counter and no
      screen asked about them, so they left the drawer unrecorded.
   3. THE ISSUE DROPDOWN OFFERED THE WHOLE WAREHOUSE. Every item in the
      catalogue could be picked, including items Reception does not hold, so the
      only possible outcome for those was a rejection after the guest had already
      been promised the item.

   Run: node --test tests-js/test-checkin-amenities.js   (from frontend/) */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { makeContext, loadScript, FRONTEND, disposeAll } = require("./harness");

test.after(() => disposeAll());

function boot() {
  const ctx = makeContext();
  loadScript(ctx, "js/config.js");
  loadScript(ctx, "js/utils.js");
  ctx.window.API = { setTokenProvider() { }, getToken() { return null; } };
  loadScript(ctx, "js/auth.js");
  loadScript(ctx, "js/dashboard.js");
  return ctx.window.JONE.dashboard;
}

function source(rel) {
  return fs.readFileSync(path.join(FRONTEND, rel), "utf8");
}

/* The payload the server returns for GET .../reception/amenities/stock/. */
const STOCK = {
  location: { id: 4, code: "RECEPTION", name: "Reception" },
  balances: [
    { item_id: 31, sku: "AMN-TISSUE", name: "Tissue Roll", base_unit: "ROLL", quantity: "6.0000" },
    { item_id: 12, sku: "AMN-SOAP", name: "Guest Soap", base_unit: "BAR", quantity: "12.0000" },
    { item_id: 14, sku: "AMN-BRUSH", name: "Toothbrush", base_unit: "PC", quantity: "0.0000" },
    { item_id: 15, sku: "AMN-PASTE", name: "Toothpaste", base_unit: "TUBE", quantity: "3.0000" },
    { item_id: 90, sku: "SLIP-01", name: "Slippers", base_unit: "PAIR", quantity: "4.0000" },
  ],
};

/* ------------------------------- the shelf --------------------------------- */

test("the picker lists only what Reception actually holds", () => {
  const dashboard = boot();
  const catalogue = dashboard.amenityIssueCatalogue(STOCK);
  const ids = Array.from(catalogue, (row) => row.item_id);
  assert.deepEqual(ids, [12, 15, 31, 90], "soap, paste, tissue first (fixed order), then other real stock");
  assert.ok(!ids.includes(14), "a zero-balance item must not be offered — it can only be refused");
});

test("the four basic amenities are named in plain words", () => {
  const dashboard = boot();
  const byId = {};
  Array.from(dashboard.amenityIssueCatalogue(STOCK)).forEach((row) => { byId[row.item_id] = row; });
  assert.equal(byId[12].name, "Soap");
  assert.equal(byId[15].name, "Toothpaste");
  assert.equal(byId[31].name, "Tissue");
  assert.equal(byId[90].name, "Slippers", "non-basic stock keeps its real name");
  assert.equal(byId[12].available, 12);
  assert.equal(byId[12].unit, "BAR");
});

test("an empty or malformed shelf yields an empty catalogue, never a crash", () => {
  const dashboard = boot();
  assert.deepEqual(Array.from(dashboard.amenityIssueCatalogue(null)), []);
  assert.deepEqual(Array.from(dashboard.amenityIssueCatalogue({})), []);
  assert.deepEqual(Array.from(dashboard.amenityIssueCatalogue({ balances: [] })), []);
});

/* ------------------------------ the decision ------------------------------- */

test("picked quantities become one submittable line per item", () => {
  const dashboard = boot();
  const catalogue = Array.from(dashboard.amenityIssueCatalogue(STOCK));
  const plan = dashboard.amenityIssuePlan(catalogue, { 12: "1", 15: "1", 31: "2", 90: "0" });
  assert.equal(plan.problems.length, 0);
  /* deepEqual across the vm realm compares prototypes — compare the data. */
  assert.equal(
    JSON.stringify(Array.from(plan.lines, (line) => [line.item_id, line.quantity])),
    JSON.stringify([[12, 1], [15, 1], [31, 2]]),
    "zero picks are not submitted"
  );
});

test("the desk cannot give away more than the shelf holds", () => {
  const dashboard = boot();
  const catalogue = Array.from(dashboard.amenityIssueCatalogue(STOCK));
  const plan = dashboard.amenityIssuePlan(catalogue, { 12: "99" });
  assert.equal(plan.lines.length, 0, "an over-draw produces no line at all");
  assert.equal(plan.problems.length, 1);
  assert.match(plan.problems[0], /Only 12 BAR of Soap/);
});

test("an item that left the shelf while the dialog was open is refused, not submitted", () => {
  const dashboard = boot();
  const catalogue = Array.from(dashboard.amenityIssueCatalogue(STOCK));
  const plan = dashboard.amenityIssuePlan(catalogue, { 999: "1" });
  assert.equal(plan.lines.length, 0);
  assert.match(plan.problems[0], /no longer on the Reception shelf/);
});

/* ---------------------------- idempotency key ------------------------------ */

test("the same amenity on the same stay on the same day reuses its key", () => {
  const dashboard = boot();
  const a = dashboard.amenityIssueKey("JN-2026-0007", 12, "2026-11-15");
  const b = dashboard.amenityIssueKey("JN-2026-0007", 12, "2026-11-15");
  assert.equal(a, b, "a double tap can never record — or deduct — twice");
});

test("a different day, stay or item is a genuinely new issuance", () => {
  const dashboard = boot();
  const base = dashboard.amenityIssueKey("JN-2026-0007", 12, "2026-11-15");
  assert.notEqual(base, dashboard.amenityIssueKey("JN-2026-0007", 12, "2026-11-16"));
  assert.notEqual(base, dashboard.amenityIssueKey("JN-2026-0008", 12, "2026-11-15"));
  assert.notEqual(base, dashboard.amenityIssueKey("JN-2026-0007", 15, "2026-11-15"));
});

/* --------------------------- what was recorded ----------------------------- */

test("already-recorded amenities are summarised for the desk", () => {
  const dashboard = boot();
  assert.equal(dashboard.amenityIssueSummary(null), "");
  assert.equal(dashboard.amenityIssueSummary({ results: [] }), "");
  assert.equal(
    dashboard.amenityIssueSummary({
      results: [
        { item_name: "Soap", quantity: "1.0000" },
        { item_name: "Tissue Roll", quantity: "2.0000" },
      ],
    }),
    "Soap \u00d7 1.0000, Tissue Roll \u00d7 2.0000"
  );
});

/* ------------------------- the wiring that was missing --------------------- */

test("check-in asks for the amenities AFTER the server confirms the check-in", () => {
  const html = source("dashboard/check-in.html");
  const checkInAt = html.indexOf("API.checkInBooking(ref)");
  /* The page also carries a per-row recorder for later issuance; the prompt this
     test is about is the FIRST one that follows the check-in call. */
  const modalAt = html.indexOf("JONE.dashboard.amenityIssueModal", checkInAt);
  assert.ok(checkInAt > -1, "the page must still check the guest in through the API");
  assert.ok(modalAt > checkInAt, "the amenity prompt belongs after a confirmed check-in, not before it");
  const between = html.slice(checkInAt, modalAt);
  assert.ok(!/catch\s*\(/.test(between), "a failed check-in must not fall through to the amenity prompt");
  assert.match(
    html.slice(modalAt, modalAt + 320),
    /catch\s*\(/,
    "the prompt is best-effort: it can never turn a successful check-in into an error"
  );
});

test("the amenity prompt exists as a shared dashboard component", () => {
  const dashboard = boot();
  for (const name of ["amenityIssueModal", "amenityIssueCatalogue", "amenityIssuePlan", "amenityIssueSummary", "amenityIssueKey"]) {
    assert.equal(typeof dashboard[name], "function", "JONE.dashboard." + name + " must be exported");
  }
});

test("the amenity prompt talks to the endpoints that already own the data", () => {
  const js = source("js/dashboard.js");
  assert.match(js, /\/api\/admin\/service-requests\/reception\/amenities\/stock\//);
  assert.match(js, /\/api\/admin\/service-requests\/reception\/amenities\/issue\//);
  assert.match(js, /\/api\/admin\/service-requests\/reception\/amenities\/issues\//);
  assert.ok(
    !/\/api\/admin\/service-requests\/reception\/amenities\/issue-set\//.test(js),
    "no second issuance endpoint: one item, one movement, one call"
  );
});

/* --------------------- root cause: the dead guest route ------------------- */

test("the guest pickers use the staff guest list, not a route that does not exist", () => {
  for (const file of ["js/amenities.js", "js/breakfast.js"]) {
    const js = source(file);
    assert.ok(
      !/API\.get\(\s*"\/api\/bookings\/guests\//.test(js),
      file + ' must not request /api/bookings/guests/ — that path resolves to the guest booking ' +
        'detail view with <lookup>="guests" and returns no guest list'
    );
    assert.match(js, /\/api\/admin\/guests\//, file + " must read the staff guest list");
  }
});

test("the issue dropdown is fed by the Reception shelf, not the whole catalogue", () => {
  const js = source("js/amenities.js");
  assert.match(js, /function loadIssueItemSelect/, "a dedicated issue dropdown loader must exist");
  const fn = js.slice(js.indexOf("function loadIssueItemSelect"), js.indexOf("function loadLocationSelect"));
  assert.match(fn, /reception\/amenities\/stock\//, "the issue dropdown reads the Reception balances");
  assert.match(fn, /Nothing on the Reception shelf yet/, "and says so plainly when the shelf is empty");
});

test("the issue form can attach the issuance to the stay it belongs to", () => {
  const html = source("dashboard/amenities.html");
  assert.match(html, /data-amenity-guest-search/, "the desk must be able to search guests by name");
  assert.match(html, /name="booking_id"/, "the staying booking travels with the issuance");
  const js = source("js/amenities.js");
  assert.match(js, /applyDeepLink|function applyDeepLink/, "?booking=REF must preselect the stay");
  assert.match(js, /booking_id/, "onIssue must send the booking it was told about");
});

test("the desk can SEE what was already recorded for a stay, not only record it", () => {
  const html = source("dashboard/check-in.html");
  assert.match(html, /reception\/amenities\/issues\/\?page_size=100/, "the page reads the issuance history once per load");
  assert.match(html, /function amenityCell/, "each in-house row carries an amenity control");
  assert.match(html, /data-amenity-btn/, "and that control opens the recorder for that stay");
  assert.match(html, /var n=amenityCounts\[String\(b\.id\)\]/, "the label states how many items are already recorded");
  assert.match(html, /n\+" amenit"/, "e.g. \"4 amenities\", not a bare icon");
});

test("the amenity history read is best-effort and can never break the check-in screen", () => {
  const html = source("dashboard/check-in.html");
  const at = html.indexOf("async function loadAmenityMarks");
  assert.ok(at > -1);
  const body = html.slice(at, at + 700);
  assert.match(body, /catch\s*\(/, "a refused or failed history read must degrade to no marks");
  assert.match(body, /amenityCounts=\{\}/, "and must leave the page with a clean, empty map");
});
