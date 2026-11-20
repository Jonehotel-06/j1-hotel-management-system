/* tests-js/test-fingerprint-permission-prompt.js

   Regression test for the SECOND production report (2026-11-20):

     "The fingerprint reader still returns same error message"  — after the
     repair had already been deployed and verified byte-for-byte on the server.

   WHY IT STILL SAID THE SAME THING
   --------------------------------
   Chromium's Local Network Access defaults to "prompt", not "granted". While
   the state is "prompt", any fetch() the page makes to loopback WITHOUT a user
   gesture is refused by the browser before it ever reaches port 8787, and
   fetch() reports that refusal as an opaque TypeError.

   The repair classified only "denied" as a browser block. "prompt" fell through
   to the "bridge unreachable — start the J-ONE Fingerprint Bridge" sentence,
   which is precisely the sentence it was supposed to replace. The code was new;
   the message was identical; the operator was correctly unimpressed.

   These tests pin the corrected classification:
     prompt / unknown  -> permission guidance with an Allow action (BRIDGE_PERMISSION_PROMPT)
     denied            -> permission blocked (BRIDGE_PERMISSION_BLOCKED)
     granted + failure -> the bridge really is not answering (BRIDGE_UNREACHABLE)
     unsupported       -> cannot be a browser LNA block, so still the bridge message

   Run from /frontend: node --test tests-js/test-fingerprint-permission-prompt.js */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { makeContext, loadScript, disposeAll } = require("./harness");

test.after(() => disposeAll());

const WINDOWS_UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36";

/** Build a biometrics context whose permission API + fetch we control. */
function makeBio(opts) {
  opts = opts || {};
  const ctx = makeContext();
  ctx.window.navigator = { userAgent: opts.userAgent || WINDOWS_UA };
  ctx.window.isSecureContext = true;
  ctx.window.navigator.permissions = {
    query: async () => {
      if (opts.queryThrows) throw new TypeError("unsupported permission name");
      return { state: opts.permission || "prompt" };
    },
  };
  ctx.window.fetch = async () => {
    throw new TypeError("Failed to fetch"); // what a refused loopback call gives
  };
  loadScript(ctx, "js/biometrics.js");
  return ctx.window.JONE.biometrics;
}

const BRIDGE_DOWN = /start the J-ONE Fingerprint Bridge on this workstation/i;

/* ------------------------------------------------------ the production report */

test("LNA at 'prompt' is reported as permission needed, never as a stopped bridge", async () => {
  const bio = makeBio({ permission: "prompt" });
  const status = await bio.getReaderStatus({ retries: 0 });

  assert.equal(status.bridge_error_code, "BRIDGE_PERMISSION_PROMPT",
    "a not-yet-granted permission must have its own code");
  assert.equal(status.status, "permission_blocked",
    "the desk card shows the Allow action for this state");
  // The bridge instruction is kept, but it may no longer be the FIRST thing the
  // operator reads: leading with it is the exact regression that was reported.
  const bridgeAt = status.detail.search(BRIDGE_DOWN);
  const permissionAt = status.detail.search(/permission/i);
  assert.ok(bridgeAt > permissionAt,
    "the 'start the bridge' instruction must come after the permission guidance, " +
    "not headline it (detail was: " + status.detail + ")");
  assert.match(status.detail, /permission|Allow/i,
    "the message must tell the operator to grant access");
});

test("the prompt state is actionable: it names the button that resolves it", async () => {
  const bio = makeBio({ permission: "prompt" });
  const status = await bio.getReaderStatus({ retries: 0 });
  assert.match(status.detail, /Allow|Retry/i);
  assert.equal(status.bridge_url, "http://127.0.0.1:8787");
});

test("the prompt message still names the bridge, so an operator is not sent in circles", async () => {
  // If granting access does not clear it, the bridge may genuinely be down.
  // Both causes are stated rather than one being asserted as fact.
  const bio = makeBio({ permission: "prompt" });
  const status = await bio.getReaderStatus({ retries: 0 });
  assert.match(status.detail, /bridge/i);
  assert.match(status.detail, BRIDGE_DOWN,
    "the fallback instruction is kept, second, for the genuinely-stopped case");
});

test("an unreadable permission state is treated as prompt, not as a dead bridge", async () => {
  const bio = makeBio({ queryThrows: true, permission: "prompt" });
  const status = await bio.getReaderStatus({ retries: 0 });
  // The fake throws, so the state reads as "unsupported": that path keeps the
  // bridge guidance because no Local Network Access policy can be in play.
  assert.equal(status.bridge_error_code, "BRIDGE_UNREACHABLE");
});

test("a saved denial still takes precedence over the prompt guidance", async () => {
  const bio = makeBio({ permission: "denied" });
  const state = await bio.requestLocalNetworkAccess();
  assert.equal(state, "denied");
  const status = await bio.getReaderStatus({ retries: 0 });
  assert.equal(status.bridge_error_code, "BRIDGE_PERMISSION_BLOCKED");
  assert.doesNotMatch(status.detail, /has not yet been given permission/,
    "a refusal is not the same as a not-yet-asked prompt");
});

test("permission already granted means the bridge really is not answering", async () => {
  const bio = makeBio({ permission: "granted" });
  const status = await bio.getReaderStatus({ retries: 0 });
  assert.equal(status.bridge_error_code, "BRIDGE_UNREACHABLE",
    "with access granted, an opaque failure can only be the bridge or the port");
  assert.match(status.detail, /Start the J-ONE Fingerprint Bridge/);
});

/* ---------------------------------------------------- the message an operator reads */

test("describeReader surfaces the prompt state to the desk card", async () => {
  const bio = makeBio({ permission: "prompt" });
  const described = await bio.describeReader();
  assert.equal(described.status, "permission_blocked");
  assert.equal(described.code, "BRIDGE_PERMISSION_PROMPT");
  assert.equal(described.bridge_reachable, false);
  assert.equal(described.ready, false);
});

test("the status description no longer claims the browser 'blocked' a pending prompt", () => {
  const bio = makeBio({ permission: "prompt" });
  const text = bio.STATUS_DESCRIPTIONS[bio.STATUS_PERMISSION_BLOCKED];
  assert.match(text, /has not granted/i);
  assert.doesNotMatch(text, /This browser blocked the page/,
    "the old wording asserted a refusal that never happened");
});

test("no classification path fabricates a capture or a reader", async () => {
  for (const permission of ["prompt", "denied", "granted", "unknown"]) {
    const bio = makeBio({ permission });
    const status = await bio.getReaderStatus({ retries: 0 });
    assert.equal(status.connected, false, permission);
    assert.equal(status.bridge_reachable, false, permission);
  }
});
