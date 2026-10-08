/* tests-js/test-update-checker.js */
/* Tests for js/update-checker.js — the deployment update detector.
   Run: node --test tests-js/test_update_checker.js   (from frontend/) */
"use strict";

const test = require("node:test");
const assert = require("node:assert");
const { makeContext, loadScript } = require("./harness");

function bootContext({ latest = "1.1.0", loaded = "1.1.0", pathname = "/index.html" } = {}) {
  const ctx = makeContext({ hostname: "www.jonehotel.test", pathname });
  ctx.window.JONE_VERSION = loaded;
  let modalOpened = 0;
  let toasts = [];
  ctx.fetch = () => Promise.resolve({
    ok: true,
    status: 200,
    json: () => Promise.resolve({ version: latest }),
  });
  loadScript(ctx, "js/utils.js");       // JONE.el used by the modal wiring
  ctx.window.JONE = ctx.window.JONE || {};
  ctx.window.JONE.ui = {
    modal: { open() { modalOpened += 1; }, close() {} },
    toast(msg, type) { toasts.push({ msg, type }); },
  };
  ctx.window.JONE.pwa = {
    isCriticalFlowActive: () => false,
  };
  loadScript(ctx, "js/update-checker.js");
  return { ctx, modalOpened: () => modalOpened, toasts: () => toasts };
}

test("same version: no modal, no toast", async () => {
  const t = bootContext({ latest: "1.1.0", loaded: "1.1.0" });
  await t.ctx.window.JONE.updateChecker.check(true);
  assert.equal(t.modalOpened(), 0, "modal must not appear for the same version");
  assert.equal(t.toasts().length, 0);
});

test("new version: modal appears exactly once", async () => {
  const t = bootContext({ latest: "1.2.0", loaded: "1.1.0" });
  const v = await t.ctx.window.JONE.updateChecker.check(true);
  assert.equal(v, "1.2.0");
  assert.equal(t.modalOpened(), 1, "modal must appear for a new version");
  // Second check (the periodic tick) must NOT stack another modal.
  await t.ctx.window.JONE.updateChecker.check(true);
  assert.equal(t.modalOpened(), 1, "modal must not be shown repeatedly");
});

test("dismissed version is not re-offered", async () => {
  const t = bootContext({ latest: "1.2.0", loaded: "1.1.0" });
  t.ctx.localStorage.setItem("jone.update.dismissedVersion", "1.2.0");
  await t.ctx.window.JONE.updateChecker.check(true);
  assert.equal(t.modalOpened(), 0, "a dismissed version must not re-prompt");
  // A NEWER version than the dismissed one prompts again.
  t2: {
    const t2 = bootContext({ latest: "1.3.0", loaded: "1.1.0" });
    t2.ctx.localStorage.setItem("jone.update.dismissedVersion", "1.2.0");
    await t2.ctx.window.JONE.updateChecker.check(true);
    assert.equal(t2.modalOpened(), 1, "a newer release must prompt again");
  }
});

test("sensitive booking page: modal deferred, quiet notice only", async () => {
  const t = bootContext({ latest: "1.2.0", loaded: "1.1.0", pathname: "/booking-confirmation.html?step=payment" });
  await t.ctx.window.JONE.updateChecker.check(true);
  assert.equal(t.modalOpened(), 0, "no modal mid-booking/payment");
  assert.ok(
    t.toasts().some((x) => /new version/i.test(x.msg)),
    "a quiet, non-blocking toast is shown instead"
  );
});

test("dashboard page: staff DO receive the update modal", async () => {
  // Staff consoles must not silently run stale code after a deployment.
  // (Dirty forms / critical flows still defer — covered separately below.)
  const t = bootContext({ latest: "1.2.0", loaded: "1.1.0", pathname: "/dashboard/bookings.html" });
  await t.ctx.window.JONE.updateChecker.check(true);
  assert.equal(t.modalOpened(), 1, "dashboard users must be told about new releases");
});

test("dashboard check-in page: still a dashboard page, still prompted", async () => {
  // The public 'check-in' sensitive keyword must not swallow dashboard paths.
  const t = bootContext({ latest: "1.2.0", loaded: "1.1.0", pathname: "/dashboard/check-in.html" });
  await t.ctx.window.JONE.updateChecker.check(true);
  assert.equal(t.modalOpened(), 1);
});

test("refresh now uses JONE.pwa.refreshToLatest when available (SW activation)", async () => {
  const ctx = makeContext({ hostname: "www.jonehotel.test", pathname: "/index.html" });
  ctx.window.JONE_VERSION = "1.1.0";
  let reloaded = 0;
  let swRefreshed = 0;
  ctx.fetch = () => Promise.resolve({ ok: true, json: () => Promise.resolve({ version: "1.2.0" }) });
  loadScript(ctx, "js/utils.js");
  ctx.window.JONE = ctx.window.JONE || {};
  ctx.window.JONE.ui = { modal: { open() {}, close() {} }, toast() {} };
  ctx.window.JONE.pwa = {
    isCriticalFlowActive: () => false,
    // The real implementation activates any WAITING service worker, then
    // invokes the reload callback. The checker must route through it.
    refreshToLatest(reload) { swRefreshed += 1; reload(); },
  };
  ctx.location.reload = () => { reloaded += 1; };
  loadScript(ctx, "js/update-checker.js");
  await ctx.window.JONE.updateChecker.check(true);
  ctx.window.JONE.updateChecker._internals.showUpdateModal("1.2.0");
  const refreshBtn = ctx.document._created.find(
    (el) => el.tagName === "BUTTON" && /Refresh now/i.test(el.textContent)
  );
  assert.ok(refreshBtn, "refresh button exists");
  (refreshBtn.listeners.click || []).forEach((fn) => fn({}));
  assert.equal(swRefreshed, 1, "refresh must go through the service-worker-aware path");
  assert.equal(reloaded, 1, "and end in a reload");
});

