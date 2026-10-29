/* Purchase-order draft creation: the "Create draft does nothing" fix.
   Run from /frontend: node --test tests-js/test-procurement-draft-create-ui.js

   Unlike a source-pattern check, these tests EXECUTE the shipped page
   functions (sliced out of dashboard/procurement.html into a vm context with
   small stubs), so they fail if the behaviour regresses:

   * the hidden "pieces per pack" control accepts the item's own whole pack
     size (min 0.01 + step 1 rejected 24 — a hidden, silent stepMismatch);
   * one idempotency key per dialog, reused on every retry;
   * success only when the server returned the order's reference and status;
   * server errors become sentences that name the line and the item;
   * the order-total preview equals the server's Decimal arithmetic;
   * the supplier-payment dialog really sends the multipart body it built. */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { FRONTEND } = require("./harness");

const PAGE = fs.readFileSync(path.join(FRONTEND, "dashboard", "procurement.html"), "utf8");
const CONFIG = fs.readFileSync(path.join(FRONTEND, "js", "config.js"), "utf8");

function sliceFunction(name) {
  const marker = PAGE.indexOf("function " + name + "(");
  assert.ok(marker > 0, name + " is missing from the shipped page");
  const start = PAGE.lastIndexOf("async ", marker) === marker - 6 ? marker - 6 : marker;
  const open = PAGE.indexOf("{", marker);
  let depth = 0;
  for (let i = open; i < PAGE.length; i += 1) {
    if (PAGE[i] === "{") depth += 1;
    else if (PAGE[i] === "}") {
      depth -= 1;
      if (depth === 0) return PAGE.slice(start, i + 1);
    }
  }
  throw new Error("unbalanced braces in " + name);
}

function sliceVar(name) {
  const match = new RegExp("var " + name + "=[^\\n]*;").exec(PAGE);
  assert.ok(match, "var " + name + " is missing from the shipped page");
  return match[0];
}

function context(extra) {
  const toasts = [];
  const ctx = Object.assign({
    Math, String, Number, Error, Object, Array, JSON, RegExp, BigInt, Promise, setTimeout,
    toasts,
    window: { APP_CONFIG: { API_ENDPOINTS: {} } },
    JONE: {
      trimQuantityValue: (value) => String(Number(value)),
      formatMoney: (value) => "NGN " + value,
      formatQuantity: (value) => String(value),
      esc: (value) => String(value),
      ui: { toast: (message, kind) => toasts.push([kind, message]) },
      dashboard: {},
    },
    OPS: { omitEmpty: (values) => Object.assign({}, values) },
  }, extra || {});
  vm.createContext(ctx);
  return ctx;
}

function load(ctx, names, vars) {
  (vars || []).forEach((name) => vm.runInContext(sliceVar(name), ctx));
  names.forEach((name) => vm.runInContext(sliceFunction(name), ctx));
  return ctx;
}

/* The HTML number-input step rule: with a step other than "any", a value is
   valid only when (value - stepBase) / step is an integer; stepBase is min. */
function satisfiesStep(control, value) {
  if (control.step === "any") return Number(value) >= Number(control.min);
  const base = Number(control.min || 0), step = Number(control.step || 1);
  const ratio = (Number(value) - base) / step;
  return Number(value) >= base && Math.abs(ratio - Math.round(ratio)) < 1e-9;
}

/* ------------------------------ Root cause -------------------------------- */

test("the original pieces-per-pack constraint rejected a whole pack size (the dead button)", () => {
  // min 0.01 + step 1: the only valid values were 0.01, 1.01, 2.01, ...
  assert.equal(satisfiesStep({ min: "0.01", step: "1" }, "24"), false);
  assert.equal(satisfiesStep({ min: "0.01", step: "1" }, "24.01"), true);
});

test("the pieces-per-pack control now accepts every real pack size, hidden or shown", () => {
  const ctx = load(context(), ["packSizeOf", "piecesPerPackField"]);
  const field = ctx.piecesPerPackField();
  assert.equal(field.step, "any");
  for (const size of ["1", "6", "12", "24", "50", "144", "1000", "0.5", "12.5"]) {
    assert.ok(satisfiesStep(field, size), "pack size " + size + " must be valid");
  }
  // The default comes from the item, and only when it is a real pack size:
  // a "0.00" default would sit hidden below min and block the form again.
  assert.equal(field.defaultValue({ units_per_pack: "24.00" }), "24");
  assert.equal(field.defaultValue({ units_per_pack: "0.00" }), "");
  assert.equal(field.defaultValue({ units_per_pack: null }), "");
  assert.equal(field.defaultValue(null), "");
});

