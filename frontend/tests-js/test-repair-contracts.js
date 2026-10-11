/* tests-js/test-repair-contracts.js
   Regression guards for the audit-repair changes (D1, D7, D11, D12, D14, D15, N9).
   Run from /frontend: node --test tests-js/test-repair-contracts.js

   Two kinds of assertion live here:
     1. Static contract checks over the shipped files (no page may reference an
        endpoint name that API_ENDPOINTS does not define, navigation must target
        the working terminals page, enquiries must use the PATCH lifecycle).
     2. Runtime checks of the shared client behaving as the backend contract
        requires (fail-fast on an unconfigured endpoint, the assignRoom body,
        a validated blob download that revokes its object URL).
*/
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { makeContext, loadScript, disposeAll, FRONTEND } = require("./harness");

test.after(() => disposeAll());

/* ---------------------------------------------------------------- helpers */

function bootApi({ endpoints, fetchImpl } = {}) {
  const ctx = makeContext({ hostname: "www.jonehotel.test", pathname: "/dashboard/accounting.html" });
  ctx.fetch = fetchImpl || (() => Promise.reject(new TypeError("fetch not configured by test")));
  ctx.APP_CONFIG = {
    API_BASE_URL: "",
    PAGE_SIZE: 10,
    STORAGE: { AUTH: "jone.auth", SESSION: "jone.session", BOOKING: "jone.booking.draft" },
  };
  loadScript(ctx, "js/config.js");
  if (endpoints) ctx.window.APP_CONFIG.API_ENDPOINTS = endpoints;
  loadScript(ctx, "js/api.js");
  return ctx;
}

function jsonResponse(status, body, headers = {}) {
  return Promise.resolve({
    status,
    ok: status >= 200 && status < 300,
    headers: { get: (key) => headers[String(key).toLowerCase()] || null },
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  });
}

function readDirFiles(relDir, extensions) {
  const dir = path.join(FRONTEND, relDir);
  return fs
    .readdirSync(dir)
    .filter((name) => extensions.some((ext) => name.endsWith(ext)))
    .map((name) => ({ name: path.join(relDir, name), source: fs.readFileSync(path.join(dir, name), "utf8") }));
}

/* ------------------------------------------------- 1. D1 endpoint coverage */

