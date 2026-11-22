/* tests-js/test-minimart-cash-session.js

   Regression tests for the three Mini Mart reports (2026-11-22):

     1. "the cash session ID doesn't display in minimart";
     2. "when guests are billed to folio, the cash session ID doesn't remove if
         another payment method is selected";
     3. "the button to confirm payment in minimart is not clicking".

   They drive the REAL dashboard/minimart.html + js/minimart.js in jsdom, so the
   selectors, the state and the submitted payload are the ones production runs.

   Run: node --test tests-js/test-minimart-cash-session.js
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
const tick = () => new Promise((r) => setTimeout(r, 40));

const PRODUCTS = [{ id: 1, name: "Test Water", sku: "TW", category: "Drinks", price: "150.00",
  quantity: "4", unit: "BOTTLE", location: "Reception", low_stock: false, out_of_stock: false }];

async function boot(options) {
  options = options || {};
  const dom = new JSDOM(fs.readFileSync(path.join(FRONTEND, "dashboard/minimart.html"), "utf8"), {
    url: "https://hotel.test/dashboard/minimart.html", runScripts: "outside-only",
  });
  const w = dom.window;
  const calls = [];
  let hold = null;   // when set, the POST promise waits for this resolver
  const envelope = (data) => ({ data });
  w.API = {
    get: async (url) => {
      calls.push({ method: "get", url });
      if (url.includes("reception/guests/")) {
        return envelope({ results: [{ id: 8, full_name: "Ada Obi", bookings: [
          { id: 9, reference: "TEST-STAY", rooms: ["206"], check_in: "2026-11-16",
            check_out: "2026-11-18", status: "CHECKED_IN" }] }] });
      }
      if (url.includes("products/")) {
        return envelope({ results: PRODUCTS,
          cash_sessions: options.noSession ? [] : [{ reference: "CS-TEST-0001", location: "Front desk" }] });
      }
      if (url.includes("sales/")) {
        return envelope({ results: [], count: 0, has_next: false,
          summary: { transactions: 0, items_sold: 0, total_sales: "0", best_selling: [], low_stock: [] } });
      }
      return envelope({});
    },
    post: async (url, data) => {
      calls.push({ method: "post", url, data });
      if (hold) await hold;
      const sale = { reference: "TEST-SALE", created_at: "2026-11-22T12:00:00Z", guest_name: "Ada Obi",
        customer_type: data.guest_id ? "GUEST" : "WALK_IN", room_number: data.guest_id ? "206" : "",
        total: "150.00", payment_status: data.payment_method === "ROOM_CHARGE" ? "NOT_APPLICABLE" : "PAID",
        payment_method: data.payment_method, amount_paid: "150.00", staff_name: "Test staff",
        lines: [{ name: "Test Water", quantity: 1, price: "150.00", total: "150.00" }] };
      return envelope({ created: true, sale, products: PRODUCTS });
    },
    put: async () => envelope({}), delete: async () => envelope({}), patch: async () => envelope({}),
  };
  for (const file of ["utils", "reception-services", "minimart"]) {
    w.eval(fs.readFileSync(path.join(FRONTEND, "js/" + file + ".js"), "utf8"));
  }
  await tick();
  return {
    w, dom, calls, $: (s) => w.document.querySelector(s),
    stopHolding: () => { hold = null; },
    holdNextPost: () => { hold = new Promise((r) => setTimeout(r, 120)); },
  };
}

const setMethod = (c, method) => {
  c.$("#mart-payment").value = method;
  c.$("#mart-payment").dispatchEvent(new c.w.Event("change"));
};
const submit = (c) => c.$("[data-mart-checkout]").dispatchEvent(
  new c.w.Event("submit", { bubbles: true, cancelable: true }));
const posts = (c) => c.calls.filter((x) => x.method === "post");

test("the cashier's open cash session is displayed, with its reference", { skip: SKIP }, async () => {
  const c = await boot();
  try {
    const select = c.$("#mart-session");
    assert.equal(select.options.length, 2, "the placeholder plus one real session");
    assert.equal(select.value, "CS-TEST-0001", "a single open session is selected automatically");
    assert.equal(select.options[1].value, "CS-TEST-0001");
    const summary = c.$("[data-mart-session-summary]").textContent;
    assert.match(summary, /CS-TEST-0001/, "the session reference is shown in words next to the field");
    assert.match(summary, /Front desk/, "the drawer location is shown too");
    assert.match(summary, /^Your open cash session is /, "it reads as the session, not as a log line");
    assert.equal(c.$("[data-mart-session-summary]").hidden, false);
  } finally { c.w.close(); }
});

test("no open cash session is explained, not left blank", { skip: SKIP }, async () => {
  const c = await boot({ noSession: true });
  try {
    assert.equal(c.$("#mart-session").value, "");
    assert.match(c.$("[data-mart-session-summary]").textContent, /No open cash session/);
    c.$("[data-add=\"1\"]").click();
    assert.equal(c.$("[data-mart-submit]").disabled, true,
      "a cash sale cannot be confirmed without the cashier's own drawer");
    assert.match(c.$("[data-mart-checkout-hint]").textContent, /no open cash session/i);
  } finally { c.w.close(); }
});

test("switching to a room charge clears the cash session and the payload", { skip: SKIP }, async () => {
  const c = await boot();
  try {
    c.$("[data-add=\"1\"]").click();
    assert.equal(c.$("#mart-session").value, "CS-TEST-0001");
    setMethod(c, "ROOM_CHARGE");
    assert.equal(c.$("[data-mart-session-field]").hidden, true);
    assert.equal(c.$("#mart-session").value, "", "the stale reference is cleared from the control");
    c.$("#mart-guest").value = "8";
    c.$("#mart-guest").dispatchEvent(new c.w.Event("change"));
    c.$("#mart-booking").value = "9";
    c.$("#mart-booking").dispatchEvent(new c.w.Event("change"));
    assert.equal(c.$("[data-mart-submit]").disabled, false, "guest + booking is enough for a folio charge");
    submit(c);
    await tick();
    const sent = posts(c);
    assert.equal(sent.length, 1);
    assert.equal(sent[0].data.payment_method, "ROOM_CHARGE");
    assert.equal(sent[0].data.cash_session_reference, undefined,
      "a folio charge must not carry a cash session");
    assert.equal(sent[0].data.transaction_reference, undefined);
    assert.equal(sent[0].data.payment_amount, undefined);
  } finally { c.w.close(); }
});

test("card and transfer also drop the cash session", { skip: SKIP }, async () => {
  const c = await boot();
  try {
    c.$("[data-add=\"1\"]").click();
    for (const method of ["CARD", "BANK_TRANSFER"]) {
      setMethod(c, method);
      assert.equal(c.$("#mart-session").value, "", method + ": session cleared");
      assert.equal(c.$("#mart-session").disabled, true, method + ": the control cannot be mistaken for in use");
      assert.equal(c.$("[data-mart-reference-field]").hidden, false, method + ": a reference is required");
    }
  } finally { c.w.close(); }
});

test("switching back to cash restores the drawer and submits it", { skip: SKIP }, async () => {
  const c = await boot();
  try {
    c.$("[data-add=\"1\"]").click();
    setMethod(c, "CARD");
    assert.equal(c.$("#mart-session").value, "");
    setMethod(c, "CASH");
    assert.equal(c.$("[data-mart-session-field]").hidden, false);
    assert.equal(c.$("#mart-session").value, "CS-TEST-0001", "the open drawer is restored");
    assert.equal(c.$("[data-mart-submit]").disabled, false);
    submit(c);
    await tick();
    const sent = posts(c);
    assert.equal(sent.length, 1);
    assert.equal(sent[0].data.payment_method, "CASH");
    assert.equal(sent[0].data.cash_session_reference, "CS-TEST-0001");
  } finally { c.w.close(); }
});

test("confirm payment responds, submits once and refuses a double click", { skip: SKIP }, async () => {
  const c = await boot();
  try {
    c.$("[data-add=\"1\"]").click();
    assert.equal(c.$("[data-mart-submit]").disabled, false, "the confirm button is enabled and clickable");
    c.holdNextPost();
    submit(c);
    submit(c);   // the impatient second press a cashier makes on a slow link
    assert.equal(c.$("[data-mart-submit]").disabled, true, "the button is disabled while the sale is in flight");
    assert.equal(c.$("[data-mart-submit]").getAttribute("aria-busy"), "true");
    await tick(); await tick(); await tick();
    const sent = posts(c);
    assert.equal(sent.length, 1, "exactly one checkout was submitted, not two");
    assert.equal(sent[0].data.expected_total, "150.00");
    assert.ok(sent[0].data.idempotency_key);
    assert.equal(c.$("[data-mart-submit]").getAttribute("aria-busy"), "false",
      "the button is no longer busy after the sale completes");
    // The cart is empty now, so it stays disabled for the RIGHT reason; filling
    // it again must re-enable it (the button is not left stuck).
    c.$("[data-add=\"1\"]").click();
    assert.equal(c.$("[data-mart-submit]").disabled, false, "a new sale can be confirmed again");
  } finally { c.w.close(); }
});
