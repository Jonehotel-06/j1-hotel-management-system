/* tests-js/test-reader-agent.js

   The dashboard panel for the J-ONE Reader Agent (js/reader-agent.js).

   These tests pin the promises the panel makes to a receptionist:

     · it never contacts a loopback/localhost address (that is what made the old
       bridge unusable on a hosted page and needed a process started by hand),
     · it starts a fingerprint action by asking the API for a single-use session
       and handing only that token to Windows,
     · it shows a result only when the API says the session is DONE,
     · a lost connection never looks like a verification,
     · a second click cannot start a second read.

   Run: node --test tests-js/test-reader-agent.js
*/
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { makeContext, loadScript, makeElement, disposeAll, FRONTEND } = require("./harness");

const SOURCE = path.join(FRONTEND, "js", "reader-agent.js");
const flush = () => new Promise((resolve) => setImmediate(resolve));

function boot(scenario) {
  const context = makeContext({ hostname: "jonehotels.com", pathname: "/dashboard/workforce.html" });
  let intervalFn = null;
  context.setInterval = (fn) => { intervalFn = fn; return 99; };
  context.clearInterval = () => { intervalFn = null; };
  context.JONE = { ui: { toast() {} } };
  /* the shared harness only exposes document.addEventListener */
  if (typeof context.addEventListener !== "function") context.addEventListener = () => {};
  const calls = [];
  context.API = {
    getWorkstationReference: () => "WS-FD-1",
    get: async (route) => {
      calls.push({ method: "GET", route });
      return scenario.get ? scenario.get(route, calls) : { data: [] };
    },
    post: async (route, body) => {
      calls.push({ method: "POST", route, body });
      return scenario.post ? scenario.post(route, body, calls) : { data: {} };
    },
  };
  loadScript(context, "js/reader-agent.js");
  const host = makeElement("div");
  /* The shared harness predates replaceChildren(); the real DOM has it. */
  host.replaceChildren = function replaceChildren() { this.children.length = 0; };
  const successes = [];
  const panel = context.JONE.readerAgent.mount(host, {
    actions: ["CLOCK_IN"],
    onSuccess: (result) => successes.push(result),
  });
  return {
    context, host, panel, calls, successes,
    tick: async () => { if (intervalFn) { await intervalFn(); await flush(); } },
    hasTimer: () => !!intervalFn,
    text: () => host.children.map((child) => child.textContent).join(" "),
    state: () => host.getAttribute("data-state"),
    frames: () => context.document._created.filter((node) => node.tagName === "IFRAME"),
    /* Repair (2026-11-26): the hand-off is now a gesture-anchored anchor click
       (a hidden-iframe navigation to an external protocol is silently dropped
       by Chromium, which is why the agent never started in production). The
       contract under test is unchanged: exactly one jone-reader: hand-off, and
       it carries nothing but the one-time session token. */
    handoffs: () => context.document._created.filter(
      (node) => /^jone-reader:/.test(node.href || node.src || "")
    ),
    actionButton: (action) => host.children
      .flatMap((child) => child.children || [])
      .find((child) => child.getAttribute && child.getAttribute("data-reader-action") === action),
  };
}

const READY_AGENT = { data: [{ reference: "RAG-1", online: true, driver_mode: "LEGACY_SDK", device_status: "ready" }] };
const SESSION = {
  data: {
    session_token: "T1", agent_reference: "RAG-1", agent_online: true,
    launch_uri: "jone-reader:capture?session=T1&api=https://api.test&action=CLOCK_IN",
  },
};

test.after(disposeAll);

test("no loopback address or bridge port survives in the executable code", () => {
  const source = fs.readFileSync(SOURCE, "utf-8");
  /* The header comment explains what was removed; only real code matters. */
  const code = source
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/^\s*\/\/.*$/gm, "");
  assert.ok(!/127\.0\.0\.1/.test(code), "no loopback address in code");
  assert.ok(!/localhost/i.test(code), "no localhost in code");
  assert.ok(!/8787/.test(code), "no bridge port in code");
  assert.ok(!/fingerprint_bridge/i.test(code), "no bridge dependency in code");
  assert.ok(!/jonebio:|wss?:\/\//.test(code), "no socket transport in code");
});