test("no purchase-order line control combines min and a step that excludes whole numbers", () => {
  const ctx = load(context(), ["packSizeOf", "piecesPerPackField", "quantityUnitField", "poLineFields", "trimmed", "baseUnitOf", "orderTotalText", "toScaled"]);
  ctx.money = (value) => "NGN " + value;
  for (const field of ctx.poLineFields()) {
    if (field.type !== "number") continue;
    for (const value of ["1", "24", "100"]) {
      assert.ok(satisfiesStep(field, value), field.name + " must accept " + value);
    }
  }
});

test("hidden invalid line controls can never fail silently again", () => {
  const lines = sliceFunction("orderLines");
  const net = sliceFunction("watchLineValidity");
  assert.match(lines, /watchLineValidity\(mount,form\)/);
  assert.match(net, /addEventListener\("invalid",function\(event\)\{/);
  assert.match(net, /\},true\);/, "the listener must capture (invalid events do not bubble)");
  assert.match(net, /wrap\.hidden=false/, "a hidden invalid control is revealed");
  assert.match(net, /control\.validationMessage/, "and its reason is shown under the lines field");
});

/* ------------------------------ Duplicates -------------------------------- */

test("one idempotency key per dialog: a retry after a timeout replays the same key", async () => {
  let minted = 0;
  const sent = [];
  let opts = null;
  let resolveModal;
  const ctx = context({
    orderFormFields: () => [],
    showOrderInList: (reference, status) => ctx.shown.push([reference, status]),
    shown: [],
    itemRecords: { 7: { id: 7, sku: "RICE" } },
  });
  ctx.OPS.newIdempotencyKey = (prefix) => prefix + "-key-" + (minted += 1);
  ctx.JONE.dashboard.formModal = (options) => { opts = options; return new Promise((resolve) => { resolveModal = resolve; }); };
  let attempt = 0;
  ctx.window.API = {
    create: async (resource, payload) => {
      sent.push(JSON.parse(JSON.stringify(payload)));
      attempt += 1;
      if (attempt === 1) {
        const err = new Error("The request timed out.");
        err.status = 0; err.kind = "timeout";
        throw err;
      }
      return { status: 200, data: { reference: "PO-20261029-AAAA", status: "DRAFT" } };
    },
  };
  load(ctx, ["newOrder", "dialogHandle", "busyLabel", "confirmedOrder", "saveOrder", "explainOrderError",
    "errorStrings", "lineErrorMessages", "linesToBaseUnits", "trimmed"], ["LINE_FIELD_LABELS"]);

  const running = ctx.newOrder();
  const values = { supplier_id: 3, delivery_location_id: 4, lines: [{ item_id: 7, quantity_unit: "PIECE", quantity_ordered: "2", unit_cost: "65000" }] };
  await assert.rejects(opts.onSubmit(values), /timed out[\s\S]*press “Create draft” again/);
  const outcome = await opts.onSubmit(values);
  resolveModal(outcome);
  await running;

  assert.equal(minted, 1, "the key is minted once, when the dialog opens");
  assert.equal(sent.length, 2);
  assert.equal(sent[0].idempotency_key, "purchase-order-key-1");
  assert.equal(sent[1].idempotency_key, sent[0].idempotency_key);
  assert.deepEqual(sent[0].lines, [{ item_id: 7, quantity_ordered: "2", unit_cost: "65000", notes: "" }]);
  // A 200 is the server returning the draft it already had: say so.
  assert.match(ctx.toasts.at(-1)[1], /already saved as PO-20261029-AAAA\. No duplicate/);
  assert.deepEqual(JSON.parse(JSON.stringify(ctx.shown)), [["PO-20261029-AAAA", "DRAFT"]]);
});

