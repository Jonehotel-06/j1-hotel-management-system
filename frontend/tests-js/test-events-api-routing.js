/* tests-js/test-events-api-routing.js

   Regression guard for the three reported Event-page failures.

   Root cause: events.js derived its base from ReceptionServices.base
   ("/api/admin/service-requests/") and appended "events/", so the page called
   /api/admin/service-requests/events/... — a namespace the backend does not
   serve. That produced the "Event spaces could not be loaded" option text, the
   "server does not recognise this request" banner (HTML 404) and "Service
   request not found" (the path was captured by the service-request detail
   route with reference="events").

   What is proven here:
     · locations, list, detail, payment and receipt calls all target
       /api/admin/events/... and never /api/admin/service-requests/events/;
     · guests still come from the service-requests namespace (unchanged);
     · a failed locations read leaves the page usable and its Retry button
       actually re-issues the request;
     · a failed events read offers a working Retry instead of a dead message.

   Run: node --test tests-js/test-events-api-routing.js
*/
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
const SKIP = JSDOM ? false : "jsdom is not installed (npm i jsdom --no-save)";

const PAGE = `<!DOCTYPE html><html><body class="reception-services">
<button data-tab="events"></button><button data-tab="new"></button>
<section data-panel="events">
  <form data-events-filter>
    <select name="location_id" id="ev-location"></select>
  </form>
  <button data-events-refresh></button>
  <div data-events-container></div>
</section>
<section data-panel="new">
  <form data-event-form>
    <input data-event-guest-search>
    <select name="guest_id" id="ev-guest"></select>
    <input name="first_name" id="ev-first"><input name="last_name" id="ev-last">
    <input name="phone" id="ev-phone"><input name="email" id="ev-email">
    <select name="location_id" id="ev-space"></select>
    <input name="event_type" id="ev-type"><input name="event_date">
    <input name="start_time"><input name="end_time">
    <input name="expected_guests" value="1"><input name="total_charged">
    <textarea name="notes"></textarea>
    <input name="amount_paid" id="ev-paid" value="0">
    <select name="payment_method" id="ev-method"></select>
    <input name="payment_reference" id="ev-reference">
    <input name="override_reason">
    <button type="submit" data-event-submit></button>
  </form>
  <div data-event-form-message></div>
  <p data-event-guest-note></p>
</section>
</body></html>`;

const LOCATIONS = [
  { id: 1, code: "INSIDE_BAR", name: "Inside Bar" },
  { id: 2, code: "OUTSIDE_BAR", name: "Outside Bar" },
  { id: 3, code: "HOTEL_COMPOUND", name: "Hotel Compound" },
];
const GUESTS = [{ id: 7, full_name: "Ada Obi", phone: "08031234567", email: "ada@example.com", bookings: [] }];
const EVENTS = [{
  id: 11, reference: "EVT-1", guest_name: "Ada Obi", guest_phone: "08031234567",
  location_name: "Inside Bar", event_date: "2026-12-01", start_time: "18:00:00",
  end_time: "22:00:00", total_charged: "150000.00", amount_paid: "100000.00",
  outstanding_balance: "50000.00", payment_status: "PARTIALLY_PAID", status: "UPCOMING",
}];

/* ``failFirst`` makes the first N locations/list reads fail, to exercise the
   error + retry states. */
function boot(opts) {
  opts = opts || {};
  const dom = new JSDOM(PAGE, { url: "https://staff.jonehotels.com/dashboard/events.html", runScripts: "outside-only" });
  const { window } = dom;
  const calls = [];
  let locationsFailures = opts.failLocations || 0;
  let listFailures = opts.failList || 0;
  window.APP_CONFIG = { API_ENDPOINTS: { events: "/api/admin/events/", serviceRequests: "/api/admin/service-requests/" } };
  window.API = {
    get: async (url) => {
      calls.push(url);
      if (url.indexOf("/api/admin/service-requests/events") === 0) {
        const err = new Error("The server does not recognise this request.");
        err.status = 404;
        throw err;
      }
      if (url.indexOf("events/locations/") !== -1) {
        if (locationsFailures > 0) {
          locationsFailures -= 1;
          const err = new Error("Event locations could not be loaded.");
          err.status = 503;
          throw err;
        }
        return { status: 200, ok: true, data: LOCATIONS };
      }
      if (url.indexOf("reception/guests/") !== -1) return { status: 200, ok: true, data: { results: GUESTS } };
      if (/events\/[^/]+\/$/.test(url)) return { status: 200, ok: true, data: EVENTS[0] };
      if (url.indexOf("events/") !== -1) {
        if (listFailures > 0) {
          listFailures -= 1;
          const err = new Error("Events could not be loaded.");
          err.status = 503;
          throw err;
        }
        return { status: 200, ok: true, data: EVENTS };
      }
      return { status: 200, ok: true, data: [] };
    },
    post: async (url) => {
      calls.push(url);
      return { status: 201, ok: true, data: { reference: "EVT-NEW" } };
    },
  };
  window.JONE = {
    esc: (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])),
    formatNaira: (v) => "NGN " + v,
    formatDateTime: (v) => String(v),
    ui: { toast: () => {}, confirm: () => Promise.resolve(true) },
    dashboard: { formModal: async () => null, panel: { open: () => {} } },
    ThermalReceipt: { print: () => {}, getWidth: () => "80" },
  };
  window.Auth = { state: { user: { role: "RECEPTIONIST" } } };
  if (opts.noConfig) delete window.APP_CONFIG;
  window.eval(fs.readFileSync(path.join(FRONTEND, "js/reception-services.js"), "utf8"));
  window.eval(fs.readFileSync(path.join(FRONTEND, "js/events.js"), "utf8"));
  return { window, doc: window.document, calls };
}

