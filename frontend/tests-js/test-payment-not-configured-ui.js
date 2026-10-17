/* tests-js/test-payment-not-configured-ui.js */
/* The reported failure: every guest payment answered HTTP 503
   PAYMENT_NOT_CONFIGURED while the rest of the site worked, so the only clue
   was a server log the hotelier never reads.

   Two places must now say it plainly:
     1. Settings → a banner tells staff online payments are OFF (or running on a
        test key) and what to set; and
     2. the guest's failure screen stops telling them to "try again in a moment"
        — retrying cannot work — and offers the hotel instead.
   Run: node --test tests-js/   (from frontend/) */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const { makeContext } = require("./harness");

const FRONTEND = path.resolve(__dirname, "..");
const SETTINGS = fs.readFileSync(path.join(FRONTEND, "dashboard", "settings.html"), "utf8");
const CONFIRM = fs.readFileSync(path.join(FRONTEND, "booking-confirmation.html"), "utf8");

/* Run the shipped Settings banner block with a given server payload. */
function renderPaymentsNotice(current) {
  const start = SETTINGS.indexOf('var payNotice = $("st-payments-status");');
  const end = SETTINGS.indexOf('var ipWarning = $("st-request-ip-warning");');
  assert.ok(start > 0 && end > start, "the settings payments banner was not found in the shipped page");

  const node = { innerHTML: "", style: { display: "" } };
  const ctx = makeContext({ pathname: "/dashboard/settings.html" });
  ctx.window.JONE = { esc: (v) => String(v == null ? "" : v) };
  ctx.$ = (id) => (id === "st-payments-status" ? node : null);
  ctx.current = current;
  vm.runInContext(SETTINGS.slice(start, end), ctx, { filename: "settings.html#payments" });
  return node;
}

test("Settings says online payments are OFF and what to set", () => {
  const notice = renderPaymentsNotice({ online_payments_configured: false, online_payments_mode: "" });
  assert.equal(notice.style.display, "");
  assert.match(notice.innerHTML, /switched OFF/i);
  assert.match(notice.innerHTML, /PAYSTACK_SECRET_KEY/);
  assert.match(notice.innerHTML, /sk_live_/, "the key type that is needed is named");
  assert.match(notice.innerHTML, /booking is saved[^.]*|booking is saved/i,
    "staff are told the guest is not simply lost");
  assert.match(notice.innerHTML, /check_payment_config/, "how to confirm it from the host");
});

test("Settings warns that a test key takes no real money", () => {
  const notice = renderPaymentsNotice({ online_payments_configured: true, online_payments_mode: "test" });
  assert.equal(notice.style.display, "");
  assert.match(notice.innerHTML, /TEST key/i);
  assert.match(notice.innerHTML, /no real money/i);
});

test("a live key shows no warning at all", () => {
  const notice = renderPaymentsNotice({ online_payments_configured: true, online_payments_mode: "live" });
  assert.equal(notice.style.display, "none");
  assert.equal(notice.innerHTML, "");
});

test("the banner container exists in the shipped form", () => {
  assert.match(SETTINGS, /id="st-payments-status"/);
  assert.match(SETTINGS, /aria-live="polite"/, "the warning is announced, not silently painted");
});

test("the guest is not told to retry a hotel-side outage", () => {
  const start = CONFIRM.indexOf('} else if (err && err.code === "PAYMENT_NOT_CONFIGURED") {');
  const end = CONFIRM.indexOf('} else if (err && (err.code === "PAYMENT_GATEWAY_ERROR"');
  assert.ok(start > 0 && end > start, "PAYMENT_NOT_CONFIGURED needs its own branch before the generic gateway one");

  const branch = CONFIRM.slice(start, end);
  assert.match(branch, /not available at the moment/i);
  assert.match(branch, /booking has been saved without payment/i);
  assert.match(branch, /Nothing has been charged/i);
  assert.match(branch, /contact the hotel/i);
  assert.match(branch, /reception/, "the pay-at-desk route is offered");
  assert.match(branch, /retry-pay/);
  assert.match(branch, /style\.display = "none"/, "Retry is withdrawn: it cannot succeed");
  assert.doesNotMatch(branch, /try again in a moment/i);
});

test("a genuine gateway outage still invites a retry", () => {
  assert.match(CONFIRM, /err\.code === "PAYMENT_GATEWAY_ERROR" \|\| err\.status === 502 \|\| err\.status === 503/);
  assert.match(CONFIRM, /We could not start your payment\. Please try again in a moment/);
});

test("the failure screen can promote Contact hotel when retry is withdrawn", () => {
  assert.match(CONFIRM, /id="failure-contact"/);
  assert.match(CONFIRM, /failure-contact"\);\s*if \(contactCta\) contactCta\.className = "btn btn-accent"/);
});
