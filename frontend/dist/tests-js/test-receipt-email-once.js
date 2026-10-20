/* Receipt send modal: one click never becomes two emails.

   A guest received the receipt twice: the send flow used to re-label the
   primary button to "Done" and set `button.onclick = close`, while the
   `addEventListener("click", send)` handler stayed attached — so the single
   click on "Done" closed the dialog AND fired a second real email.

   These tests drive the SHIPPED js/receipt-email-modal.js inside a vm with a
   minimal DOM + modal stub, and count the requests the module actually makes.
   They pin, in order:

     * the send listener is REMOVED once a send settles (not just guarded), and
       no inline `onclick` handler exists that could run alongside a listener;
     * "Done" closes and can never send — repeatedly, and after the dialog is
       closed;
     * a double click / slow response cannot start a second request;
     * a stale dialog (opened before another instance mailed the guest) cannot
       send a second copy by accident;
     * re-opening for a booking already sent in this session is an explicit
       "Send another copy", and
     * a retry after an honest failure keeps the SAME idempotency key, so a
       retry of a send that actually reached the mail server is de-duplicated
       by the backend instead of mailing the guest twice.

   Run from /frontend: node --test tests-js/test-receipt-email-once.js */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const { makeContext, loadScript, FRONTEND, disposeAll } = require("./harness");

const SOURCE = fs.readFileSync(path.join(FRONTEND, "js", "receipt-email-modal.js"), "utf8");

/* ------------------------------- test rig -------------------------------- */

/* A tiny environment: JONE.el (real element stub), a recording modal whose
   close() fires onClose exactly like js/ui.js does, a toast recorder and a
   controllable API.sendReceipt. */
