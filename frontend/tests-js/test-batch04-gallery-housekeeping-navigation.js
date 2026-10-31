/* tests-js/test-batch04-gallery-housekeeping-navigation.js
   Batch 04 contracts: homepage hero slots, housekeeping status retirement and
   the inventory → procurement reorder hand-off.
   Run from /frontend: node --test tests-js/test-batch04-gallery-housekeeping-navigation.js

   Static contract checks only; behaviour is exercised end to end in the
   browser during verification (see ai_updates/batch_04_gallery_housekeeping_navigation). */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { FRONTEND } = require("./harness");

const read = (rel) => fs.readFileSync(path.join(FRONTEND, rel), "utf8");

const GALLERY = read("dashboard/gallery.html");
const SITE_IMAGES = read("js/site-images.js");
const CAROUSEL = read("js/carousel.js");
const API = read("js/api.js");
const INDEX = read("index.html");
const HOUSEKEEPING = read("dashboard/housekeeping.html");
const INVENTORY = read("dashboard/inventory.html");
const PROCUREMENT = read("dashboard/procurement.html");

/* ----------------------------- Task 1: hero slots ----------------------------- */

test("the dashboard manages four independent hero slots, each with its own save and remove", () => {
  for (const key of ["hero", "hero_2", "hero_3", "hero_4"]) {
    assert.match(GALLERY, new RegExp("\\['" + key + "','Hero slide \\d'"), key + " slot is listed");
  }
  assert.match(GALLERY, /slotURL\(key\)/, "each slot writes to its own homepage/<section>/ endpoint");
  assert.doesNotMatch(GALLERY, /homepage\/hero_5/, "there is no fifth hero slot");
});

