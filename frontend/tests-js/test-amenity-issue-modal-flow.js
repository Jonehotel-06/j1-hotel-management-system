/* tests-js/test-amenity-issue-modal-flow.js */
/* The REAL "Record amenities given to the guest" dialog, driven in a real DOM.

   Reported symptom (front desk): clicking the amenities action for a booking
   popped "This booking has no guest record — reload the page and try again."
   even though the booking and its guest exist.

   The audit traced it to the booking→guest retrieval inside
   JONE.dashboard.amenityIssueModal (js/dashboard.js): the booking-detail read
   was wrapped in .catch(() => null), so EVERY failure of that read — expired
   session, network blip, or a deployed API out of step with this frontend —
   was silently converted into the misleading "no guest record" toast. The
   dialog also never distinguished a genuinely guest-less booking from a
   failed read.

   Here the shipped js/dashboard.js, js/api.js, js/ui.js and js/utils.js run
   against a jsdom document and a stubbed API, and the dialog is opened the
   way the desk opens it: with a booking REFERENCE, exactly as the check-in
   screen and the in-house list pass it.

   Covered:
     · reference lookup  -> booking detail read -> the issue dialog opens
                           (the reported "no guest record" toast must NOT fire)
     · the already-recorded pre-check is queried by the numeric booking id,
       never by the reference
     · a failed booking-detail read is reported as a failed read, not as a
       missing guest
     · a booking that genuinely has no guest record is refused with that
       message (safe missing-record handling) and no shelf is read
     · a failed pre-check never blocks the dialog (fail-safe)

   jsdom is an optional dev dependency here (npm i jsdom --no-save). When it
   is not installed the whole file SKIPS with that reason printed — it never
   passes silently.

   Run: node --test tests-js/test-amenity-issue-modal-flow.js   (from frontend/) */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { FRONTEND } = require("./harness");

let JSDOM = null;
try {
  ({ JSDOM } = require(path.join(FRONTEND, "node_modules", "jsdom")));
} catch (_) {
  JSDOM = null;
}

const SKIP = JSDOM ? false : "jsdom is not installed (npm i jsdom --no-save) — the dialog was NOT exercised";

/* The payload the server returns for GET .../reception/amenities/stock/. */
const STOCK = {
  location: { id: 4, code: "RECEPTION", name: "Reception" },
  balances: [
    { item_id: 31, sku: "AMN-TISSUE", name: "Tissue Roll", base_unit: "ROLL", quantity: "6.0000" },
    { item_id: 12, sku: "AMN-SOAP", name: "Guest Soap", base_unit: "BAR", quantity: "12.0000" },
  ],
};

/* The booking detail the desk's booking reference resolves to. */
const BOOKING_DETAIL = {
  id: 7,
  booking_reference: "J1-TEST-00007",
  guest: { id: 3, full_name: "Ada Obi", email: "ada@example.com" },
  room_assignments: [{ id: 11, room_number: "101", arrival_status: "CHECKED_IN" }],
};

const LOOKUP = "J1-TEST-00007";

function jsonResponse(payload, status = 200) {
  return {
    ok: status < 400,
    status,
    headers: { get: (k) => (String(k).toLowerCase() === "content-type" ? "application/json" : null) },
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  };
}

/* Boot a document with the shipped scripts and open the amenity dialog the
   way the desk does. `bookingDetail` controls how the booking-detail read
   answers: "ok" | "no-guest" | "fail" (the two failure modes), and
   `issuesFail` forces the already-recorded pre-check to fail. */
