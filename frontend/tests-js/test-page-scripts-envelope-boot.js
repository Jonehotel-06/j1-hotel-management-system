/* tests-js/test-page-scripts-envelope-boot.js */
/* The Breakfast and Amenities page scripts must BOOT and populate their pages.

   Reported symptom: the Breakfast and Amenities screens rendered a dashboard
   shell but nothing worked — no guest picker, no stock, no actions. The audit
   found three chained defects, all of which this suite pins:

   1. THE PAGES DID NOT LOAD THEIR SCRIPT. dashboard/breakfast.html and
      dashboard/amenities.html shipped without <script src="../js/<page>.js">,
      so the page logic never ran at all. (Pinned by
      test-dashboard-page-scripts.js; here we exercise the script itself.)
   2. THE SCRIPTS READ THE RESPONSE ENVELOPE AS IF IT WERE THE PAYLOAD.
      window.API.get resolves to {status, ok, data, message}; the scripts read
      data.results / data.location / data.balances directly, so even with the
      script loaded every dropdown stayed empty and the stock table never
      rendered. They now unwrap with the same contract as API.unwrap.
   3. THEY COULD NOT SEE THE API OBJECT. The scripts read the API as
      window.JONE.api, but nothing ever assigned it — var API was undefined
      and init() returned before doing anything. api.js now exposes the API
      object on the JONE namespace, like JONE.ui and JONE.dashboard.

   Here the REAL page HTML and the REAL page scripts run in jsdom against a
   stubbed API that answers with proper envelopes, exactly as the backend
   does. The assertions are on the DOM the desk actually sees.

   jsdom is an optional dev dependency here (npm i jsdom --no-save). When it
   is not installed the whole file SKIPS with that reason printed — it never
   passes silently.

   Run: node --test tests-js/test-page-scripts-envelope-boot.js   (from frontend/) */
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

const SKIP = JSDOM ? false : "jsdom is not installed (npm i jsdom --no-save) — the page boot was NOT exercised";

/* The core scripts every dashboard page loads before its page script. */
const CORE = ["js/config.js", "js/utils.js", "js/icons.js", "js/api.js", "js/theme.js", "js/ui.js", "js/auth.js", "js/dashboard.js"];

function jsonResponse(payload) {
  return {
    ok: true,
    status: 200,
    headers: { get: (k) => (String(k).toLowerCase() === "content-type" ? "application/json" : null) },
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  };
}

/* Stub the API the way the backend answers: every response is the
   {success, data} envelope the api.js layer unwraps. */
function stubFetch() {
  return async (url) => {
    const u = String(url);
    if (u.includes("/reception/guests/")) {
      return jsonResponse({ success: true, data: { results: [{ id: 3, full_name: "Ada Obi", bookings: [{id:7,reference:"J1-TEST",check_in:"2026-11-16",check_out:"2026-11-18",status:"CHECKED_IN",rooms:["206"]}] }] } });
    }
    if (u.includes("/breakfast/config/")) {
      return jsonResponse({ success: true, data: { is_enabled: true, service_start: "2026-11-01", service_end: "2026-12-31" } });
    }
    if (u.includes("/breakfast/issues/")) {
      return jsonResponse({ success: true, data: { results: [] } });
    }
    if (u.includes("/inventory/items/")) {
      return jsonResponse({ success: true, data: { results: [{ id: 5, sku: "AMN-SOAP", name: "Guest Soap", base_unit: "BAR", is_active: true }] } });
    }
    if (u.includes("/inventory/locations/")) {
      return jsonResponse({ success: true, data: { results: [{ id: 9, code: "STORE", name: "Store" }] } });
    }
    if (u.includes("/reception/amenities/stock/")) {
      return jsonResponse({ success: true, data: {
        location: { id: 4, code: "RECEPTION", name: "Reception" },
        balances: [{ item_id: 12, sku: "AMN-SOAP", name: "Guest Soap", base_unit: "BAR", quantity: "12.0000" }],
      } });
    }
    if (u.includes("/reception/amenities/issues/")) {
      return jsonResponse({ success: true, data: { results: [] } });
    }
    return jsonResponse({ success: true, data: { results: [] } });
  };
}

/* Boot the real page: parse the real HTML (scripts NOT auto-run), eval the
   core scripts and the page script, and let init() settle. */
async function bootPage(pageName, scriptName) {
  const html = fs.readFileSync(path.join(FRONTEND, "dashboard", pageName), "utf8");
  const dom = new JSDOM(html, {
    url: "https://www.jonehotel.test/dashboard/" + pageName,
    runScripts: "outside-only",
    pretendToBeVisual: true,
  });
  const { window } = dom;
  window.fetch = stubFetch();
  window.matchMedia = window.matchMedia || (() => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }));
  for (const f of CORE.concat(["js/reception-services.js", scriptName])) {
    window.eval(fs.readFileSync(path.join(FRONTEND, f), "utf8"));
  }
  await new Promise((r) => setTimeout(r, 80));
  return window;
}

test("the page scripts get a working API from the standard core script set", { skip: SKIP }, () => {
  /* The page scripts resolve the API as (JONE.api) || window.API. api.js
     must set window.API — that is the contract the page boot depends on;
     without it the scripts see API undefined and init() returns immediately. */
  const dom = new JSDOM("<!doctype html><html><body></body></html>", { runScripts: "outside-only" });
  const { window } = dom;
  window.eval(fs.readFileSync(path.join(FRONTEND, "js/api.js"), "utf8"));
  assert.equal(typeof window.API, "object", "api.js must expose window.API for the page scripts");
  assert.equal(typeof window.API.get, "function", "window.API.get must be callable");
  assert.equal(typeof window.API.unwrap, "function", "window.API.unwrap must be callable (the envelope contract)");
});

test("breakfast.html: the guest picker and the config banner populate", { skip: SKIP }, async () => {
  const window = await bootPage("breakfast.html", "js/breakfast.js");
  const doc = window.document;
  const options = Array.from(doc.querySelectorAll("[data-breakfast-guest-select] option")).map((o) => o.textContent);
  assert.ok(options.some((t) => t.includes("Ada Obi")),
    "the guest picker must populate from the API envelope: " + JSON.stringify(options));
  const banner = doc.querySelector("[data-breakfast-config-status]");
  assert.ok(banner && /Enabled/i.test(banner.textContent),
    "the config banner must render the config: " + (banner && banner.textContent));
});

test("amenities.html: the guest picker, the issue picker and the stock table populate", { skip: SKIP }, async () => {
  const window = await bootPage("amenities.html", "js/amenities.js");
  const doc = window.document;
  const guestOptions = Array.from(doc.querySelectorAll("[data-amenity-guest-select] option")).map((o) => o.textContent);
  assert.ok(guestOptions.some((t) => t.includes("Ada Obi")),
    "the guest picker must populate from the API envelope: " + JSON.stringify(guestOptions));
  const itemOptions = Array.from(doc.querySelectorAll("[data-amenity-issue-item-select] option")).map((o) => o.textContent);
  assert.ok(itemOptions.some((t) => t.includes("Guest Soap")),
    "the issue picker must populate from the Reception stock: " + JSON.stringify(itemOptions));
  const stock = doc.querySelector("[data-amenity-stock-container]");
  assert.ok(stock && /Guest Soap/.test(stock.textContent),
    "the stock table must render the balances: " + (stock && stock.textContent.slice(0, 80)));
});
