/* tests-js/test-login-error-states.js
   The staff sign-in page: one failed attempt must not break the next one, and
   each failure must be reported as what it actually was.

   Covers the reported sequence — a receptionist is refused, an administrator
   then signs in from the same page — and Test I/J of the fix brief: HTTP 401,
   403, 429, 500, a timeout, a genuine network failure and an unreadable
   response are labelled differently, the submit button is never left locked,
   and no session, token or Authorization header survives a failure.

   Run: node --test tests-js/test-login-error-states.js   (from frontend/) */
"use strict";

const test = require("node:test");
const assert = require("node:assert");
const { makeContext, loadScript, makeElement, disposeAll } = require("./harness");

const LOGIN_URL = "https://api.example.test/api/auth/login/";
const RESTRICTED_MESSAGE =
  "Staff sign-in is available only from the Receptionist Desktop. Ask the front desk to sign you in, or contact an administrator.";

/* A sign-in page fixture: the two boxes login.html actually has, plus the
   submit button, driven through the real handlers. */
function makeLoginForm(ctx) {
  const submit = makeElement("button");
  submit.type = "submit";
  const errorBox = makeElement("div");
  errorBox.style.display = "none";
  const noticeBox = makeElement("div");
  noticeBox.style.display = "none";
  const listeners = {};

  const form = {
    listeners,
    submitted: 0,
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    querySelector(sel) {
      if (sel === "[type=submit]") return submit;
      if (sel === "[data-form-error]") return errorBox;
      if (sel === "[data-desktop-notice]") return noticeBox;
      return null;
    },
    async submit() {
      form.submitted += 1;
      for (const fn of listeners.submit || []) {
        await fn({ preventDefault() {}, target: form });
      }
    },
  };
  return { form, submit, errorBox, noticeBox };
}

function jsonResponse(status, body, headers = { "content-type": "application/json" }) {
  return Promise.resolve({
    status,
    ok: status >= 200 && status < 300,
    headers: { get: (k) => headers[String(k).toLowerCase()] || null },
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  });
}

function loginOk(email) {
  return jsonResponse(200, {
    success: true,
    data: {
      user: { id: 1, email, role: "ADMIN", first_name: "Ada", permissions: [] },
      tokens: { access: "access-token", refresh: "refresh-token" },
    },
  });
}

/* Boot api.js + auth.js the way login.html does, with a scripted fetch queue. */
function bootLoginPage({ responses = [], credentials = {} } = {}) {
  const ctx = makeContext({ hostname: "www.jonehotel.test", pathname: "/login.html" });
  ctx.window.APP_CONFIG = {
    API_BASE_URL: "https://api.example.test",
    STORAGE: { AUTH: "jone.auth", SESSION: "jone.session", TERMINAL: "jone.terminal.reference" },
    API_ENDPOINTS: {},
  };
  const calls = [];
  let index = 0;
  ctx.fetch = (url, init) => {
    calls.push({ url: String(url), init: init || {} });
    const next = responses[Math.min(index, responses.length - 1)];
    index += 1;
    return typeof next === "function" ? next() : next;
  };
  loadScript(ctx, "js/api.js");
  // utils.js supplies guardSubmit/releaseGuard; load it if it can boot here.
  try { loadScript(ctx, "js/utils.js"); } catch (_) { /* fall back to the stubs below */ }
  if (!ctx.window.JONE || typeof ctx.window.JONE.guardSubmit !== "function") {
    ctx.window.JONE = ctx.window.JONE || {};
    ctx.window.JONE.guardSubmit = (btn) => {
      if (!btn || btn.dataset.busy === "true") return false;
      btn.dataset.busy = "true";
      btn.setAttribute("aria-busy", "true");
      return true;
    };
    ctx.window.JONE.releaseGuard = (btn) => {
      if (!btn) return;
      btn.dataset.busy = "false";
      btn.removeAttribute("aria-busy");
    };
  }
  loadScript(ctx, "js/auth.js");
  const toasts = [];
  ctx.window.JONE = ctx.window.JONE || {};
  ctx.window.JONE.ui = { toast: (msg, kind) => toasts.push({ msg, kind }) };

  // FormData replacement: the fixture form carries the credentials.
  ctx.FormData = function FormData() {
    this.get = (key) => (Object.prototype.hasOwnProperty.call(credentials, key) ? credentials[key] : null);
  };

  const page = makeLoginForm(ctx);
  ctx.document.querySelector = (sel) => (sel === "#login-form" ? page.form : null);
  return { ctx, page, calls, toasts, FormData: ctx.FormData };
}

