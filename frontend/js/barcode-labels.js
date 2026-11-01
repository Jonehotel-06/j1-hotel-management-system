/* frontend/js/barcode-labels.js
 *
 * Task 01 of targeted_fixes_01 — SEARCHABLE BARCODE LABEL PRINTING.
 *
 * What this module adds to the existing label screen:
 *
 *   • one search box that matches a product by NAME, SKU, BARCODE or ID;
 *   • live results with a debounce, plus an explicit search button and Enter;
 *   • a per-product quantity (how many labels for THIS product);
 *   • "select all filtered" and per-row select/deselect, with a counter of
 *     how many labels the current selection will actually print;
 *   • an on-screen PREVIEW of exactly the labels that will print;
 *   • printing ONLY the selected products, with their per-product quantities.
 *
 * What it deliberately does NOT do:
 *
 *   • it never invents a barcode — a product with no stored barcode prints its
 *     SKU, exactly as before, and a code Code 128 cannot encode is refused
 *     rather than faked;
 *   • it does not change POS menu management, pricing or stock levels;
 *   • it does not alter the existing POS/inventory list endpoints: searching
 *     uses their documented `search` parameter.
 *
 * The page keeps its own data loading, so this file is pure state + rendering
 * and can be tested without a browser (see tests-js/test-barcode-label-print.js).
 */