test("the key is created outside onSubmit (shipped source)", () => {
  const create = sliceFunction("newOrder");
  const submit = create.slice(create.indexOf("onSubmit:"));
  assert.match(create, /draftContext\.idempotency_key=OPS\.newIdempotencyKey\("purchase-order"\)/);
  assert.doesNotMatch(submit, /newIdempotencyKey/);
  assert.match(submit, /payload\.idempotency_key=draftContext\.idempotency_key/);
});

/* ---------------------------- Honest outcomes ----------------------------- */

test("success is reported only when the server confirmed a reference and status", () => {
  const ctx = load(context(), ["confirmedOrder"]);
  assert.throws(() => ctx.confirmedOrder({ status: 200, data: "<html>proxy page</html>" }), /did not confirm/);
  assert.throws(() => ctx.confirmedOrder({ status: 201, data: {} }), /did not confirm/);
  assert.throws(() => ctx.confirmedOrder({ status: 201, data: { reference: "PO-1" } }), /did not confirm/);
  assert.throws(() => ctx.confirmedOrder(undefined), /did not confirm/);
  const made = ctx.confirmedOrder({ status: 201, data: { reference: "PO-1", status: "DRAFT" } });
  assert.equal(made.reference, "PO-1");
  assert.equal(made.status, "DRAFT");
  assert.equal(made.replayed, false);
  assert.equal(ctx.confirmedOrder({ status: 200, data: { reference: "PO-1", status: "DRAFT" } }).replayed, true);
});

test("a nested line error names the line and the item instead of [object Object]", () => {
  const ctx = load(context({ itemRecords: { 9: { id: 9, sku: "WATER-75CL" } } }),
    ["explainOrderError", "errorStrings", "lineErrorMessages"], ["LINE_FIELD_LABELS"]);
  const err = { status: 400, message: "Validation failed.", data: { errors: {
    lines: { 1: { quantity_ordered: ["Ensure that there are no more than 10 digits before the decimal point."] } },
  } } };
  ctx.explainOrderError(err, [{ item_id: 4, record: { sku: "RICE" } }, { item_id: 9 }]);
  assert.deepEqual(JSON.parse(JSON.stringify(err.data.errors.lines)), [
    "Line 2 (WATER-75CL) — Quantity (in pieces): Ensure that there are no more than 10 digits before the decimal point.",
  ]);
  // The older list shape and the list-level message shape are handled too.
  const listShape = { status: 400, data: { errors: { lines: [{}, { unit_cost: ["Ensure this value is greater than or equal to 0."] }] } } };
  ctx.explainOrderError(listShape, [{ item_id: 4, record: { sku: "RICE" } }, { item_id: 9 }]);
  assert.deepEqual(JSON.parse(JSON.stringify(listShape.data.errors.lines)), [
    "Line 2 (WATER-75CL) — Unit cost per piece: Ensure this value is greater than or equal to 0.",
  ]);
  const plain = { status: 400, data: { errors: { lines: ["An item may appear only once per purchase order: RICE is on more than one line."] } } };
  ctx.explainOrderError(plain, []);
  assert.deepEqual(JSON.parse(JSON.stringify(plain.data.errors.lines)), ["An item may appear only once per purchase order: RICE is on more than one line."]);
});

test("business-rule, permission and connection failures stay actionable", () => {
  const ctx = load(context(), ["explainOrderError", "errorStrings", "lineErrorMessages"], ["LINE_FIELD_LABELS"]);
  const rule = { status: 400, message: "Validation failed.", data: { errors: ["Could not allocate a unique purchase-order reference; please retry."] } };
  ctx.explainOrderError(rule);
  assert.deepEqual(JSON.parse(JSON.stringify(rule.data.errors)), { non_field_errors: ["Could not allocate a unique purchase-order reference; please retry."] });

  const denied = { status: 403, message: "You do not have the required permission for this action.", data: {} };
  ctx.explainOrderError(denied);
  assert.match(denied.message, /procurement access/);
  const reasoned = { status: 403, message: "A requester cannot approve their own purchase order.", data: {} };
  ctx.explainOrderError(reasoned);
  assert.equal(reasoned.message, "A requester cannot approve their own purchase order.", "a specific server reason is kept");

  const offline = { status: 0, kind: "network", message: "We couldn't reach our servers." };
  ctx.explainOrderError(offline, [], "Press again; it is safe.");
  assert.equal(offline.message, "We couldn't reach our servers. Press again; it is safe.");
  const crashed = { status: 500, message: "We're having trouble completing this request." };
  ctx.explainOrderError(crashed, [], "Press again; it is safe.");
  assert.match(crashed.message, /Press again; it is safe\.$/);
});

