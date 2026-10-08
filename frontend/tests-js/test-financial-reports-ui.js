/* Financial-report dashboard contract tests.
   Run from /frontend: node --test tests-js/test-financial-reports-ui.js */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { makeContext, makeElement, loadScript, disposeAll, FRONTEND } = require("./harness");

test.after(() => disposeAll());

function controllerSource() {
  const page = fs.readFileSync(path.join(FRONTEND, "dashboard", "reports.html"), "utf8");
  const start = page.indexOf("(function(){\n  if (!__reportsAllowed) return;");
  const end = page.indexOf("\n})();\n\n</script>", start);
  assert.notEqual(start, -1, "reports controller must exist");
  assert.notEqual(end, -1, "reports controller must have a closing IIFE");
  return page.slice(start, end + "\n})();".length);
}

function reportMetric(amount, transactions) {
  const out = { amount };
  if (transactions !== undefined) out.transactions = transactions;
  return out;
}

async function settle() {
  // The controller independently handles three promise responses. Yield past
  // the current event-loop turn so already-resolved VM promises can advance;
  // this is not polling and does not involve a production timer.
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
}

function bootReportController() {
  const ctx = makeContext();
  loadScript(ctx, "js/config.js");
  loadScript(ctx, "js/utils.js");

  const financeBody = makeElement("tbody");
  const bookingBody = makeElement("tbody");
  const financeKpis = makeElement("section");
  const financeBasis = makeElement("p");
  const range = makeElement("select");
  const from = makeElement("input");
  const to = makeElement("input");
  const apply = makeElement("button");
  const exportButton = makeElement("button");
  range.value = "30";

  const rp = {};
  [
    "recognized-revenue", "recognized-revenue-sub",
    "collections", "collections-sub",
    "operating-expenses", "operating-expenses-sub",
    "refunds", "refunds-sub", "operational-summary"
  ].forEach((name) => { rp[name] = makeElement("span"); });

  const bySelector = {
    "[data-tbody=report-finance]": financeBody,
    "[data-tbody=report-bookings]": bookingBody,
    "[data-finance-kpis]": financeKpis,
    "[data-finance-basis]": financeBasis,
  };
  ctx.document.querySelector = (selector) => bySelector[selector] || null;
  ctx.document.querySelectorAll = (selector) => {
    const match = /^\[data-rp=(.+)\]$/.exec(selector);
    return match && rp[match[1]] ? [rp[match[1]]] : [];
  };
  ctx.document.getElementById = (id) => ({
    "rp-range": range,
    "rp-from": from,
    "rp-to": to,
    "rp-apply": apply,
    "export-csv": exportButton,
  })[id] || null;

  ctx.window.__reportsAllowed = true;
  ctx.window.JONE.hotelTodayISO = () => "2026-10-08";
  ctx.window.JONE.ui = { toast() {} };
  ctx.window.JONE.dashboard = {
    DATA: {
      loading(el) { el.innerHTML = "loading"; },
      error(el, title, message) { el.innerHTML = "error: " + title + " — " + message; },
    },
  };

  const report = {
    basis: "posted_immutable_ledger_business_date",
    currency: "NGN",
    ledger_transactions: 5,
    guest_charges: reportMetric("110.00", 1),
    recognized_revenue: reportMetric("100.00", 1),
    tax_accrued: reportMetric("10.00", 1),
    collections: reportMetric("110.00", 1),
    refunds: reportMetric("20.00", 1),
    net_collections: "90.00",
    discounts: reportMetric("0.00", 0),
    operating_expenses: reportMetric("15.00", 1),
    inventory_acquisitions: reportMetric("30.00", 1),
    cash_paid_out: reportMetric("45.00", 2),
    gross_operating_result: "85.00",
  };
  const requests = [];
  ctx.window.API = {
    get(endpoint, options) {
      requests.push({ endpoint, options });
      if (endpoint === ctx.window.APP_CONFIG.API_ENDPOINTS.financeSummary) {
        return Promise.resolve({ data: report });
      }
      if (endpoint === ctx.window.APP_CONFIG.API_ENDPOINTS.reportsBookings) {
        return Promise.resolve({ data: { total_bookings: 3, cancelled: 1, no_shows: 0, by_status: [{ status: "CONFIRMED", count: 2 }] } });
      }
      if (endpoint === ctx.window.APP_CONFIG.API_ENDPOINTS.reportsOccupancy) {
        return Promise.resolve({ data: { average_occupancy_percent: 75, active_rooms: 8, occupied_room_nights: 12 } });
      }
      return Promise.reject(new Error("Unexpected endpoint: " + endpoint));
    },
  };

  vm.runInContext(controllerSource(), ctx, { filename: "dashboard/reports-controller.js" });
  return { ctx, requests, financeBody, bookingBody, financeBasis, rp };
}

test("configuration exposes the ledger summary separately from legacy report projections", () => {
  const ctx = makeContext();
  loadScript(ctx, "js/config.js");

  assert.equal(ctx.window.APP_CONFIG.API_ENDPOINTS.financeSummary, "/api/admin/finance/reports/summary/");
  assert.equal(ctx.window.APP_CONFIG.API_ENDPOINTS.reportsRevenue, "/api/admin/reports/revenue/");
});

test("reports dashboard requests ledger finance and operational projections concurrently", async () => {
  const page = fs.readFileSync(path.join(FRONTEND, "dashboard", "reports.html"), "utf8");
  const booted = bootReportController();

  // All calls are started before the controller awaits a result; this prevents
  // the range filter from serializing three otherwise independent requests.
  assert.equal(booted.requests.length, 3);
  assert.deepEqual(
    booted.requests.map((request) => request.endpoint),
    [
      booted.ctx.window.APP_CONFIG.API_ENDPOINTS.financeSummary,
      booted.ctx.window.APP_CONFIG.API_ENDPOINTS.reportsBookings,
      booted.ctx.window.APP_CONFIG.API_ENDPOINTS.reportsOccupancy,
    ]
  );
  assert.equal(booted.requests[0].options.params.start, "2026-09-09");
  assert.equal(booted.requests[0].options.params.end, "2026-10-08");
  assert.equal(booted.requests[0].options.params.currency, "NGN");
  assert.doesNotMatch(page, /reportsRevenue/);

  await settle();
  assert.equal(booted.rp["recognized-revenue"].textContent, "₦100");
  assert.equal(booted.rp.collections.textContent, "₦110");
  assert.equal(booted.rp["operating-expenses"].textContent, "₦15");
  assert.match(booted.financeBasis.textContent, /Posted immutable ledger/);
  assert.match(booted.financeBody.innerHTML, /Gross operating result/);
  assert.match(booted.financeBody.innerHTML, /Cash paid-out/);
  assert.match(booted.bookingBody.innerHTML, /confirmed/);
  assert.match(booted.rp["operational-summary"].textContent, /3 bookings/);
  assert.match(booted.rp["operational-summary"].textContent, /75% avg occupancy/);
});
