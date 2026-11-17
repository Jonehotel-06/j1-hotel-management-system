/* tests-js/test-minimart-catalogue-forms.js

   Reported defects this file locks down
   -------------------------------------
   · "A database error prevented this request. Please try again." across the
     Mini Mart fields (the backend answered 503 for an unapplied migration; the
     UI fix here is that a real API failure is REPORTED as a failure).
   · The Add Category and Add Product forms appeared automatically, without
     their buttons being pressed.
   · A newly created category did not appear in the product form.

   Root cause proven here for the last one: the catalogue endpoints return
   `{success, message, data: [...], pagination}`. `API.list` strips the
   envelope, so the payload is an array — but `rows()` read `.results` first,
   which is `undefined` for an array. Categories were therefore never added to
   the product/category dropdowns even though the create had succeeded.

   The real page markup (dashboard/minimart.html) and the real script
   (js/minimart-admin.js) are used unmodified.

   Run: node --test tests-js/test-minimart-catalogue-forms.js
*/
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { FRONTEND } = require("./harness");

let JSDOM = null;
try {
  ({ JSDOM } = require(path.join(FRONTEND, "node_modules", "jsdom")));
} catch (_) {
  JSDOM = null;
}
const SKIP = JSDOM ? false : "jsdom is not installed (npm i jsdom --no-save)";

/* The shipped page markup, with scripts stripped (they are loaded explicitly). */
function pageMarkup() {
  return fs
    .readFileSync(path.join(FRONTEND, "dashboard/minimart.html"), "utf8")
    .replace(/<script[^>]*src=[^>]*><\/script>/g, "")
    .replace(/<script>[\s\S]*?<\/script>/g, "");
}

const CATEGORIES = [
  { id: 1, name: "Beverages", description: "Drinks", is_active: true, product_count: 2 },
  { id: 2, name: "Snacks", description: "", is_active: false, product_count: 0 },
];
const PRODUCTS = [
  {
    id: 5, stock_item_id: 50, name: "Cola 50cl", sku: "MM-COLA", barcode: "",
    description: "", category_id: 1, category: "Beverages", purchase_cost: "300.00",
    selling_price: "500.00", base_unit: "BOTTLE", base_unit_label: "Bottles",
    stock_quantity: "10.0000", reorder_level: "2.0000", location_id: 1,
    location: "Main Store", location_name: "Main Store", is_active: true, status: "ACTIVE",
  },
];
const UNITS = [{ id: 1, code: "BOTTLE", label: "Bottles", is_active: true }];
const LOCATIONS = [{ id: 1, code: "MAIN", name: "Main Store", is_active: true }];

function boot(opts = {}) {
  const dom = new JSDOM(pageMarkup(), {
    url: "https://staff.jonehotels.com/dashboard/minimart.html",
    runScripts: "outside-only",
  });
  const { window } = dom;
  const created = [];
  const updated = [];
  let categories = opts.categories || CATEGORIES;
  let products = opts.products || PRODUCTS;

  window.API = {
    list: async (name) => {
      if (opts.failList && name === opts.failList) {
        const err = new Error("The database is temporarily unavailable. Please try again.");
        err.status = 503;
        err.data = { code: "DATABASE_UNAVAILABLE" };
        throw err;
      }
      const data =
        name === "minimartCategories" ? categories
        : name === "minimartAdminProducts" ? products
        : name === "inventoryUnits" ? UNITS
        : name === "inventoryLocations" ? LOCATIONS
        : [];
      return { status: 200, ok: true, data };
    },
    create: async (name, payload) => {
      created.push([name, payload]);
      if (name === "minimartCategories") {
        const row = { id: 99, is_active: true, product_count: 0, ...payload };
        categories = categories.concat([row]);
        return { status: 201, ok: true, data: row };
      }
      return { status: 201, ok: true, data: { id: 100, ...payload } };
    },
    update: async (name, id, payload) => {
      updated.push([name, id, payload]);
      return { status: 200, ok: true, data: { id, ...payload } };
    },
  };
  window.JONE = {
    esc: (v) => String(v == null ? "" : v),
    ui: { toast: () => {} },
    formatNaira: (v) => "₦" + v,
  };
  window.Auth = { state: { user: { role: opts.role || "ADMIN" } } };
  window.eval(fs.readFileSync(path.join(FRONTEND, "js/minimart-admin.js"), "utf8"));
  return { window, doc: window.document, created, updated };
}

