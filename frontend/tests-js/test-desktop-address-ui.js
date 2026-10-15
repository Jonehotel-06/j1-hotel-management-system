/* Receptionist Desktop by registered address — the browser side.

   The front desk no longer has to paste a key into the desk browser: an
   administrator can register the address the desk reaches the API from, and
   front desk staff then sign in with their email and password alone. These
   tests drive the shipped code for the two screens involved:

     * dashboard/terminals.html — the administrator registers/clears the
       address, and the page shows what the SERVER sees this browser as; and
     * login.html + js/auth.js — a refusal shows the address back, so the person
       who can fix it knows which address to register.

   Run from /frontend: node --test tests-js/test-desktop-address-ui.js */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const { makeContext, makeElement, loadScript, disposeAll, FRONTEND } = require("./harness");

const read = (rel) => fs.readFileSync(path.join(FRONTEND, rel), "utf8");
const TERMINALS = read("dashboard/terminals.html");
const LOGIN = read("login.html");
const AUTH = read("js/auth.js");
const API = read("js/api.js");
const CONFIG = read("js/config.js");

/* ---------------------------------------------------------------- terminals */

/* Run the shipped desk helpers in a vm with a stub API, so the assertions
   cannot drift from what the page actually does. */
function loadTerminalHelpers({ address = "102.89.34.7", source = "peer", fail = null } = {}) {
  const ctx = makeContext();
  const calls = { update: [], confirm: 0, confirmPayload: null, toasts: [] };
  const notice = makeElement("p");
  const confirmPromise = [];

  ctx.window.JONE = {
    esc: (v) => String(v == null ? "" : v),
    ui: {
      toast: (m, k) => calls.toasts.push({ message: String(m), kind: k }),
      confirm: (opts) => { calls.confirm += 1; calls.confirmPayload = opts; confirmPromise.push(opts); return Promise.resolve(true); },
    },
  };
  ctx.window.API = {
    getClientAddress: async () => {
      if (fail) throw fail;
      return { data: { client_ip: address, source: source } };
    },
    update: async (name, id, payload) => { calls.update.push({ name, id, payload }); return { data: {} }; },
  };
  ctx.window.document.querySelector = (sel) => (sel === "[data-desktop-detected-ip]" ? notice : null);

  const start = TERMINALS.indexOf("function deskState(item)");
  const end = TERMINALS.indexOf('rows.addEventListener("click"');
  assert.ok(start > 0 && end > start, "the desk helpers were not found in the shipped terminals page");
  vm.runInContext(TERMINALS.slice(start, end), ctx, { filename: "terminals.html#desk-helpers" });
  return { ctx: ctx.window, calls, notice };
}

const deskRow = (over = {}) => Object.assign({
  id: 7, reference: "WKS-7", name: "Front desk 1", department: "FRONT_DESK",
  is_active: true, sign_in_key: false, receptionist_key_configured: false,
  ip_address: "", receptionist_desktop_ready: false,
}, over);

test("a front desk row shows how it is enrolled, by key and/or by address", () => {
  const { ctx } = loadTerminalHelpers();
  const byNeither = ctx.deskState(deskRow());
  assert.match(byNeither, /Receptionist Desktop: no key · no registered address/);
  assert.match(byNeither, /Not enrolled/, "an unenrolled desk is flagged, not left looking configured");

  const byKey = ctx.deskState(deskRow({ receptionist_key_configured: true, receptionist_desktop_ready: true }));
  assert.match(byKey, /key issued/);
  assert.ok(!/Not enrolled/.test(byKey));

  const byAddress = ctx.deskState(deskRow({ ip_address: "102.89.34.7", receptionist_desktop_ready: true }));
  assert.match(byAddress, /Receptionist Desktop: no key · sign-in from 102\.89\.34\.7/);
  assert.ok(!/Not enrolled/.test(byAddress), "a desk enrolled by address is ready for sign-in");
});

test("non-front-desk workstations never claim to be a Receptionist Desktop", () => {
  const { ctx } = loadTerminalHelpers();
  assert.equal(ctx.deskState(deskRow({ department: "KITCHEN" })), "");
});

test("registering uses the address the SERVER reported, never a typed guess", async () => {
  const { ctx, calls } = loadTerminalHelpers({ address: "102.89.34.7", source: "forwarded" });
  await ctx.loadDetectedAddress();
  await ctx.registerTerminalAddress(7, "Front desk 1");
  assert.equal(calls.update.length, 1);
  assert.equal(calls.update[0].name, "workstations");
  assert.equal(calls.update[0].id, 7);
  assert.equal(calls.update[0].payload.ip_address, "102.89.34.7");
  assert.equal(calls.confirm, 1, "registering a desk's address is confirmed first");
  assert.match(calls.confirmPayload.message, /every device sharing this address/i,
    "the shared-address risk is spelled out before anything is written");
});