test("an unconfigured computer is told to run the one-click setup, not to start scripts", async () => {
  const app = boot({ get: () => ({ data: [] }) });
  await flush();
  assert.equal(app.state(), "not_provisioned");
  assert.match(app.text(), /Set up fingerprint reader on this computer/);
});

test("a provisioned but stopped agent offers a Windows start, not a command line", async () => {
  const app = boot({
    get: () => ({ data: [{ reference: "RAG-1", online: false, driver_mode: "LEGACY_SDK", device_status: "ready" }] }),
  });
  await flush();
  assert.equal(app.state(), "agent_missing");
  assert.match(app.text(), /Start reader agent/);
});

test("a WBF (Windows Hello) driver is reported as a driver-mode problem", async () => {
  const app = boot({
    get: () => ({ data: [{ reference: "RAG-1", online: true, driver_mode: "WBF", device_status: "not_connected" }] }),
  });
  await flush();
  assert.equal(app.state(), "driver_mode_mismatch");
  assert.match(app.text(), /Windows Hello \(WBF\) driver/);
});

test("a ready reader says so, and nothing more", async () => {
  const app = boot({
    get: () => ({ data: [{ reference: "RAG-1", online: true, driver_mode: "LEGACY_SDK",
                            device_status: "ready", device_serial: "SER-9" }] }),
  });
  await flush();
  assert.equal(app.state(), "ready");
  assert.match(app.text(), /SER-9/);
});

test("clocking in creates a session and hands only the token to Windows", async () => {
  const app = boot({ get: () => READY_AGENT, post: () => SESSION });
  await flush();
  await app.actionButton("CLOCK_IN").click();
  await flush();

  const post = app.calls.find((call) => call.method === "POST");
  assert.equal(post.route, "/api/admin/staff-operations/reader-agent/sessions/");
  assert.equal(post.body.action, "CLOCK_IN");
  assert.ok(!("staff_id" in post.body), "identification comes from the finger, not the page");
  const handoff = app.handoffs().pop();
  assert.ok(handoff, "a jone-reader: hand-off should be launched");
  assert.match(handoff.href, /^jone-reader:capture\?session=T1/);
  assert.equal(app.state(), "waiting_finger");
  assert.ok(app.hasTimer(), "the面板 must poll for the signed result");
});

test("a second click cannot start a second read", async () => {
  const app = boot({ get: () => READY_AGENT, post: () => SESSION });
  await flush();
  const button = app.actionButton("CLOCK_IN");
  await Promise.all([button.click(), button.click(), button.click()]);
  await flush();
  assert.equal(app.calls.filter((call) => call.method === "POST").length, 1);
});

test("success is shown only after the API says the session is DONE", async () => {
  let polls = 0;
  const app = boot({
    get: (route) => {
      if (route.includes("/sessions/")) {
        polls += 1;
        if (polls < 2) return { data: { status: "PENDING" } };
        return { data: { status: "DONE", result: {
          staff_id: 7, staff_name: "Ada Obi", attendance_reference: "ATT-9001",
        } } };
      }
      return READY_AGENT;
    },
    post: () => SESSION,
  });
  await flush();
  await app.actionButton("CLOCK_IN").click();
  await flush();
  await app.tick();
  assert.equal(app.state(), "waiting_finger");
  assert.equal(app.successes.length, 0, "a pending session is not a verification");
  await app.tick();
  assert.equal(app.state(), "success");
  assert.match(app.text(), /Ada Obi/);
  assert.match(app.text(), /ATT-9001/);
  assert.equal(app.successes.length, 1);
});

test("a refused fingerprint shows the reason and records nothing", async () => {
  const app = boot({
    get: (route) => {
      if (route.includes("/sessions/")) {
        return { data: { status: "FAILED", error_code: "NO_MATCH",
                         error_message: "No enrolled staff member matched this fingerprint." } };
      }
      return READY_AGENT;
    },
    post: () => SESSION,
  });
  await flush();
  await app.actionButton("CLOCK_IN").click();
  await flush();
  await app.tick();
  assert.equal(app.state(), "failed");
  assert.match(app.text(), /did not match any enrolled staff member/);
  assert.equal(app.successes.length, 0);
});

test("a lost connection is never reported as a verification", async () => {
  const app = boot({
    get: (route) => {
      if (route.includes("/sessions/")) throw new Error("network down");
      return READY_AGENT;
    },
    post: () => SESSION,
  });
  await flush();
  await app.actionButton("CLOCK_IN").click();
  await flush();
  await app.tick();
  assert.ok(["offline", "backend_unavailable"].includes(app.state()));
  assert.equal(app.successes.length, 0);
  assert.doesNotMatch(app.text(), /verified/i);
});