const settle = (ms = 250) => new Promise((resolve) => setTimeout(resolve, ms));
const visible = (el) => !!el && el.hidden === false;

test("both catalogue forms start hidden and the listing is shown", { skip: SKIP }, async () => {
  const { doc } = boot();
  await settle();
  assert.ok(!doc.querySelector("[data-mart-admin]").hidden, "catalogue section shown to admin");
  assert.ok(!visible(doc.querySelector("[data-mart-admin-category-panel]")), "category form hidden");
  assert.ok(!visible(doc.querySelector("[data-mart-admin-product-panel]")), "product form hidden");
  assert.ok(doc.querySelector("[data-mart-admin-categories]").innerHTML.includes("Beverages"), "categories listed");
  assert.ok(doc.querySelector("[data-mart-admin-products]").innerHTML.includes("Cola 50cl"), "products listed");
});

test("Add Category opens only the category form; Add Product closes it", { skip: SKIP }, async () => {
  const { doc } = boot();
  await settle();
  const cPanel = doc.querySelector("[data-mart-admin-category-panel]");
  const pPanel = doc.querySelector("[data-mart-admin-product-panel]");

  doc.querySelector("[data-mart-admin-add-category]").click();
  await settle(50);
  assert.ok(visible(cPanel), "category form opened");
  assert.ok(!visible(pPanel), "product form still hidden");

  doc.querySelector("[data-mart-admin-add-product]").click();
  await settle(50);
  assert.ok(visible(pPanel), "product form opened");
  assert.ok(!visible(cPanel), "category form closed");
});

test("Cancel hides the form without submitting anything", { skip: SKIP }, async () => {
  const { doc, created } = boot();
  await settle();
  doc.querySelector("[data-mart-admin-add-category]").click();
  await settle(50);
  const form = doc.querySelector("[data-mart-admin-category-form]");
  form.elements.name.value = "Abandoned";
  doc.querySelector("[data-mart-admin-cancel-category]").click();
  await settle(50);
  assert.ok(!visible(doc.querySelector("[data-mart-admin-category-panel]")), "form hidden");
  assert.equal(created.length, 0, "nothing was submitted");
});

test("saving a category closes the form and refreshes the listing and dropdown", { skip: SKIP }, async () => {
  const { doc, window, created } = boot();
  await settle();
  doc.querySelector("[data-mart-admin-add-category]").click();
  await settle(50);
  const form = doc.querySelector("[data-mart-admin-category-form]");
  form.elements.name.value = "Confectionery";
  form.elements.description.value = "Sweets";
  form.dispatchEvent(new window.Event("submit", { cancelable: true, bubbles: true }));
  await settle();

  assert.equal(created.length, 1, "one create call");
  assert.equal(created[0][1].name, "Confectionery");
  assert.ok(!visible(doc.querySelector("[data-mart-admin-category-panel]")), "form closed on success");
  const listing = doc.querySelector("[data-mart-admin-categories]").innerHTML;
  assert.ok(listing.includes("Confectionery"), "new category listed immediately");
  const selector = doc.querySelector("#mart-admin-category");
  const options = Array.from(selector.children).map((o) => o.textContent);
  assert.ok(options.includes("Confectionery"), "new category selectable in the product form: " + options);
});

