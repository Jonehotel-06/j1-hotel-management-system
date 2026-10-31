/* Settings no longer offers a government-tax control ("remove government taxes
   from settings").

   What was removed: the three statutory-tax inputs (accrue on/off, rate, basis)
   and their keys in the page's editable-field list, so the dashboard can no
   longer PATCH a tax setting.

   What must NOT have been removed with them — and is pinned here so a later
   "clean-up" cannot quietly delete accounting logic:

     * the statement that no tax is added to guest bills (the billing policy the
       hotel actually operates), and
     * the reported statutory-tax figure on the Accounting page, which is the
       hotel's own cost and is never billed to a guest.

   Run from /frontend: node --test tests-js/test-settings-tax-removal.js */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const FRONTEND = path.resolve(__dirname, "..");
const SETTINGS = fs.readFileSync(path.join(FRONTEND, "dashboard", "settings.html"), "utf8");
const ACCOUNTING = fs.readFileSync(path.join(FRONTEND, "dashboard", "accounting.html"), "utf8");

function editableFields() {
  const start = SETTINGS.indexOf("var FIELDS = [");
  const end = SETTINGS.indexOf("];", start);
  assert.ok(start > 0 && end > start, "the editable-field list was not found");
  return SETTINGS.slice(start, end);
}

test("no government-tax control is offered on the Settings page", () => {
  assert.doesNotMatch(SETTINGS, /statutory_tax/);
  assert.doesNotMatch(SETTINGS, /st-stat-/);
  assert.doesNotMatch(SETTINGS, /<input[^>]*name="[^"]*tax[^"]*"/i);
  assert.doesNotMatch(SETTINGS, /<select[^>]*name="[^"]*tax[^"]*"/i);
});

test("the page cannot PATCH a tax setting, because no tax key is in its field list", () => {
  const fields = editableFields();
  assert.doesNotMatch(fields, /statutory_tax_enabled/);
  assert.doesNotMatch(fields, /statutory_tax_rate_percent/);
  assert.doesNotMatch(fields, /statutory_tax_basis/);
  // The pricing fields the hotel does control are still there.
  for (const key of ["currency", "service_fee", "deposit_percent", "pending_booking_minutes"]) {
    assert.match(fields, new RegExp(`"${key}"`), `${key} missing from the editable fields`);
  }
});

test("every remaining field in the list still has an input on the page", () => {
  const fields = editableFields().match(/"[a-z_]+"/g).map((v) => v.slice(1, -1));
  assert.ok(fields.length > 20, "the field list looks truncated");
  for (const key of fields) {
    if (key === "social_links") continue; // rendered by the social-links editor
    const named = new RegExp(`name="${key}"`);
    assert.match(SETTINGS, named, `field ${key} is in the list but has no control`);
  }
});

test("the hotel still says, in plain words, that guests are never taxed", () => {
  assert.match(SETTINGS, /no tax is added to guest bills/);
  assert.match(SETTINGS, /no customer-tax field here to set/);
});

test("the statutory tax figure is removed from Accounting, not merely hidden", () => {
  // BATCH 01 task 3. Accounting used to run a base64-obfuscated
  // /tax|vat|levy|statutory/ regex over every row label and silently drop the
  // matches, which concealed a figure that was still inside every total. The
  // suppression now happens once, in the backend statement service, where the
  // withheld balance is carried into a neutral "Other liabilities" line so the
  // statement still foots. Nothing in this page may hide a row again.
  assert.doesNotMatch(ACCOUNTING, /statutory_tax_accrued/);
  assert.doesNotMatch(ACCOUNTING, /Statutory tax accrued/);
  assert.doesNotMatch(ACCOUNTING, /tax_accrued/);
  // The obfuscated hide-list and both of its call sites are gone.
  assert.doesNotMatch(ACCOUNTING, /atob\(/);
  assert.doesNotMatch(ACCOUNTING, /\bvar HIDE\b/);
  assert.doesNotMatch(ACCOUNTING, /hidden\(/);
});

test("no other dashboard page sends a tax setting either", () => {
  const dir = path.join(FRONTEND, "dashboard");
  const offenders = fs
    .readdirSync(dir)
    .filter((name) => name.endsWith(".html"))
    .filter((name) => {
      const body = fs.readFileSync(path.join(dir, name), "utf8");
      return /statutory_tax_enabled|statutory_tax_rate_percent|statutory_tax_basis/.test(body);
    });
  assert.deepEqual(offenders, [], `tax settings still referenced in: ${offenders.join(", ")}`);
});
