/* tests-js/test-minimart-room-charge.js

   Frontend contract for the Mini Mart guest room charge added by this repair.

   What is proven here:
     · the payment selector offers a room-charge option;
     · choosing it switches to the hotel-guest customer type and shows the
       guest picker instead of silently charging a walk-in;
     · Checkout stays disabled until a guest AND a booking are chosen;
     · the submitted payload carries the guest/booking ids and no cash session,
       no transaction reference and no payment amount;
     · a walk-in cash sale is unchanged (regression guard).

   Run: node --test tests-js/test-minimart-room-charge.js
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
const tick = () => new Promise((r) => setTimeout(r, 35));

async function boot() {
  const dom = new JSDOM(fs.readFileSync(path.join(FRONTEND, "dashboard/minimart.html"), "utf8"), {
    url: "https://hotel.test/dashboard/minimart.html", runScripts: "outside-only",
  });
  const w = dom.window;
  const calls = [];
  const products = [
    { id: 1, name: "Test Water", sku: "TW", category: "Drinks", price: "150.00", quantity: "4", unit: "BOTTLE", location: "Reception", low_stock: false, out_of_stock: false },
  ];
  const envelope = (data) => ({ data });
  w.API = {
    get: async (url) => {
      calls.push({ method: "get", url });
      if (url.includes("reception/guests/")) {
        return envelope({ results: [{
          id: 8, full_name: "Ada Obi",
          bookings: [
            { id: 9, reference: "TEST-STAY", rooms: ["206"], check_in: "2026-11-16", check_out: "2026-11-18", status: "CHECKED_IN" },
            { id: 10, reference: "TEST-STAY-OLD", rooms: ["207"], check_in: "2026-12-20", check_out: "2026-12-22", status: "CONFIRMED" },
          ],
        }] });
      }
      if (url.includes("products/")) return envelope({ results: products, cash_sessions: [{ reference: "TEST-CASH" }] });
      if (url.includes("sales/")) {
        return envelope({ results: [], count: 0, has_next: false, summary: { transactions: 0, items_sold: 0, total_sales: "0", best_selling: [], low_stock: [] } });
      }
      return envelope({});
    },
    post: async (url, data) => {
      calls.push({ method: "post", url, data });
      const sale = {
        reference: "TEST-SALE", created_at: "2026-11-16T12:00:00Z",
        guest_name: data.guest_id ? "Ada Obi" : "Walk-in customer",
        customer_type: data.guest_id ? "GUEST" : "WALK_IN",
        room_number: data.guest_id ? "206" : "", total: "150.00",
        payment_status: data.payment_method === "ROOM_CHARGE" ? "NOT_APPLICABLE" : "PAID",
        payment_method: data.payment_method, amount_paid: data.payment_method === "ROOM_CHARGE" ? "0.00" : "150.00",
        staff_name: "Test staff",
        lines: [{ name: "Test Water", quantity: 1, price: "150.00", total: "150.00" }],
      };
      return envelope({ created: true, sale, products });
    },
  };
  for (const file of ["utils", "reception-services", "minimart"]) {
    w.eval(fs.readFileSync(path.join(FRONTEND, "js/" + file + ".js"), "utf8"));
  }
  await tick();
  return { w, dom, calls, $: (s) => w.document.querySelector(s) };
}

const submit = (c) => c.$("[data-mart-checkout]").dispatchEvent(new c.w.Event("submit", { bubbles: true, cancelable: true }));

test("the payment selector offers a room charge", { skip: SKIP }, async () => {
  const c = await boot();
  try {
    const options = Array.from(c.$("#mart-payment").options).map((o) => o.value);
    assert.ok(options.includes("ROOM_CHARGE"));
    for (const method of ["CASH", "CARD", "BANK_TRANSFER", "PAYSTACK"]) assert.ok(options.includes(method), method);
  } finally { c.w.close(); }
});

test("choosing room charge switches to the guest workflow and hides tender fields", { skip: SKIP }, async () => {
  const c = await boot();
  try {
    c.$("#mart-payment").value = "ROOM_CHARGE";
    c.$("#mart-payment").dispatchEvent(new c.w.Event("change"));
    assert.equal(c.$("#mart-customer").value, "GUEST", "was switched to the hotel-guest type");
    assert.equal(c.$("[data-mart-guest-fields]").hidden, false, "guest picker is visible");
    assert.equal(c.$("[data-mart-session-field]").hidden, true, "no cash session is asked for");
    assert.equal(c.$("[data-mart-reference-field]").hidden, true, "no transaction reference is asked for");
    assert.equal(c.$("[data-mart-amount-field]").hidden, true, "no payment amount is asked for");
    assert.equal(c.$("[data-mart-room-charge-hint]").hidden, false);
    assert.match(c.$("[data-mart-submit]").textContent, /Charge to guest room/);
  } finally { c.w.close(); }
});

test("checkout stays disabled until a guest and booking are selected", { skip: SKIP }, async () => {
  const c = await boot();
  try {
    c.$("#mart-payment").value = "ROOM_CHARGE";
    c.$("#mart-payment").dispatchEvent(new c.w.Event("change"));
    c.$("[data-add=\"1\"]").click();
    assert.equal(c.$("[data-mart-submit]").disabled, true, "no guest yet");
    c.$("#mart-guest").value = "8";
    c.$("#mart-guest").dispatchEvent(new c.w.Event("change"));
    assert.equal(c.$("[data-mart-submit]").disabled, true, "guest chosen but no booking");
    c.$("#mart-booking").value = "9";
    c.$("#mart-booking").dispatchEvent(new c.w.Event("change"));
    assert.equal(c.$("[data-mart-submit]").disabled, false, "guest + booking are ready");
  } finally { c.w.close(); }
});

test("the room-charge payload carries the stay and no tender fields", { skip: SKIP }, async () => {
  const c = await boot();
  try {
    c.$("#mart-payment").value = "ROOM_CHARGE";
    c.$("#mart-payment").dispatchEvent(new c.w.Event("change"));
    c.$("[data-add=\"1\"]").click();
    c.$("#mart-guest").value = "8";
    c.$("#mart-guest").dispatchEvent(new c.w.Event("change"));
    c.$("#mart-booking").value = "9";
    c.$("#mart-booking").dispatchEvent(new c.w.Event("change"));
    submit(c);
    await tick();
    const posts = c.calls.filter((x) => x.method === "post");
    assert.equal(posts.length, 1);
    const data = posts[0].data;
    assert.equal(data.payment_method, "ROOM_CHARGE");
    assert.equal(data.guest_id, 8);
    assert.equal(data.booking_id, 9);
    assert.equal(data.expected_total, "150.00");
    assert.ok(data.idempotency_key, "idempotency key present");
    assert.equal(data.cash_session_reference, undefined);
    assert.equal(data.transaction_reference, undefined);
    assert.equal(data.payment_amount, undefined);
    assert.equal(JSON.stringify(data.lines), JSON.stringify([{ product_id: 1, quantity: 1 }]));
    assert.match(c.$("[data-mart-message]").textContent, /Sale completed/);
    assert.match(c.$("[data-mart-cart]").textContent, /empty/, "cart cleared after success");
  } finally { c.w.close(); }
});

test("a room charge without a booking is refused before any request", { skip: SKIP }, async () => {
  const c = await boot();
  try {
    c.$("#mart-payment").value = "ROOM_CHARGE";
    c.$("#mart-payment").dispatchEvent(new c.w.Event("change"));
    c.$("[data-add=\"1\"]").click();
    c.$("#mart-guest").value = "8";
    c.$("#mart-guest").dispatchEvent(new c.w.Event("change"));
    submit(c);
    await tick();
    assert.equal(c.calls.filter((x) => x.method === "post").length, 0);
    assert.match(c.$("[data-mart-message]").textContent, /in-house guest and their current booking/);
  } finally { c.w.close(); }
});

test("a walk-in cash sale is unchanged", { skip: SKIP }, async () => {
  const c = await boot();
  try {
    c.$("[data-add=\"1\"]").click();
    submit(c);
    await tick();
    const data = c.calls.filter((x) => x.method === "post")[0].data;
    assert.equal(data.payment_method, "CASH");
    assert.equal(data.cash_session_reference, "TEST-CASH");
    assert.equal(data.guest_id, undefined);
    assert.equal(data.booking_id, undefined);
  } finally { c.w.close(); }
});
