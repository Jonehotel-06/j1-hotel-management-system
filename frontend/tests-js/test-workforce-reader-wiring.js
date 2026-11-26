/* tests-js/test-workforce-reader-wiring.js

   PROOF THAT THE LIVE PAGES USE THE NEW FINGERPRINT PANEL — AND ONLY IT.

   The first repair shipped js/reader-agent.js and added it to workforce.html, but
   left the old bridge panel and its six probe-driven buttons active on the same
   page. Production therefore showed "Biometric service unavailable — the
   fingerprint bridge is not reachable at http://127.0.0.1:8787" above the new
   panel, and the numeric console posted a synthetic "probe" to
   reception-terminal/ so that endpoint would accept it.

   These tests read the shipped HTML/JS the way a browser would: which modules the
   page loads, in what order, which panel the buttons belong to, and whether a
   loopback address or a fabricated probe survives anywhere in the active path.
   They are intentionally static — no jsdom, no network — so they fail loudly in a
   release build that reintroduces the old path.

   Run: node --test tests-js/test-workforce-reader-wiring.js
*/
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const FRONTEND = path.resolve(__dirname, "..");
const DASH = path.join(FRONTEND, "dashboard");

function read(...parts) {
  return fs.readFileSync(path.join(FRONTEND, ...parts), "utf8");
}

const PAGES = [
  { name: "workforce.html", file: path.join(DASH, "workforce.html"), actions: 6 },
  { name: "terminals.html", file: path.join(DASH, "terminals.html"), actions: 6 },
  { name: "staff-profile.html", file: path.join(DASH, "staff-profile.html"), actions: 2 },
];

/* The old bridge modules must not be loaded by any page that records attendance. */
test("no active page loads the loopback bridge modules", () => {
  for (const page of PAGES) {
    const html = fs.readFileSync(page.file, "utf8");
    const tags = html.match(/<script[^>]+src="[^"]+"/g) || [];
    const sources = tags.join("\n");
    assert.ok(!/js\/biometrics\.js/.test(sources), `${page.name} still loads js/biometrics.js`);
    assert.ok(!/js\/reader-check\.js/.test(sources), `${page.name} still loads js/reader-check.js`);
  }
});

test("no active page contains bridge instructions or a loopback address", () => {
  for (const page of PAGES) {
    const html = fs.readFileSync(page.file, "utf8");
    assert.ok(!/127\.0\.0\.1/.test(html), `${page.name} mentions 127.0.0.1`);
    assert.ok(!/:\s*8787/.test(html), `${page.name} mentions the bridge port`);
    assert.ok(!/start the (j-one )?fingerprint bridge/i.test(html),
      `${page.name} still tells staff to start a bridge`);
    assert.ok(!/local network access/i.test(html),
      `${page.name} still talks about browser local-network permission`);
  }
});

test("no active page fabricates a biometric probe or posts one to the terminal", () => {
  for (const page of PAGES) {
    const html = fs.readFileSync(page.file, "utf8");
    assert.ok(!/captureMinutiaeProbe/.test(html), `${page.name} still calls captureMinutiaeProbe`);
    assert.ok(!/executeReceptionTerminalAction/.test(html),
      `${page.name} still posts a browser-made probe through executeReceptionTerminalAction`);
    assert.ok(!/JONE\.biometrics/.test(html), `${page.name} still depends on JONE.biometrics`);
    assert.ok(!/fingerprint_probe|"minutiae"/.test(html), `${page.name} still sends a probe payload`);
  }
});