test("critical flow in progress: no modal", async () => {
  const ctx = makeContext({ hostname: "www.jonehotel.test", pathname: "/index.html" });
  ctx.window.JONE_VERSION = "1.1.0";
  let modalOpened = 0;
  ctx.fetch = () => Promise.resolve({ ok: true, json: () => Promise.resolve({ version: "1.2.0" }) });
  loadScript(ctx, "js/utils.js");
  ctx.window.JONE = ctx.window.JONE || {};
  ctx.window.JONE.ui = { modal: { open() { modalOpened += 1; }, close() {} }, toast() {} };
  ctx.window.JONE.pwa = { isCriticalFlowActive: () => true };  // payment in flight
  loadScript(ctx, "js/update-checker.js");
  await ctx.window.JONE.updateChecker.check(true);
  assert.equal(modalOpened, 0, "never interrupt an active booking/payment flow");
});

test("refresh button reloads the page and never clears storage", async () => {
  const ctx = makeContext({ hostname: "www.jonehotel.test", pathname: "/index.html" });
  ctx.window.JONE_VERSION = "1.1.0";
  let modalConfig = null;
  let reloaded = 0;
  ctx.fetch = () => Promise.resolve({ ok: true, json: () => Promise.resolve({ version: "1.2.0" }) });
  loadScript(ctx, "js/utils.js");
  ctx.window.JONE = ctx.window.JONE || {};
  ctx.window.JONE.ui = { modal: { open(cfg) { modalConfig = cfg; }, close() {} }, toast() {} };
  ctx.window.JONE.pwa = { isCriticalFlowActive: () => false };
  loadScript(ctx, "js/update-checker.js");
  await ctx.window.JONE.updateChecker.check(true);
  assert.ok(modalConfig, "modal opened");
  ctx.location.reload = () => { reloaded += 1; };
  ctx.sessionStorage.setItem("jone.booking.draft", '{"guest":{"email":"x@y.z"}}');
  ctx.sessionStorage.setItem("jone.session", '{"access":"abc"}');
  // Drive the injected "Refresh now" button directly: the modal was built via
  // document.createElement, so find it among the recorded elements.
  ctx.window.JONE.updateChecker._internals.showUpdateModal("1.2.0");
  const refreshBtn = ctx.document._created.find(
    (el) => el.tagName === "BUTTON" && /Refresh now/i.test(el.textContent)
  );
  assert.ok(refreshBtn, "refresh button exists and is clearly labelled");
  assert.ok(
    /refresh now/i.test(refreshBtn.getAttribute("aria-label") || ""),
    "refresh button communicates its action accessibly"
  );
  (refreshBtn.listeners.click || []).forEach((fn) => fn({}));
  assert.equal(reloaded, 1, "refresh reloads the page");
  // Storage (booking draft + session) must be untouched by the update flow.
  assert.ok(ctx.sessionStorage.getItem("jone.booking.draft"), "booking draft preserved");
  assert.ok(ctx.sessionStorage.getItem("jone.session"), "auth session preserved");
});

test("version check failure is silent (never breaks the site)", async () => {
  const t = (() => {
    const ctx = makeContext({ hostname: "www.jonehotel.test", pathname: "/index.html" });
    ctx.window.JONE_VERSION = "1.1.0";
    ctx.fetch = () => Promise.reject(new TypeError("network down"));
    loadScript(ctx, "js/utils.js");
    ctx.window.JONE = ctx.window.JONE || {};
    let opened = 0;
    ctx.window.JONE.ui = { modal: { open() { opened += 1; }, close() {} }, toast() {} };
    ctx.window.JONE.pwa = { isCriticalFlowActive: () => false };
    loadScript(ctx, "js/update-checker.js");
    return { ctx, opened: () => opened };
  })();
  const v = await t.ctx.window.JONE.updateChecker.check(true);
  assert.equal(v, null);
  assert.equal(t.opened(), 0, "a failed version fetch must be invisible");
});

test("no embedded version marker: checker stays inert", async () => {
  const ctx = makeContext({ hostname: "www.jonehotel.test", pathname: "/index.html" });
  // window.JONE_VERSION intentionally NOT set (very old cached page).
  let fetched = 0;
  ctx.fetch = () => { fetched += 1; return Promise.reject(new Error("should not fetch")); };
  loadScript(ctx, "js/utils.js");
  ctx.window.JONE = ctx.window.JONE || {};
  ctx.window.JONE.ui = { modal: { open() {}, close() {} }, toast() {} };
  loadScript(ctx, "js/update-checker.js");
  const v = await ctx.window.JONE.updateChecker.check(true);
  assert.equal(v, null);
  assert.equal(fetched, 0);
});


test.after(() => { require("./harness").disposeAll(); });
