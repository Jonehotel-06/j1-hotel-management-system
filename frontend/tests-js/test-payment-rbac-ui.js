/* Capability-scoped payment/receipt frontend contracts. */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { makeContext, loadScript, FRONTEND, disposeAll } = require("./harness");

test.after(() => disposeAll());

function response(data) {
  return Promise.resolve({
    status: 200,
    ok: true,
    headers: { get: (key) => key === "content-type" ? "application/json" : null },
    json: () => Promise.resolve({ success: true, data }),
    text: () => Promise.resolve(JSON.stringify({ success: true, data })),
  });
}

test("guest and staff receipt helpers use separate authorization routes", async () => {
  const ctx = makeContext();
  loadScript(ctx, "js/config.js");
  const requests = [];
  ctx.fetch = (url, init) => {
    requests.push({ url, init });
    return response({ booking_reference: "J1-RBAC-1" });
  };
  loadScript(ctx, "js/api.js");

  await ctx.window.API.getBookingReceipt("J1/TEST-1");
  await ctx.window.API.getStaffBookingReceipt("J1/TEST-1");

  assert.equal(requests[0].url, "/api/bookings/J1%2FTEST-1/receipt/");
  assert.equal(requests[1].url, "/api/admin/bookings/J1%2FTEST-1/receipt/");
});

test("payment and receipt controls are gated by their distinct capabilities", () => {
  const read = (file) => fs.readFileSync(path.join(FRONTEND, file), "utf8");
  const nav = read("js/dashboard.js");
  const auth = read("js/auth.js");
  const payments = read("dashboard/payments.html");
  const receipts = read("dashboard/receipts.html");

  assert.match(nav, /Payments[^\n]*capabilities: \["payment\.read"\]/);
  assert.match(nav, /Receipts[^\n]*capabilities: \["payment\.read"\]/);
  assert.match(auth, /\["accounts_manager", "accountant"\]\.includes\(r\)\) return "dashboard\/payments\.html"/);
  assert.match(payments, /data-record-payment-panel hidden/);
  assert.match(payments, /hasCapability\("booking\.manage"\)[\s\S]*hasCapability\("payment\.capture"\)/);
  assert.match(payments, /hasCapability\("booking\.read"\)[\s\S]*hasCapability\("booking\.manage"\)/);
  assert.match(receipts, /getStaffBookingReceipt\(ref\)/);
  assert.match(receipts, /hasCapability\("payment\.receipt\.send"\)/);
  assert.match(receipts, /hasCapability\("booking\.read"\)[\s\S]*hasCapability\("booking\.manage"\)/);
});

test("receipt list waits for capabilities before fetching rows or deriving controls", () => {
  const read = (file) => fs.readFileSync(path.join(FRONTEND, file), "utf8");
  const receipts = read("dashboard/receipts.html");
  const readyAt = receipts.indexOf("var receiptCapabilitiesReady =");
  const loadAt = receipts.indexOf("receiptCapabilitiesReady.then(function(){ load(1); });");
  assert.ok(readyAt >= 0 && loadAt > readyAt, "rows must load only after Auth.loadCapabilities settles");
  assert.match(receipts, /Auth\.loadCapabilities\(\)\.catch\(function\(\)\{ return \[\]; \}\)/);
});

test("payment detail booking shortcut is hidden without both booking scope and a legacy detail role", () => {
  const dashboard = fs.readFileSync(path.join(FRONTEND, "js/dashboard.js"), "utf8");
  assert.match(dashboard, /function canOpenPaymentBooking\(\)[\s\S]*hasAnyRole\(\["admin", "manager", "receptionist"\]\)[\s\S]*hasCapability\("booking\.read"\)[\s\S]*hasCapability\("booking\.manage"\)/);
  assert.match(dashboard, /initial\.booking_reference && canOpenPaymentBooking\(\)/);
});

test("standalone staff receipt awaits capabilities and hides unauthorized actions/booking links", () => {
  const details = fs.readFileSync(path.join(FRONTEND, "dashboard/receipt-details.html"), "utf8");
  assert.match(details, /var receiptDetailCapabilitiesReady =[^;]*Auth\.loadCapabilities\(\)/);
  assert.ok(details.indexOf("await receiptDetailCapabilitiesReady;") < details.indexOf("getStaffBookingReceipt(ref)"));
  assert.match(details, /hasCapability\("payment\.receipt\.send"\)/);
  assert.match(details, /hasAnyRole\(\["admin", "manager", "receptionist"\]\)/);
});
