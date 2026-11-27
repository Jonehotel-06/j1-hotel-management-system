/* Mini Mart catalogue management: categories and inventory-backed products.
   Sales, stock depletion, POS evidence and finance posting still happen only
   in the server's existing checkout service. This UI edits master data only. */
(function () {
  "use strict";
  var API = window.API;
  var esc = (window.JONE && window.JONE.esc) || function (value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  };
  var $ = function (selector) { return document.querySelector(selector); };
  var section, categories = [], units = [], locations = [], products = [];

  /* Payload -> array of records.
     The list endpoints answer { success, message, data: [...], pagination } so
     after unwrapping the envelope the payload is ALREADY an array. Reading
     `.results` first built every category list and both product/category
     dropdowns from `undefined`, so a correctly-saved category never appeared
     in the product form. */
  function rows(response) {
    var data = response && response.data !== undefined ? response.data : response;
    if (Array.isArray(data)) return data;
    if (data && Array.isArray(data.results)) return data.results;
    if (data && Array.isArray(data.items)) return data.items;
    if (response && Array.isArray(response.results)) return response.results;
    return [];
  }
  function debounce(fn, wait) {
    var timer = null;
    return function () {
      var args = arguments, scope = this;
      clearTimeout(timer);
      timer = setTimeout(function () { fn.apply(scope, args); }, wait || 250);
    };
  }
  function key() {
    return (window.crypto && window.crypto.randomUUID)
      ? window.crypto.randomUUID()
      : "mm-product-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 12);
  }
  function message(text, failed) {
    var box = $("[data-mart-admin-message]");
    if (box) box.innerHTML = text ? '<div class="alert' + (failed ? " alert-error" : "") + '" role="status">' + esc(text) + "</div>" : "";
  }
  function toast(text, failed) {
    if (window.JONE && window.JONE.ui && window.JONE.ui.toast) window.JONE.ui.toast(text, failed ? "error" : "success");
    message(text, failed);
  }
  function allowed() {
    var user = window.Auth && window.Auth.state && window.Auth.state.user;
    var role = String((user && user.role) || "").toUpperCase();
    return ["ADMIN", "GENERAL_MANAGER", "MANAGER", "STOREKEEPER", "INVENTORY_CLERK"].indexOf(role) !== -1;
  }
  function setOptions(select, values, placeholder, selected) {
    if (!select) return;
    select.innerHTML = '<option value="">' + esc(placeholder) + "</option>";
    values.forEach(function (row) { select.add(new Option(row.label, String(row.value))); });
    if (selected != null) select.value = String(selected);
  }
  function displayList(box, items, emptyText, render) {
    if (!box) return;
    box.innerHTML = items.length ? items.map(render).join("") : '<p class="text-muted">' + esc(emptyText) + "</p>";
  }
  async function loadOptions() {
    if (!API || !API.list) return;
    try {
      var [unitResponse, locationResponse] = await Promise.all([
        API.list("inventoryUnits", { active: "true", page_size: 200 }),
        API.list("inventoryLocations", { active: "true", page_size: 200 }),
      ]);
      units = rows(unitResponse).map(function (u) { return { value: u.code, label: (u.label || u.code) + " (" + u.code + ")" }; });
      locations = rows(locationResponse).map(function (l) { return { value: l.id, label: (l.code || "") + " · " + (l.name || "") }; });
      setOptions($("#mart-admin-base-unit"), units, "Select a configured unit", "");
      setOptions($("#mart-admin-opening-unit"), units, "Use the item's base unit", "");
      setOptions($("#mart-admin-location"), locations, "Select an active store", "");
    } catch (err) { message((err && err.message) || "Catalogue options could not be loaded.", true); }
  }
  async function loadCategories() {
    var search = $("#mart-admin-category-search");
    var params = { page_size: 200 };
    if (search && search.value.trim()) params.search = search.value.trim();
    try {
      var response = await API.list("minimartCategories", params);
      categories = rows(response);
      setOptions($("#mart-admin-category"), categories.filter(function (c) { return c.is_active; }).map(function (c) {
        return { value: c.id, label: c.name };
      }), "No category", $("#mart-admin-category") && $("#mart-admin-category").value);
      setOptions($("#mart-admin-category-filter"), categories.map(function (c) {
        return { value: c.id, label: c.name };
      }), "All categories", $("#mart-admin-category-filter") && $("#mart-admin-category-filter").value);
      displayList($("[data-mart-admin-categories]"), categories, "No Mini Mart categories configured.", function (c) {
        return '<div class="service-context"><strong>' + esc(c.name) + "</strong> · " +
          esc(c.product_count || 0) + " product(s) · " + esc(c.is_active ? "Active" : "Inactive") +
          (c.description ? '<div class="caption">' + esc(c.description) + "</div>" : "") +
          '<div class="form-actions"><button type="button" class="btn btn-outline" data-mart-admin-edit-category="' + esc(c.id) + '">Edit</button>' +
          '<button type="button" class="btn btn-outline" data-mart-admin-toggle-category="' + esc(c.id) + '" data-active="' + (c.is_active ? "false" : "true") + '">' +
          (c.is_active ? "Deactivate" : "Activate") + "</button></div></div>";
      });
    } catch (err) { message((err && err.message) || "Categories could not be loaded.", true); }
  }
  async function loadProducts() {
    var search = $("#mart-admin-product-search"), filter = $("#mart-admin-category-filter"), active = $("#mart-admin-active-filter");
    var params = { page_size: 100 };
    if (search && search.value.trim()) params.search = search.value.trim();
    if (filter && filter.value) params.category_id = filter.value;
    if (active && active.value === "true") params.active = "true";
    try {
      var response = await API.list("minimartAdminProducts", params);
      products = rows(response);
      displayList($("[data-mart-admin-products]"), products, "No Mini Mart products match this filter.", function (p) {
        return '<details><summary><strong>' + esc(p.name) + "</strong> · " + esc(p.sku) + " · " +
          esc((window.JONE && JONE.formatNaira) ? JONE.formatNaira(p.selling_price) : ("₦" + p.selling_price)) +
          " · " + esc(p.status) + "</summary><p>" + esc(p.category || "Uncategorized") + " · " +
          esc(p.location || "No store") + " · " + esc(p.stock_quantity || "0") + " " + esc(p.base_unit_label || p.base_unit || "") +
          " on hand · cost " + esc(p.purchase_cost || "0.00") + "</p>" +
          (p.description ? "<p>" + esc(p.description) + "</p>" : "") +
          '<div class="form-actions"><button type="button" class="btn btn-outline" data-mart-admin-edit-product="' + esc(p.id) + '">Edit</button>' +
          '<button type="button" class="btn btn-outline" data-mart-admin-toggle-product="' + esc(p.id) + '" data-active="' + (p.is_active ? "false" : "true") + '">' +
          (p.is_active ? "Deactivate" : "Activate") + "</button></div></details>";
      });
    } catch (err) { message((err && err.message) || "Products could not be loaded.", true); }
  }
  function resetCategoryForm() {
    var form = $("[data-mart-admin-category-form]");
    if (!form) return;
    form.reset(); form.elements.category_id.value = ""; form.elements.is_active.value = "true";
  }
  function fillCategoryForm(row) {
    var form = $("[data-mart-admin-category-form]");
    if (!form || !row) return;
    form.elements.category_id.value = row.id;
    form.elements.name.value = row.name || "";
    form.elements.description.value = row.description || "";
    form.elements.is_active.value = String(!!row.is_active);
  }
  function resetProductForm() {
    var form = $("[data-mart-admin-product-form]");
    if (!form) return;
    form.reset();
    form.elements.product_id.value = "";
    form.elements.is_active.value = "true";
    form.elements.base_unit.value = units[0] ? units[0].value : "";
    form.elements.opening_quantity.disabled = false;
    if (form.elements.counted_quantity) form.elements.counted_quantity.value = "";
    if (form.elements.stock_adjustment_reason) form.elements.stock_adjustment_reason.value = "";
    showStockAdjustmentForm(false);   // opening stock covers a new product
  }
  function stockAdjustmentFields() {
    return Array.prototype.slice.call(
      document.querySelectorAll("[data-mart-admin-stock-adjustment]")
    );
  }
  function showStockAdjustmentForm(show) {
    stockAdjustmentFields().forEach(function (node) {
      node.hidden = !show;
      node.classList.toggle("hidden", !show);
    });
  }
  function fillProductForm(row) {
    var form = $("[data-mart-admin-product-form]");
    if (!form || !row) return;
    form.elements.product_id.value = row.id;
    form.elements.name.value = row.name || "";
    form.elements.sku.value = row.sku || "";
    form.elements.barcode.value = row.barcode || "";
    form.elements.description.value = row.description || "";
    form.elements.category_id.value = row.category_id || "";
    form.elements.purchase_cost.value = row.purchase_cost || "";
    form.elements.selling_price.value = row.selling_price || "";
    form.elements.base_unit.value = row.base_unit || "";
    form.elements.reorder_level.value = row.reorder_level || "0";
    form.elements.location_id.value = row.location_id || "";
    form.elements.is_active.value = String(!!row.is_active);
    form.elements.opening_quantity.value = "";
    form.elements.opening_quantity.disabled = true;
    /* Correcting the stock of an EXISTING product is a real, authorized need
       (breakage, miscount, found stock). It is not the opening-stock field —
       that would bypass the ledger — so it is a separate, reason-backed
       correction that the server posts as an ADJUSTMENT movement. */
    if (form.elements.counted_quantity) {
      form.elements.counted_quantity.value = "";
      form.elements.counted_quantity.max = "";
      form.elements.counted_quantity.placeholder = String(row.stock_quantity || "0");
    }
    if (form.elements.stock_adjustment_reason) form.elements.stock_adjustment_reason.value = "";
    showStockAdjustmentForm(!form.elements.location_id.disabled);
  }
  function compact(values) {
    Object.keys(values).forEach(function (name) {
      if (values[name] === "" || values[name] == null) delete values[name];
    });
    return values;
  }

  /* ------------------------- Form visibility state -------------------------
     The catalogue section holds two independent forms. They must BOTH start
     hidden and only ever be revealed by their own button: the page opens on
     the listing, "Add Category" reveals only the category form and "Add
     Product" only the product form. Visibility is therefore owned by one
     helper here — never by leftover inline styles, a stray `hidden` attribute
     on a wrapper, or a validation pass — so no code path can open one form
     because the other was being initialised. */
  var PANELS = {
    category: {
      panel: "[data-mart-admin-category-panel]",
      launcher: "[data-mart-admin-add-category]",
      form: "[data-mart-admin-category-form]",
    },
    product: {
      panel: "[data-mart-admin-product-panel]",
      launcher: "[data-mart-admin-add-product]",
      form: "[data-mart-admin-product-form]",
    },
  };

  function setPanel(kind, open) {
    var spec = PANELS[kind];
    if (!spec) return;
    var panel = $(spec.panel), launcher = $(spec.launcher);
    if (panel) {
      panel.hidden = !open;
      panel.classList.toggle("hidden", !open);
      panel.setAttribute("aria-hidden", open ? "false" : "true");
    }
    if (launcher) {
      launcher.setAttribute("aria-expanded", open ? "true" : "false");
      launcher.hidden = !!open;
    }
  }

  function closePanels() {
    setPanel("category", false);
    setPanel("product", false);
  }

  function openPanel(kind) {
    // Only ONE form is ever on screen, so a partially filled product form can
    // never be submitted because the category form was "already open".
    closePanels();
    setPanel(kind, true);
    var spec = PANELS[kind];
    var form = spec && $(spec.form);
    var first = form && form.querySelector("input:not([type=hidden]),select,textarea");
    if (first && typeof first.focus === "function") first.focus();
    message("");
  }

  function clearFieldErrors(form) {
    if (!form) return;
    if (typeof form.querySelectorAll !== "function") return;
    form.querySelectorAll("[aria-invalid=true]").forEach(function (field) {
      field.removeAttribute("aria-invalid");
      field.removeAttribute("aria-describedby");
    });
    form.querySelectorAll("[data-mart-field-error]").forEach(function (node) {
      node.textContent = "";
    });
  }

  /* Render a 400 field-by-field so the offending input is highlighted inside
     the correct form; the message stays visible until the next attempt. */
  function showFieldErrors(form, err) {
    var detail = (err && err.data && err.data.errors) || null;
    var shown = false;
    if (form && detail && typeof detail === "object") {
      Object.keys(detail).forEach(function (field) {
        var input = form.elements[field];
        var text = detail[field];
        if (Array.isArray(text)) text = text[0];
        if (input && input.setAttribute) {
          input.setAttribute("aria-invalid", "true");
          shown = true;
        }
        var slot = form.querySelector('[data-mart-field-error="' + field + '"]');
        if (slot) { slot.textContent = String(text == null ? "" : text); shown = true; }
      });
    }
    return shown;
  }
  async function submitCategory(e) {
    e.preventDefault();
    var form = e.target, id = form.elements.category_id.value;
    var button = form.querySelector("[type=submit]");
    if (button && button.disabled) return;   // duplicate-submission guard
    var values = compact({
      name: form.elements.name.value.trim(),
      description: form.elements.description.value.trim(),
      is_active: form.elements.is_active.value === "true",
    });
    clearFieldErrors(form);
    if (button) button.disabled = true;
    try {
      if (id) await API.update("minimartCategories", id, values);
      else await API.create("minimartCategories", values);
      toast("Mini Mart category saved.");
      resetCategoryForm();
      setPanel("category", false);           // successful submit closes the form
      await loadCategories(); await loadProducts();
    } catch (err) {
      toast((err && err.message) || "Category could not be saved.", true);
      if (!showFieldErrors(form, err)) setPanel("category", true);
    } finally { if (button) button.disabled = false; }
  }
  async function submitProduct(e) {
    e.preventDefault();
    var form = e.target, id = form.elements.product_id.value;
    var values = compact({
      name: form.elements.name.value.trim(),
      sku: form.elements.sku.value.trim(),
      barcode: form.elements.barcode.value.trim(),
      description: form.elements.description.value.trim(),
      category_id: form.elements.category_id.value,
      purchase_cost: form.elements.purchase_cost.value,
      selling_price: form.elements.selling_price.value,
      base_unit: form.elements.base_unit.value,
      reorder_level: form.elements.reorder_level.value,
      location_id: form.elements.location_id.value,
      is_active: form.elements.is_active.value === "true",
    });
    if (!id) {
      values.opening_quantity = form.elements.opening_quantity.value;
      values.opening_unit = form.elements.opening_unit.value;
      values.opening_conversion_factor = form.elements.opening_conversion_factor.value;
      values.idempotency_key = key();
    }
    values = compact(values);
    var counted = id && form.elements.counted_quantity
      ? String(form.elements.counted_quantity.value || "").trim() : "";
    var adjustmentReason = id && form.elements.stock_adjustment_reason
      ? String(form.elements.stock_adjustment_reason.value || "").trim() : "";
    clearFieldErrors(form);
    var button = form.querySelector("[type=submit]");
    if (button && button.disabled) return;   // duplicate-submission guard
    if (counted && adjustmentReason.length < 5) {
      toast("Give the reason for the stock correction (at least 5 characters).", true);
      if (form.elements.stock_adjustment_reason) form.elements.stock_adjustment_reason.focus();
      return;
    }
    if (button) button.disabled = true;
    try {
      if (id) await API.update("minimartAdminProducts", id, values);
      else await API.create("minimartAdminProducts", values);
      if (counted) {
        /* The quantity is never written directly: the server posts the
           difference as an immutable stock adjustment with this reason. */
        var adjusted = await API.post("/api/admin/inventory/minimart/products/" + encodeURIComponent(id) + "/stock-adjustment/", {
          counted_quantity: counted,
          reason: adjustmentReason
        });
        var result = adjusted && adjusted.data;
        var after = result && result.product ? result.product.stock_quantity : "";
        toast("Stock corrected and recorded in the ledger" + (after !== "" ? " — now " + after + " on hand." : "."));
      }
      toast("Mini Mart product saved.");
      resetProductForm();
      setPanel("product", false);            // successful submit closes the form
      await loadOptions(); await loadProducts();
    } catch (err) {
      toast((err && err.message) || "Product could not be saved.", true);
      if (!showFieldErrors(form, err)) setPanel("product", true);
    } finally { if (button) button.disabled = false; }
  }
  function bindActions(box, attr, getList, run) {
    if (!box) return;
    box.addEventListener("click", function (event) {
      var button = event.target.closest("button[" + attr + "]");
      if (!button) return;
      var id = button.getAttribute(attr);
      var row = getList().find(function (item) { return String(item.id) === String(id); });
      run(row, button.getAttribute("data-active") === "true");
    });
  }
  function init() {
    section = $("[data-mart-admin]");
    if (!section || !API || !API.list || !API.create || !API.update) return;
    if (!allowed()) { section.hidden = true; return; }
    section.hidden = false;
    /* Both forms start hidden: the page opens on the listing interface. The
       markup ships them inside [hidden] panels and this line makes the state
       explicit even if a stale cached stylesheet or a previous session left a
       wrapper visible. */
    closePanels();

    function on(selector, event, handler) {
      var node = $(selector);
      if (node) node.addEventListener(event, handler);
    }

    on("[data-mart-admin-category-form]", "submit", submitCategory);
    on("[data-mart-admin-product-form]", "submit", submitProduct);
    on("[data-mart-admin-reset-category]", "click", function () { resetCategoryForm(); clearFieldErrors($("[data-mart-admin-category-form]")); });
    on("[data-mart-admin-reset-product]", "click", function () { resetProductForm(); clearFieldErrors($("[data-mart-admin-product-form]")); });
    on("[data-mart-admin-add-category]", "click", function () { resetCategoryForm(); openPanel("category"); });
    on("[data-mart-admin-add-product]", "click", function () { resetProductForm(); openPanel("product"); });
    on("[data-mart-admin-cancel-category]", "click", function () { resetCategoryForm(); closePanels(); });
    on("[data-mart-admin-cancel-product]", "click", function () { resetProductForm(); closePanels(); });

    /* Search is debounced: one request per typing pause instead of one per
       keystroke, and in-flight work is never multiplied. */
    on("[data-mart-admin-category-search]", "input", debounce(loadCategories, 250));
    on("[data-mart-admin-product-search]", "input", debounce(loadProducts, 250));
    on("[data-mart-admin-category-filter]", "change", loadProducts);
    on("[data-mart-admin-active-filter]", "change", loadProducts);
    on("[data-mart-admin-refresh]", "click", function () { loadOptions(); loadCategories(); loadProducts(); });
    bindActions($("[data-mart-admin-categories]"), "data-mart-admin-edit-category",
      function () { return categories; }, function (row) { fillCategoryForm(row); openPanel("category"); });
    bindActions($("[data-mart-admin-categories]"), "data-mart-admin-toggle-category",
      function () { return categories; }, async function (row, active) {
        if (!row) return;
        try { await API.update("minimartCategories", row.id, { is_active: active }); toast("Category updated."); await loadCategories(); await loadProducts(); }
        catch (err) { toast((err && err.message) || "Category could not be updated.", true); }
      });
    bindActions($("[data-mart-admin-products]"), "data-mart-admin-edit-product",
      function () { return products; }, function (row) { fillProductForm(row); openPanel("product"); });
    bindActions($("[data-mart-admin-products]"), "data-mart-admin-toggle-product",
      function () { return products; }, async function (row, active) {
        if (!row) return;
        try { await API.update("minimartAdminProducts", row.id, { is_active: active }); toast("Product updated."); await loadProducts(); }
        catch (err) { toast((err && err.message) || "Product could not be updated.", true); }
      });
    loadOptions(); loadCategories(); loadProducts();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
})();
