/* tests-js/test-audit-detail-modal.js
   Audit log drill-down: one read-only renderer shared by the list modal and the
   standalone page, opened from the row without losing filter/page state.
   Run from /frontend: node --test tests-js/test-audit-detail-modal.js */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { makeContext, loadScript, disposeAll, FRONTEND } = require("./harness");

const RECORD = {
  id: 42,
  action: "TEMPORARY_WORKSTATION_CREDENTIAL_ISSUED",
  created_at: "2026-10-14T09:15:00Z",
  actor_name: "Adaeze Manager",
  actor_email: "manager@jonehotel.com",
  object_type: "TemporaryWorkstationCredential",
  object_id: "17",
  ip_address: "10.0.0.4",
  summary: "Code issued for the front desk terminal",
  changes: { status: ["PENDING", "ACTIVE"] },
  metadata: { staff_id: 7, workstation: "WS-RECEPTION-MAIN" },
};

function bootDetail({ fail } = {}) {
  const ctx = makeContext({ pathname: "/dashboard/audit-logs.html" });
  loadScript(ctx, "js/utils.js");
  ctx.window.JONE = ctx.window.JONE || {};
  ctx.window.JONE.esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
  ctx.window.JONE.formatDateTime = (v) => "formatted:" + v;
  ctx.window.JONE.icons = { inject() {} };
  let opened = 0;
  let closed = 0;
  ctx.window.JONE.ui = {
    modal: {
      open(opts) { opened += 1; ctx._modalOpts = opts; },
      close() { closed += 1; },
    },
    toast() {},
  };
  let requested = [];
  ctx.window.API = {
    getOne(name, id) {
      requested.push([name, id]);
      if (fail) return Promise.reject(fail);
      return Promise.resolve({ data: RECORD });
    },
  };
  loadScript(ctx, "js/audit-detail.js");
  return { ctx, opened: () => opened, closed: () => closed, requested: () => requested };
}

test("renderer shows the immutable record fields, changes and metadata", () => {
  const { ctx } = bootDetail();
  const html = ctx.window.JONE.auditDetail.render(RECORD);
  assert.match(html, /Adaeze Manager/);
  assert.match(html, /manager@jonehotel\.com/);
  assert.match(html, /formatted:2026-10-14T09:15:00Z/);
  assert.match(html, /Changes/);
  assert.match(html, /Metadata/);
  assert.match(html, /WS-RECEPTION-MAIN/);
  // Read-only: no control may mutate or remove an audit record.
  assert.doesNotMatch(html, /<button/i);
  assert.doesNotMatch(html, /<form/i);
  assert.doesNotMatch(html, /audit-edit|audit-delete|data-audit-delete/i);
  disposeAll();
});

test("missing fields degrade to an em dash instead of printing null", () => {
  const { ctx } = bootDetail();
  const html = ctx.window.JONE.auditDetail.render({ id: 1, action: "STAFF_SIGN_IN" });
  assert.doesNotMatch(html, /null/);
  assert.doesNotMatch(html, /undefined/);
  assert.match(html, /\u2014/);
  disposeAll();
});

test("values are escaped (an audit summary can never inject markup)", () => {
  const { ctx } = bootDetail();
  const html = ctx.window.JONE.auditDetail.render({
    id: 2, action: "X", summary: '<img src=x onerror="alert(1)">',
  });
  assert.doesNotMatch(html, /<img/);
  assert.match(html, /&lt;img/);
  disposeAll();
});

test("open() fetches the record once, in the shared modal, with a link to the full page", async () => {
  const t = bootDetail();
  t.ctx.window.JONE.auditDetail.open(42, { title: "Temporary workstation credential issued" });
  assert.equal(t.opened(), 1, "one modal");
  assert.deepEqual(t.requested(), [["auditLogs", 42]], "the existing detail endpoint is reused");
  assert.equal(t.ctx._modalOpts.title, "Temporary workstation credential issued");
  const footer = t.ctx._modalOpts.footer;
  assert.ok(footer, "a footer holds the escape hatch to the standalone page");
  disposeAll();
});

test("a refused or missing record renders an explanation, never a blank dialog", async () => {
  const notFound = Object.assign(new Error("not found"), { status: 404 });
  const t = bootDetail({ fail: notFound });
  t.ctx.window.JONE.auditDetail.open(999);
  await Promise.resolve();
  const copy = t.ctx.window.JONE.auditDetail.errorCopy(notFound);
  assert.equal(copy[0], "Audit event not found");
  const denied = Object.assign(new Error("forbidden"), { status: 403 });
  assert.equal(t.ctx.window.JONE.auditDetail.errorCopy(denied)[0], "Access denied");
  disposeAll();
});

test("audit-logs.html opens the modal without navigating away from the filtered list", () => {
  const html = fs.readFileSync(path.join(FRONTEND, "dashboard", "audit-logs.html"), "utf8");
  assert.ok(html.includes("js/audit-detail.js"), "the shared renderer is loaded");
  assert.match(html, /data-audit-open=/, "rows carry the modal hook");
  assert.match(html, /event\.preventDefault\(\);/, "the click is intercepted instead of navigating");
  assert.match(html, /tbody\.addEventListener\("click"/, "ONE delegated listener on the table body");
  assert.match(html, /event\.metaKey\|\|event\.ctrlKey\|\|event\.shiftKey\|\|event\.altKey/, "modified clicks still open the page");
  assert.match(html, /href="'\+href\+'"/, "the href stays on the link (works without JS)");
  assert.doesNotMatch(html, /data-audit-open[^>]*onclick=/, "no inline handlers");
});

test("the standalone detail page renders through the same shared renderer", () => {
  const html = fs.readFileSync(path.join(FRONTEND, "dashboard", "audit-log-details.html"), "utf8");
  assert.ok(html.includes("js/audit-detail.js"), "the shared renderer is loaded");
  assert.match(html, /JONE\.auditDetail\.render\(a\)/, "the page reuses the renderer instead of a private copy");
  assert.doesNotMatch(html, /audit-json/, "the duplicated inline body is gone");
  assert.match(html, /href="audit-logs\.html"/, "navigation back to the list is preserved");
});

test.after(() => disposeAll());
