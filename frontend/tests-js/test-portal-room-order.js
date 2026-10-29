/* Guest room-service cart and duplicate-safe retry contracts. */
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { makeContext, loadScript, makeElement, FRONTEND, disposeAll } = require("./harness");

test.after(() => disposeAll());

function setup(extra) {
  const ctx = makeContext();
  loadScript(ctx, "js/portal-catalog.js");
  loadScript(ctx, "js/portal-order.js");
  let keyCount = 0;
  const opts = Object.assign({
    getStays: () => [{ reference: "STAY-1", booking_reference: "BOOK-1", rooms: [{ id: 17, room_number: "101" }] }],
    newKey: () => "order-key-" + (++keyCount),
    submit: async () => ({ data: { reference: "SRQ-1", pos_order: { status: "DRAFT", total_amount: "0.00" } } }),
  }, extra || {});
  const cart = ctx.window.JONE.portalOrder.createController(opts);
  cart.syncStays();
  return { ctx, cart, keyCount: () => keyCount };
}
function item(id = 1, overrides = {}) {
  return Object.assign({ id, name: "Rice", price: "1250.10", currency: "NGN", is_available: true, available_quantity: 5 }, overrides);
}
function plain(value) { return JSON.parse(JSON.stringify(value)); }

test("cart collects server IDs, quantities and exact price acknowledgements, not client totals", async () => {
  const calls = [];
  const { cart, keyCount } = setup({ submit: async (body) => {
    calls.push(plain(body));
    return { data: { reference: "SRQ-1", pos_order: { status: "DRAFT", total_amount: "3300.20" } } };
  } });
  assert.equal(cart.add(item()), true);
  assert.equal(cart.add(item()), true);
  assert.equal(cart.add(item(2, { name: "Malt", price: "800.00" })), true);
  assert.equal(cart.total(), 330020);
  cart.setNotes("  Deliver to door  ");
  assert.equal(await cart.submit(), true);
  assert.equal(keyCount(), 1);
  assert.deepEqual(calls[0], { stay_reference: "STAY-1", room_id: 17, payment_method: "FOLIO", notes: "Deliver to door", idempotency_key: "order-key-1",
    lines: [{ menu_item_id: 1, quantity: 2, expected_unit_price: "1250.10" }, { menu_item_id: 2, quantity: 1, expected_unit_price: "800.00" }] });
  assert.equal(cart.state.lines.length, 0);
  assert.equal(cart.state.confirmed.reference, "SRQ-1");
  assert.equal(cart.state.confirmed.pos_order.status, "DRAFT");
});

test("availability, quantity, currency and line bounds are enforced without silently dropping items", () => {
  const { cart } = setup();
  assert.equal(cart.add(item(1, { is_available: false })), false);
  assert.equal(cart.add(item(1, { available_quantity: 0 })), false);
  assert.equal(cart.add(item(1, { price: "not money" })), false);
  assert.equal(cart.add(item()), true);
  assert.equal(cart.setQuantity(1, "1.5"), false);
  assert.equal(cart.setQuantity(1, "0"), false);
  assert.equal(cart.setQuantity(1, "101"), false);
  assert.equal(cart.setQuantity(1, "6"), true);
  assert.match(cart.lineIssue(cart.state.lines[0]), /Only 5/);
  assert.equal(cart.add(item(2, { currency: "USD" })), false);
  assert.equal(cart.state.lines.length, 1);
  assert.equal(cart.setQuantity(1, "3"), true);
  assert.equal(cart.lineIssue(cart.state.lines[0]), "");
  cart.remove(1);
  assert.equal(cart.state.lines.length, 0);
});

test("price or availability changes are shown per item and never rewrite a selected price", async () => {
  const { cart } = setup();
  cart.add(item());
  cart.reconcile([{ items: [item(1, { price: "1300.00", available_quantity: 1 })] }]);
  assert.equal(cart.state.lines[0].price, "1250.10");
  assert.match(cart.lineIssue(cart.state.lines[0]), /price changed/i);
  assert.equal(await cart.submit(), false);
  assert.equal(cart.state.lines.length, 1);
  cart.remove(1);
  cart.add(item(1, { price: "1300.00" }));
  assert.equal(cart.state.lines[0].price, "1300.00");
  cart.reconcile([]);
  assert.match(cart.lineIssue(cart.state.lines[0]), /unavailable/i);
  assert.equal(cart.state.lines.length, 1);
});

test("uncertain result freezes the payload and key; double-click and retry cannot duplicate it", async () => {
  const calls = [];
  let finishFirst;
  const { cart, keyCount } = setup({ submit: (body) => {
    calls.push(plain(body));
    if (calls.length === 1) return new Promise((resolve, reject) => { finishFirst = reject; });
    return Promise.resolve({ data: { reference: "SRQ-1", pos_order: { status: "DRAFT", total_amount: "1250.10" } } });
  } });
  cart.add(item());
  const first = cart.submit();
  assert.equal(await cart.submit(), false); // an in-flight double click
  assert.equal(calls.length, 1);
  finishFirst({ status: 0, kind: "timeout", message: "timeout" });
  assert.equal(await first, false);
  assert.ok(Object.isFrozen(cart.state.pending));
  assert.ok(Object.isFrozen(cart.state.pending.lines[0]));
  assert.equal(cart.add(item(2)), false);
  assert.equal(cart.setQuantity(1, 2), false);
  assert.equal(cart.remove(1), false);
  assert.match(cart.state.message, /retry|check/i);
  assert.equal(await cart.submit(), true);
  assert.deepEqual(calls[1], calls[0]);
  assert.equal(keyCount(), 1);
  assert.equal(cart.state.confirmed.reference, "SRQ-1");
});

