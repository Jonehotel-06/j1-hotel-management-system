/* C1/C2 — walk-in booking: instant quote + per-room guest collection.
 *
 * Critical repair C1: receptionists must see an INSTANT booking quote before
 * confirming a walk-in — dates, room type, nights, nightly rate, rooms and
 * total — with the server as the calculator (display only, no tax line).
 *
 * Critical repair C2: multi-room bookings must collect a guest name AND an
 * email for each additional room (so every occupant is recorded and later
 * invited to the portal at their own address), without ever overwriting the
 * booking contact and without letting one person occupy two rooms.
 */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const html = fs.readFileSync(path.join(__dirname, "..", "dashboard", "bookings.html"), "utf8");

test("C1: the walk-in form quotes through the authoritative endpoint", () => {
  assert.ok(html.includes("window.API.quoteBooking("), "walk-in dialog must call the quote endpoint");
  // Display only: the server's arithmetic is rendered, never recomputed here.
  assert.ok(!/price_per_night\s*\*\s*nights/.test(html), "no client-side price arithmetic");
  // The authoritative response fields the block renders.
  for (const field of ["data.check_in", "data.check_out", "data.nights", "data.price_per_night", "data.rooms", "data.total"]) {
    assert.ok(html.includes(field), `quote block renders ${field}`);
  }
});

test("C1: the quote shows no tax line and refreshes on stay-input changes", () => {
  assert.ok(!/data\.tax/.test(html), "the quote must not render the withdrawn tax line");
  // The quote refreshes when the stay-defining inputs change (debounced).
  assert.ok(html.includes("scheduleQuote"), "quote refresh is wired");
  assert.ok(html.includes('onChange: function(sel, reason)'), "calendar hook exists");
  const onChange = html.indexOf("onChange: function(sel, reason)");
  assert.ok(html.slice(onChange, onChange + 900).includes("scheduleQuote();"), "calendar changes re-quote");
  for (const input of ["room_type", "rooms", "adults", "children", "offer_code"]) {
    const field = `"${input}"`;
    assert.ok(html.includes(field), `${input} is an input the quote listens to`);
  }
  // Stale-reply protection like the public flow.
  assert.ok(html.includes("quoteSeq"), "late/superseded quote replies are dropped");
});

test("C2: the walk-in form collects a name AND an email for every additional room", () => {
  assert.ok(html.includes('name: "room_guests"'), "per-room guests field exists");
  assert.ok(html.includes("data-rg-name"), "each room row has a guest-name input");
  assert.ok(html.includes("data-rg-email"), "each room row has a guest-email input");
  assert.ok(html.includes('Room \' + (i + 1)'), "rows are labelled per room (Room 2, Room 3, …)");
  // Rows grow/shrink with the room count.
  assert.ok(/roomsInput\.addEventListener\("input", buildRows\)/.test(html), "rows rebuild when the room count changes");
});

test("C2: guests are sent at creation as room_guests with their room index", () => {
  assert.ok(html.includes("payload.room_guests = roomGuests"), "create payload carries the room guests");
  assert.ok(html.includes("room_index: i + 1"), "each guest is bound to their room (contact keeps room 0)");
  // The booking contact is never overwritten: rows only cover ADDITIONAL rooms.
  assert.ok(/for \(var i = 1; i < rooms; i\+\+\)/.test(html), "additional rooms only — the contact is the first room's guest");
});

test("C2: the desk-side validation rejects bad input before it reaches the server", () => {
  // Partial rows are completed or rejected — a guest without an identity
  // cannot slip through, and a bare email is not a person's name.
  assert.ok(html.includes("please enter the guest's name"), "names are validated");
  assert.ok(html.includes("is not a valid email address"), "emails are validated");
  // No overwriting the primary guest.
  assert.ok(html.includes("is the booking contact"), "the booking contact cannot be added as a room guest");
  // No duplicate identities within the booking.
  assert.ok(html.includes("Each guest can only be in one room of this booking"), "one room per guest is enforced");
});