/* --------------------------- Costs and units ------------------------------ */

test("the order-total preview uses the server's exact Decimal arithmetic", () => {
  const ctx = load(context(), ["orderTotalText", "toScaled"]);
  // 2 packs x NGN 1,000 (24 per pack) are stored as 48 x 41.67.
  assert.equal(ctx.orderTotalText([{ quantity_ordered: "48", unit_cost: "41.67" }]), "2000.16");
  // Rounded half-up ONCE at the order level, like PurchaseOrder.total_amount.
  assert.equal(ctx.orderTotalText([
    { quantity_ordered: "0.3333", unit_cost: "0.15" },
    { quantity_ordered: "1", unit_cost: "0.01" },
  ]), "0.06");
  assert.equal(ctx.orderTotalText([{ quantity_ordered: "0.1", unit_cost: "0.2" }, { quantity_ordered: "0.2", unit_cost: "0.1" }]), "0.04");
  assert.equal(ctx.orderTotalText([{ quantity_ordered: "9999999999.9999", unit_cost: "9999999999.99" }]), "99999999999899000000.00");
  assert.equal(ctx.orderTotalText([]), "0.00");
  assert.equal(ctx.orderTotalText([{ quantity_ordered: "1e-7", unit_cost: "1" }]), null);
  assert.equal(ctx.orderTotalText([{ quantity_ordered: "1.00001", unit_cost: "1" }]), null);
});

test("lines resolve SKU and pack size from the records the picker displayed", () => {
  const ctx = load(context(), ["linesToBaseUnits", "trimmed"]);
  const records = { 8: { id: 8, sku: "WATER", units_per_pack: "24.00" } };
  const converted = ctx.linesToBaseUnits([
    { item_id: 8, quantity_unit: "PACK", pieces_per_pack: "", quantity_ordered: "2", unit_cost: "2400" },
  ], records);
  assert.equal(JSON.stringify(converted), JSON.stringify([{ item_id: 8, quantity_ordered: "48", unit_cost: "100", notes: "" }]));
  assert.throws(() => ctx.linesToBaseUnits([{ item_id: 8, quantity_unit: "PIECE", quantity_ordered: "0", unit_cost: "1" }], records),
    /Enter a quantity for WATER/);
});

test("pack pricing discloses the per-piece rounding it causes", () => {
  const ctx = load(context(), ["packSizeOf", "piecesPerPackField", "quantityUnitField", "poLineFields", "trimmed", "baseUnitOf", "orderTotalText", "toScaled"]);
  ctx.money = (value) => "NGN " + value;
  const cost = ctx.poLineFields().find((field) => field.name === "unit_cost");
  const hint = cost.hint({ base_unit: "BOTTLE" }, { quantity_unit: "PACK", pieces_per_pack: "24", quantity_ordered: "2", unit_cost: "1000" });
  assert.match(hint, /= NGN 41\.67 per BOTTLE/);
  assert.match(hint, /Recorded line total NGN 2000\.16 \(quoted NGN 2000\.00\)/);
  const exact = cost.hint({ base_unit: "BOTTLE" }, { quantity_unit: "PACK", pieces_per_pack: "24", quantity_ordered: "2", unit_cost: "2400" });
  assert.doesNotMatch(exact, /Recorded line total/);
  assert.match(cost.hint({ base_unit: "BAG" }, { quantity_unit: "PIECE", unit_cost: "0" }), /free of charge/);
});

/* ------------------------------- Lifecycle -------------------------------- */

