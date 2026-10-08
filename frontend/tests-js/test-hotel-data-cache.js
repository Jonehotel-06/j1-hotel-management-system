/* Hotel settings hydration should not add a request on every hard navigation. */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { makeContext, loadScript, disposeAll } = require("./harness");

test.after(() => disposeAll());

function boot(cached) {
  const ctx = makeContext();
  ctx.document.dispatchEvent = () => true;
  ctx.CustomEvent = class CustomEvent { constructor(type, init) { this.type = type; this.detail = init && init.detail; } };
  if (cached) ctx.localStorage.setItem("jone.hotel.cache", JSON.stringify(cached));
  loadScript(ctx, "js/config.js");
  loadScript(ctx, "js/utils.js");
  return ctx;
}

test("a fresh persisted hotel cache hydrates without another API request", async () => {
  const ctx = boot({
    settings: { name: "J-ONE", timezone: "Africa/Lagos" },
    saved_at: Date.now(),
  });
  let requests = 0;
  ctx.window.API = { getHotelInfo: async () => { requests += 1; return { data: {} }; } };
  loadScript(ctx, "js/hotel-data.js");

  await ctx.window.JONE.hotel.init();

  assert.equal(requests, 0);
  assert.equal(ctx.window.JONE.hotel.get().name, "J-ONE");
});

test("an absent cache fetches once, then the same page reuses the fresh result", async () => {
  const ctx = boot();
  let requests = 0;
  ctx.window.API = {
    getHotelInfo: async () => {
      requests += 1;
      return { data: { settings: { name: "J-ONE", timezone: "Africa/Lagos" } } };
    },
  };
  loadScript(ctx, "js/hotel-data.js");

  await ctx.window.JONE.hotel.init();
  await ctx.window.JONE.hotel.init();

  assert.equal(requests, 1);
  assert.equal(ctx.window.JONE.hotel.get().timezone, "Africa/Lagos");
});