test("each active page has exactly ONE fingerprint panel, and it is the agent's", () => {
  for (const page of PAGES) {
    const html = fs.readFileSync(page.file, "utf8");
    /* A page either declares the auto-mounted host attribute or mounts one
       container explicitly with JONE.readerAgent.mount(...). Never both, and
       never twice: two panels mean two sets of buttons and two sessions. */
    const hosts = (html.match(/data-jone-reader-agent-host/g) || []).length;
    const explicitMounts = (html.match(/JONE\.readerAgent\.mount\(/g) || []).length;
    assert.equal(hosts + explicitMounts, 1,
      `${page.name} declares ${hosts} host attribute(s) and ${explicitMounts} explicit mount(s)`);
    const attribute = html.match(/data-reader-actions="([^"]+)"/);
    const explicit = html.match(/actions:\s*\[([^\]]+)\]/);
    const raw = attribute ? attribute[1] : (explicit ? explicit[1] : "");
    assert.ok(raw, `${page.name} does not declare its reader actions`);
    const actions = raw.split(",").map((value) => value.trim().replace(/"/g, ""));
    assert.equal(actions.length, page.actions, `${page.name} action list: ${actions.join(",")}`);
    for (const action of actions) {
      assert.ok(
        ["CLOCK_IN", "CLOCK_OUT", "BREAK_START", "BREAK_END", "ISSUE_CODE", "VERIFY"].includes(action),
        `${page.name} declares an unknown action ${action}`
      );
    }
  }
});

test("the workforce console keeps no second set of fingerprint buttons", () => {
  const html = fs.readFileSync(path.join(DASH, "workforce.html"), "utf8");
  assert.ok(!/data-wf-rt-action=/.test(html), "the duplicate fingerprint buttons are still in the console");
  assert.ok(!/data-wf-rt-reader-badge/.test(html), "the old bridge status card is still in the page");
  /* The four clock actions and the code/verify actions each appear once, on the panel. */
  const declared = html.match(/data-reader-actions="([^"]+)"/)[1].split(",");
  for (const action of ["CLOCK_IN", "CLOCK_OUT", "BREAK_START", "BREAK_END", "ISSUE_CODE", "VERIFY"]) {
    assert.ok(declared.includes(action), `the panel is missing ${action}`);
  }
});

test("the reader module is loaded BEFORE the inline code that uses it", () => {
  for (const page of PAGES) {
    const html = fs.readFileSync(page.file, "utf8");
    const moduleAt = html.indexOf("js/reader-agent.js");
    assert.ok(moduleAt > -1, `${page.name} does not load js/reader-agent.js`);
    const usesAt = html.search(/JONE\.readerAgent(\.terminal)?\.(mount|startAction|terminal)/);
    if (usesAt > -1) {
      assert.ok(moduleAt < usesAt,
        `${page.name} uses JONE.readerAgent before loading js/reader-agent.js`);
    }
  }
});

test("the workforce page still uses the panel helpers it needs (no dead references)", () => {
  const html = fs.readFileSync(path.join(DASH, "workforce.html"), "utf8");
  assert.ok(/readerAgent\.terminal/.test(html),
    "the desk-state line must get its helpers from the reader module");
  assert.ok(/terminalApi\.status\(\)/.test(html),
    "the desk-state line must read the terminal status from the reader module");
  assert.ok(/terminalApi\.fallback\(/.test(html),
    "the supervised fallback must post through the reader module's probe-free helper");
  assert.ok(/terminalApi\.attendanceNote\(/.test(html),
    "the fallback result must use the shared attendance note");
});

test("no page binds its own click handler to a reader action button", () => {
  /* The panel creates and binds its own buttons. A page-level
     querySelectorAll("[data-reader-action]") listener would double-fire every
     scan (two sessions, two polls, two audit rows). */
  for (const page of PAGES) {
    const html = fs.readFileSync(page.file, "utf8");
    assert.ok(!/querySelectorAll\(["'][^"']*data-reader-action/.test(html),
      `${page.name} binds a second listener to the reader buttons`);
  }
});

test("the module itself keeps a single mount per host", () => {
  const source = read("js", "reader-agent.js");
  assert.ok(/data-jone-reader-agent", "mounted"/.test(source),
    "mount() must mark the host so a second bootstrap cannot mount twice");
  assert.ok(/getAttribute\("data-jone-reader-agent"\) === "mounted"/.test(source),
    "autoMount() must skip hosts that are already mounted");
});

test("the service worker precaches the panel and no longer ships the bridge modules", () => {
  const sw = read("sw.js");
  assert.ok(/\/js\/reader-agent\.js/.test(sw), "js/reader-agent.js must be precached");
  assert.ok(!/"\/js\/biometrics\.js"/.test(sw), "js/biometrics.js must not be precached any more");
  assert.ok(!/"\/js\/reader-check\.js"/.test(sw), "js/reader-check.js must not be precached any more");
});

test("every page stamp equals the release version (no page asks for an older panel)", () => {
  const version = JSON.parse(read("version.json")).version;
  for (const page of PAGES) {
    const html = fs.readFileSync(page.file, "utf8");
    const stamps = new Set((html.match(/\?v=([0-9.]+)/g) || []).map((value) => value.slice(3)));
    assert.deepEqual([...stamps], [version],
      `${page.name} is stamped ${[...stamps].join("/")} instead of ${version}`);
  }
  const sw = read("sw.js");
  assert.ok(sw.includes(`jone-v${version}`), "the worker cache namespace must match the release");
});
