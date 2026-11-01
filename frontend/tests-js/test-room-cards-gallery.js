/* tests-js/test-room-cards-gallery.js
 *
 * Tasks 04, 05 and 06 of targeted_fixes_01 — the public room experience.
 *
 *   04  amenity icons resolve from the backend's icon vocabulary, with a
 *       fallback glyph instead of an empty <svg>;
 *   05  a guest can see EVERY image of a room — a counter, prev/next, a
 *       lightbox and no duplicates or broken links;
 *   06  one reusable card is used by the featured strip, the listing page and
 *       the detail page, with no hardcoded rooms or prices, and the booking
 *       link still points at the existing booking flow.
 *
 * The real production scripts run inside a vm context, so these assertions are
 * about behaviour rather than the shape of the HTML.
 */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const FRONTEND = path.resolve(__dirname, "..");
const read = (rel) => fs.readFileSync(path.join(FRONTEND, rel), "utf8");

function loadRoomCard() {
  const sandbox = {
    window: {
      JONE: {
        esc: (value) =>
          String(value === undefined || value === null ? "" : value)
            .replace(/&/g, "&amp;").replace(/</g, "&lt;")
            .replace(/>/g, "&gt;").replace(/"/g, "&quot;"),
      },
    },
    console,
  };
  vm.createContext(sandbox);
  vm.runInContext(read("js/icons.js"), sandbox, { filename: "icons.js" });
  vm.runInContext(read("js/room-card.js"), sandbox, { filename: "room-card.js" });
  return sandbox.window.JONE;
}

const JONE = loadRoomCard();
const RC = JONE.roomCard;

function sampleRoom(overrides) {
  return Object.assign({
    id: 1,
    slug: "mini-standard",
    name: "Mini Standard",
    short_description: "A cosy room with a queen bed.",
    base_price: "33000.00",
    max_guests: 2,
    bed_type: "Queen",
    bed_count: 1,
    room_size: "18 sqm",
    view: "Garden",
    is_featured: true,
    primary_image_url: "/media/rooms/mini-1.jpg",
    image_count: 3,
    images: [
      { id: 1, image_url: "/media/rooms/mini-1.jpg", alt_text: "Bedroom", display_order: 0, is_primary: true },
      { id: 2, image_url: "/media/rooms/mini-2.jpg", alt_text: "Bathroom", display_order: 1, is_primary: false },
      { id: 3, image_url: "/media/rooms/mini-3.jpg", alt_text: "Balcony", display_order: 2, is_primary: false },
    ],
    amenities: [
      { name: "Air conditioning", icon: "air-vent" },
      { name: "Flat-screen TV", icon: "tv2" },
      { name: "Desk lamp", icon: "lamp_desk" },
    ],
  }, overrides || {});
}

/* ------------------------------------------------------------------ *
 * Task 05 — the complete gallery
 * ------------------------------------------------------------------ */

test("every image the API returns is in the gallery", () => {
  const room = sampleRoom();
  const images = RC.imagesOf(room);
  assert.equal(images.length, 3);
  assert.deepEqual([...images.map((image) => image.src)],
    ["/media/rooms/mini-1.jpg", "/media/rooms/mini-2.jpg", "/media/rooms/mini-3.jpg"]);
});

test("duplicate image rows are shown once", () => {
  const room = sampleRoom({
    images: [
      { id: 1, image_url: "/a.jpg", display_order: 0, is_primary: true },
      { id: 2, image_url: "/b.jpg", display_order: 1, is_primary: false },
      { id: 2, image_url: "/b.jpg", display_order: 1, is_primary: false },
      { id: 4, image_url: "/b.jpg", display_order: 2, is_primary: false },
    ],
  });
  const images = RC.imagesOf(room);
  assert.equal(images.length, 2, "the same URL appears once");
  assert.deepEqual([...images.map((image) => image.src)], ["/a.jpg", "/b.jpg"]);
});

test("the gallery falls back to the primary image when the list is empty", () => {
  const room = sampleRoom({ images: [], primary_image_url: "/only.jpg", image_count: 1 });
  const images = RC.imagesOf(room);
  assert.equal(images.length, 1);
  assert.equal(images[0].src, "/only.jpg");
});

test("a room with no images at all renders a placeholder, never a broken <img>", () => {
  const room = sampleRoom({ images: [], primary_image_url: "", image_count: 0 });
  assert.equal(RC.imagesOf(room).length, 0);
  const html = RC.markup(room);
  assert.match(html, /rc-media-empty|rc-placeholder/);
  assert.doesNotMatch(html, /<img[^>]+src=""/);
});

test("the counter reports position out of the real total", () => {
  const html = RC.markup(sampleRoom());
  assert.match(html, />\s*1\s*\/\s*3\s*</);
});

test("prev and next controls exist and are labelled for a screen reader", () => {
  const html = RC.markup(sampleRoom());
  assert.match(html, /rc-nav[^"]*prev|data-rc-prev/);
  assert.match(html, /rc-nav[^"]*next|data-rc-next/);
  assert.match(html, /aria-label="[^"]*photo|aria-label="[^"]*image/i);
});

test("the gallery markup exposes a single slide container for the binder", () => {
  const html = RC.markup(sampleRoom());
  assert.match(html, /data-rc-gallery/);
  assert.match(html, /rc-slides/);
  assert.equal((html.match(/rc-slide\b/g) || []).length >= 3, true);
});

/* ------------------------------------------------------------------ *
 * Task 06 — the reusable card
 * ------------------------------------------------------------------ */

test("the card shows the name, the short description and the from-price", () => {
  const room = sampleRoom({ short_description: "A cosy room with a queen bed." });
  const html = RC.markup(room);
  assert.match(html, /Mini Standard/);
  assert.match(html, /A cosy room with a queen bed\./);
  assert.match(html, /33,000|33000/);
  assert.match(html, /rc-from|From|from/i);
});

test("the card renders a category badge", () => {
  const html = RC.markup(sampleRoom());
  assert.match(html, /rc-badge/);
});

test("amenity highlights come from the room's real data", () => {
  const room = sampleRoom({
    amenities: [
      { name: "Air conditioning", icon: "air-vent" },
      { name: "Free Wi-Fi", icon: "wifi" },
    ],
  });
  const html = RC.markup(room);
  assert.match(html, /Air conditioning/);
  assert.match(html, /Free Wi-Fi/);
  assert.doesNotMatch(html, /Swimming pool/, "no amenity is invented");
});

test("the room-features list uses check icons", () => {
  const html = RC.markup(sampleRoom());
  assert.match(html, /Room features/);
  assert.match(html, /rc-check/);
});

test("the booking button keeps the existing booking flow", () => {
  const html = RC.markup(sampleRoom(), { actions: "both" });
  assert.match(html, /booking\.html\?room=mini-standard/);
  assert.match(html, /Check availability/);
});

test("a room with no slug still produces a valid booking link", () => {
  const room = sampleRoom({ slug: "", id: 42 });
  const href = RC.bookingHref(room);
  assert.match(href, /booking\.html\?/);
  assert.doesNotMatch(href, /undefined/);
});

test("the card is data-driven: no room name or price is hardcoded in the component", () => {
  const source = read("js/room-card.js");
  for (const literal of ["Mini Standard", "Deluxe", "Executive Suite", "Presidential", "33,000"]) {
    assert.ok(!source.includes(literal), `room-card.js must not contain the literal "${literal}"`);
  }
  /* Prices and names come only from the payload: the module reads `base_price`
     and `name` off the room object and never declares one of its own. */
  const code = source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
  assert.doesNotMatch(code, /["']\d{2,}[,.]?\d{3}\.00["']/, "no money literal in executable code");
});

test("the same component renders every category without special-casing", () => {
  const rooms = [
    sampleRoom({ id: 1, slug: "a", name: "Standard", base_price: "20000.00" }),
    sampleRoom({ id: 2, slug: "b", name: "Deluxe", base_price: "45000.00" }),
    sampleRoom({ id: 3, slug: "c", name: "Executive Suite", base_price: "90000.00" }),
  ];
  const rendered = rooms.map((room) => RC.markup(room));
  assert.match(rendered[0], /Standard/);
  assert.match(rendered[1], /Deluxe/);
  assert.match(rendered[2], /Executive Suite/);
  for (const html of rendered) assert.match(html, /rc-card/);
});

/* ------------------------------------------------------------------ *
 * Task 04 — amenity icons
 * ------------------------------------------------------------------ */

test("the icon vocabulary resolves the aliases the backend actually stores", () => {
  const cases = {
    "air-vent": "airVent",
    "air_conditioning": "airVent",
    "tv": "tv",
    "tv2": "tv",
    "flat_screen_tv": "tv",
    "lamp_desk": "lampDesk",
    "desk_lamp": "lampDesk",
    "wifi": "wifi",
    "refrigerator": "refrigerator",
    "bath": "bath",
    "shower": "showerHead",
  };
  for (const [key, expected] of Object.entries(cases)) {
    assert.equal(JONE.icons.resolveName(key, "sparkle"), expected, `${key} should resolve to ${expected}`);
  }
});

test("an unknown icon key falls back instead of rendering an empty <svg>", () => {
  const fallback = JONE.icons.resolveName("a-key-that-does-not-exist", "sparkle");
  assert.equal(fallback, "sparkle");
  /* And the fallback glyph actually has geometry. */
  const svg = JONE.icons.get(fallback);
  assert.ok(svg && svg.length > 40, "the fallback glyph must render something");
  assert.doesNotMatch(svg, /<svg[^>]*><\/svg>/);
});

test("an empty or null icon never produces an empty icon element", () => {
  for (const value of ["", null, undefined, "   "]) {
    assert.equal(JONE.icons.resolveName(value, "sparkle"), "sparkle");
  }
});

test("every amenity in a rendered card has visible glyph geometry", () => {
  const room = sampleRoom({
    amenities: [
      { name: "Air conditioning", icon: "air-vent" },
      { name: "Flat-screen TV", icon: "tv2" },
      { name: "Desk lamp", icon: "lamp_desk" },
      { name: "A thing nobody mapped", icon: "zzz-unknown" },
    ],
  });
  const html = RC.markup(room);
  const emptySvgs = html.match(/<svg[^>]*>\s*<\/svg>/g) || [];
  assert.equal(emptySvgs.length, 0, "no amenity may render an empty <svg>");
});

test("the detail page also resolves amenity icons through the shared mapping", () => {
  const details = read("room-details.html");
  assert.match(details, /JONE\.icons\.resolveName\(/);
  assert.match(details, /room-amenity-line/);
  assert.match(details, /room-amenity-glyph/);
  /* ...and facilities.html does the same. */
  assert.match(read("facilities.html"), /JONE\.icons\.resolveName\(/);
});

/* ------------------------------------------------------------------ *
 * Wiring: every public surface uses the one component
 * ------------------------------------------------------------------ */

test("the featured strip, the listing page and the detail page all use the component", () => {
  for (const page of ["index.html", "rooms.html", "room-details.html"]) {
    const html = read(page);
    assert.match(html, /js\/room-card\.js/, `${page} must load the shared component`);
    assert.match(html, /JONE\.roomCard\./, `${page} must call the shared component`);
  }
});

test("the card styles ship with the site", () => {
  const main = read("css/main.css");
  assert.match(main, /room-cards\.css/);
  const cards = read("css/room-cards.css");
  for (const selector of [".room-grid", ".rc-card", ".rc-counter", ".rc-badge", ".rc-check", ".rc-hero", ".room-amenity-line"]) {
    assert.ok(cards.includes(selector), `room-cards.css must style ${selector}`);
  }
});

test("the mobile-first breakpoints keep the card usable on a phone", () => {
  const cards = read("css/room-cards.css");
  assert.match(cards, /@media \(max-width: 480px\)/);
  assert.match(cards, /aspect-ratio/);
  assert.match(cards, /prefers-reduced-motion/);
});
