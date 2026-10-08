/* tests-js/harness.js */
/* Minimal DOM/browser stubs shared by the Node-based frontend tests.
   Loaded into a `vm` context so the real production scripts run unmodified. */
"use strict";

const vm = require("node:vm");
const fs = require("node:fs");
const path = require("node:path");

const FRONTEND = path.resolve(__dirname, "..");

/* Every timer created inside a test context, so the Node test runner's event
   loop can exit. Call disposeAll() from test.after(). */
const pendingTimers = new Set();

function makeElement(tag) {
  const el = {
    tagName: String(tag || "div").toUpperCase(),
    children: [],
    attributes: {},
    dataset: {},
    style: {},
    textContent: "",
    innerHTML: "",
    value: "",
    defaultValue: "",
    checked: false,
    defaultChecked: false,
    disabled: false,
    type: "text",
    hidden: false,
    listeners: {},
    setAttribute(k, v) { this.attributes[k] = String(v); },
    getAttribute(k) { return this.attributes[k] === undefined ? null : this.attributes[k]; },
    removeAttribute(k) { delete this.attributes[k]; },
    appendChild(c) { this.children.push(c); return c; },
    addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); },
    removeEventListener(t, fn) {
      const l = this.listeners[t] || [];
      const i = l.indexOf(fn); if (i >= 0) l.splice(i, 1);
    },
    click() { (this.listeners.click || []).forEach((f) => f({ preventDefault() {}, target: this })); },
    focus() { this._focused = true; },
    contains() { return false; },
    querySelectorAll() { return []; },
    querySelector() { return null; },
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
  };
  return el;
}

function makeStorage() {
  const store = new Map();
  return {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
    clear: () => store.clear(),
  };
}

function makeContext({ hostname = "www.jonehotel.test", pathname = "/index.html", search = "" } = {}) {
  const created = [];   // every element createElement ever produced (for tests)
  const document = {
    readyState: "complete",
    visibilityState: "visible",
    title: "",
    body: makeElement("body"),
    documentElement: makeElement("html"),
    createElement(tag) {
      const el = makeElement(tag);
      created.push(el);
      return el;
    },
    querySelectorAll() { return []; },
    querySelector() { return null; },
    getElementById() { return null; },
    addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); },
    listeners: {},
    _created: created,
  };

  const context = {
    console,
    Math,
    Date,
    JSON,
    Promise,
    navigator: { onLine: true },
    FormData: class FormData { },
    // Functional AbortController: abort() notifies listeners (api.js relies on
    // this to convert its own request timeout into a fetch abort).
    AbortController: class {
      constructor() {
        const listeners = [];
        this.signal = {
          aborted: false,
          addEventListener(t, fn) { listeners.push(fn); },
          removeEventListener(t, fn) { const i = listeners.indexOf(fn); if (i >= 0) listeners.splice(i, 1); },
        };
        this.abort = () => {
          if (this.signal.aborted) return;
          this.signal.aborted = true;
          listeners.slice().forEach((fn) => fn());
        };
      }
    },
    URLSearchParams: class {
      constructor(init) { this._p = new Map(init ? Object.entries(init) : []); }
      append(k, v) { this._p.set(k, v); }
      set(k, v) { this._p.set(k, v); }
      get(k) { return this._p.get(k); }
      toString() { return [...this._p.entries()].map(([k, v]) => `${k}=${v}`).join("&"); }
    },
    document,
    location: { hostname, pathname, search, href: "https://" + hostname + pathname, protocol: "https:" },
    localStorage: makeStorage(),
    sessionStorage: makeStorage(),
    fetch: () => Promise.reject(new Error("fetch not configured by test")),
    // Real timers (promises actually settle), tracked for disposal.
    setTimeout(fn, ms, ...args) {
      const h = setTimeout(() => {
        pendingTimers.delete(entry);
        fn(...args);
      }, ms);
      const entry = { h, interval: false };
      pendingTimers.add(entry);
      return h;
    },
    clearTimeout(h) {
      clearTimeout(h);
      for (const e of [...pendingTimers]) if (e.h === h) pendingTimers.delete(e);
    },
    setInterval(fn, ms, ...args) {
      const h = setInterval(fn, ms, ...args);
      pendingTimers.add({ h, interval: true });
      return h;
    },
    clearInterval(h) {
      clearInterval(h);
      for (const e of [...pendingTimers]) if (e.h === h) pendingTimers.delete(e);
    },
  };
  context.window = context;
  context.globalThis = context;
  return vm.createContext(context);
}

function loadScript(context, relPath) {
  const code = fs.readFileSync(path.join(FRONTEND, relPath), "utf-8");
  vm.runInContext(code, context, { filename: relPath });
}

function disposeAll() {
  for (const { h, interval } of [...pendingTimers]) {
    if (interval) clearInterval(h); else clearTimeout(h);
  }
  pendingTimers.clear();
}

module.exports = { makeContext, loadScript, makeElement, disposeAll, FRONTEND };