test("hero uploads show progress through the progress-capable upload helper", () => {
  assert.match(GALLERY, /window\.API\.uploadWithProgress\(slotURL\(key\)/);
  assert.match(GALLERY, /data-slot-progress/);
  assert.match(GALLERY, /Uploading \W*\s*'\+p\+'%/);
  assert.match(API, /function uploadWithProgress\(path, formData, opts = \{\}, _retry = false\)/);
  assert.match(API, /xhr\.upload\.onprogress/);
  assert.match(API, /uploadWithProgress,/, "helper is exported from the API object");
});

test("client-side hero validation keeps the 5 MB JPEG/PNG/WebP limit", () => {
  assert.match(GALLERY, /file\.size>5\*1024\*1024/);
  assert.match(GALLERY, /\['image\/jpeg','image\/png','image\/webp'\]/);
});

test("the public hero builds its slideshow from every configured hero slot, in order", () => {
  assert.match(SITE_IMAGES, /var HERO_SLOTS = \["hero", "hero_2", "hero_3", "hero_4"\];/);
  assert.match(SITE_IMAGES, /function renderHero\(list\)/);
  assert.match(SITE_IMAGES, /if \(!list\.length\) return; \/\/ no custom hero: the built-in slideshow is the fallback/);
});

test("slides beyond the first defer their image via data-src and carousel.js promotes it", () => {
  assert.match(SITE_IMAGES, /' data-src="' \+ JONE\.esc\(item\.url\) \+ '"'/);
  assert.match(CAROUSEL, /function hydrate\(n\)/);
  assert.match(CAROUSEL, /hydrate\(index\);\s*hydrate\(\(index \+ 1\) % slides\.length\);/);
});

test("a broken uploaded hero falls back to the shipped image for its position, then hides", () => {
  assert.match(SITE_IMAGES, /builtInHeroImages\[index\]/);
  assert.match(SITE_IMAGES, /img\.style\.display = "none";/);
});

test("the shipped five-slide hero markup is untouched in index.html", () => {
  for (const asset of ["hero-1.jpg", "hero-2.jpg", "hero-3.jpg", "hero-5.jpg", "hero-6.jpg"]) {
    assert.ok(INDEX.includes("assets/images/" + asset), asset + " still ships in the built-in slideshow");
  }
});

/* ------------------------- Task 2: housekeeping statuses ---------------------- */

test("housekeeping filters no longer offer Assigned or Cancelled", () => {
  const filter = HOUSEKEEPING.match(/<select class="select" id="hk-status">[\s\S]*?<\/select>/);
  assert.ok(filter, "status filter exists");
  assert.doesNotMatch(filter[0], /value="ASSIGNED"/);
  assert.doesNotMatch(filter[0], /value="CANCELLED"/);
  assert.match(filter[0], /value="OPEN"/);
  assert.match(filter[0], /value="COMPLETED"/);
});

test("housekeeping has no Claim button, claim handler or Cancel transition", () => {
  assert.doesNotMatch(HOUSEKEEPING, /data-hk-claim/);
  assert.doesNotMatch(HOUSEKEEPING, /function claim\(/);
  assert.doesNotMatch(HOUSEKEEPING, /resourceAction\("housekeepingTasks",[^)]*"claim"/);
  const map = HOUSEKEEPING.match(/var transitions=(\{[^;]*\});/);
  assert.ok(map, "transition map exists");
  assert.doesNotMatch(map[1], /CANCELLED/);
  assert.match(map[1], /OPEN:\["ACCEPTED"\]/);
});

test("legacy ASSIGNED and CANCELLED rows display as Open and Closed", () => {
  assert.match(HOUSEKEEPING, /function statusLabel\(status\)\{return \{ASSIGNED:"Open",CANCELLED:"Closed"\}\[status\]\|\|undefined;\}/);
  assert.match(HOUSEKEEPING, /statusPill\(task\.status,statusLabel\(task\.status\)\)/);
});

test("housekeeping keeps dispatch, status progression and the creation path", () => {
  assert.match(HOUSEKEEPING, /data-hk-assign=/);
  assert.match(HOUSEKEEPING, /data-hk-status=/);
  assert.match(HOUSEKEEPING, /Create housekeeping task/);
});

/* ---------------------------- Task 4: reorder hand-off ------------------------ */

test("the inventory Reorder link carries the low-stock row's store", () => {
  assert.match(INVENTORY, /function reorderHref\(itemId,locationId\)/);
  assert.match(INVENTORY, /esc\(reorderHref\(row\.item,row\.location\)\)/);
});

test("the procurement reorder dialog defaults to the linked store when the item is held there", () => {
  assert.match(PROCUREMENT, /function reorderDefaultLocationId\(context,fromLocationId\)/);
  assert.match(PROCUREMENT, /async function reorderItem\(itemId,fromLocationId\)/);
  assert.match(PROCUREMENT, /locationPicker\(deliveryLocationId!==null\?reorderLocationInitial\(context,deliveryLocationId\):null/);
  assert.match(PROCUREMENT, /data-proc-reorder-source/);
});

test("the ?reorder= auto-open validates the optional store parameter as digits", () => {
  assert.match(PROCUREMENT, /var fromLocation=params\.get\("location"\);if\(fromLocation&&!\/\^\\d\+\$\/\.test\(String\(fromLocation\)\)\)fromLocation=null;/);
  assert.match(PROCUREMENT, /reorderItem\(wanted,fromLocation\|\|null\)/);
});

test("procurement keeps its access gate before any reorder work runs", () => {
  assert.match(PROCUREMENT, /var __procurementAllowed = JONE\.dashboard\.boot\(/);
  assert.match(PROCUREMENT, /if\(!__procurementAllowed\)return;/);
});

/* ------------------------------ Task 3: sidebar ------------------------------- */

const DASHBOARD_JS = read("js/dashboard.js");

test("the sidebar no longer lists the Service Links entry, on desktop or mobile", () => {
  assert.doesNotMatch(DASHBOARD_JS, /label: "Service Links"/);
  assert.doesNotMatch(DASHBOARD_JS, /href: "service-qr\.html"/);
});

test("removing the sidebar entry keeps the service-qr page, its access rule and its APIs", () => {
  assert.match(DASHBOARD_JS, /"service-qr\.html": \{ roles: MANAGEMENT \}/);
  assert.match(read("js/config.js"), /serviceQrLinks:\s+"\/api\/admin\/service-qr-links\/"/);
  assert.ok(fs.existsSync(path.join(FRONTEND, "dashboard", "service-qr.html")));
});

test("the shared grid icon is still used by other entries, so it is kept", () => {
  assert.match(DASHBOARD_JS, /label: "Availability", href: "availability\.html", icon: "grid"/);
});