test("every endpoint name the dashboard references is defined in API_ENDPOINTS", () => {
  const ctx = bootApi();
  const defined = new Set(Object.keys(ctx.window.APP_CONFIG.API_ENDPOINTS));

  const files = [
    ...readDirFiles("js", [".js"]),
    ...readDirFiles("dashboard", [".html"]),
  ];

  const patterns = [
    /* API.list("rooms"), API.getOne("enquiries", id), API.update("rooms", …) */
    /API\.(?:list|getOne|create|update|remove|resourceAction)\(\s*["']([A-Za-z0-9_]+)["']/g,
    /* resourceEp("posOrders") inside js/api.js */
    /resourceEp\(\s*["']([A-Za-z0-9_]+)["']\s*\)/g,
    /* APP_CONFIG.API_ENDPOINTS.financeTransactions (the D1 accounting defect) */
    /API_ENDPOINTS\.([A-Za-z0-9_]+)/g,
  ];

  const missing = [];
  for (const file of files) {
    for (const pattern of patterns) {
      pattern.lastIndex = 0;
      let match;
      while ((match = pattern.exec(file.source)) !== null) {
        const name = match[1];
        /* A local shadow (e.g. `const API_ENDPOINTS = …` in a page) would be a
           page-level object, but every page reads the shared config, so an
           unknown name here is a real configuration gap. */
        if (!defined.has(name)) missing.push(`${file.name}: ${name}`);
      }
    }
  }

  assert.deepEqual(
    missing,
    [],
    "the dashboard references endpoint names that API_ENDPOINTS does not define:\n  " + missing.join("\n  ")
  );
});

test("the accounting page reads the four finance collections and the summary", () => {
  const ctx = bootApi();
  const eps = ctx.window.APP_CONFIG.API_ENDPOINTS;
  assert.equal(eps.financeSummary, "/api/admin/finance/reports/summary/");
  assert.equal(eps.financeTransactions, "/api/admin/finance/transactions/");
  assert.equal(eps.financeExpenses, "/api/admin/finance/expenses/");
  assert.equal(eps.financeApprovals, "/api/admin/finance/approvals/");
  assert.equal(eps.financeCashSessions, "/api/admin/finance/cash-sessions/");
});

test("an unconfigured endpoint fails fast instead of requesting an undefined URL", async () => {
  let fetched = 0;
  const ctx = bootApi({
    fetchImpl: () => {
      fetched += 1;
      return jsonResponse(200, { success: true, data: [] });
    },
  });

  const unknown = await ctx.window.API.list("notConfiguredModule").catch((error) => error);
  assert.equal(unknown.status, 0);
  assert.match(unknown.message, /isn't configured/i);

  const badPath = await ctx.window.API.get("/api/admin/undefined/").catch((error) => error);
  assert.equal(badPath.code, "ENDPOINT_NOT_CONFIGURED");
  assert.match(badPath.message, /not sent/i);

  const blank = await ctx.window.API.get("").catch((error) => error);
  assert.equal(blank.code, "ENDPOINT_NOT_CONFIGURED");

  assert.equal(fetched, 0, "a misconfigured request must never reach the network");
});

/* ------------------------------------------------------ 2. D7 assignRoom */

test("assignRoom sends the documented {assignment_id, room_id} contract", async () => {
  let captured = null;
  const ctx = bootApi({
    fetchImpl: (url, init) => {
      captured = { url, init };
      return jsonResponse(200, { success: true, data: { assignment_id: 7, room_number: "101" } });
    },
  });

  const reference = "J1-20261010-TESTREF";
  await ctx.window.API.assignRoom(reference, 7, 12);

  assert.match(captured.url, /\/api\/admin\/bookings\/J1-20261010-TESTREF\/assign-room\/$/);
  assert.equal(captured.init.method, "POST");
  assert.deepEqual(JSON.parse(captured.init.body), { assignment_id: 7, room_id: 12 });

  /* The old helper sent {room: id} — the backend ignored it and answered 200
     with no change (the audit's silent no-op). Guard against a regression. */
  assert.equal("room" in JSON.parse(captured.init.body), false);
});

test("assignRoom refuses an incomplete room change before calling the API", () => {
  let fetched = 0;
  const ctx = bootApi({
    fetchImpl: () => {
      fetched += 1;
      return jsonResponse(200, { success: true, data: {} });
    },
  });

  /* The guard throws synchronously: an invalid payload must never become a
     request, and the caller never sees a 200 that changed nothing. */
  for (const [assignmentId, roomId] of [["", 12], [7, null], ["abc", 12], [0, 12]]) {
    let error = null;
    try {
      ctx.window.API.assignRoom("J1-TEST", assignmentId, roomId);
    } catch (err) {
      error = err;
    }
    assert.ok(error, `expected a guard for assignment ${assignmentId} / room ${roomId}`);
    assert.match(error.message, /valid (assignment id|room id) is required/i);
    assert.equal(error.code, "INVALID_ROOM_ASSIGNMENT");
  }
  assert.equal(fetched, 0);
});

/* ---------------------------------------------------- 3. D14 downloads */

function bootDownload({ contentType = "application/pdf", size = 2048 } = {}) {
  const uploaded = [];
  const revoked = [];
  const clicks = [];
  const ctx = bootApi({
    fetchImpl: () =>
      Promise.resolve({
        status: 200,
        ok: true,
        headers: { get: (key) => (String(key).toLowerCase() === "content-type" ? contentType : null) },
        blob: () => Promise.resolve(new ctx.Blob(["%PDF-1.4 test"], { type: contentType })),
      }),
  });

  ctx.Blob = class TestBlob {
    constructor(parts, options) {
      this.parts = parts;
      this.type = (options && options.type) || "";
      this.size = size;
    }
  };
  ctx.URL = {
    createObjectURL: (blob) => {
      uploaded.push(blob);
      return "blob:test-object-url";
    },
    revokeObjectURL: (url) => revoked.push(url),
  };

  const createElement = ctx.document.createElement.bind(ctx.document);
  ctx.document.createElement = (tag) => {
    const el = createElement(tag);
    if (typeof el.remove !== "function") {
      el.remove = function remove() {
        this.removed = true;
      };
    }
    const click = el.click.bind(el);
    el.click = function clickCapture(event) {
      clicks.push({ tag: el.tagName, href: el.href, download: el.download, rel: el.rel });
      return click(event);
    };
    return el;
  };
  return { ctx, uploaded, revoked, clicks };
}

test("downloadReport builds the export URL, saves a blob, and revokes the object URL", async () => {
  const { ctx, uploaded, revoked, clicks } = bootDownload({ contentType: "application/pdf" });

  const result = await ctx.window.API.downloadReport(
    ctx.window.APP_CONFIG.API_ENDPOINTS.financeSummary,
    "pdf",
    { start: "2026-10-01", end: "2026-10-31" }
  );

  assert.equal(uploaded.length, 1, "the server blob must reach createObjectURL");
  assert.equal(uploaded[0].type, "application/pdf");
  assert.equal(clicks.length, 1, "a download link must be clicked");
  assert.equal(clicks[0].href, "blob:test-object-url");
  assert.equal(clicks[0].download, "report-2026-10-01_2026-10-31.pdf");
  assert.equal(clicks[0].rel, "noopener");
  assert.equal(result.size, 2048);

  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(revoked, ["blob:test-object-url"], "the object URL must be released");
});

test("downloadReport refuses an unsupported format and a mislabelled body", async () => {
  const { ctx, uploaded } = bootDownload();

  const unsupported = await ctx.window.API.downloadReport(
    ctx.window.APP_CONFIG.API_ENDPOINTS.financeSummary, "docx", { start: "2026-10-01", end: "2026-10-31" }
  ).catch((error) => error);
  assert.equal(unsupported.code, "UNSUPPORTED_EXPORT_FORMAT");

  const mismatch = await ctx.window.API.downloadReport(
    ctx.window.APP_CONFIG.API_ENDPOINTS.financeSummary, "xlsx", { start: "2026-10-01", end: "2026-10-31" }
  ).catch((error) => error);
  assert.equal(mismatch.code, "UNEXPECTED_EXPORT_TYPE", "an HTML error page must not be saved as .xlsx");
  assert.equal(uploaded.length, 0, "nothing may be downloaded when the type is wrong");
});

test("downloadReport refuses an empty body", async () => {
  const { ctx, uploaded } = bootDownload({ size: 0 });
  const error = await ctx.window.API.downloadReport(
    ctx.window.APP_CONFIG.API_ENDPOINTS.financeSummary, "csv", { start: "2026-10-01", end: "2026-10-31" }
  ).catch((err) => err);
  assert.match(error.message, /empty file|did not return a downloadable file/i);
  assert.equal(uploaded.length, 0);
});

/* ------------------------------------- 4. D12 legacy POS cash-session base */

test("the legacy POS cash-session base is used only for its action routes", async () => {
  const calls = [];
  const ctx = bootApi({
    fetchImpl: (url, init) => {
      calls.push({ url, method: init.method });
      return jsonResponse(201, { success: true, data: {} });
    },
  });

  await ctx.window.API.openPosCashSession({ opening_float: "0.00" });
  await ctx.window.API.closePosCashSession("CS-1", { counted_cash: "0.00" });

  assert.equal(calls.length, 2);
  assert.match(calls[0].url, /\/api\/admin\/pos\/cash-sessions\/open\/$/);
  assert.match(calls[1].url, /\/api\/admin\/pos\/cash-sessions\/CS-1\/close\/$/);
  for (const call of calls) {
    assert.equal(
      /\/api\/admin\/pos\/cash-sessions\/$/.test(call.url),
      false,
      "the bare legacy collection route does not exist on the backend"
    );
  }
});

test("cash sessions are listed through the finance collection", () => {
  const page = fs.readFileSync(path.join(FRONTEND, "dashboard", "accounting.html"), "utf8");
  assert.match(page, /API_ENDPOINTS\.financeCashSessions/, "the sessions panel must read the finance collection");

  /* The legacy/action base stays declared (and documented) in the config so the
     open/close actions keep one source of truth, but no page may LIST from it. */
  const config = fs.readFileSync(path.join(FRONTEND, "js", "config.js"), "utf8");
  assert.match(config, /posCashSessions:\s*"\/api\/admin\/pos\/cash-sessions\/"/);
  assert.match(config, /financeCashSessions:\s*"\/api\/admin\/finance\/cash-sessions\/"/);

  for (const { name, source } of readDirFiles("dashboard", [".html"])) {
    assert.equal(
      /API\.list\(\s*["']posCashSessions["']/.test(source),
      false,
      name + " lists cash sessions through the legacy POS base"
    );
  }
});

/* -------------------------------------------- 5. D11 navigation + N9 nav */

test("the sidebar targets the working terminals page", () => {
  const nav = fs.readFileSync(path.join(FRONTEND, "js", "dashboard.js"), "utf8");
  assert.match(nav, /href:\s*"terminals\.html"/, "navigation must point at terminals.html");
  assert.equal(
    /href:\s*["']workstations\.html["']/.test(nav),
    false,
    "the unlinked legacy workstations.html must not be re-introduced into navigation"
  );
  assert.ok(
    fs.existsSync(path.join(FRONTEND, "dashboard", "terminals.html")),
    "the terminals page must ship with the dashboard"
  );
});

test("audit log navigation stays administrator-only", () => {
  const nav = fs.readFileSync(path.join(FRONTEND, "js", "dashboard.js"), "utf8");
  const entry = nav
    .split("\n")
    .find((line) => /audit-logs\.html/.test(line) || /label:\s*"Audit Logs"/.test(line));
  assert.ok(entry, "the audit log navigation entry must exist");
  /* The nav entry carries a single admin-only gate (`role: "admin"`); a list
     form is accepted too so the intent, not the spelling, is what is pinned. */
  assert.match(
    entry,
    /(roles:\s*\[[^\]]*"admin"[^\]]*\]|role:\s*"admin")/,
    "audit navigation must be gated to the admin role"
  );
  assert.equal(/general_manager|manager|receptionist|accounts_manager/.test(entry), false,
    "no non-admin role may be granted the audit workspace entry");
});

/* ------------------------------------------- 6. D15 enquiry lifecycle */

test("enquiries use the supported PATCH lifecycle only", () => {
  const page = fs.readFileSync(path.join(FRONTEND, "dashboard", "enquiries.html"), "utf8");
  assert.equal(
    /acknowledge|resolve_|reply|"reply"/.test(page.replace(/RESOLVED/g, "")),
    false,
    "the page must not call enquiry action routes the backend does not implement"
  );
  assert.match(page, /API\.update\(\s*["']enquiries["']/, "status changes go through the PATCH detail route");
  assert.match(page, /API\.list\(\s*["']enquiries["']/);
  assert.match(page, /API\.getOne\(\s*["']enquiries["']/);
});