test("the panel reports the address the server sees, and how it was obtained", async () => {
  // On the deployed host the address comes from the edge, not from the
  // connection, and an administrator needs to see which it was.
  const viaEdge = loadTerminalHelpers({ address: "102.89.34.7", source: "true-client-ip" });
  await viaEdge.ctx.loadDetectedAddress();
  assert.match(viaEdge.notice.textContent, /102\.89\.34\.7/);
  assert.match(viaEdge.notice.textContent, /reported by true-client-ip/);

  const direct = loadTerminalHelpers({ address: "192.168.1.57", source: "peer" });
  await direct.ctx.loadDetectedAddress();
  assert.match(direct.notice.textContent, /192\.168\.1\.57/);
  assert.ok(!/reported by/.test(direct.notice.textContent), "a direct connection needs no qualifier");
});

test("without a server-reported address nothing is registered", async () => {
  const { ctx, calls } = loadTerminalHelpers({ fail: new Error("The address is unavailable.") });
  await ctx.loadDetectedAddress();
  await ctx.registerTerminalAddress(7, "Front desk 1");
  assert.equal(calls.update.length, 0, "no address is written when the server did not report one");
  assert.equal(calls.confirm, 0);
  assert.ok(calls.toasts.some((t) => t.kind === "error"), "the refusal is surfaced to the administrator");
});

test("clearing the registered address warns that the desk then needs the key", async () => {
  const { ctx, calls } = loadTerminalHelpers();
  await ctx.clearTerminalAddress(7, "Front desk 1", "102.89.34.7");
  assert.equal(calls.update.length, 1);
  assert.equal(calls.update[0].payload.ip_address, "");
  assert.match(calls.confirmPayload.message, /desktop key/, "the consequence is stated before it is removed");
});

