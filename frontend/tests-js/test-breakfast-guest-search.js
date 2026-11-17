/* tests-js/test-breakfast-guest-search.js

   Reported defects this file locks down
   -------------------------------------
   · Guest names are not displayed on the Breakfast page.
   · Search does not work.
   · Booking-reference search does not work
     (example: "J1-20261115-6157FF07").

   The Breakfast page delegates its guest/booking/room pickers to
   js/reception-services.js. This file drives the REAL shared helper with the
   REAL response shape from /api/admin/service-requests/reception/guests/ and
   asserts the behaviour the desk depends on: names appear, a booking reference
   (full or partial, any case, any surrounding spaces) finds the stay, the stay
   populates the booking and room pickers, typing is debounced, and an empty
   result is stated explicitly rather than rendered as a blank list.

   Run: node --test tests-js/test-breakfast-guest-search.js
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

const FORM = `<form data-breakfast-issue-form>
  <input data-guest-search type="search">
  <select name="booking_id"></select>
  <select name="guest_id"></select>
  <select name="room_number"></select>
  <input name="service_date">
  <p data-guest-context></p>
</form>`;

const BOOKING_REF = "J1-20261115-6157FF07";
const STAYS = {
  results: [
    {
      id: 7,
      full_name: "Ada Obi",
      email: "ada@test.dev",
      phone: "08031234567",
      bookings: [
        {
          id: 21, reference: BOOKING_REF, status: "CHECKED_IN",
          check_in: "2026-11-15", check_out: "2026-11-18", rooms: ["101"],
        },
      ],
    },
    {
      id: 8,
      full_name: "Bola Ade",
      email: "bola@test.dev",
      phone: "08039998888",
      bookings: [],
    },
  ],
};

function boot() {
  const dom = new JSDOM(FORM, {
    url: "https://staff.jonehotels.com/dashboard/breakfast.html",
    runScripts: "outside-only",
  });
  const { window } = dom;
  const calls = [];
  window.API = {
    get: async (url) => {
      calls.push(url);
      const term = decodeURIComponent((url.split("search=")[1] || "")).trim().toLowerCase();
      if (!term) return { status: 200, ok: true, data: STAYS };
      const rows = STAYS.results.filter((guest) => {
        const hay = [
          guest.full_name, guest.email, guest.phone,
          ...guest.bookings.map((b) => b.reference),
          ...guest.bookings.flatMap((b) => b.rooms),
        ].join(" ").toLowerCase();
        return hay.includes(term);
      });
      return { status: 200, ok: true, data: { results: rows } };
    },
    post: async () => ({ status: 200, ok: true, data: { created: true } }),
  };
  window.JONE = {
    esc: (v) => String(v == null ? "" : v),
    ui: { toast: () => {} },
    hotelTodayISO: () => "2026-11-17",
  };
  window.eval(fs.readFileSync(path.join(FRONTEND, "js/reception-services.js"), "utf8"));
  const form = window.document.querySelector("[data-breakfast-issue-form]");
  window.ReceptionServices.guests(form, {});
  return { window, form, calls };
}

const settle = (ms = 300) => new Promise((resolve) => setTimeout(resolve, ms));
const texts = (select) => Array.from(select.children).map((o) => o.textContent);

test("guest names are displayed in the picker", { skip: SKIP }, async () => {
  const { form } = boot();
  await settle();
  const select = form.querySelector("[name=guest_id]");
  assert.ok(texts(select).some((t) => t.includes("Ada Obi")), "Ada Obi listed: " + texts(select));
  assert.ok(texts(select).some((t) => t.includes("Bola Ade")));
  assert.ok(texts(select).some((t) => t.includes("Room 101")), "room shown next to the name");
});

test("a full booking reference finds the guest", { skip: SKIP }, async () => {
  const { form, window } = boot();
  await settle();
  const search = form.querySelector("[data-guest-search]");
  search.value = BOOKING_REF;
  search.dispatchEvent(new window.Event("input"));
  await settle(500);
  const select = form.querySelector("[name=guest_id]");
  const options = texts(select).filter((t) => t.includes("Ada Obi"));
  assert.equal(options.length, 1, "exactly one match for the reference");
  assert.ok(!texts(select).some((t) => t.includes("Bola Ade")), "non-matching guest removed");
});

test("partial / padded / different-case search still matches", { skip: SKIP }, async () => {
  const { form, window } = boot();
  await settle();
  const search = form.querySelector("[data-guest-search]");
  const select = form.querySelector("[name=guest_id]");

  search.value = "  6157ff07  ";
  search.dispatchEvent(new window.Event("input"));
  await settle(500);
  assert.ok(texts(select).some((t) => t.includes("Ada Obi")), "partial ref, lower case, padded");

  search.value = "bola";
  search.dispatchEvent(new window.Event("input"));
  await settle(500);
  assert.ok(texts(select).some((t) => t.includes("Bola Ade")), "name search");
});

test("a room number finds the guest staying in it", { skip: SKIP }, async () => {
  const { form, window } = boot();
  await settle();
  const search = form.querySelector("[data-guest-search]");
  search.value = "101";
  search.dispatchEvent(new window.Event("input"));
  await settle(500);
  const select = form.querySelector("[name=guest_id]");
  assert.ok(texts(select).some((t) => t.includes("Ada Obi")));
});

test("selecting a guest fills the booking and room pickers and the stay summary", { skip: SKIP }, async () => {
  const { form, window } = boot();
  await settle();
  const select = form.querySelector("[name=guest_id]");
  select.value = "7";
  select.dispatchEvent(new window.Event("change"));
  await settle(50);

  const booking = form.querySelector("[name=booking_id]");
  const room = form.querySelector("[name=room_number]");
  assert.equal(booking.value, "21", "the stay is pre-selected");
  assert.ok(texts(booking).some((t) => t.includes(BOOKING_REF)), "booking reference shown");
  assert.equal(room.value, "101", "the only assigned room is pre-selected");
  const context = form.querySelector("[data-guest-context]").textContent;
  assert.ok(context.includes("Ada Obi"), context);
  assert.ok(context.includes(BOOKING_REF), context);
  assert.ok(context.includes("101"), context);
});

test("typing is debounced to one request per pause", { skip: SKIP }, async () => {
  const { form, window, calls } = boot();
  await settle();
  calls.length = 0;
  const search = form.querySelector("[data-guest-search]");
  for (const ch of BOOKING_REF.split("")) {
    search.value += ch;
    search.dispatchEvent(new window.Event("input"));
    await settle(25);
  }
  await settle(500);
  assert.equal(calls.length, 1, "one request for a whole typed reference: " + JSON.stringify(calls));
});

test("an empty result is stated explicitly and clears dependent pickers", { skip: SKIP }, async () => {
  const { form, window } = boot();
  await settle();
  const search = form.querySelector("[data-guest-search]");
  search.value = "nobody-here";
  search.dispatchEvent(new window.Event("input"));
  await settle(500);
  const select = form.querySelector("[name=guest_id]");
  assert.ok(texts(select)[0].includes("No guest matches"), texts(select)[0]);
  const context = form.querySelector("[data-guest-context]").textContent;
  assert.ok(/No guest, booking reference or room number matches/.test(context), context);
});

test("pressing Enter searches instead of submitting a half-typed form", { skip: SKIP }, async () => {
  const { form, window } = boot();
  await settle();
  let submitted = false;
  form.addEventListener("submit", (event) => { submitted = true; event.preventDefault(); });
  const search = form.querySelector("[data-guest-search]");
  search.value = BOOKING_REF;
  const event = new window.KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true });
  search.dispatchEvent(event);
  await settle(400);
  assert.equal(submitted, false, "Enter did not submit the form");
  assert.equal(event.defaultPrevented, true);
  const select = form.querySelector("[name=guest_id]");
  assert.ok(texts(select).some((t) => t.includes("Ada Obi")), "Enter ran the search");
});

test("Escape clears the search back to the full list", { skip: SKIP }, async () => {
  const { form, window } = boot();
  await settle();
  const search = form.querySelector("[data-guest-search]");
  search.value = "bola";
  search.dispatchEvent(new window.Event("input"));
  await settle(400);
  const select = form.querySelector("[name=guest_id]");
  assert.ok(!texts(select).some((t) => t.includes("Ada Obi")), "filtered");

  search.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  await settle(400);
  assert.equal(search.value, "", "input cleared");
  assert.ok(texts(select).some((t) => t.includes("Ada Obi")), "full list restored");
});

test("a failed read is reported, not rendered as an empty guest list", { skip: SKIP }, async () => {
  const dom = new JSDOM(FORM, { url: "https://staff.jonehotels.com/dashboard/breakfast.html", runScripts: "outside-only" });
  const { window } = dom;
  window.API = {
    get: async () => {
      const err = new Error("The database is temporarily unavailable. Please try again.");
      err.status = 503;
      throw err;
    },
  };
  window.JONE = { esc: (v) => String(v == null ? "" : v), ui: { toast: () => {} }, hotelTodayISO: () => "2026-11-17" };
  window.eval(fs.readFileSync(path.join(FRONTEND, "js/reception-services.js"), "utf8"));
  const form = window.document.querySelector("[data-breakfast-issue-form]");
  window.ReceptionServices.guests(form, {});
  await settle(300);
  const select = form.querySelector("[name=guest_id]");
  assert.ok(texts(select)[0].includes("Unable to load guests"), texts(select)[0]);
  const context = form.querySelector("[data-guest-context]").textContent;
  assert.ok(/temporarily unavailable/i.test(context), context);
});