function rig() {
  const ctx = makeContext();
  const state = {
    calls: [],          // { reference, idempotencyKey, force? }
    opened: [],         // modal configurations
    closed: 0,
    resolve: null,      // resolves the in-flight request
    reject: null,
    toasts: [],
  };

  vm.runInContext("window.JONE = window.JONE || {};", ctx);
  const ctxWindow = ctx.window;
  state.window = ctxWindow;
  ctxWindow.JONE.el = function el(tag, attrs) {
    const node = ctx.document.createElement(tag);
    const merge = Object.assign({}, attrs || {});
    Object.keys(merge).forEach(function (key) {
      if (key === "class") { node.className = merge[key]; return; }
      node[key] = merge[key];
    });
    /* Setting innerHTML on a real element discards its text content. */
    Object.defineProperty(node, "innerHTML", {
      configurable: true,
      get() { return this._innerHTML || ""; },
      set(v) { this._innerHTML = v; this.textContent = ""; },
    });
    for (let i = 2; i < arguments.length; i += 1) {
      if (arguments[i]) node.appendChild(arguments[i]);
    }
    return node;
  };
  ctxWindow.JONE.ui = {
    modal: {
      open(config) {
        const record = Object.assign({ closed: 0 }, config);
        state.opened.push(record);
        state.last = record;
      },
      close() {
        const record = state.last;
        state.closed += 1;
        if (!record) return;
        record.closed += 1;
        if (record.onClose) record.onClose();
      },
    },
    toast(message, type, opts) { state.toasts.push({ message, type, opts }); },
  };
  ctxWindow.API = {
    sendReceipt(reference, options) {
      state.calls.push({
        reference,
        idempotencyKey: (options && options.idempotency_key) || "",
      });
      return new Promise(function (resolve, reject) {
        state.resolve = resolve;
        state.reject = reject;
      });
    },
  };

  loadScript(ctx, "js/receipt-email-modal.js");
  return { ctx, state, window: ctxWindow };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

function openedRecord(state, index) {
  const record = state.opened[index];
  assert.ok(record, `modal ${index} was never opened`);
  return record;
}
/* footer is built as (closeBtn, sendBtn). */
const closeButton = (record) => record.footer.children[0];
const sendButton = (record) => record.footer.children[1];
const statusLine = (record) => record.body.children[2];

function openSend(state, reference, email) {
  state.window.JONE.receiptEmailModal.open({
    bookingReference: reference,
    guestName: "Ada Obi",
    guestEmail: email || "ada@example.com",
  });
  return openedRecord(state, state.opened.length - 1);
}

const sent = () => ({ data: { status: "SENT", deduplicated: false } });

/* ------------------------------- the tests -------------------------------- */

test("One click on Send issues exactly one request", async () => {
  const { state } = rig();
  const dialog = openSend(state, "J1-1001");
  sendButton(dialog).click();
  assert.equal(state.calls.length, 1, "the first click must send once");
  state.resolve(sent());
  await settle();
  assert.equal(state.calls.length, 1);
  assert.equal(sendButton(dialog).textContent, "Done");
});

test("The settle path removes the send listener — Done is not a send button", () => {
  /* The exact regression: a handler that survives next to the close handler. */
  assert.doesNotMatch(SOURCE, /\.onclick\s*=/, "no inline onclick handler may coexist with a listener");
  assert.match(SOURCE, /sendBtn\.removeEventListener\("click", onSend\)/);
  assert.match(SOURCE, /async function onSend\(\)/);
});

test("Clicking Done closes the dialog and never sends again", async () => {
  const { state } = rig();
  const dialog = openSend(state, "J1-1002");
  sendButton(dialog).click();
  state.resolve(sent());
  await settle();

  const done = sendButton(dialog);
  assert.equal(done.textContent, "Done");
  done.click();
  assert.equal(dialog.closed, 1, "Done closes the dialog");
  assert.equal(state.calls.length, 1, "Done must not send a second receipt");

  /* Hammering it — and a click after the dialog closed — still sends nothing. */
  done.click();
  done.click();
  assert.equal(state.calls.length, 1);
});

test("A double click while the receipt is in flight sends only once", async () => {
  const { state } = rig();
  const dialog = openSend(state, "J1-1003");
  const button = sendButton(dialog);
  button.click();          // first request, still unresolved
  button.click();          // impatient second click
  button.click();
  assert.equal(state.calls.length, 1, "in-flight requests must be de-duplicated");
  state.resolve(sent());
  await settle();
  assert.equal(state.calls.length, 1);
  assert.equal(sendButton(dialog).textContent, "Done");
});

test("Re-opening for a booking already sent in this session says so and asks for a deliberate copy", async () => {
  const { state } = rig();
  const first = openSend(state, "J1-1004");
  sendButton(first).click();
  state.resolve(sent());
  await settle();
  sendButton(first).click();                       // Done
  assert.equal(state.calls.length, 1);

  const second = openSend(state, "J1-1004");
  assert.equal(sendButton(second).textContent, "Send another copy");
  assert.match(statusLine(second).textContent, /already sent to ada@example\.com/);
  assert.equal(state.calls.length, 1, "opening the dialog never sends by itself");

  /* A deliberate copy is still allowed, and only on a real click. */
  sendButton(second).click();
  assert.equal(state.calls.length, 2);
  assert.notEqual(state.calls[1].idempotencyKey, state.calls[0].idempotencyKey);
  state.resolve(sent());
  await settle();
  assert.equal(sendButton(second).textContent, "Done");
});

test("A stale dialog cannot send once another instance has mailed the guest", async () => {
  const { state } = rig();
  const stale = openSend(state, "J1-1005");        // opened as a first send
  const fresh = openSend(state, "J1-1005");
  assert.equal(sendButton(stale).textContent, "Send receipt");
  sendButton(fresh).click();
  state.resolve(sent());
  await settle();
  assert.equal(state.calls.length, 1);

  /* The old dialog is still on screen with a live-looking "Send receipt". */
  sendButton(stale).click();
  assert.equal(state.calls.length, 1, "a stale dialog must never re-send");
  assert.equal(sendButton(stale).textContent, "Done");
  assert.match(statusLine(stale).textContent, /already sent/);
});

test("A backend IN_PROGRESS answer is treated as settled, not as a reason to send again", async () => {
  const { state } = rig();
  const dialog = openSend(state, "J1-1006");
  sendButton(dialog).click();
  state.resolve({ data: { status: "IN_PROGRESS", deduplicated: true } });
  await settle();
  assert.equal(sendButton(dialog).textContent, "Done");
  assert.match(statusLine(dialog).textContent, /already being processed/);
  sendButton(dialog).click();
  assert.equal(state.calls.length, 1);
  assert.equal(dialog.closed, 1);
});

test("An honest failure stays retryable and the retry keeps the same idempotency key", async () => {
  const { state } = rig();
  const dialog = openSend(state, "J1-1007");
  sendButton(dialog).click();
  state.resolve({ data: { status: "FAILED", error: "The mail server refused the message." } });
  await settle();
  assert.equal(sendButton(dialog).textContent, "Retry");
  assert.match(statusLine(dialog).textContent, /mail server refused/);

  sendButton(dialog).click();
  assert.equal(state.calls.length, 2);
  assert.equal(
    state.calls[1].idempotencyKey, state.calls[0].idempotencyKey,
    "a retry must reuse the key so a lost answer cannot mail the guest twice",
  );
  state.resolve(sent());
  await settle();
  assert.equal(sendButton(dialog).textContent, "Done");
  sendButton(dialog).click();
  assert.equal(state.calls.length, 2);
});

test("A thrown error is retryable too, and never reported as sent", async () => {
  const { state } = rig();
  const dialog = openSend(state, "J1-1008");
  sendButton(dialog).click();
  state.reject(new Error("Network request failed"));
  await settle();
  assert.equal(sendButton(dialog).textContent, "Retry");
  /* A bare transport error is summarised by the shipped wording (only a
     sentence from the server is shown verbatim) — never as a success. */
  assert.match(statusLine(dialog).textContent, /could not be sent/);
  assert.equal(state.calls.length, 1);
  assert.equal(
    state.toasts.filter((t) => t.type === "success").length, 0,
    "a failed send must never claim success",
  );
});

test("Closing the dialog unhooks the send path", () => {
  const { state } = rig();
  const dialog = openSend(state, "J1-1009");
  closeButton(dialog).click();
  assert.equal(dialog.closed, 1);
  sendButton(dialog).click();
  assert.equal(state.calls.length, 0, "a closed dialog must not send");
});

test.after(() => disposeAll());