test("the page explains both enrolment routes and asks for the desk, not a phone", () => {
  assert.match(TERMINALS, /data-desktop-detected-ip/);
  assert.match(TERMINALS, /registered address, front desk staff sign in with their email and password alone/);
  assert.match(TERMINALS, /never a phone or laptop/);
  assert.match(TERMINALS, /data-terminal-action="register-ip"/);
  assert.match(TERMINALS, /data-terminal-action="clear-ip"/);
  // Both row actions are reachable from the delegated table handler.
  assert.match(TERMINALS, /action==="register-ip"\)registerTerminalAddress\(/);
  assert.match(TERMINALS, /action==="clear-ip"\)clearTerminalAddress\(/);
  assert.match(TERMINALS, /loadDetectedAddress\(\)/, "the browser's address is read when the page loads");
  // The address travels through the API layer, not a hand-written path.
  assert.ok(!/fetch\(/.test(TERMINALS), "the page must not call the API directly");
  assert.match(TERMINALS, /API\.update\("workstations",id,\{ip_address:/);
});

test("the address endpoint is declared once, in config, and consumed through api.js", () => {
  assert.match(CONFIG, /clientAddress:\s*"\/api\/auth\/client-address\/"/);
  assert.match(API, /function getClientAddress\(opts = \{\}\)/);
  assert.match(API, /getClientAddress,/);
  assert.ok(!/auth\/client-address/.test(TERMINALS), "the page must not hard-code the endpoint path");
});

/* -------------------------------------------------------------------- login */

test("a refused sign-in shows the address back so an administrator can register it", () => {
  const start = LOGIN.indexOf("function desktopRefusalNotice(err)");
  const end = LOGIN.indexOf('window.Auth.bindLoginForm("#login-form"');
  assert.ok(start > 0 && end > start, "login.html is missing the refusal handler");

  const ctx = makeContext({ pathname: "/login.html" });
  const box = makeElement("div");
  ctx.window.document.querySelector = (sel) => (sel === "[data-desktop-notice]" ? box : null);
  vm.runInContext(LOGIN.slice(start, end), ctx, { filename: "login.html#refusal" });
  ctx.window.desktopRefusalNotice({
    code: "STAFF_SIGN_IN_RESTRICTED",
    message: "Staff sign-in is available only from the Receptionist Desktop.",
    data: { code: "STAFF_SIGN_IN_RESTRICTED", errors: { client_ip: "102.89.34.7" } },
  });
  assert.equal(box.style.display, "block");
  assert.match(box.textContent, /102\.89\.34\.7/, "the caller's own address is shown back");
  assert.match(box.textContent, /register that address/, "and what to do about it");
  assert.match(box.textContent, /6-digit code/, "the route that works today is still offered");
  assert.match(LOGIN, /onRestricted: desktopRefusalNotice/, "the page wires the handler into the shared sign-in form");
});

test("the network-pinned refusal says so instead of blaming the address", () => {
  const start = LOGIN.indexOf("function desktopRefusalNotice(err)");
  const end = LOGIN.indexOf('window.Auth.bindLoginForm("#login-form"');
  const ctx = makeContext({ pathname: "/login.html" });
  const box = makeElement("div");
  ctx.window.document.querySelector = (sel) => (sel === "[data-desktop-notice]" ? box : null);
  vm.runInContext(LOGIN.slice(start, end), ctx, { filename: "login.html#refusal" });
  ctx.window.desktopRefusalNotice({
    data: { errors: { client_ip: "203.0.113.9", reason: "front_desk_ip_mismatch" } },
  });
  assert.match(box.textContent, /pinned to a fixed network address/);
  assert.ok(!/register that address/.test(box.textContent), "registering the address is not the fix for a pinned front desk");
});

test("auth.js hands the whole refusal to the page (the address travels with it)", () => {
  assert.match(AUTH, /opts\.onRestricted\(err\)/);
});

test("bindLoginForm calls onRestricted with the error when the desk check refuses", async () => {
  const ctx = makeContext({ pathname: "/login.html" });
  ctx.window.API = {
    setTokenProvider() {}, setRefreshProvider() {},
    login: async () => {
      const err = new Error("Staff sign-in is available only from the Receptionist Desktop.");
      err.status = 403;
      err.code = "STAFF_SIGN_IN_RESTRICTED";
      err.data = { code: "STAFF_SIGN_IN_RESTRICTED", errors: { client_ip: "102.89.34.7" } };
      throw err;
    },
  };
  loadScript(ctx, "js/auth.js");

  const form = makeElement("form");
  const submitBtn = makeElement("button");
  const errBox = makeElement("div");
  const fields = { email: "frontdesk@jone.test", password: "pw" };
  form.querySelector = (sel) => (sel === "[type=submit]" ? submitBtn : (sel === "[data-form-error]" ? errBox : null));
  ctx.window.document.querySelector = (sel) => (sel === "#login-form" ? form : null);
  ctx.window.FormData = class { get(k) { return fields[k] == null ? null : fields[k]; } };
  const seen = [];
  ctx.window.JONE.guardSubmit = () => true;
  ctx.window.JONE.releaseGuard = () => {};
  const toasts = [];
  ctx.window.JONE.ui = { toast: (m, k) => toasts.push({ message: String(m), kind: k }) };
  ctx.window.location.href = "https://www.jonehotel.test/login.html";

  ctx.window.Auth.bindLoginForm("#login-form", { onRestricted: (err) => seen.push(err) });
  await form.listeners.submit[0]({ preventDefault() {} });
  await new Promise((r) => setImmediate(r));

  assert.equal(seen.length, 1, "the page was told the sign-in was refused");
  assert.equal(seen[0].data.errors.client_ip, "102.89.34.7");
  assert.equal(errBox.style.display, "block");
  assert.ok(!/^102\.89\.34\.7$/.test(toasts[0].message), "the toast keeps the sentence, not a bare address");
  disposeAll();
});

test("the refusal sentence wins over the machine-readable address in the toast", async () => {
  // Exactly the deployed response: a message authored for staff plus
  // errors.client_ip. The address must not be promoted to the whole message.
  const envelope = {
    success: false, code: "STAFF_SIGN_IN_RESTRICTED",
    message: "Staff sign-in is available only from the Receptionist Desktop. Ask the front desk to sign you in, or contact an administrator.",
    errors: { client_ip: "102.89.34.7" },
  };
  const ctx = makeContext({ pathname: "/login.html" });
  ctx.window.APP_CONFIG = {
    API_BASE_URL: "https://api.example.test",
    STORAGE: { AUTH: "jone.auth", SESSION: "jone.session", TERMINAL: "jone.terminal" },
    API_ENDPOINTS: {},
  };
  ctx.fetch = () => Promise.resolve({
    status: 403, ok: false,
    headers: { get: () => "application/json" },
    json: () => Promise.resolve(envelope),
    text: () => Promise.resolve(JSON.stringify(envelope)),
  });
  loadScript(ctx, "js/api.js");
  const err = await ctx.window.API.post("/api/auth/login/", {}).catch((e) => e);
  assert.equal(err.code, "STAFF_SIGN_IN_RESTRICTED");
  assert.match(err.message, /only from the Receptionist Desktop/);
  assert.equal(err.data.errors.client_ip, "102.89.34.7");
  disposeAll();
});

test("a field error still wins when the envelope message is the generic one", async () => {
  const envelope = {
    success: false, code: "VALIDATION_ERROR", message: "Validation failed.",
    errors: { check_in: ["Check-in must be in the future."] },
  };
  const ctx = makeContext({ pathname: "/login.html" });
  ctx.window.APP_CONFIG = {
    API_BASE_URL: "https://api.example.test",
    STORAGE: { AUTH: "jone.auth", SESSION: "jone.session", TERMINAL: "jone.terminal" },
    API_ENDPOINTS: {},
  };
  ctx.fetch = () => Promise.resolve({
    status: 400, ok: false,
    headers: { get: () => "application/json" },
    json: () => Promise.resolve(envelope),
    text: () => Promise.resolve(JSON.stringify(envelope)),
  });
  loadScript(ctx, "js/api.js");
  const err = await ctx.window.API.post("/api/admin/bookings/", {}).catch((e) => e);
  assert.match(err.message, /Check-in must be in the future/);
  disposeAll();
});