function queueFor(entries) {
  // A fresh queue per test, consumed in order.
  return { responses: entries, index: { value: 0 } };
}

function scriptedQueue(entries) {
  const state = { index: 0, entries };
  return () => {
    const entry = state.entries[Math.min(state.index, state.entries.length - 1)];
    state.index += 1;
    return typeof entry === "function" ? entry() : entry;
  };
}

/* --------------------------------------------------------------------------
   Test E / Test J — the reported sequence, in one page, without a reload
   -------------------------------------------------------------------------- */
test("a refused receptionist sign-in does not stop the administrator signing in next", async () => {
  const next = scriptedQueue([
    () => jsonResponse(403, {
      success: false,
      code: "STAFF_SIGN_IN_RESTRICTED",
      message: RESTRICTED_MESSAGE,
      errors: { client_ip: "105.112.216.76" },
    }),
    () => loginOk("admin@jone.test"),
  ]);
  const { ctx, page, calls, toasts } = bootLoginPage({
    responses: [() => next(), () => next()],
    credentials: { email: "reception@jone.test", password: "Str0ng!Pass" },
  });
  let restrictedNotice = "";
  ctx.window.Auth.bindLoginForm("#login-form", {
    onRestricted: (err) => {
      restrictedNotice = String((err.data && err.data.errors && err.data.errors.client_ip) || "");
      page.noticeBox.textContent = "This browser reached the server from " + restrictedNotice + ".";
      page.noticeBox.style.display = "block";
    },
  });

  // 1. The receptionist is refused.
  await page.form.submit();
  assert.equal(page.errorBox.textContent, RESTRICTED_MESSAGE, "the refusal must be shown as itself");
  assert.equal(restrictedNotice, "105.112.216.76", "the address the server saw is surfaced for the administrator");
  assert.equal(page.submit.dataset.busy, "false", "the submit button must be released after a failure");
  assert.equal(ctx.sessionStorage.getItem("jone.session"), null, "a refused sign-in must store no tokens");

  // 2. The administrator signs in on the same page, with no reload.
  ctx.FormData = class { constructor() {} get(k) { return k === "email" ? "admin@jone.test" : (k === "password" ? "Str0ng!Pass" : null); } };
  await page.form.submit();

  const stored = JSON.parse(ctx.sessionStorage.getItem("jone.session") || "null");
  assert.ok(stored && stored.access === "access-token", "the administrator must be signed in");
  assert.equal(page.errorBox.style.display, "none", "the previous failure must be cleared before the retry");
  assert.equal(page.submit.dataset.busy, "false");
  assert.equal(calls.length, 2, "exactly one request per attempt");
  for (const call of calls) {
    assert.equal(call.url, LOGIN_URL);
    assert.equal(call.init.headers.Authorization, undefined, "a sign-in request must never carry a stale token");
  }
  assert.ok(toasts.some((t) => t.kind === "success"), "the successful sign-in is confirmed to the user");
  disposeAll();
});

test("a repeated failure is reported again, and the form stays usable", async () => {
  const { ctx, page, toasts } = bootLoginPage({
    responses: [() => jsonResponse(403, {
      success: false, code: "STAFF_SIGN_IN_RESTRICTED", message: RESTRICTED_MESSAGE,
      errors: { client_ip: "105.112.216.76" },
    })],
    credentials: { email: "reception@jone.test", password: "Str0ng!Pass" },
  });
  ctx.window.Auth.bindLoginForm("#login-form", {});
  await page.form.submit();
  await page.form.submit();
  await page.form.submit();
  assert.equal(page.form.submitted, 3);
  assert.equal(page.submit.dataset.busy, "false");
  assert.equal(toasts.filter((t) => t.kind === "error").length, 3);
  assert.equal(ctx.sessionStorage.getItem("jone.session"), null);
  disposeAll();
});

