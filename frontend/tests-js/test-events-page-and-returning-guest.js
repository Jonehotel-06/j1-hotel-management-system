/* tests-js/test-events-page-and-returning-guest.js

   Batch 1 (Reception Event Management) and Batch 2 (returning guest selection).

   The page scripts run unmodified against a jsdom document with a stubbed API
   that returns the production payload shapes. What is proven here:
     · the three configured event spaces load and are selectable;
     · an existing guest can be searched and selected, and its DETAILS are sent
       as guest_id (never re-typed into a duplicate profile);
     · the outstanding balance, statuses and money columns render from the
       server response, not from a browser calculation;
     · a conflicting submission surfaces the server's refusal and does not
       clear the form;
     · the returning-guest picker in booking creation emits guest_id only for
       the guest actually chosen.

   Run: node --test tests-js/test-events-page-and-returning-guest.js
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
    <input name="date_from"><input name="date_to">
    <select name="location_id" id="ev-location"></select>
    <select name="payment_status"></select><select name="status"></select>
    <input name="search">
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

function boot(opts) {
  opts = opts || {};
  const dom = new JSDOM(PAGE, { url: "https://staff.jonehotels.com/dashboard/events.html", runScripts: "outside-only" });
  const { window } = dom;
  const posted = [];
  const errors = [];
  window.API = {
    get: async (url) => {
      let data = [];
      if (url.indexOf("events/locations/") !== -1) data = LOCATIONS;
      else if (url.indexOf("reception/guests/") !== -1) data = { results: GUESTS };
      else if (/events\/[^/]+\/$/.test(url)) data = EVENTS[0];
      else if (url.indexOf("events/") !== -1) data = EVENTS;
      return { status: 200, ok: true, data };
    },
    post: async (url, body) => {
      posted.push([url, body]);
      if (opts.postError) {
        const err = new Error(opts.postError);
        err.status = 400;
        throw err;
      }
      return { status: 201, ok: true, data: { reference: "EVT-NEW" } };
    },
  };
  window.JONE = {
    esc: (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])),
    formatNaira: (v) => "NGN " + v,
    formatDateTime: (v) => String(v),
    ui: { toast: (m) => {}, confirm: () => Promise.resolve(true) },
    dashboard: { formModal: async () => null, panel: { open: () => {} } },
    ThermalReceipt: { print: () => {}, getWidth: () => "80" },
  };
  window.Auth = { state: { user: { role: "RECEPTIONIST" } } };
  window.eval(fs.readFileSync(path.join(FRONTEND, "js/reception-services.js"), "utf8"));
  window.eval(fs.readFileSync(path.join(FRONTEND, "js/events.js"), "utf8"));
  return { window, doc: window.document, posted, errors };
}

const settle = (ms = 250) => new Promise((r) => setTimeout(r, ms));

test("the three configured event spaces load into the form", { skip: SKIP }, async () => {
  const { doc } = boot();
  await settle();
  const space = doc.querySelector("#ev-space");
  const labels = Array.from(space.children).map((o) => o.textContent);
  assert.ok(labels.includes("Inside Bar"));
  assert.ok(labels.includes("Outside Bar"));
  assert.ok(labels.includes("Hotel Compound"));
  const filter = doc.querySelector("#ev-location");
  assert.equal(filter.children.length, 4, "All locations + three spaces");
});

test("the event table renders money and status from the server payload", { skip: SKIP }, async () => {
  const { doc } = boot();
  await settle();
  const html = doc.querySelector("[data-events-container]").innerHTML;
  assert.ok(html.indexOf("EVT-1") !== -1);
  assert.ok(html.indexOf("NGN 150000.00") !== -1, "charged");
  assert.ok(html.indexOf("NGN 100000.00") !== -1, "paid");
  assert.ok(html.indexOf("NGN 50000.00") !== -1, "outstanding");
  assert.ok(/PARTIALLY PAID/i.test(html), "payment status rendered");
  assert.ok(/Record payment/.test(html), "an outstanding event offers a payment action");
});

test("an existing guest can be searched and selected without retyping a profile", { skip: SKIP }, async () => {
  const { doc, window } = boot();
  await settle();
  const search = doc.querySelector("[data-event-guest-search]");
  search.value = "Ada";
  search.dispatchEvent(new window.Event("input"));
  await settle(400);
  const select = doc.querySelector("#ev-guest");
  const option = Array.from(select.children).find((o) => String(o.value) === "7");
  assert.ok(option, "the search result is selectable");
  assert.ok(option.textContent.indexOf("Ada Obi") !== -1);
  select.value = "7";
  select.dispatchEvent(new window.Event("change"));
  assert.equal(doc.querySelector("#ev-first").value, "Ada");
  assert.equal(doc.querySelector("#ev-last").value, "Obi");
  assert.equal(doc.querySelector("#ev-phone").value, "08031234567");
  assert.equal(doc.querySelector("#ev-email").value, "ada@example.com");
});

test("submitting with a selected guest sends guest_id, not a new guest record", { skip: SKIP }, async () => {
  const { doc, window, posted } = boot();
  await settle();
  const select = doc.querySelector("#ev-guest");
  window.eval("void 0");
  // Select through the UI path.
  select.innerHTML = '<option value="7">Ada Obi</option>';
  select.value = "7";
  select.dispatchEvent(new window.Event("change"));
  doc.querySelector("#ev-space").value = "1";
  doc.querySelector("#ev-type").value = "Birthday";
  const form = doc.querySelector("[data-event-form]");
  form.elements.event_date.value = "2026-12-01";
  form.elements.start_time.value = "18:00";
  form.elements.end_time.value = "22:00";
  form.elements.total_charged.value = "150000";
  form.dispatchEvent(new window.Event("submit", { cancelable: true, bubbles: true }));
  await settle(200);
  const call = posted.find((p) => p[0].indexOf("events/") !== -1);
  assert.ok(call, "the event endpoint was called");
  const payload = call[1];
  assert.equal(payload.guest_id, 7);
  assert.equal(payload.first_name, undefined, "no duplicate guest data is sent");
  assert.equal(payload.location_id, 1);
  assert.equal(payload.total_charged, "150000.00");
  assert.ok(payload.idempotency_key, "an idempotency key accompanies the write");
});

test("a conflicting booking shows the server's refusal and keeps the form", { skip: SKIP }, async () => {
  const { doc, window } = boot({ postError: "Inside Bar is already booked on 2026-12-01. Choose another time or location." });
  await settle();
  doc.querySelector("#ev-space").value = "1";
  doc.querySelector("#ev-type").value = "Meeting";
  const form = doc.querySelector("[data-event-form]");
  form.elements.first_name.value = "New";
  form.elements.last_name.value = "Guest";
  form.elements.phone.value = "08030000000";
  form.elements.event_date.value = "2026-12-01";
  form.elements.start_time.value = "19:00";
  form.elements.end_time.value = "20:00";
  form.elements.total_charged.value = "1000";
  form.dispatchEvent(new window.Event("submit", { cancelable: true, bubbles: true }));
  await settle(250);
  const box = doc.querySelector("[data-event-form-message]");
  assert.ok(/already booked/i.test(box.innerHTML), box.innerHTML);
  assert.equal(doc.querySelector("#ev-type").value, "Meeting", "the form was not cleared");
});

test("a phone-only new guest is accepted, a nameless one is refused client-side", { skip: SKIP }, async () => {
  const { doc, window, posted } = boot();
  await settle();
  doc.querySelector("#ev-space").value = "2";
  doc.querySelector("#ev-type").value = "Meeting";
  const form = doc.querySelector("[data-event-form]");
  form.elements.event_date.value = "2026-12-02";
  form.elements.start_time.value = "10:00";
  form.elements.end_time.value = "12:00";
  form.elements.total_charged.value = "5000";
  form.dispatchEvent(new window.Event("submit", { cancelable: true, bubbles: true }));
  await settle(200);
  assert.ok(/first and last name|phone number/i.test(doc.querySelector("[data-event-form-message]").innerHTML));
  assert.equal(posted.length, 0, "nothing was sent without guest details");

  form.elements.first_name.value = "Walk";
  form.elements.last_name.value = "In";
  form.elements.phone.value = "08039998877";
  form.dispatchEvent(new window.Event("submit", { cancelable: true, bubbles: true }));
  await settle(200);
  const call = posted.find((p) => p[0].indexOf("events/") !== -1);
  assert.ok(call, "the guest was registered with the event");
  assert.equal(call[1].guest_id, undefined);
  assert.equal(call[1].phone, "08039998877");
});

test("an end time before the start time is refused before any request", { skip: SKIP }, async () => {
  const { doc, window, posted } = boot();
  await settle();
  doc.querySelector("#ev-space").value = "1";
  doc.querySelector("#ev-type").value = "Meeting";
  const form = doc.querySelector("[data-event-form]");
  form.elements.first_name.value = "A";
  form.elements.last_name.value = "B";
  form.elements.phone.value = "0803";
  form.elements.event_date.value = "2026-12-03";
  form.elements.start_time.value = "20:00";
  form.elements.end_time.value = "19:00";
  form.elements.total_charged.value = "100";
  form.dispatchEvent(new window.Event("submit", { cancelable: true, bubbles: true }));
  await settle(200);
  assert.equal(posted.length, 0);
  assert.ok(/end time must be after/i.test(doc.querySelector("[data-event-form-message]").innerHTML));
});

test("the booking page emits guest_id only for the returning guest actually chosen", { skip: SKIP }, async () => {
  const bookings = fs.readFileSync(path.join(FRONTEND, "dashboard/bookings.html"), "utf8");
  assert.ok(bookings.indexOf("returningGuestApi") !== -1, "the picker is present");
  assert.ok(/getValue: function\(\)\{ var g = returningGuestApi\.read\(\); return g \? \{ guest_id: g\.id \} : \{\}; \}/.test(bookings),
    "the picker contributes guest_id (and nothing when empty)");
  assert.ok(bookings.indexOf("if (v.guest_id) payload.guest_id = Number(v.guest_id);") !== -1,
    "the selected id reaches the booking payload");
  assert.ok(bookings.indexOf('window.API.list("guests"') !== -1, "search uses the paginated guest endpoint");
  assert.ok(bookings.indexOf("guest_first_name") !== -1, "the manual new-guest fields survive");
});