async function openAmenityDialog({ bookingDetail = "ok", issuesFail = false } = {}) {
  const dom = new JSDOM("<!doctype html><html><body></body></html>", {
    url: "https://www.jonehotel.test/dashboard/check-in.html",
    runScripts: "outside-only",
    pretendToBeVisual: true,
  });
  const { window } = dom;
  const calls = [];
  const toasts = [];

  window.fetch = async (url, opts = {}) => {
    const u = String(url);
    calls.push({ method: (opts.method || "GET").toUpperCase(), url: u, body: opts.body ? JSON.parse(opts.body) : null });

    if (u.includes("/api/admin/bookings/" + encodeURIComponent(LOOKUP) + "/")) {
      if (bookingDetail === "fail") {
        return jsonResponse({ detail: "Internal server error." }, 500);
      }
      const data = bookingDetail === "no-guest"
        ? Object.assign({}, BOOKING_DETAIL, { guest: null })
        : BOOKING_DETAIL;
      return jsonResponse({ success: true, data });
    }
    if (u.includes("/reception/amenities/stock/")) {
      return jsonResponse({ success: true, data: STOCK });
    }
    if (u.includes("/reception/amenities/issues/")) {
      if (issuesFail) return jsonResponse({ detail: "Internal server error." }, 500);
      return jsonResponse({ success: true, data: { results: [] } });
    }
    return jsonResponse({ success: true, data: {} });
  };
  window.matchMedia = window.matchMedia || (() => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }));

  for (const f of ["js/config.js", "js/utils.js", "js/icons.js", "js/api.js", "js/ui.js", "js/auth.js", "js/dashboard.js"]) {
    window.eval(fs.readFileSync(path.join(FRONTEND, f), "utf8"));
  }

  /* Capture the toasts the dialog shows instead of whatever the real UI
     paints: this suite is about exactly which sentence the desk sees. */
  const origToast = window.JONE.ui.toast.bind(window.JONE.ui);
  window.JONE.ui.toast = (msg, kind) => {
    toasts.push({ msg: String(msg), kind });
    return origToast(msg, kind);
  };

  const modalPromise = window.JONE.dashboard.amenityIssueModal({ lookup: LOOKUP, title: "Amenities given to Ada Obi" });
  /* Let the async booking read, shelf read and pre-check settle. The submit
     path stays pending by design (nothing submits in these tests). */
  await new Promise((r) => setTimeout(r, 80));
  const doc = window.document;
  return { window, doc, calls, toasts, modalPromise };
}

/* Resolve or give up — used where the dialog is expected to finish on its
   own (the error paths). */
async function settle(promise, ms = 80) {
  let value;
  await Promise.race([
    promise.then((v) => { value = v; }, () => { value = "rejected"; }),
    new Promise((r) => setTimeout(r, ms)),
  ]);
  return value;
}

test("a booking REFERENCE opens the record dialog — no 'no guest record' toast", { skip: SKIP }, async () => {
  const { doc, toasts, modalPromise } = await openAmenityDialog();
  assert.ok(doc.querySelector(".modal-panel"), "the amenity issue dialog must open for a reference lookup");
  assert.equal(
    toasts.filter((t) => /no guest record/i.test(t.msg)).length, 0,
    "the reported misleading toast must not fire when the booking resolves"
  );
  /* The dialog stays open (nothing submitted); its promise is pending. */
  assert.equal(await settle(modalPromise, 30), undefined, "the dialog promise stays pending until submitted or closed");
});

test("the already-recorded pre-check is queried by the numeric booking id", { skip: SKIP }, async () => {
  const { calls } = await openAmenityDialog();
  const issuesCall = calls.find((c) => c.url.includes("/reception/amenities/issues/"));
  assert.ok(issuesCall, "the pre-check must be read");
  assert.ok(issuesCall.url.includes("booking_id=7"), "the pre-check must use the numeric booking id — the API filters booking_id, not the reference");
  assert.ok(!issuesCall.url.includes(LOOKUP), "the reference must not be sent as the booking_id filter");
});

test("a failed booking-detail read is reported as a failed read, never as a missing guest", { skip: SKIP }, async () => {
  const { toasts, calls, modalPromise } = await openAmenityDialog({ bookingDetail: "fail" });
  const value = await settle(modalPromise);
  assert.equal(value, null, "the dialog must stop cleanly");
  assert.ok(
    toasts.some((t) => /booking details could not be loaded/i.test(t.msg)),
    "the real failure must be reported: " + JSON.stringify(toasts)
  );
  assert.equal(
    toasts.filter((t) => /no guest record/i.test(t.msg)).length, 0,
    "a failed read must never masquerade as a missing guest record"
  );
  assert.equal(
    calls.filter((c) => c.url.includes("/reception/amenities/")).length, 0,
    "nothing else is fetched when the booking could not be read"
  );
});

test("a booking with genuinely no guest record is refused, and the shelf is not read", { skip: SKIP }, async () => {
  const { toasts, calls, modalPromise } = await openAmenityDialog({ bookingDetail: "no-guest" });
  const value = await settle(modalPromise);
  assert.equal(value, null, "the dialog must stop cleanly");
  assert.ok(
    toasts.some((t) => /This booking has no guest record/i.test(t.msg)),
    "the guest-record refusal keeps its message: " + JSON.stringify(toasts)
  );
  assert.equal(
    calls.filter((c) => c.url.includes("/reception/amenities/")).length, 0,
    "there is no guest to record against, so the shelf is never read"
  );
});

test("a failed already-recorded pre-check never blocks the dialog", { skip: SKIP }, async () => {
  const { doc, toasts } = await openAmenityDialog({ issuesFail: true });
  assert.ok(doc.querySelector(".modal-panel"), "the dialog must still open");
  assert.equal(
    toasts.filter((t) => /no guest record/i.test(t.msg)).length, 0,
    "a failed pre-check is not a guest-record problem"
  );
});
