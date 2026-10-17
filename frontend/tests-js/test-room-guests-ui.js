/* Room guests and per-room check-in — the browser side.

   Run from /frontend: node --test tests-js/test-room-guests-ui.js

   The request: a guest who books several rooms can attach the email of the
   person staying in each OTHER room (so that person can raise their own
   requests), and the desk keeps a record of whether every room actually
   checked in.

   Four surfaces carry that:
     booking-review.html        — asks for the other rooms' emails
     booking-confirmation.html  — sends them with the booking
     dashboard/booking-details  — records arrivals room by room
     js/api.js                  — the two endpoints
   The server enforces the same rules (tests/test_room_guests_and_per_room_checkin.py),
   so these tests check that the page agrees with it. */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { FRONTEND } = require("./harness");

const REVIEW = fs.readFileSync(path.join(FRONTEND, "booking-review.html"), "utf8");
const CONFIRM = fs.readFileSync(path.join(FRONTEND, "booking-confirmation.html"), "utf8");
const DETAILS = fs.readFileSync(path.join(FRONTEND, "dashboard", "booking-details.html"), "utf8");
const API = fs.readFileSync(path.join(FRONTEND, "js", "api.js"), "utf8");
const PORTAL = fs.readFileSync(path.join(FRONTEND, "portal", "index.html"), "utf8");
const CHECKIN = fs.readFileSync(path.join(FRONTEND, "dashboard", "check-in.html"), "utf8");
const REPORTS = fs.readFileSync(path.join(FRONTEND, "dashboard", "reports.html"), "utf8");

