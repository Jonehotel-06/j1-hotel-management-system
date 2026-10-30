"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { makeContext, loadScript, makeElement, disposeAll, FRONTEND } = require("./harness");
test.after(disposeAll);

function setup(canWrite = true) {
  const ctx = makeContext();
  ctx.APP_CONFIG = { API_ENDPOINTS: { posKitchenFood: "/configured/food/" } };
  ctx.Auth = { hasCapability: () => canWrite };
  ctx.JONE = { esc: value => String(value), dashboard: {}, ui: {} };
  const root = makeElement("div"), nodes = {};
  ["[data-food-list]", "[data-food-error]", "[data-food-prev]", "[data-food-next]", "[data-food-search]"].forEach(k => nodes[k] = makeElement("div"));
  root.querySelector = selector => nodes[selector];
  const calls = [];
  ctx.API = {
    list: async () => ({ data: [{ id: 7, name: "Pepper soup", is_available: true, availability_label: "Ready", portion_tracking: false, prepared_portions: 0 }], pagination: { next: null } }),
    post: async (url, body) => { calls.push({ url, body }); return { data: {} }; },
  };
  loadScript(ctx, "js/kitchen-food.js");
  const panel = ctx.JONE.kitchenFood.mount(root);
  async function click(action) {
    const button = { getAttribute: key => key === "data-food-id" ? "7" : action };
    const event = { target: { closest: selector => selector === "[data-food-action]" ? button : null } };
    return root.listeners.click[0](event);
  }
  return { ctx, root, nodes, calls, panel, click };
}

test("prepared quantities require bounded whole numbers", () => {
  const { ctx } = setup();
  assert.equal(ctx.JONE.kitchenFood.quantity("15"), 15);
  for (const value of [0, -1, 1.5, "rice", "", 1000001]) assert.throws(() => ctx.JONE.kitchenFood.quantity(value));
});

test("simple food controls need neither recipes nor ingredients", async () => {
  const { ctx, panel, nodes, click, calls } = setup();
  await panel.load();
  assert.match(nodes["[data-food-list]"].innerHTML, /Mark Not Available/);
  assert.match(nodes["[data-food-list]"].innerHTML, /No portion tracking/);
  ctx.JONE.dashboard.formModal = async opts => {
    assert.equal(opts.fields.some(f => /recipe|ingredient/i.test(f.name)), false);
    return opts.onSubmit({ reason: "Kitchen closed" });
  };
  await click("NOT_AVAILABLE");
  assert.equal(calls[0].url, "/configured/food/7/");
  assert.equal(calls[0].body.action, "NOT_AVAILABLE");
  assert.equal(calls[0].body.quantity, 0);
  assert.ok(calls[0].body.idempotency_key);
});

test("uncertain-network retries retain the exact preparation idempotency key", async () => {
  const { ctx, panel, click, calls } = setup();
  await panel.load();
  ctx.API.post = async (url, body) => {
    calls.push({ url, body });
    if (calls.length === 1) throw new Error("Network timeout after possible commit");
    return { data: {} };
  };
  ctx.JONE.dashboard.formModal = async opts => {
    await assert.rejects(opts.onSubmit({ quantity: "15", reason: "Lunch" }));
    return opts.onSubmit({ quantity: "15", reason: "Lunch" });
  };
  await click("REPLENISH");
  assert.equal(calls.length, 2);
  assert.equal(calls[0].body.quantity, 15);
  assert.equal(calls[0].body.idempotency_key, calls[1].body.idempotency_key);
});

test("read-only kitchen staff see status but no write controls", async () => {
  const { panel, nodes } = setup(false);
  await panel.load();
  assert.match(nodes["[data-food-list]"].innerHTML, /Ready/);
  assert.doesNotMatch(nodes["[data-food-list]"].innerHTML, /data-food-action/);
});

test("a failed refresh retains the last food list and reports an error", async () => {
  const { ctx, panel, nodes } = setup();
  await panel.load();
  const before = nodes["[data-food-list]"].innerHTML;
  ctx.API.list = async () => { throw new Error("Offline"); };
  assert.equal(await panel.load(true), false);
  assert.equal(nodes["[data-food-list]"].innerHTML, before);
  assert.equal(nodes["[data-food-error]"].textContent, "Offline");
});

test("unlimited Ready food never claims 100 prepared portions in the guest badge", () => {
  const ctx = makeContext();
  loadScript(ctx, "js/portal-catalog.js");
  const badge = ctx.JONE.portalCatalog.availabilityBadge({ is_available: true, available_quantity: 100, quantity_limited: false });
  assert.equal(badge.label, "Available");
});

test("POS refresh preserves modifiers and uses the existing live refresh loop", () => {
  const source = fs.readFileSync(path.join(FRONTEND, "dashboard/pos.html"), "utf8");
  assert.match(source, /refreshes.push\(loadMenu\(state.menuArea,true\)\)/);
  assert.match(source, /selectedModifiers.indexOf\(input.value\)/);
  assert.match(source, /data-kitchen-food-panel/);
  assert.doesNotMatch(source, /location\.reload\(/);
});