test("a session without a provisioned agent does not launch anything", async () => {
  const app = boot({
    get: () => ({ data: [{ reference: "", online: false }] }),
    post: () => ({ data: { session_token: "T1", agent_reference: "" } }),
  });
  await flush();
  await app.actionButton("CLOCK_IN").click();
  await flush();
  assert.equal(app.handoffs().length, 0);
  assert.equal(app.state(), "not_provisioned");
});

/* --------------------------------------------------------------------------
   Provisioning, and the refusal that produced "SETUP REFUSED — Only attendance
   management can provision a fingerprint reader agent" in production with no
   explanation of which permission was missing or who could grant it.
-------------------------------------------------------------------------- */

const STATUS_RECEPTIONIST = {
  data: {
    can_provision: false,
    missing_capability: "attendance.manage",
    capability_label: "Manage attendance",
    role: "RECEPTIONIST",
    department: "FRONT_DESK",
    department_context_valid: true,
    capability_count: 6,
    holds: ["attendance.clock", "leave.request"],
    reason: "This account does not hold the attendance management capability, which is what provisioning a reader requires.",
    grant_hint: "Sign in on this computer with an administrator, general manager or attendance manager account and press Set up again.",
    workstation_reference: "WS-FD-1",
    setup_ready: false,
    agent: null,
  },
};

const STATUS_MANAGER = {
  data: {
    can_provision: true,
    missing_capability: "",
    role: "MANAGER",
    department: "MANAGEMENT",
    department_context_valid: true,
    capability_count: 22,
    holds: ["attendance.manage", "staff.view"],
    reason: "",
    grant_hint: "",
    workstation_reference: "WS-FD-1",
    setup_ready: true,
    agent: null,
  },
};

function bootWithStatus(status, extra) {
  const app = boot(Object.assign({
    get: (route) => (route.includes("setup-status") ? status : { data: [] }),
    post: () => ({ data: {} }),
  }, extra || {}));
  return app;
}

test("a receptionist is told WHICH permission is missing and who can grant it", async () => {
  const app = bootWithStatus(STATUS_RECEPTIONIST);
  await flush();
  await flush();
  assert.equal(app.state(), "setup_forbidden");
  const text = app.text();
  assert.match(text, /manager/i, "the message must say a manager has to do it");
  assert.match(text, /attendance management/i, "the message must name the missing authority");
  const setup = app.host.children.find(
    (child) => child.tagName === "BUTTON" && /Set up fingerprint reader/.test(child.textContent || "")
  );
  assert.ok(setup, "the setup button must exist");
  assert.equal(setup.hidden, true, "a receptionist must not be offered a button the server will refuse");
  assert.equal(setup.disabled, false, "hiding is a UI affordance only; the server still enforces the rule");
});

test("a manager gets the one-click setup button", async () => {
  const app = bootWithStatus(STATUS_MANAGER);
  await flush();
  await flush();
  const setup = app.host.children.find(
    (child) => child.tagName === "BUTTON" && /Set up fingerprint reader/.test(child.textContent || "")
  );
  assert.ok(setup, "the setup button must exist");
  assert.equal(setup.hidden, false, "a manager must be able to provision from this computer");
});

test("a computer with a valid agent is never re-keyed from the panel", async () => {
  const status = JSON.parse(JSON.stringify(STATUS_MANAGER));
  status.data.agent = { reference: "RAG-1", status: "ACTIVE", label: "Reception reader" };
  const app = bootWithStatus(status);
  await flush();
  await flush();
  const setup = app.host.children.find(
    (child) => child.tagName === "BUTTON" && /Set up fingerprint reader/.test(child.textContent || "")
  );
  assert.equal(setup.hidden, true, "an existing ACTIVE agent must not be re-provisioned by a normal press");
});

test("provisioning that the server answers with 'already provisioned' launches nothing", async () => {
  const app = bootWithStatus(STATUS_MANAGER, {
    post: () => ({ data: { already_provisioned: true, reference: "RAG-1", workstation_reference: "WS-FD-1" } }),
  });
  await flush();
  await flush();
  const setup = app.host.children.find(
    (child) => child.tagName === "BUTTON" && /Set up fingerprint reader/.test(child.textContent || "")
  );
  await setup.click();
  await flush();
  await flush();
  assert.equal(app.handoffs().length, 0, "no jone-reader: hand-off may run when the key was kept");
});