test("a throwing page callback cannot leave the form locked", async () => {
  const { ctx, page } = bootLoginPage({
    responses: [() => jsonResponse(403, {
      success: false, code: "STAFF_SIGN_IN_RESTRICTED", message: RESTRICTED_MESSAGE, errors: {},
    })],
    credentials: { email: "reception@jone.test", password: "Str0ng!Pass" },
  });
  ctx.window.Auth.bindLoginForm("#login-form", {
    onRestricted: () => { throw new Error("page callback exploded"); },
  });
  await page.form.submit();
  assert.equal(page.submit.dataset.busy, "false", "the guard is released even when a callback throws");
  assert.equal(page.errorBox.textContent, RESTRICTED_MESSAGE, "the real error is still reported");
  disposeAll();
});

/* --------------------------------------------------------------------------
   Test I — every condition gets its own, accurate message
   -------------------------------------------------------------------------- */
async function messageFor(responseFactory) {
  const { ctx, page } = bootLoginPage({
    responses: [responseFactory],
    credentials: { email: "staff@jone.test", password: "Str0ng!Pass" },
  });
  ctx.window.Auth.bindLoginForm("#login-form", {});
  await page.form.submit();
  disposeAll();
  return page.errorBox.textContent;
}

test("invalid credentials are reported as invalid credentials, never as a network problem", async () => {
  const msg = await messageFor(() => jsonResponse(401, {
    success: false, code: "UNAUTHORIZED", message: "Invalid email or password.",
  }));
  assert.equal(msg, "Incorrect email or password.");
  assert.ok(!/reach our servers|connection/i.test(msg));
});

test("a workstation refusal is reported as a workstation refusal", async () => {
  const msg = await messageFor(() => jsonResponse(403, {
    success: false, code: "STAFF_SIGN_IN_RESTRICTED", message: RESTRICTED_MESSAGE,
    errors: { client_ip: "105.112.216.76" },
  }));
  assert.equal(msg, RESTRICTED_MESSAGE);
});

test("another permission refusal keeps the server's own explanation", async () => {
  const msg = await messageFor(() => jsonResponse(403, {
    success: false, code: "FORBIDDEN", message: "Your account is not authorized to sign in to this department dashboard.",
  }));
  assert.match(msg, /not authorized to sign in to this department dashboard/i);
});

test("an inactive account keeps the server's explanation when it gives one", async () => {
  const msg = await messageFor(() => jsonResponse(403, {
    success: false, code: "ACCOUNT_INACTIVE", message: "This account is inactive. Contact an administrator.",
  }));
  assert.match(msg, /inactive/i);
});

test("a rate limit says so, instead of blaming the network", async () => {
  const msg = await messageFor(() => jsonResponse(429, {
    success: false, code: "RATE_LIMITED", message: "Too many requests. Try again in 42 seconds.",
  }));
  assert.match(msg, /try again in 42 seconds/i);
  assert.ok(!/reach our servers/i.test(msg));
});

test("a server error is a server error, not an authorization or network failure", async () => {
  const msg = await messageFor(() => jsonResponse(500, {
    success: false, code: "SERVER_ERROR", message: "An unexpected error occurred. Please try again later.",
  }));
  assert.match(msg, /unexpected error|server/i);
  assert.ok(!/reach our servers/i.test(msg));
});

test("a timeout is reported as a timeout", async () => {
  const msg = await messageFor(() => new Promise((_resolve, reject) => {
    const err = new Error("aborted");
    err.name = "AbortError";
    reject(err);
  }));
  // The api layer's own timeout abort is classified as kind=timeout.
  assert.match(msg, /taking longer than expected|may still complete/i);
});

test("a genuine connection failure is the only case that says we couldn't reach the server", async () => {
  const msg = await messageFor(() => Promise.reject(new TypeError("Failed to fetch")));
  assert.match(msg, /couldn't reach our servers/i);
});

test("an unreadable response is reported as such, not as a network failure", async () => {
  const msg = await messageFor(() => Promise.resolve({
    status: 200,
    ok: true,
    headers: { get: () => "application/json" },
    json: () => Promise.resolve(null),
    text: () => Promise.resolve(""),
  }));
  assert.match(msg, /couldn't read|could not complete/i);
  assert.ok(!/reach our servers/i.test(msg));
});
