/* A3 — the accounting purchase-order review carries the review material.
 *
 * Critical repair A3: the accountant's PO detail must show the request date,
 * the procurement justification/notes and the supporting documents (approval
 * evidence + every recorded supplier payment with its evidence and its A4
 * accounting settlement link), and the VERIFIED supplier purchase-order
 * reference must be recordable from the accounting side — Procurement can
 * only edit it while the order is still a draft or submitted.
 */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const html = fs.readFileSync(path.join(__dirname, "..", "dashboard", "accounting.html"), "utf8");

test("A3: the PO detail shows the request date and the procurement notes", () => {
  assert.ok(html.includes('expenseDetailRow("Requested on"'), "request date row is rendered");
  assert.ok(html.includes("Procurement notes / justification"), "notes/justification section is rendered");
  assert.ok(/po\.notes/.test(html), "the section reads the notes from the detail payload");
});

test("A3: the PO detail shows the supporting documents", () => {
  assert.ok(html.includes("Supporting documents"), "documents section is rendered");
  assert.ok(html.includes("po.approval_receipt_url"), "the approval evidence link is rendered");
  assert.ok(html.includes("Supplier payments recorded by Procurement"), "procurement payments are rendered");
  assert.ok(html.includes("payment.evidence_url"), "each payment links its own evidence");
  // The A4 settlement link travels with each payment row.
  assert.ok(html.includes("payment.accounting_settlement_reference"), "each payment shows its accounting settlement link");
});

test("A3: the verified supplier reference is recordable from accounting", () => {
  assert.ok(html.includes("data-po-supplier-reference"), "the detail offers the reference edit");
  assert.ok(html.includes("setVerifiedSupplierReference"), "the edit handler exists");
  // It posts to the accounting hand-off endpoint and touches nothing else.
  assert.ok(
    html.includes('"/supplier-reference/"'),
    "the handler posts to the supplier-reference endpoint"
  );
  assert.ok(
    html.includes('{ supplier_reference: String(values.supplier_reference || "").trim() }'),
    "only the supplier reference is submitted"
  );
  // The dialog is delegated, not bound per render (no duplicate handlers).
  const guard = html.indexOf("__delegatedBound");
  assert.ok(guard > 0, "delegated listeners use the bound-once guard");
  assert.ok(
    html.indexOf('closest("[data-po-supplier-reference]")') > guard,
    "the reference edit lives inside the bound-once block"
  );
});
