/* frontend/js/menu-print.js
 *
 * Task 02 of targeted_fixes_01 — SEARCHABLE / SELECTIVE MENU-ITEM PRINTING.
 *
 * This is a SEPARATE workflow from barcode label printing (task 01): it prints
 * menu information sheets — the item's name, its SKU/barcode and its category,
 * plus whatever the operator ticks in the "columns" row — for a menu board, a
 * kitchen sheet or a price check. It is not, and does not become, a barcode
 * label printer.
 *
 * Boundaries this module respects:
 *
 *   • it never writes anything: no menu price, quantity, category or POS
 *     record is created or changed — the page is read-only;
 *   • it never invents a SKU or barcode; an item with neither prints "—";
 *   • it reuses the SAME search semantics as task 01 (name, SKU, barcode,
 *     category, ID) so staff learn one search box, not two.
 *
 * Rendered output is data-driven from the API payload, so nothing about the
 * hotel's menu is hardcoded here.
 */
(function (window) {
  "use strict";

  var JONE = (window.JONE = window.JONE || {});

  /* ------------------------------------------------------------------ *
   * Normalisation
   * ------------------------------------------------------------------ */

  function nameOf(item) {
    return String((item && (item.name || item.item_name)) || "Menu item");
  }

  function skuOf(item) {
    return String((item && (item.sku || item.item_sku)) || "").trim();
  }

  function barcodeOf(item) {
    return String((item && item.barcode) || "").trim();
  }

  function categoryOf(item) {
    if (!item) return "";
    return String(item.category_name || item.category || "").trim();
  }

  function keyOf(item) {
    if (!item) return "";
    if (item.id != null) return String(item.id);
    return skuOf(item) || nameOf(item);
  }

  function matches(item, query) {
    var q = String(query || "").trim().toLowerCase();
    if (!q) return true;
    return [nameOf(item), skuOf(item), barcodeOf(item), categoryOf(item), keyOf(item)]
      .join(" ")
      .toLowerCase()
      .indexOf(q) !== -1;
  }

  /* ------------------------------------------------------------------ *
   * State
   * ------------------------------------------------------------------ */

  function createState() {
    return {
      items: [],
      query: "",
      selected: {},
      category: "",                 // "" = every category
      columns: { price: true, area: true, code: true, description: false },
      onePerPage: false,
      lastPrinted: [],
    };
  }

  function setItems(state, items) {
    state.items = (items || []).slice();
    var alive = {};
    state.items.forEach(function (item) { alive[keyOf(item)] = true; });
    Object.keys(state.selected).forEach(function (key) {
      if (!alive[key]) delete state.selected[key];
    });
    return state;
  }

  function setQuery(state, query) {
    state.query = String(query || "");
    return state;
  }

  function setCategory(state, category) {
    state.category = String(category || "");
    return state;
  }

  function setColumn(state, name, on) {
    state.columns[name] = !!on;
    return state;
  }

  function categories(state) {
    var seen = {};
    state.items.forEach(function (item) {
      var name = categoryOf(item);
      if (name) seen[name] = true;
    });
    return Object.keys(seen).sort();
  }

  function filtered(state, items) {
    var source = items || state.items;
    return source.filter(function (item) {
      if (state.category && categoryOf(item) !== state.category) return false;
      return matches(item, state.query);
    });
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

  function selectAllFiltered(state, on) {
    var rows = filtered(state);
    var next = on === undefined
      ? !rows.every(function (row) { return isSelected(state, row); })
      : !!on;
    rows.forEach(function (row) { toggle(state, row, next); });
    return state;
  }

  function selectedItems(state) {
    return state.items.filter(function (item) { return isSelected(state, item); });
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

  function money(value, currency) {
    var fn = JONE && JONE.formatNaira;
    if (typeof fn === "function" && value !== undefined && value !== null && value !== "") {
      try { return fn(value, currency ? { currency: currency } : undefined); } catch (error) { /* fall through */ }
    }
    var numeric = Number(value);
    if (!isFinite(numeric)) return String(value === undefined || value === null ? "" : value);
    return String(currency || "NGN") + " " + numeric.toLocaleString("en-NG", {
      minimumFractionDigits: 2, maximumFractionDigits: 2,
    });
  }

  /** One menu information card. Only the columns the operator ticked appear. */
  function cardMarkup(state, item) {
    var columns = state.columns || {};
    var rows = [];
    if (columns.code) {
      var code = skuOf(item);
      var barcode = barcodeOf(item);
      var codeBits = [];
      if (code) codeBits.push("SKU " + esc(code));
      if (barcode && barcode !== code) codeBits.push("Barcode " + esc(barcode));
      rows.push(['<dt>Code</dt><dd>' + (codeBits.join(" · ") || "—") + "</dd>"]);
    }
    if (columns.price && item.base_price != null && item.base_price !== "") {
      rows.push(["<dt>Price</dt><dd>" + esc(money(item.base_price, item.currency)) + "</dd>"]);
    }
    if (columns.area && (item.service_area || item.category_name)) {
      var area = String(item.service_area || "").trim();
      var label = area ? area.charAt(0) + area.slice(1).toLowerCase() : "";
      var categoryName = categoryOf(item);
      var bits = [categoryName, label].filter(function (value) { return !!value; });
      if (bits.length) rows.push(["<dt>Category</dt><dd>" + esc(bits.join(" · ")) + "</dd>"]);
    }
    var description = columns.description ? String(item.description || "").trim() : "";
    if (description) rows.push(["<dt>Description</dt><dd>" + esc(description) + "</dd>"]);
    return (
      '<article class="menu-print-card" data-menu-card="' + esc(keyOf(item)) + '">' +
      "<h3>" + esc(nameOf(item)) + "</h3>" +
      '<dl class="menu-print-facts">' + rows.map(function (pair) { return pair.join(""); }).join("") + "</dl>" +
      "</article>"
    );
  }

  /** The whole printable sheet, from the same selection the preview shows. */
  function sheetMarkup(state) {
    var picked = selectedItems(state);
    if (!picked.length) {
      return (
        '<div class="empty-state"><h3>Nothing selected</h3>' +
        "<p>Search for a menu item, tick it, then choose what to print.</p></div>"
      );
    }
    var klass = state.onePerPage ? "menu-print-sheet is-one-per-page" : "menu-print-sheet";
    return '<div class="' + klass + '">' +
      picked.map(function (item) { return cardMarkup(state, item); }).join("") +
      "</div>";
  }

  function rowMarkup(state, item) {
    var key = keyOf(item);
    var checked = isSelected(state, item);
    var bits = [];
    if (categoryOf(item)) bits.push(esc(categoryOf(item)));
    if (skuOf(item)) bits.push("SKU " + esc(skuOf(item)));
    if (barcodeOf(item) && barcodeOf(item) !== skuOf(item)) bits.push("Barcode " + esc(barcodeOf(item)));
    if (item.base_price != null && item.base_price !== "") bits.push(esc(money(item.base_price, item.currency)));
    return (
      '<tr data-menu-row="' + esc(key) + '"' + (checked ? ' class="is-selected"' : "") + ">" +
      '<td class="menu-pick"><input type="checkbox" data-menu-select="' + esc(key) + '"' +
      (checked ? " checked" : "") + ' aria-label="Select ' + esc(nameOf(item)) + '"></td>' +
      "<td><strong>" + esc(nameOf(item)) + "</strong>" +
      '<br><small class="caption">' + (bits.join(" · ") || "No code on record") + "</small></td>" +
      "</tr>"
    );
  }

  function resultsMarkup(state) {
    if (!state.items.length) {
      return (
        '<div class="empty-state"><h3>No menu items loaded</h3>' +
        "<p>Check your menu permissions, or reload the page.</p></div>"
      );
    }
    var rows = filtered(state);
    if (!rows.length) {
      return (
        '<div class="empty-state"><h3>Nothing matches</h3>' +
        "<p>Search by item name, SKU, barcode or category.</p></div>"
      );
    }
    return (
      '<table class="table menu-print-table"><thead><tr>' +
      '<th class="menu-pick"><span class="sr-only">Select</span></th><th>Menu item</th>' +
      "</tr></thead><tbody>" +
      rows.map(function (item) { return rowMarkup(state, item); }).join("") +
      "</tbody></table>"
    );
  }

  function summary(state) {
    var rows = filtered(state);
    var picked = selectedItems(state);
    return {
      matching: rows.length,
      total: state.items.length,
      selected: picked.length,
      selectedInView: rows.filter(function (item) { return isSelected(state, item); }).length,
      allViewSelected: rows.length > 0 && rows.every(function (item) { return isSelected(state, item); }),
    };
  }

  JONE.menuPrint = {
    createState: createState,
    setItems: setItems,
    setQuery: setQuery,
    setCategory: setCategory,
    setColumn: setColumn,
    categories: categories,
    filtered: filtered,
    isSelected: isSelected,
    toggle: toggle,
    selectAllFiltered: selectAllFiltered,
    selectedItems: selectedItems,
    matches: matches,
    nameOf: nameOf,
    skuOf: skuOf,
    barcodeOf: barcodeOf,
    categoryOf: categoryOf,
    keyOf: keyOf,
    cardMarkup: cardMarkup,
    sheetMarkup: sheetMarkup,
    rowMarkup: rowMarkup,
    resultsMarkup: resultsMarkup,
    summary: summary,
  };
})(typeof window !== "undefined" ? window : globalThis);