test("a validation error keeps the form open and marks the field", { skip: SKIP }, async () => {
  const { doc, window } = boot();
  await settle();
  // Replace create with a 400 carrying per-field errors.
  window.API.create = async () => {
    const err = new Error("Validation failed.");
    err.status = 400;
    err.data = { errors: { name: ["Beverages already exists."] } };
    throw err;
  };
  doc.querySelector("[data-mart-admin-add-category]").click();
  await settle(50);
  const form = doc.querySelector("[data-mart-admin-category-form]");
  form.elements.name.value = "Beverages";
  form.dispatchEvent(new window.Event("submit", { cancelable: true, bubbles: true }));
  await settle();
  assert.ok(visible(doc.querySelector("[data-mart-admin-category-panel]")), "form stays open");
  assert.equal(form.elements.name.getAttribute("aria-invalid"), "true", "offending field marked");
});

test("saving a product posts the real fields and closes the form", { skip: SKIP }, async () => {
  const { doc, window, created } = boot();
  await settle();
  doc.querySelector("[data-mart-admin-add-product]").click();
  await settle(50);
  const form = doc.querySelector("[data-mart-admin-product-form]");
  form.elements.name.value = "Fanta 50cl";
  form.elements.sku.value = "MM-FANTA";
  form.elements.purchase_cost.value = "300";
  form.elements.selling_price.value = "500";
  form.elements.base_unit.value = "BOTTLE";
  form.elements.location_id.value = "1";
  form.elements.category_id.value = "1";
  form.elements.opening_quantity.value = "12";
  form.dispatchEvent(new window.Event("submit", { cancelable: true, bubbles: true }));
  await settle();

  const call = created.find((entry) => entry[0] === "minimartAdminProducts");
  assert.ok(call, "product create called");
  assert.equal(call[1].sku, "MM-FANTA");
  assert.equal(call[1].location_id, "1");
  assert.equal(call[1].category_id, "1");
  assert.ok(call[1].idempotency_key, "idempotency key prevents a duplicate product");
  assert.ok(!visible(doc.querySelector("[data-mart-admin-product-panel]")), "form closed on success");
});

test("editing a product opens the form prefilled and PATCHes only that product", { skip: SKIP }, async () => {
  const { doc, window, updated } = boot();
  await settle();
  const edit = doc.querySelector("[data-mart-admin-edit-product]");
  assert.ok(edit, "edit control rendered");
  edit.click();
  await settle(50);
  const form = doc.querySelector("[data-mart-admin-product-form]");
  assert.ok(visible(doc.querySelector("[data-mart-admin-product-panel]")), "form opened");
  assert.equal(form.elements.name.value, "Cola 50cl", "prefilled");
  assert.equal(form.elements.opening_quantity.disabled, true, "opening stock locked when editing");

  form.elements.selling_price.value = "550";
  form.dispatchEvent(new window.Event("submit", { cancelable: true, bubbles: true }));
  await settle();
  const call = updated.find((entry) => entry[0] === "minimartAdminProducts");
  assert.ok(call, "update called");
  // The id travels through a data-* attribute, so it is a string on the wire.
  assert.equal(String(call[1]), "5", "right product id");
  assert.equal(call[2].selling_price, "550");
});

test("deactivating a category PATCHes is_active instead of deleting", { skip: SKIP }, async () => {
  const { doc, updated } = boot();
  await settle();
  const toggle = doc.querySelector('[data-mart-admin-toggle-category][data-active="false"]');
  assert.ok(toggle, "deactivate control rendered for the active category");
  toggle.click();
  await settle();
  const call = updated.find((entry) => entry[0] === "minimartCategories");
  assert.ok(call, "update called");
  assert.equal(call[2].is_active, false);
});

test("a database outage is reported to the user, never rendered as success", { skip: SKIP }, async () => {
  const { doc } = boot({ failList: "minimartCategories" });
  await settle();
  const box = doc.querySelector("[data-mart-admin-message]");
  assert.ok(/temporarily unavailable|database/i.test(box.innerHTML), "failure surfaced: " + box.innerHTML);
  assert.ok(!box.innerHTML.includes("alert-success"));
});

test("a receptionist does not get the catalogue management section", { skip: SKIP }, async () => {
  const { doc } = boot({ role: "RECEPTIONIST" });
  await settle();
  assert.ok(doc.querySelector("[data-mart-admin]").hidden, "section hidden for a receptionist");
});
