/* tests-js/test-desk-key.js
   Receptionist Desktop enrolment in the browser: the key is stored only in this
   browser, sent as X-JONE-Desktop-Key on login and refresh, and the login page
   exposes the setup panel. The server enforces the policy; these tests cover the
   client side only. */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { makeContext, loadScript, disposeAll, FRONTEND } = require("./harness");

const DESK_KEY_STORAGE = "jone.receptionistDesktopKey";

function loadAuth() {
  const ctx = makeContext({ pathname: "/login.html" });
  // auth.js attaches to the API client at load time; stub the surface it touches.
  ctx.window.API = {
    setTokenProvider() {}, setRefreshHandler() {}, setUnauthorizedHandler() {},
    login: async () => ({ data: {} }),
  };
  try {
    loadScript(ctx, "js/auth.js");
  } catch (err) {
    assert.fail("auth.js failed to load in the test harness: " + err.message);
  }
  return ctx;
}

test("desk key is saved, read back and removed, and stored only under the expected key", () => {
  const ctx = loadAuth();
  const Auth = ctx.window.Auth;
  assert.equal(Auth.receptionistDesktopKey(), "");
  assert.equal(Auth.setReceptionistDesktopKey("  desk-abc-123  "), true);
  assert.equal(Auth.receptionistDesktopKey(), "desk-abc-123");
  assert.equal(ctx.localStorage.getItem(DESK_KEY_STORAGE), "desk-abc-123");
  assert.equal(Auth.setReceptionistDesktopKey(""), true);
  assert.equal(Auth.receptionistDesktopKey(), "");
  assert.equal(ctx.localStorage.getItem(DESK_KEY_STORAGE), null);
  disposeAll();
});

test("login sends the desk key header only when a key is saved", async () => {
  const ctx = loadAuth();
  const sent = [];
  ctx.window.API = {
    login: async (_body, opts) => { sent.push(opts); return { data: { access: "a", refresh: "r", user: { role: "manager" } } }; },
  };
  try {
    await ctx.window.Auth.login("manager@example.test", "pw").catch(() => {});
    assert.equal(JSON.stringify(sent[0] || {}), "{}", "no header without a saved key");
    ctx.window.Auth.setReceptionistDesktopKey("desk-xyz");
    await ctx.window.Auth.login("manager@example.test", "pw").catch(() => {});
    assert.equal(JSON.stringify(sent[1]), JSON.stringify({ headers: { "X-JONE-Desktop-Key": "desk-xyz" } }));
  } finally {
    disposeAll();
  }
});

test("login page contains the desk setup panel wired to auth.js", () => {
  const html = fs.readFileSync(path.join(FRONTEND, "login.html"), "utf8");
  for (const id of ["desk-setup", "desk-key", "desk-key-save", "desk-key-clear", "desk-key-status"]) {
    assert.ok(html.includes(`id="${id}"`), `login.html is missing #${id}`);
  }
  assert.ok(html.indexOf("js/auth.js") !== -1, "login.html must load js/auth.js");
  assert.ok(!/desk-key[^>]*value=/.test(html), "the desk key input must never be pre-filled");
});