test("even an unreadable 2xx or post-commit 404 remains uncertain and keeps the same key", async () => {
  let mode = 0; const calls = [];
  const { cart, keyCount } = setup({ submit: async (body) => {
    calls.push(plain(body)); mode++;
    if (mode === 1) return { data: {} };
    if (mode === 2) throw { status: 404, kind: "http", data: { success: false } };
    return { data: { reference: "SRQ-FOUND", pos_order: { status: "DELIVERED", charge_posted: true } } };
  } });
  cart.add(item());
  assert.equal(await cart.submit(), false);
  assert.ok(cart.state.pending);
  assert.equal(await cart.submit(), false);
  assert.ok(cart.state.pending);
  assert.equal(await cart.submit(), true);
  assert.deepEqual(calls[0], calls[1]); assert.deepEqual(calls[1], calls[2]);
  assert.equal(keyCount(), 1);
});

test("definitive stock conflict names affected products and permits correction", async () => {
  const calls = [];
  const { cart, keyCount } = setup({ submit: async (body) => {
    calls.push(plain(body));
    if (calls.length === 1) throw { status: 409, kind: "http", message: "Rice cannot be ordered now.",
      data: { success: false, errors: { items: [{ menu_item_id: 1, item_name: "Rice" }] } } };
    return { data: { reference: "SRQ-2", pos_order: { status: "DRAFT" } } };
  } });
  cart.add(item()); cart.setQuantity(1, 3);
  assert.equal(await cart.submit(), false);
  assert.equal(cart.state.pending, null);
  assert.equal(cart.state.lines.length, 1);
  assert.match(cart.lineIssue(cart.state.lines[0]), /cannot be ordered/i);
  cart.setQuantity(1, 2);
  assert.equal(await cart.submit(), true);
  assert.equal(keyCount(), 2);
  assert.equal(calls[1].lines[0].quantity, 2);
});

test("active stay and a specifically assigned room are required", async () => {
  let live = [{ reference: "S1", rooms: [{ id: 1 }, { id: 2 }] }, { reference: "S2", rooms: [{ id: 3 }] }];
  const { cart } = setup({ getStays: () => live });
  cart.add(item());
  assert.equal(await cart.submit(), false);
  cart.chooseStay("S1"); cart.chooseRoom("2");
  assert.equal(await cart.submit(), true);
  cart.add(item());
  live = [];
  cart.syncStays();
  assert.equal(await cart.submit(), false);
  assert.match(cart.state.message, /active in-house stay/i);
});

test("mount renders accessible cart, totals, errors and escaped product text", async () => {
  const ctx = makeContext(); loadScript(ctx, "js/portal-catalog.js"); loadScript(ctx, "js/portal-order.js");
  const root = makeElement("div");
  const cart = ctx.window.JONE.portalOrder.mount(root, {
    getStays: () => [{ reference: "S", booking_reference: "B", rooms: [{ id: 7, room_number: "101" }] }],
    newKey: () => "k", submit: async () => { throw { status: 0, kind: "timeout" }; },
  });
  assert.match(root.innerHTML, /Your order is empty/);
  cart.add(item(1, { name: '<img src=x onerror="alert(1)">' }));
  assert.match(root.innerHTML, /&lt;img/); assert.doesNotMatch(root.innerHTML, /<img/);
  assert.match(root.innerHTML, /Quantity/); assert.match(root.innerHTML, /Order total/);
  assert.match(root.innerHTML, /₦1,250\.10/);
  assert.equal(await cart.submit(), false);
  assert.match(root.innerHTML, /Retry same order/);
  assert.match(root.innerHTML, /Check Service requests/);
  assert.match(root.innerHTML, / disabled/);
});

test("a retry returning a cancelled order never claims it will be charged on delivery", async () => {
  const ctx = makeContext(); loadScript(ctx, "js/portal-catalog.js"); loadScript(ctx, "js/portal-order.js");
  const root = makeElement("div");
  const cart = ctx.window.JONE.portalOrder.mount(root, {
    getStays: () => [{ reference: "S", rooms: [{ id: 7, room_number: "101" }] }],
    newKey: () => "k", submit: async () => ({ data: { reference: "SRQ-CANCELLED",
      pos_order: { status: "CANCELLED", total_amount: "1250.10", currency: "NGN", charge_posted: false } } }),
  });
  cart.add(item());
  assert.equal(await cart.submit(), true);
  assert.match(root.innerHTML, /Order cancelled/);
  assert.match(root.innerHTML, /No charge was posted for this cancelled order/);
  assert.doesNotMatch(root.innerHTML, /charged to your folio on delivery/);
});

test("portal integrates the cart and displays order status without storing portal data", () => {
  const page = fs.readFileSync(path.join(FRONTEND, "portal", "index.html"), "utf8");
  const script = fs.readFileSync(path.join(FRONTEND, "js", "portal-order.js"), "utf8");
  assert.match(page, /portal-order\.js/);
  assert.match(page, /JONE\.portalOrder\.mount/);
  assert.match(page, /cart\.add\(item\)/);
  assert.match(page, /orderDetailHtml\(request\.pos_order\)/);
  assert.match(page, /row\.pos_order\.can_cancel/);
  assert.doesNotMatch(script, /localStorage|sessionStorage|document\.cookie/);
  assert.doesNotMatch(script, /\/api\/|localhost|127\.0\.0\.1/);
});