(function (window) {
  "use strict";

  var JONE = (window.JONE = window.JONE || {});

  /* ------------------------------------------------------------------ *
   * Normalisation helpers
   * ------------------------------------------------------------------ */

  /** The code a label encodes: the stored barcode, else the SKU, else "". */
  function codeOf(item) {
    if (!item) return "";
    return String(item.barcode || item.sku || item.item_sku || "").trim();
  }

  /** A stable key for one product, used for selection and quantities. */
  function keyOf(item) {
    if (!item) return "";
    if (item.source === "menu" && item.id != null) return "menu:" + item.id;
    if (item.source === "stock" && item.id != null) return "stock:" + item.id;
    if (item.id != null) return String(item.id);
    return codeOf(item) || String(item.name || "");
  }

  function nameOf(item) {
    return String((item && (item.name || item.item_name)) || "Product");
  }

  function idOf(item) {
    return item && item.id != null ? String(item.id) : "";
  }

  function matches(item, query) {
    var q = String(query || "").trim().toLowerCase();
    if (!q) return true;
    var haystack = [
      nameOf(item),
      item && item.sku,
      item && item.barcode,
      item && item.item_sku,
      idOf(item),
      item && item.category_name,
      item && item.category,
      item && item.base_unit,
    ]
      .filter(function (value) { return value !== undefined && value !== null; })
      .join(" ")
      .toLowerCase();
    return haystack.indexOf(q) !== -1;
  }

  /* ------------------------------------------------------------------ *
   * State
   * ------------------------------------------------------------------ */

  function createState() {
    return {
      items: [],            // every loaded product (POS menu + stock)
      query: "",            // current search text
      selected: {},         // key -> true
      quantities: {},       // key -> integer labels requested
      copies: 1,            // default labels per product
      lastPrinted: [],      // what the previous print actually contained
    };
  }

  function syncSelection(state) {
    /* Drop selections for products no longer loaded, so a stale key can never
       contribute phantom labels to a print. */
    var alive = {};
    state.items.forEach(function (item) { alive[keyOf(item)] = true; });
    Object.keys(state.selected).forEach(function (key) {
      if (!alive[key]) delete state.selected[key];
    });
    Object.keys(state.quantities).forEach(function (key) {
      if (!alive[key]) delete state.quantities[key];
    });
    return state;
  }

  function setItems(state, items) {
    state.items = (items || []).slice();
    return syncSelection(state);
  }

  /** Add products from a second catalogue without duplicating a load. */
  function addItems(state, items) {
    var byKey = {};
    state.items.forEach(function (item) { byKey[keyOf(item)] = item; });
    (items || []).forEach(function (item) { byKey[keyOf(item)] = item; });
    state.items = Object.keys(byKey).map(function (key) { return byKey[key]; });
    return syncSelection(state);
  }

  function setQuery(state, query) {
    state.query = String(query || "");
    return state;
  }

  function filtered(state, items) {
    var source = items || state.items;
    return source.filter(function (item) { return matches(item, state.query); });
  }

  /* ------------------------------------------------------------------ *
   * Selection
   * ------------------------------------------------------------------ */

  function isSelected(state, item) {
    return !!state.selected[keyOf(item)];
  }

  function toggle(state, item, on) {
    var key = keyOf(item);
    var next = on === undefined ? !state.selected[key] : !!on;
    if (next) state.selected[key] = true;
    else delete state.selected[key];
    return state;
  }

  /** Select (or deselect) every product currently matching the filter. */
  function selectAllFiltered(state, on) {
    var rows = filtered(state);
    var next = on === undefined ? !rows.every(function (row) { return isSelected(state, row); }) : !!on;
    rows.forEach(function (row) { toggle(state, row, next); });
    return state;
  }

  function selectedItems(state) {
    return state.items.filter(function (item) { return isSelected(state, item); });
  }

  /* ------------------------------------------------------------------ *
   * Per-product quantity
   * ------------------------------------------------------------------ */

  var MAX_LABELS_PER_PRODUCT = 100;

  function clampQuantity(value, fallback) {
    var n = parseInt(value, 10);
    if (!isFinite(n) || n < 1) return fallback === undefined ? 1 : clampQuantity(fallback, 1);
    return Math.min(MAX_LABELS_PER_PRODUCT, n);
  }

  function quantityFor(state, item) {
    var key = keyOf(item);
    var stored = state.quantities[key];
    return stored === undefined ? clampQuantity(state.copies, 1) : clampQuantity(stored, 1);
  }

  function setQuantity(state, item, value) {
    state.quantities[keyOf(item)] = clampQuantity(value, 1);
    return state;
  }

  function setDefaultCopies(state, value) {
    state.copies = clampQuantity(value, 1);
    return state;
  }

  /** Total labels the current selection will print. */
  function labelCount(state) {
    return selectedItems(state).reduce(function (total, item) {
      return total + quantityFor(state, item);
    }, 0);
  }

  /* ------------------------------------------------------------------ *
   * The print plan — ONE array of label objects, used by the preview AND by
   * the print itself, so what an operator sees is exactly what prints.
   * ------------------------------------------------------------------ */

  function printPlan(state) {
    var plan = [];
    selectedItems(state).forEach(function (item) {
      var code = codeOf(item);
      var count = quantityFor(state, item);
      for (var i = 0; i < count; i++) {
        plan.push({
          key: keyOf(item),
          code: code,
          name: nameOf(item),
          id: idOf(item),
          sku: String((item && item.sku) || ""),
          barcode: String((item && item.barcode) || ""),
          category: String((item && (item.category_name || item.category)) || ""),
          unit: String((item && item.base_unit) || ""),
          price: item && item.base_price != null ? String(item.base_price) : "",
          source: (item && item.source) || "",
          index: i + 1,
          of: count,
        });
      }
    });
    return plan;
  }

  /* ------------------------------------------------------------------ *
   * Rendering
   * ------------------------------------------------------------------ */

  function esc(value) {
    var helper = JONE && JONE.esc;
    if (typeof helper === "function") return helper(value);
    return String(value === undefined || value === null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function barcodeSvg(code, options) {
    var api = JONE && JONE.barcode;
    if (!api || typeof api.generateBarcodeSvg !== "function") return null;
    try {
      return api.generateBarcodeSvg(code, options || { height: 42, barWidth: 2 });
    } catch (error) {
      return null;
    }
  }

  /** One printable label. Unencodable codes are refused, never faked. */
  function labelMarkup(entry) {
    var svg = barcodeSvg(entry.code);
    var body = svg
      ? svg
      : '<p class="label-error">This code cannot be printed as Code 128. ' +
        "Correct the product's barcode or SKU first.</p>";
    var subtitle = entry.sku && entry.barcode && entry.sku !== entry.barcode
      ? '<p class="label-meta">SKU ' + esc(entry.sku) + "</p>"
      : "";
    return (
      '<div class="label-card" data-label-key="' + esc(entry.key) + '" ' +
      'data-label-code="' + esc(entry.code) + '">' +
      '<h4>' + esc(entry.name) + "</h4>" +
      (entry.category ? '<p class="label-meta">' + esc(entry.category) + "</p>" : "") +
      body +
      '<p class="label-code">Code 128 · ' + esc(entry.code) + "</p>" +
      subtitle +
      (entry.of > 1 ? '<p class="label-copy">Label ' + entry.index + " of " + entry.of + "</p>" : "") +
      "</div>"
    );
  }

  /** The whole printable sheet for the current selection. */
  function sheetMarkup(state) {
    var plan = printPlan(state);
    if (!plan.length) {
      return (
        '<div class="empty-state"><h3>No labels selected</h3>' +
        "<p>Search for a product, tick it, then choose how many labels to print.</p></div>"
      );
    }
    return plan.map(labelMarkup).join("");
  }

  /** One row of the result list. */
  function rowMarkup(state, item) {
    var key = keyOf(item);
    var checked = isSelected(state, item);
    var code = codeOf(item);
    var warn = barcodeSvg(code) ? "" : ' <span class="tag tag-warn">Code cannot be printed</span>';
    var bits = [];
    if (idOf(item)) bits.push("ID " + esc(idOf(item)));
    if (item.sku) bits.push("SKU " + esc(item.sku));
    if (item.barcode) bits.push("Barcode " + esc(item.barcode));
    if (item.category_name || item.category) bits.push(esc(item.category_name || item.category));
    if (item.base_unit) bits.push(esc(item.base_unit));
    return (
      '<tr data-label-row="' + esc(key) + '"' + (checked ? ' class="is-selected"' : "") + ">" +
      '<td class="label-pick"><input type="checkbox" data-label-select="' + esc(key) + '"' +
      (checked ? " checked" : "") + ' aria-label="Select ' + esc(nameOf(item)) + '"></td>' +
      "<td><strong>" + esc(nameOf(item)) + "</strong>" + warn +
      '<br><small class="caption">' + (bits.join(" · ") || "No code on record") + "</small></td>" +
      '<td class="label-qty"><input class="input" type="number" min="1" max="' + MAX_LABELS_PER_PRODUCT +
      '" step="1" value="' + quantityFor(state, item) + '" data-label-qty="' + esc(key) +
      '" aria-label="Labels for ' + esc(nameOf(item)) + '"></td>' +
      '<td class="num">' + (checked ? quantityFor(state, item) : 0) + "</td>" +
      "</tr>"
    );
  }

  function resultsMarkup(state) {
    var rows = filtered(state);
    if (!state.items.length) {
      return (
        '<div class="empty-state"><h3>No products loaded</h3>' +
        "<p>Load the POS menu, the stock catalogue, or both.</p></div>"
      );
    }
    if (!rows.length) {
      return (
        '<div class="empty-state"><h3>Nothing matches “' + esc(state.query) + '”</h3>' +
        "<p>Search by product name, SKU, barcode or ID.</p></div>"
      );
    }
    return (
      '<table class="table label-table"><thead><tr>' +
      '<th class="label-pick"><span class="sr-only">Select</span></th>' +
      "<th>Product</th><th>Labels</th><th class=\"num\">Selected</th>" +
      "</tr></thead><tbody>" + rows.map(function (item) { return rowMarkup(state, item); }).join("") +
      "</tbody></table>"
    );
  }

  function summary(state) {
    var rows = filtered(state);
    var picked = selectedItems(state);
    var selectedInView = rows.filter(function (item) { return isSelected(state, item); }).length;
    return {
      matching: rows.length,
      total: state.items.length,
      selected: picked.length,
      selectedInView: selectedInView,
      labels: labelCount(state),
      allViewSelected: rows.length > 0 && selectedInView === rows.length,
    };
  }

  JONE.barcodeLabels = {
    createState: createState,
    setItems: setItems,
    addItems: addItems,
    setQuery: setQuery,
    setDefaultCopies: setDefaultCopies,
    setQuantity: setQuantity,
    quantityFor: quantityFor,
    toggle: toggle,
    selectAllFiltered: selectAllFiltered,
    selectedItems: selectedItems,
    filtered: filtered,
    isSelected: isSelected,
    labelCount: labelCount,
    printPlan: printPlan,
    matches: matches,
    codeOf: codeOf,
    keyOf: keyOf,
    nameOf: nameOf,
    clampQuantity: clampQuantity,
    labelMarkup: labelMarkup,
    sheetMarkup: sheetMarkup,
    rowMarkup: rowMarkup,
    resultsMarkup: resultsMarkup,
    summary: summary,
    MAX_LABELS_PER_PRODUCT: MAX_LABELS_PER_PRODUCT,
  };
})(typeof window !== "undefined" ? window : globalThis);