function sliceFunction(source, name) {
  const start = source.indexOf("function " + name + "(");
  assert.ok(start > 0, name + " is missing from the shipped page");
  const open = source.indexOf("{", start);
  let depth = 0;
  for (let i = open; i < source.length; i += 1) {
    if (source[i] === "{") depth += 1;
    else if (source[i] === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  throw new Error("unbalanced braces in " + name);
}

/* ------------------------- guest-side booking form ----------------------- */

test("the booking form asks for exactly the OTHER rooms, never room 1", () => {
  const ctx = { Math, Number, Array };
  vm.createContext(ctx);
  vm.runInContext(sliceFunction(REVIEW, "additionalRoomIndexes"), ctx);

  assert.deepEqual(Array.from(ctx.additionalRoomIndexes(1)), []);
  assert.deepEqual(Array.from(ctx.additionalRoomIndexes(3)), [1, 2]);
  // A broken/absent room count must never generate a phantom row.
  assert.deepEqual(Array.from(ctx.additionalRoomIndexes(0)), []);
  assert.deepEqual(Array.from(ctx.additionalRoomIndexes(undefined)), []);
});

test("the block is hidden for a single-room booking", () => {
  assert.match(REVIEW, /if \(roomCount < 2\) \{ otherRoomsBlock\.hidden = true; otherRoomsWrap\.innerHTML = ""; return; \}/);
  assert.match(REVIEW, /<div id="other-rooms" hidden>/);
});

test("each room email is submitted with its room index, not as a flat list", () => {
  const read = sliceFunction(REVIEW, "readOtherRoomGuests");
  assert.match(read, /room_index: index/);
  assert.match(read, /email: email/);
  // A row left blank is simply not sent — the provision is optional.
  assert.match(read, /if \(!email\) continue;/);
  // Emails are normalised so the server's uniqueness check matches what the
  // guest actually typed regardless of case.
  assert.match(read, /\.trim\(\)\.toLowerCase\(\)/);
});

test("the review step shows who is in the other rooms", () => {
  assert.match(REVIEW, /data-r-room-guests-row/);
  assert.match(REVIEW, /"Room " \+ \(Number\(guest\.room_index\) \+ 1\) \+ ": " \+ guest\.email/);
  assert.match(REVIEW, /row\.hidden = !roomGuests\.length;/);
});

test("the booking payload carries the room guest list, and only when it applies", () => {
  assert.match(CONFIRM, /if \(Number\(criteria\.rooms\) > 1 && \(draft\.room_guests \|\| \[\]\)\.length\) \{/);
  assert.match(CONFIRM, /payload\.room_guests = draft\.room_guests;/);
  // A retry after adding a room guest is a DIFFERENT booking, so the key must
  // change with it — otherwise the retry silently returns the first booking.
  assert.match(CONFIRM, /payload\.room_guests/);
  const idem = CONFIRM.slice(CONFIRM.indexOf("function bookingIdemKey"), CONFIRM.indexOf("function clearBookingIdemKey"));
  assert.match(idem, /guest\.room_index/);
  assert.match(idem, /guest\.email/);
});

/* --------------------------- desk: room records -------------------------- */

function loadDeskHelpers(booking) {
  const ctx = { Math, String, Number, Array, book: booking };
  vm.createContext(ctx);
  vm.runInContext(sliceFunction(DETAILS, "roomGuestsEditable"), ctx);
  vm.runInContext(sliceFunction(DETAILS, "roomCanBeCheckedIn"), ctx);
  vm.runInContext(sliceFunction(DETAILS, "roomGuestPayload"), ctx);
  return ctx;
}

test("room guests can only be edited while the booking is live", () => {
  const ctx = loadDeskHelpers({ status: "CONFIRMED", room_assignments: [] });
  assert.equal(ctx.roomGuestsEditable("PENDING"), true);
  assert.equal(ctx.roomGuestsEditable("CONFIRMED"), true);
  assert.equal(ctx.roomGuestsEditable("CHECKED_IN"), true);
  assert.equal(ctx.roomGuestsEditable("CHECKED_OUT"), false);
  assert.equal(ctx.roomGuestsEditable("CANCELLED"), false);
  assert.equal(ctx.roomGuestsEditable("EXPIRED"), false);
  assert.equal(ctx.roomGuestsEditable("NO_SHOW"), false);
});

test("a room can only be checked in once, and only on a live booking", () => {
  const ctx = loadDeskHelpers({ status: "CONFIRMED", room_assignments: [] });
  assert.equal(ctx.roomCanBeCheckedIn({ id: 1 }), true);
  assert.equal(ctx.roomCanBeCheckedIn({ id: 1, checked_in_at: "2026-10-17T10:00:00Z" }), false);
  assert.equal(ctx.roomCanBeCheckedIn({ id: 1, not_arrived_at: "2026-10-18T10:00:00Z" }), false);

  const closed = loadDeskHelpers({ status: "CHECKED_OUT", room_assignments: [] });
  assert.equal(closed.roomCanBeCheckedIn({ id: 1 }), false);

  const draft = loadDeskHelpers({ status: "PENDING", room_assignments: [] });
  assert.equal(draft.roomCanBeCheckedIn({ id: 1 }), false);
});

test("saving one room re-sends every room, because the API replaces the list", () => {
  const ctx = loadDeskHelpers({
    status: "CONFIRMED",
    room_assignments: [
      { id: 10, room_number: "201", occupants: [] },
      { id: 11, room_number: "202", occupants: [{ email: "ada@example.com", full_name: "Ada", phone: "" }] },
      { id: 12, room_number: "203", occupants: [{ email: "chinedu@example.com", full_name: "Chinedu", phone: "0803" }] },
    ],
  });
  const payload = ctx.roomGuestPayload(11, [{ email: "new@example.com", full_name: "New" }]);

  assert.equal(payload.length, 3, "a replace must carry every room, not just the edited one");
  assert.deepEqual(JSON.parse(JSON.stringify(payload[0].guests)), []);
  assert.deepEqual(JSON.parse(JSON.stringify(payload[1].guests)), [{ email: "new@example.com", full_name: "New" }]);
  assert.deepEqual(
    JSON.parse(JSON.stringify(payload[2].guests)),
    [{ email: "chinedu@example.com", full_name: "Chinedu", phone: "0803" }],
    "untouched rooms are sent back exactly as the server gave them",
  );
  assert.deepEqual(payload.map((row) => row.assignment_id), [10, 11, 12]);
});

test("the desk sees arrival state per room and the count of rooms that arrived", () => {
  assert.match(DETAILS, /var ARRIVAL_LABELS = \{/);
  assert.match(DETAILS, /DID_NOT_ARRIVE: "Did not arrive"/);
  assert.match(DETAILS, /AWAITING_ARRIVAL: "Awaiting arrival"/);
  assert.match(DETAILS, /checked \+ " of " \+ rooms\.length \+ " room\(s\) checked in"/);
  assert.match(DETAILS, /data-room-checkin="' \+ assignment\.id \+ '"/);
  assert.match(DETAILS, /data-room-guests="' \+ assignment\.id \+ '"/);
  // Re-rendered rooms are handled by delegation, not stale per-button handlers.
  assert.match(DETAILS, /roomsHost\.addEventListener\("click"/);
});

test("checking out warns about rooms that never arrived", () => {
  assert.match(DETAILS, /var awaiting=Number\(book\.rooms_awaiting_arrival\|\|0\)/);
  assert.match(DETAILS, /have not arrived yet; they will be recorded as/);
});

test("the desk page calls the per-room endpoints, not the whole-booking one", () => {
  assert.match(DETAILS, /window\.API\.saveBookingRoomGuests\(ref, roomGuestPayload\(assignment\.id, draft\)\)/);
  assert.match(DETAILS, /window\.API\.checkInBookingRoom\(ref, assignmentId\)/);
});

/* ------------------------------- API client ------------------------------- */

test("the API client exposes a replace-style room guest save and a per-room check-in", () => {
  assert.match(API, /const saveBookingRoomGuests = \(lookup, rooms, opts = \{\}\) => \{/);
  assert.match(API, /put\(`\$\{base\}\/\$\{encodeURIComponent\(lookup\)\}\/room-guests\/`, \{ rooms: rooms \|\| \[\] \}, opts\)/);
  assert.match(API, /const checkInBookingRoom = \(lookup, assignmentId, opts = \{\}\) => \{/);
  assert.match(API, /post\(`\$\{base\}\/\$\{encodeURIComponent\(lookup\)\}\/rooms\/\$\{id\}\/check-in\/`, \{\}, opts\)/);
  assert.match(API, /saveBookingRoomGuests, checkInBookingRoom,/);
  // An invalid assignment id is refused before a pointless round trip.
  assert.match(API, /INVALID_ROOM_ASSIGNMENT/);
});

/* ------------------------------- guest portal ----------------------------- */

test("a room guest is told which room their requests belong to", () => {
  assert.match(PORTAL, /booking\.is_booking_contact===false/);
  assert.match(PORTAL, /booking\.your_room_numbers/);
  assert.match(PORTAL, /requests are filed against your room/);
});

/* ------------------------- arrivals queue (check-in) ---------------------- */

function loadCheckInHelpers() {
  const ctx = { Math, String, Number, Array, JONE: { esc: (v) => String(v ?? "") } };
  vm.createContext(ctx);
  vm.runInContext(sliceFunction(CHECKIN, "refOf"), ctx);
  vm.runInContext(sliceFunction(CHECKIN, "roomLabel"), ctx);
  vm.runInContext(sliceFunction(CHECKIN, "roomCell"), ctx);
  vm.runInContext(sliceFunction(CHECKIN, "roomsLink"), ctx);
  return ctx;
}

test("the arrivals list says how many of the booked rooms are actually here", () => {
  const ctx = loadCheckInHelpers();
  const partial = ctx.roomCell({
    number_of_rooms: 3, rooms_checked_in: 1, rooms_awaiting_arrival: 2, room_numbers: ["201", "202", "203"],
  });
  assert.match(partial, /201, 202, 203/);
  assert.match(partial, /1 of 3 room\(s\) in house/);
  assert.match(partial, /2 awaiting arrival/);

  // A single-room booking stays a plain cell — no noise.
  const single = ctx.roomCell({ number_of_rooms: 1, rooms_checked_in: 1, room_numbers: ["101"] });
  assert.equal(single, "101");
});

test("the arrivals list links to the room-by-room panel only for multi-room bookings", () => {
  const ctx = loadCheckInHelpers();
  assert.equal(ctx.roomsLink({ number_of_rooms: 1, booking_reference: "J1-1" }), "");
  const link = ctx.roomsLink({ number_of_rooms: 2, booking_reference: "J1-2" });
  assert.match(link, /booking-details\.html\?ref=J1-2#rooms-panel/);
  assert.match(link, />Rooms</);
});

test("both arrivals tables use the room-aware cell", () => {
  const arrivals = CHECKIN.slice(CHECKIN.indexOf("function render(list)"), CHECKIN.indexOf("function apply()"));
  assert.match(arrivals, /\+roomCell\(b\)\+/);
  const inhouse = CHECKIN.slice(CHECKIN.indexOf("function renderInHouse(list)"), CHECKIN.indexOf("function refOf(b)"));
  assert.match(inhouse, /\+roomCell\(b\)\+/);
  // The panel the link points at really exists.
  assert.match(DETAILS, /<section class="panel" id="rooms-panel">/);
});

test("the reports KPI separates rooms sold from rooms a guest actually occupied", () => {
  assert.match(REPORTS, /arrivedNights=latest\.occupancy\.arrived_room_nights/);
  assert.match(REPORTS, /room-nights sold/);
  assert.match(REPORTS, /with a guest checked in/);
  assert.match(REPORTS, /never arrived/);
});