test("a setup refusal is shown verbatim, with the permission the server named", async () => {
  const app = bootWithStatus(STATUS_MANAGER, {
    post: () => { throw new Error("Provisioning a fingerprint reader requires the “Manage attendance” permission (attendance.manage)."); },
  });
  await flush();
  await flush();
  const setup = app.host.children.find(
    (child) => child.tagName === "BUTTON" && /Set up fingerprint reader/.test(child.textContent || "")
  );
  await setup.click();
  await flush();
  await flush();
  assert.equal(app.state(), "setup_forbidden");
  assert.match(app.text(), /Manage attendance/);
  assert.match(app.text(), /attendance\.manage/);
});

/* --------------------------------------------------------------------------
   The 6-digit code: issued by the API after a verified finger, displayed by the
   panel, and never made up by the browser.
-------------------------------------------------------------------------- */

test("an issued 6-digit code is displayed only when the API reports it", async () => {
  const app = boot({
    get: (route) => (route.includes("setup-status") ? STATUS_MANAGER : { data: { status: "DONE", result: {} } }),
    post: () => ({ data: { session_token: "T9", agent_reference: "RAG-1", agent_online: true, launch_uri: "jone-reader:capture?session=T9" } }),
  });
  await flush();
  await app.actionButton("CLOCK_IN").click();
  await flush();
  await app.tick();
  assert.doesNotMatch(app.text(), /6-DIGIT WORKSTATION CODE/, "no code may appear without one from the API");
});

test("a code the API issued is rendered large, with its expiry rule", async () => {
  const context = makeContext({ hostname: "jonehotels.com", pathname: "/dashboard/workforce.html" });
  let intervalFn = null;
  context.setInterval = (fn) => { intervalFn = fn; return 98; };
  context.clearInterval = () => { intervalFn = null; };
  context.JONE = { ui: { toast() {} } };
  if (typeof context.addEventListener !== "function") context.addEventListener = () => {};
  context.API = {
    getWorkstationReference: () => "WS-FD-1",
    get: async (route) => (route.includes("setup-status") ? STATUS_MANAGER : {
      data: { status: "DONE", result: { staff_id: 7, staff_name: "Ada Obi", access_code: "482917", attendance_reference: "ATT-1" } },
    }),
    post: async () => ({
      data: { session_token: "T7", agent_reference: "RAG-1", agent_online: true, launch_uri: "jone-reader:capture?session=T7" },
    }),
  };
  loadScript(context, "js/reader-agent.js");
  const host = makeElement("div");
  host.replaceChildren = function replaceChildren() { this.children.length = 0; };
  context.JONE.readerAgent.mount(host, { actions: ["ISSUE_CODE"] });
  await flush();
  const button = host.children
    .flatMap((child) => child.children || [])
    .find((child) => child.getAttribute && child.getAttribute("data-reader-action") === "ISSUE_CODE");
  assert.ok(button, "the panel must offer the 6-digit code action");
  await button.click();
  await flush();
  await intervalFn();
  await flush();
  const box = host.children.find((child) => child.className === "reader-agent-code");
  assert.ok(box, "the issued code must be rendered in its own block");
  const text = [box.textContent].concat(box.children.map((child) => child.textContent)).join(" ");
  assert.match(text, /6-DIGIT WORKSTATION CODE/);
  assert.match(text, /482917/);
  assert.match(text, /clock out/i);
});

test("a mounted panel marks its host, so the deployment check works on every page", async () => {
  /* staff-profile.html mounts explicitly on a div with no markup attribute, so
     the panel itself has to stamp the host. Without that, the documented check
     (exactly one [data-jone-reader-agent-host] per page, carrying the build
     version) would silently pass on two pages and fail on the third. */
  const app = boot({ get: async () => READY_AGENT });
  await flush();
  assert.equal(app.host.getAttribute("data-jone-reader-agent-host"), "explicit",
    "an explicitly mounted host must still be identifiable");
  assert.equal(app.host.getAttribute("data-jone-reader-agent-version"), "1.1.37");
  assert.equal(app.host.getAttribute("data-jone-reader-agent"), "mounted");
});
