/* tests-js/test-extend-stay-modal-e2e.js */
/* The REAL "Extend stay" dialog, driven in a real DOM.

   This is the guard the reported failure did not have: no test in this
   repository ever opened the extend dialog. Here the shipped js/dashboard.js,
   js/api.js, js/ui.js and js/utils.js are executed against a jsdom document and
   a stubbed API, and the test types into the two boxes the desk uses, reads the
   price the dialog paints, presses Confirm, and inspects exactly what was sent.

   Covered:
     · date typed  -> POST …/extend-quote/ {new_check_out}  -> price painted
                   -> Confirm -> POST …/extend-stay/ {new_check_out, provider}
     · nights typed-> POST …/extend-quote/ {additional_nights} -> same path
     · the quote endpoint answering with Django's HTML 404 (the exact cause of
       "The requested item could not be found.") -> the desk is told the API
       deployment does not include the feature, and nothing is submitted
     · the stay the server says cannot be extended -> inputs disabled up front

   jsdom is an optional dev dependency here (npm i jsdom --no-save). When it is
   not installed the whole file SKIPS with that reason printed — it never passes
   silently.

   Run: node --test tests-js/test-extend-stay-modal-e2e.js   (from frontend/) */
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

const QUOTE = {
  booking_reference: "J1-TEST-00007", rooms: ["101"], check_in: "2026-11-14",
  current_check_out: "2026-11-16", new_check_out: "2026-11-18", additional_nights: 2,
  total_nights_after_extension: 4, nightly_rate: "20000.00", number_of_rooms: 1,
  extension_amount: "40000.00", currency: "NGN", amount_due_before_extension: "0.00",
  available: true, conflicts: [], extension_count: 0, can_extend: true,
};

const BOOKING = {
  id: 7, booking_reference: "J1-TEST-00007", guest_name: "Ada Obi", room_numbers: ["101"],
  check_in: "2026-11-14", check_out: "2026-11-16", status: "CHECKED_IN",
  currency: "NGN", amount_due: "0.00", number_of_rooms: 1,
};

function jsonResponse(payload, status = 200) {
  return {
    ok: status < 400,
    status,
    headers: { get: (k) => (String(k).toLowerCase() === "content-type" ? "application/json" : null) },
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  };
}

function html404() {
  return {
    ok: false,
    status: 404,
    headers: { get: (k) => (String(k).toLowerCase() === "content-type" ? "text/html" : null) },
    json: async () => { throw new Error("not json"); },
    text: async () => "<!doctype html><html><head><title>Not Found</title></head><body>Not Found</body></html>",
  };
}

/* Boot a document with the shipped scripts, then open the dialog. */
async function openDialog({ quoteResponse, history } = {}) {
  const dom = new JSDOM("<!doctype html><html><body></body></html>", {
    url: "https://www.jonehotel.test/dashboard/check-in.html",
    runScripts: "outside-only",
    pretendToBeVisual: true,
  });
  const { window } = dom;
  const calls = [];
  window.fetch = async (url, opts = {}) => {
    const u = String(url);
    const method = (opts.method || "GET").toUpperCase();
    calls.push({ method, url: u, body: opts.body ? JSON.parse(opts.body) : null });
    if (u.includes("/extend-quote/")) {
      return quoteResponse === "html404" ? html404() : jsonResponse({ success: true, data: QUOTE });
    }
    if (u.includes("/extend-stay/")) {
      return jsonResponse({ success: true, message: "Stay extended by 2 night(s).", data: { extension: {}, booking: {} } });
    }
    if (u.includes("/extensions/")) {
      return jsonResponse({ success: true, data: Object.assign({ results: [], count: 0, can_extend: true, can_extend_reason: "" }, history || {}) });
    }
    return jsonResponse({ success: true, data: { results: [], count: 0 } });
  };
  window.matchMedia = window.matchMedia || (() => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }));

  for (const f of ["js/config.js", "js/utils.js", "js/icons.js", "js/api.js", "js/ui.js", "js/auth.js", "js/dashboard.js"]) {
    window.eval(fs.readFileSync(path.join(FRONTEND, f), "utf8"));
  }

  const done = window.JONE.dashboard.extendStayModal(BOOKING, {});
  await new Promise((r) => setTimeout(r, 60));
  const doc = window.document;
  return { window, doc, calls, done, panel: doc.querySelector(".modal-panel") };
}