test("the next permitted step follows the stored status and the derived progress", () => {
  const ctx = load(context(), ["nextStep"]);
  assert.equal(ctx.nextStep({ status: "DRAFT" }), "Next: submit for approval");
  assert.match(ctx.nextStep({ status: "SUBMITTED" }), /approval/);
  assert.match(ctx.nextStep({ status: "ORDERED" }), /goods receipt/);
  assert.match(ctx.nextStep({ status: "RECEIVED", payment_progress: "UNPAID" }), /supplier payment/);
  assert.equal(ctx.nextStep({ status: "RECEIVED", payment_progress: "PAID" }), "Received");
  assert.match(ctx.nextStep({ status: "PAID", receipt_progress: "NOT_RECEIVED" }), /goods still to be received/);
  assert.equal(ctx.nextStep({ status: "PAID", receipt_progress: "RECEIVED" }), "Complete");
  assert.match(ctx.nextStep({ status: "CANCELLED" }), /Cancelled/);
});

test("approvers can reject and procurement can cancel, each with a reason", () => {
  assert.match(PAGE, /if\(order\.status==="SUBMITTED"&&manager\(\)\)action\("Reject"/);
  assert.match(PAGE, /if\(canCancel\(order\.status\)\)action\("Cancel order"/);
  assert.match(sliceFunction("rejectOrder"), /resourceAction\("purchaseOrders",order\.reference,"reject",\{reason:/);
  assert.match(sliceFunction("cancelOrder"), /resourceAction\("purchaseOrders",order\.reference,"cancel",\{reason:/);
  const ctx = load(context(), ["canCancel"]);
  assert.deepEqual(["DRAFT", "SUBMITTED", "APPROVED", "PAID", "CANCELLED"].map(ctx.canCancel), [true, true, false, false, false]);
});

test("the form searches the procurement lookups, with a safe fallback", () => {
  assert.match(CONFIG, /procurementLocations:\s*"\/api\/admin\/inventory\/procurement\/locations\/"/);
  assert.match(CONFIG, /procurementItems:\s*"\/api\/admin\/inventory\/procurement\/items\/"/);
  assert.match(CONFIG, /purchaseOrderAccounting:\s*"\/api\/admin\/inventory\/accounting\/purchase-orders\/"/);
  const ctx = load(context(), ["resourceOr"]);
  assert.equal(ctx.resourceOr("procurementItems", "inventoryItems"), "inventoryItems", "stale cached config.js");
  ctx.window.APP_CONFIG.API_ENDPOINTS.procurementItems = "/api/admin/inventory/procurement/items/";
  assert.equal(ctx.resourceOr("procurementItems", "inventoryItems"), "procurementItems");
  assert.match(sliceFunction("locationPicker"), /resourceOr\("procurementLocations","inventoryLocations"\)/);
  assert.match(sliceFunction("orderLines"), /resourceOr\("procurementItems","inventoryItems"\)/);
});

/* ---------------------------- Supplier payment ---------------------------- */

test("the payment dialog sends the multipart body it built, prefilled with the balance", async () => {
  let opts = null;
  const calls = [];
  const ctx = context({ loadOrders: () => {}, money: (value) => "NGN " + value, getOrder: async () => ({}) });
  ctx.JONE.dashboard.formModal = async (options) => { opts = options; return null; };
  ctx.window.API = { resourceAction: async (...args) => { calls.push(args); return { data: {} }; } };
  load(ctx, ["recordPayment"]);
  await ctx.recordPayment("PO-9", { reference: "PO-9", total_amount: "130000.00", outstanding_amount: "130000.00", currency: "NGN" });
  assert.equal(opts.values.amount, "130000", "formModal prefills from values, so the balance must be there");
  const body = { has: () => true, marker: "multipart-body" };
  await opts.onSubmit({ amount: "1000", method: "CASH", reference: "" }, body);
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0].slice(0, 3), ["purchaseOrders", "PO-9", "pay"]);
  assert.equal(calls[0][3], body, "the FormData formModal built is what gets sent");
  await assert.rejects(opts.onSubmit({ amount: "-5", method: "CASH" }, body), /greater than zero/);
  await assert.rejects(opts.onSubmit({ amount: "200000", method: "CASH" }, body), /cannot exceed/);
  await assert.rejects(opts.onSubmit({ amount: "10", method: "BANK_TRANSFER", reference: " " }, body), /reference/);
  assert.equal(calls.length, 1);
  assert.doesNotMatch(sliceFunction("recordPayment"), /JONE\.esc\(/, "text contexts must not be HTML-escaped");
});
