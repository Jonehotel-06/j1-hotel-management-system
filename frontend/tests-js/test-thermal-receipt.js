/* tests-js/test-thermal-receipt.js */
/* 58 mm / 80 mm thermal receipt layouts (POS printing, tax-free receipts). */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { makeContext, loadScript, disposeAll } = require("./harness");

test.after(() => disposeAll());

function boot(width) {
  const ctx = makeContext();
  loadScript(ctx, "js/thermal-receipt.js");
  const T = ctx.window.JONE.ThermalReceipt;
  if (width) T.setWidth(width);
  return { ctx, T };
}

const RECEIPT = {
  hotel: {
    name: "J-ONE HOTEL & LODGE",
    address: "Plot 566 Mgbowo Street, off Ezike Street, Enugu State",
    phone: "+234 803 211 2874",
  },
  reference: "POS-20261011-ABC123",
  issued_at: "2026-10-11 14:32",
  cashier: "Demo Cashier (DEMO)",
  workstation: "Front Desk Terminal 1",
  mode: "RESTAURANT",
  items: [
    { name: "Jollof Rice & Chicken", quantity: 2, unit_price: 2500, line_total: 5000 },
    {
      name: "Edikang Ikong & Pounded Yam with Extra Assorted Meat Platter Special",
      quantity: 1, unit_price: 3200, line_total: 3200,
    },
  ],
  subtotal: 8200,
  discount: 500,
  tax: 0,
  total: 7700,
  paid: 10000,
  change: 2300,
  balance_due: 0,
  payment_method: "CASH",
  payment_reference: "PAY-20261011-9F2A",
};

test("paper widths are configurable at 58 mm (32 cols) and 80 mm (48 cols)", () => {
  const { T } = boot();
  assert.deepEqual(Object.keys(T.PAPER).sort(), ["58", "80"]);
  assert.equal(T.PAPER["58"].chars, 32);
  assert.equal(T.PAPER["80"].chars, 48);
  assert.equal(T.PAPER["58"].mm, 58);
  assert.equal(T.PAPER["80"].mm, 80);
  assert.equal(T.getWidth(), "80", "default is 80 mm");
  assert.equal(T.setWidth("58"), "58");
  assert.equal(T.getWidth(), "58", "58 mm preference persists");
  assert.throws(() => T.setWidth("76"), /Unsupported/);
});

test("lines never exceed the paper width at either size (no clipping)", () => {
  for (const width of ["58", "80"]) {
    const { T } = boot(width);
    const chars = T.PAPER[width].chars;
    for (const line of T.renderLines(RECEIPT, width)) {
      assert.ok(line.length <= chars, `line too wide for ${width}mm: ${JSON.stringify(line)}`);
    }
  }
});

test("money columns are right-aligned to the exact paper width", () => {
  const { T } = boot("58");
  const lines = T.renderLines(RECEIPT, "58");
  const subtotal = lines.find((l) => l.startsWith("Subtotal"));
  const total = lines.find((l) => l.startsWith("TOTAL"));
  const paid = lines.find((l) => l.startsWith("Paid"));
  assert.equal(subtotal.length, 32);
  assert.ok(subtotal.endsWith(T.naira(8200)));
  assert.equal(total.length, 32);
  assert.ok(total.endsWith(T.naira(7700)));
  assert.equal(paid.length, 32);
  assert.ok(paid.endsWith(T.naira(10000)));
});

test("long item names wrap above their amounts instead of clipping", () => {
  const { T } = boot("58");
  const rows = T.itemRows(RECEIPT.items[1], 32);
  assert.ok(rows.length >= 2, "long name wraps");
  assert.ok(rows[rows.length - 1].trim().endsWith(T.naira(3200)), "amount line intact");
  for (const row of rows) assert.ok(row.length <= 32);
  assert.ok(rows.slice(0, -1).join(" ").includes("Edikang Ikong"));
});

test("new customer receipts hide the tax line entirely", () => {
  for (const width of ["58", "80"]) {
    const { T } = boot(width);
    const lines = T.renderLines({ ...RECEIPT, tax: 0 }, width);
    assert.ok(!lines.some((l) => /tax/i.test(l)), `no tax line on a ${width}mm receipt`);
  }
});

test("historical receipts keep their original tax breakdown", () => {
  const { T } = boot("80");
  const lines = T.renderLines({ ...RECEIPT, tax: 615, total: 8315 }, "80");
  const taxLine = lines.find((l) => l.startsWith("Tax (as charged)"));
  assert.ok(taxLine, "historical tax still printed");
  assert.ok(taxLine.endsWith(T.naira(615)));
});

test("discounts, totals, change and payment references are all present", () => {
  const { T } = boot("80");
  const lines = T.renderLines(RECEIPT, "80");
  const joined = lines.join("\n");
  assert.ok(lines.some((l) => l.startsWith("Discount") && l.endsWith("-" + T.naira(500))));
  assert.ok(joined.includes("Receipt: POS-20261011-ABC123"));
  assert.ok(joined.includes("Ref: PAY-20261011-9F2A"));
  assert.ok(joined.includes("Method: CASH"));
  assert.ok(lines.some((l) => l.startsWith("Change") && l.endsWith(T.naira(2300))));
});

test("HTML print sheet declares the paper size in millimetres", () => {
  const { T } = boot("58");
  const html = T.renderHTML(RECEIPT, "58");
  assert.match(html, /data-paper-mm="58"/);
  assert.match(html, /thermal-58/);
  const html80 = T.renderHTML(RECEIPT, "80");
  assert.match(html80, /data-paper-mm="80"/);
});