function type(window, el, value) {
  el.value = value;
  el.dispatchEvent(new window.Event("input", { bubbles: true }));
}

test("a typed date is priced by the server and then extended", { skip: SKIP }, async () => {
  const { window, doc, calls } = await openDialog();
  const dateInput = doc.querySelector("[data-ex-date]");
  assert.ok(dateInput, "the date box must exist");
  assert.equal(dateInput.min, "2026-11-17", "the earliest extension is the day after the current check-out");

  type(window, dateInput, "2026-11-18");
  await new Promise((r) => setTimeout(r, 400));

  const amount = doc.querySelector("[data-ex-amount]");
  const nights = doc.querySelector("[data-ex-nightsout]");
  const submit = doc.querySelector('.modal-panel button[type="submit"]');
  assert.equal(amount.textContent.replace(/\u00a0/g, " "), "\u20a640,000.00", "the price the desk is shown is the server's");
  assert.equal(nights.textContent, "2 nights");
  assert.equal(submit.disabled, false, "a priced extension can be confirmed");

  const quoteCall = calls.find((c) => c.url.includes("/extend-quote/"));
  assert.equal(JSON.stringify(quoteCall.body), '{"new_check_out":"2026-11-18"}');

  doc.querySelector("form.modal-body").dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
  await new Promise((r) => setTimeout(r, 200));

  const confirmCall = calls.find((c) => c.url.includes("/extend-stay/"));
  assert.ok(confirmCall, "Confirm must reach extend-stay");
  assert.equal(JSON.stringify(confirmCall.body), '{"new_check_out":"2026-11-18","provider":"CASH","notes":""}');
  assert.equal(doc.querySelector(".modal-panel"), null, "and the dialog closes on success");
});

test("typed nights travel as additional_nights", { skip: SKIP }, async () => {
  const { window, doc, calls } = await openDialog();
  const nightsInput = doc.querySelector("[data-ex-nights]");
  type(window, nightsInput, "3");
  await new Promise((r) => setTimeout(r, 400));
  assert.equal(doc.querySelector("[data-ex-date]").value, "", "date and nights are mutually exclusive");
  const quoteCall = calls.find((c) => c.url.includes("/extend-quote/"));
  assert.equal(JSON.stringify(quoteCall.body), '{"additional_nights":3}');

  doc.querySelector("form.modal-body").dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
  await new Promise((r) => setTimeout(r, 200));
  const confirmCall = calls.find((c) => c.url.includes("/extend-stay/"));
  assert.equal(JSON.stringify(confirmCall.body), '{"additional_nights":3,"provider":"CASH","notes":""}');
});

test("an endpoint the API does not serve is reported as that, and nothing is sent", { skip: SKIP }, async () => {
  const { window, doc, calls } = await openDialog({ quoteResponse: "html404" });
  type(window, doc.querySelector("[data-ex-date]"), "2026-11-18");
  await new Promise((r) => setTimeout(r, 400));

  const datesError = doc.querySelector("[data-ex-dates-error]");
  assert.match(datesError.textContent, /does not recognise this request/i);
  assert.ok(!/requested item could not be found/i.test(datesError.textContent), "the reported wording must be gone");

  const submit = doc.querySelector('.modal-panel button[type="submit"]');
  assert.equal(submit.disabled, true, "there is no price, so there is nothing to confirm");
  doc.querySelector("form.modal-body").dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
  await new Promise((r) => setTimeout(r, 150));
  assert.equal(calls.filter((c) => c.url.includes("/extend-stay/")).length, 0, "no extension is attempted");
  assert.match(
    doc.querySelector("[data-ex-error]").textContent,
    /does not recognise this request/i,
    "pressing Confirm repeats the real reason instead of blaming the form"
  );
});

test("a stay the server says cannot be extended is refused before the desk types", { skip: SKIP }, async () => {
  const { doc } = await openDialog({
    history: { can_extend: false, can_extend_reason: "This booking has no physical room assigned yet." },
  });
  assert.equal(doc.querySelector("[data-ex-date]").disabled, true);
  assert.equal(doc.querySelector("[data-ex-nights]").disabled, true);
  assert.match(doc.querySelector("[data-ex-dates-error]").textContent, /no physical room assigned/i);
});