const settle = (ms = 250) => new Promise((r) => setTimeout(r, ms));

test("every event request targets the events namespace, never service-requests", { skip: SKIP }, async () => {
  const { calls } = boot();
  await settle();
  assert.ok(calls.length >= 3, "locations, events and guests are read on boot");
  const leaked = calls.filter((url) => url.indexOf("/api/admin/service-requests/events") === 0);
  assert.deepEqual(leaked, [], "no event call leaks into the service-request namespace");
  const eventCalls = calls.filter((url) => url.indexOf("/api/admin/events/") === 0);
  assert.ok(eventCalls.some((url) => url === "/api/admin/events/locations/"), "locations use /api/admin/events/");
  assert.ok(eventCalls.some((url) => url.indexOf("/api/admin/events/") === 0 && url.indexOf("locations") === -1));
  /* Guests legitimately remain on the service-request namespace. */
  assert.ok(calls.some((url) => url.indexOf("/api/admin/service-requests/reception/guests/") === 0));
});

test("the event spaces load into the selector from the backend payload", { skip: SKIP }, async () => {
  const { doc } = boot();
  await settle();
  const labels = Array.from(doc.querySelector("#ev-space").children).map((o) => o.textContent);
  for (const name of ["Inside Bar", "Outside Bar", "Hotel Compound"]) assert.ok(labels.includes(name), name);
  assert.equal(doc.querySelector("#ev-location").children.length, 4);
});

test("a failed locations read keeps the page usable and its Retry refetches", { skip: SKIP }, async () => {
  const { doc, window, calls } = boot({ failLocations: 1 });
  await settle();
  const space = doc.querySelector("#ev-space");
  assert.match(space.textContent, /could not be loaded/);
  /* The events table still loads — one failing call must not break the page. */
  assert.ok(doc.querySelector("[data-events-container]").innerHTML.indexOf("EVT-1") !== -1);
  const before = calls.filter((u) => u.indexOf("events/locations/") !== -1).length;
  const retry = doc.querySelector("[data-event-locations-retry]");
  assert.ok(retry, "the error state offers a real retry control");
  retry.dispatchEvent(new window.Event("click"));
  await settle();
  const after = calls.filter((u) => u.indexOf("events/locations/") !== -1).length;
  assert.equal(after, before + 1, "retry re-issued the exact failed request");
  const labels = Array.from(doc.querySelector("#ev-space").children).map((o) => o.textContent);
  assert.ok(labels.includes("Inside Bar"), "spaces are populated after a successful retry");
});

test("a failed events read offers a Retry that reloads the list", { skip: SKIP }, async () => {
  const { doc, window, calls } = boot({ failList: 1 });
  await settle();
  const container = doc.querySelector("[data-events-container]");
  assert.match(container.innerHTML, /could not be loaded/);
  const retry = doc.querySelector("[data-events-retry]");
  assert.ok(retry, "the list error state offers a retry control");
  const before = calls.length;
  retry.dispatchEvent(new window.Event("click"));
  await settle();
  assert.ok(calls.length > before);
  assert.ok(doc.querySelector("[data-events-container]").innerHTML.indexOf("EVT-1") !== -1);
});

test("the fallback literal keeps the events base correct without APP_CONFIG", { skip: SKIP }, async () => {
  const { calls } = boot({ noConfig: true });
  await settle();
  assert.ok(calls.some((url) => url === "/api/admin/events/locations/"), "fallback reaches the events namespace");
  assert.ok(calls.every((url) => url.indexOf("/api/admin/service-requests/events") !== 0));
});
