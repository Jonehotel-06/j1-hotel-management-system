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

  function rows(response) {
    var data = response && response.data !== undefined ? response.data : response;
    if (Array.isArray(data)) return data;
    return (data && (data.results || data.items)) || [];
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
  }
  function compact(values) {
    Object.keys(values).forEach(function (name) {
      if (values[name] === "" || values[name] == null) delete values[name];
    });
    return values;
  }
  async function submitCategory(e) {
    e.preventDefault();
    var form = e.target, id = form.elements.category_id.value;
    var values = compact({
      name: form.elements.name.value.trim(),
      description: form.elements.description.value.trim(),
      is_active: form.elements.is_active.value === "true",
    });
    try {
      if (id) await API.update("minimartCategories", id, values);
      else await API.create("minimartCategories", values);
      toast("Mini Mart category saved.");
      resetCategoryForm(); await loadCategories(); await loadProducts();
    } catch (err) { toast((err && err.message) || "Category could not be saved.", true); }
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
    try {
      if (id) await API.update("minimartAdminProducts", id, values);
      else await API.create("minimartAdminProducts", values);
      toast("Mini Mart product saved.");
      resetProductForm(); await loadProducts();
    } catch (err) { toast((err && err.message) || "Product could not be saved.", true); }
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
    $("[data-mart-admin-category-form]").addEventListener("submit", submitCategory);
    $("[data-mart-admin-product-form]").addEventListener("submit", submitProduct);
    $("[data-mart-admin-reset-category]").addEventListener("click", resetCategoryForm);
    $("[data-mart-admin-reset-product]").addEventListener("click", resetProductForm);
    $("[data-mart-admin-category-search]").addEventListener("input", function () { loadCategories(); });
    $("[data-mart-admin-product-search]").addEventListener("input", function () { loadProducts(); });
    $("[data-mart-admin-category-filter]").addEventListener("change", function () { loadProducts(); });
    $("[data-mart-admin-active-filter]").addEventListener("change", function () { loadProducts(); });
    $("[data-mart-admin-refresh]").addEventListener("click", function () { loadOptions(); loadCategories(); loadProducts(); });
    bindActions($("[data-mart-admin-categories]"), "data-mart-admin-edit-category",
      function () { return categories; }, function (row) { fillCategoryForm(row); });
    bindActions($("[data-mart-admin-categories]"), "data-mart-admin-toggle-category",
      function () { return categories; }, async function (row, active) {
        if (!row) return;
        try { await API.update("minimartCategories", row.id, { is_active: active }); toast("Category updated."); await loadCategories(); await loadProducts(); }
        catch (err) { toast((err && err.message) || "Category could not be updated.", true); }
      });
    bindActions($("[data-mart-admin-products]"), "data-mart-admin-edit-product",
      function () { return products; }, function (row) { fillProductForm(row); });
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
