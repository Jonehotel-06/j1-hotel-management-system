/* js/operations.js */
/* ==========================================================================
   Shared helpers for capability-scoped hotel operations screens.
   They deliberately keep selectors server-backed: a picker sends a bounded
   search only after an operator types, rather than downloading guest, room or
   staff directories into the browser.
   ========================================================================== */

(function () {
  "use strict";

  var J = window.JONE = window.JONE || {};
  var OPS = J.operations = J.operations || {};

  function randomKey(prefix) {
    var suffix = (window.crypto && window.crypto.randomUUID)
      ? window.crypto.randomUUID()
      : Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 10);
    return (prefix || "operation") + "-ui-" + suffix;
  }

  function listItems(response) {
    return (window.API && window.API.normalizeList(response && response.data, response && response.pagination).items) || [];
  }

  function label(value, fallback) {
    var text = String(value == null || value === "" ? (fallback || "—") : value);
    return text.toLowerCase().replace(/_/g, " ");
  }

  function omitEmpty(values) {
    var output = {};
    Object.keys(values || {}).forEach(function (key) {
      var value = values[key];
      if (value !== "" && value !== null && value !== undefined) output[key] = value;
    });
    return output;
  }

  /* A formModal custom field that searches a paginated resource on demand.
     options: {
       name (API field name), resource, placeholder, required, minChars,
       params (object), display(row) => text, value(row) => API value
     }
     The returned field integrates with dashboard.formModal's custom-field
     contract and emits no key at all while an optional picker is blank. */
  function searchPicker(options) {
    options = options || {};
    var selected = null;
    var currentRows = [];
    var mount;
    var input;
    var results;
    var chosen;
    var minChars = options.minChars == null ? 2 : Math.max(1, Number(options.minChars) || 2);

    function display(row) {
      if (typeof options.display === "function") return options.display(row);
      return row && (row.full_name || row.name || row.room_number || row.reference || row.email || String(row.id));
    }
    function value(row) {
      return typeof options.value === "function" ? options.value(row) : row && row.id;
    }
    function paintSelection() {
      if (!chosen) return;
      chosen.innerHTML = "";
      if (!selected) {
        chosen.hidden = true;
        return;
      }
      chosen.hidden = false;
      var text = document.createElement("span");
      text.textContent = "Selected: " + display(selected);
      var clear = document.createElement("button");
      clear.type = "button";
      clear.className = "btn btn-sm btn-ghost";
      clear.textContent = "Change";
      clear.setAttribute("aria-label", "Change selected " + (options.label || "record").toLowerCase());
      clear.addEventListener("click", function () {
        selected = null;
        paintSelection();
        if (input) { input.value = ""; input.focus(); }
        if (results) results.innerHTML = '<p class="caption">Search to select a record.</p>';
      });
      chosen.appendChild(text);
      chosen.appendChild(clear);
    }
    function renderRows(rows) {
      currentRows = rows || [];
      results.innerHTML = "";
      if (!currentRows.length) {
        var empty = document.createElement("p");
        empty.className = "caption";
        empty.textContent = "No matching records found.";
        results.appendChild(empty);
        return;
      }
      currentRows.forEach(function (row, index) {
        var button = document.createElement("button");
        button.type = "button";
        button.className = "op-picker-option";
        button.dataset.opPickerIndex = String(index);
        button.textContent = display(row);
        results.appendChild(button);
      });
    }
    async function search() {
      var term = String(input && input.value || "").trim();
      if (term.length < minChars) {
        results.innerHTML = '<p class="caption">Enter at least ' + minChars + ' characters to search.</p>';
        return;
      }
      if (!window.API || typeof window.API.list !== "function") {
        results.textContent = "Search is unavailable. Refresh and try again.";
        return;
      }
      if (J.dashboard && J.dashboard.DATA) J.dashboard.DATA.loading(results, "list");
      try {
        var params = Object.assign({}, options.params || {}, { search: term, page_size: options.pageSize || 8 });
        var response = await window.API.list(options.resource, params);
        renderRows(listItems(response));
      } catch (err) {
        if (J.dashboard && J.dashboard.DATA) {
          J.dashboard.DATA.error(results, "Search couldn't be completed", (err && err.message) || "Please try again.", 1);
        } else {
          results.textContent = (err && err.message) || "Search couldn't be completed.";
        }
      }
    }
    var debouncedSearch = function () { search(); };

    return {
      name: options.name,
      label: options.label,
      type: "custom",
      required: !!options.required,
      help: options.help,
      render: function (target) {
        mount = target;
        var wrap = document.createElement("div");
        wrap.className = "op-picker";
        input = document.createElement("input");
        input.type = "search";
        input.className = "input";
        input.autocomplete = "off";
        input.placeholder = options.placeholder || "Start typing to search";
        input.setAttribute("aria-label", "Search " + (options.label || "records").toLowerCase());
        chosen = document.createElement("div");
        chosen.className = "op-picker-selected";
        chosen.hidden = true;
        results = document.createElement("div");
        results.className = "op-picker-results";
        results.innerHTML = '<p class="caption">Enter at least ' + minChars + ' characters to search.</p>';
        input.addEventListener("input", J.debounce(debouncedSearch, options.debounceMs || 280));
        results.addEventListener("click", function (event) {
          var button = event.target.closest && event.target.closest("[data-op-picker-index]");
          if (!button) return;
          var row = currentRows[Number(button.dataset.opPickerIndex)];
          if (!row) return;
          selected = row;
          paintSelection();
          results.innerHTML = "";
          input.value = "";
        });
        wrap.appendChild(input);
        wrap.appendChild(chosen);
        wrap.appendChild(results);
        mount.appendChild(wrap);
      },
      getValue: function () {
        if (!selected) return {};
        var output = {};
        output[options.name] = value(selected);
        return output;
      },
      validate: function () {
        if (options.required && !selected) return "Select a " + (options.label || "record").toLowerCase() + " from search results.";
        return "";
      },
      clear: function () {
        selected = null;
        currentRows = [];
        paintSelection();
      }
    };
  }

  /* Multi-select variant for bounded lists such as stock-count snapshots.
     It keeps selected records locally only after an explicit search result was
     chosen; it never treats arbitrary typed text as an authoritative ID. */
  function multiSearchPicker(options) {
    options = options || {};
    var selected = [];
    var currentRows = [];
    var input, results, chips;
    var minChars = options.minChars == null ? 2 : Math.max(1, Number(options.minChars) || 2);
    function display(row) {
      if (typeof options.display === "function") return options.display(row);
      return row && (row.name || row.full_name || row.sku || row.reference || String(row.id));
    }
    function value(row) { return typeof options.value === "function" ? options.value(row) : row && row.id; }
    function paintChips() {
      if (!chips) return;
      chips.innerHTML = "";
      selected.forEach(function (row, index) {
        var chip = document.createElement("span");
        chip.className = "op-picker-chip";
        var text = document.createElement("span"); text.textContent = display(row);
        var remove = document.createElement("button");
        remove.type = "button"; remove.className = "btn btn-sm btn-ghost"; remove.textContent = "Remove";
        remove.setAttribute("aria-label", "Remove " + display(row));
        remove.addEventListener("click", function () { selected.splice(index, 1); paintChips(); });
        chip.appendChild(text); chip.appendChild(remove); chips.appendChild(chip);
      });
      chips.hidden = !selected.length;
    }
    function renderRows(rows) {
      currentRows = rows || []; results.innerHTML = "";
      if (!currentRows.length) { results.innerHTML = '<p class="caption">No matching records found.</p>'; return; }
      currentRows.forEach(function (row, index) {
        var button = document.createElement("button");
        button.type = "button"; button.className = "op-picker-option"; button.dataset.opMultiPickerIndex = String(index);
        button.textContent = display(row); results.appendChild(button);
      });
    }
    async function search() {
      var term = String(input && input.value || "").trim();
      if (term.length < minChars) { results.innerHTML = '<p class="caption">Enter at least ' + minChars + ' characters to search.</p>'; return; }
      if (J.dashboard && J.dashboard.DATA) J.dashboard.DATA.loading(results, "list");
      try {
        var response = await window.API.list(options.resource, Object.assign({}, options.params || {}, { search: term, page_size: options.pageSize || 8 }));
        renderRows(listItems(response));
      } catch (err) {
        if (J.dashboard && J.dashboard.DATA) J.dashboard.DATA.error(results, "Search couldn't be completed", (err && err.message) || "Please try again.", 1);
        else results.textContent = (err && err.message) || "Search couldn't be completed.";
      }
    }
    return {
      name: options.name, label: options.label, type: "custom", required: !!options.required, help: options.help,
      render: function (mount) {
        var wrap = document.createElement("div"); wrap.className = "op-picker";
        input = document.createElement("input"); input.type = "search"; input.className = "input"; input.autocomplete = "off";
        input.placeholder = options.placeholder || "Start typing to search";
        input.setAttribute("aria-label", "Search " + (options.label || "records").toLowerCase());
        chips = document.createElement("div"); chips.className = "op-picker-chips"; chips.hidden = true;
        results = document.createElement("div"); results.className = "op-picker-results";
        results.innerHTML = '<p class="caption">Enter at least ' + minChars + ' characters to search.</p>';
        input.addEventListener("input", J.debounce(search, options.debounceMs || 280));
        results.addEventListener("click", function (event) {
          var button = event.target.closest && event.target.closest("[data-op-multi-picker-index]");
          if (!button) return;
          var row = currentRows[Number(button.dataset.opMultiPickerIndex)];
          if (!row || selected.some(function (picked) { return String(value(picked)) === String(value(row)); })) return;
          selected.push(row); paintChips(); results.innerHTML = ""; input.value = "";
        });
        wrap.appendChild(input); wrap.appendChild(chips); wrap.appendChild(results); mount.appendChild(wrap);
      },
      getValue: function () {
        if (!selected.length) return {};
        var output = {}; output[options.name] = selected.map(value); return output;
      },
      validate: function () { return options.required && !selected.length ? "Select at least one " + (options.label || "record").toLowerCase() + "." : ""; }
    };
  }

  /* Search-and-line builder for immutable BOMs and purchase-order drafts.
     Every line starts from a selected server result and carries explicit,
     operator-entered quantities/costs; the backend remains authoritative for
     validation and workflow state. */
  function linePicker(options) {
    options = options || {};
    var lines = [], currentRows = [], input, results, lineList;
    var minChars = options.minChars == null ? 2 : Math.max(1, Number(options.minChars) || 2);
    var fields = options.lineFields || [];
    function display(row) {
      if (typeof options.display === "function") return options.display(row);
      return row && (row.name || row.full_name || row.sku || row.reference || String(row.id));
    }
    function value(row) { return typeof options.value === "function" ? options.value(row) : row && row.id; }
    function paintLines() {
      if (!lineList) return;
      lineList.innerHTML = "";
      lines.forEach(function (line, index) {
        var row = document.createElement("div"); row.className = "op-line-picker-row";
        var title = document.createElement("strong"); title.textContent = display(line.record); row.appendChild(title);
        var controls = document.createElement("div"); controls.className = "op-line-picker-controls";
        fields.forEach(function (field) {
          var wrap = document.createElement("label"); wrap.className = "field";
          var labelNode = document.createElement("span"); labelNode.className = "field-label"; labelNode.textContent = field.label || field.name;
          var control = document.createElement("input"); control.className = "input"; control.type = field.type || "number";
          control.name = field.name; if (field.step) control.step = field.step; if (field.min != null) control.min = field.min;
          control.value = line.values[field.name] == null ? "" : line.values[field.name];
          control.addEventListener("input", function () { line.values[field.name] = control.value; });
          wrap.appendChild(labelNode); wrap.appendChild(control); controls.appendChild(wrap);
        });
        var remove = document.createElement("button"); remove.type = "button"; remove.className = "btn btn-sm btn-ghost"; remove.textContent = "Remove";
        remove.addEventListener("click", function () { lines.splice(index, 1); paintLines(); });
        row.appendChild(controls); row.appendChild(remove); lineList.appendChild(row);
      });
      lineList.hidden = !lines.length;
    }
    function renderRows(rows) {
      currentRows = rows || []; results.innerHTML = "";
      if (!currentRows.length) { results.innerHTML = '<p class="caption">No matching records found.</p>'; return; }
      currentRows.forEach(function (row, index) {
        var button = document.createElement("button"); button.type = "button"; button.className = "op-picker-option";
        button.dataset.opLinePickerIndex = String(index); button.textContent = display(row); results.appendChild(button);
      });
    }
    async function search() {
      var term = String(input && input.value || "").trim();
      if (term.length < minChars) { results.innerHTML = '<p class="caption">Enter at least ' + minChars + ' characters to search.</p>'; return; }
      if (J.dashboard && J.dashboard.DATA) J.dashboard.DATA.loading(results, "list");
      try { var response = await window.API.list(options.resource, Object.assign({}, options.params || {}, { search: term, page_size: options.pageSize || 8 })); renderRows(listItems(response)); }
      catch (err) { if (J.dashboard && J.dashboard.DATA) J.dashboard.DATA.error(results, "Search couldn't be completed", (err && err.message) || "Please try again.", 1); else results.textContent = (err && err.message) || "Search couldn't be completed."; }
    }
    return {
      name: options.name, label: options.label, type: "custom", required: !!options.required, help: options.help,
      render: function (mount) {
        var wrap = document.createElement("div"); wrap.className = "op-picker";
        input = document.createElement("input"); input.type = "search"; input.className = "input"; input.autocomplete = "off";
        input.placeholder = options.placeholder || "Start typing to add a line";
        input.setAttribute("aria-label", "Search " + (options.label || "records").toLowerCase());
        lineList = document.createElement("div"); lineList.className = "op-line-picker-list"; lineList.hidden = true;
        results = document.createElement("div"); results.className = "op-picker-results"; results.innerHTML = '<p class="caption">Enter at least ' + minChars + ' characters to search.</p>';
        input.addEventListener("input", J.debounce(search, options.debounceMs || 280));
        results.addEventListener("click", function (event) {
          var button = event.target.closest && event.target.closest("[data-op-line-picker-index]"); if (!button) return;
          var record = currentRows[Number(button.dataset.opLinePickerIndex)]; if (!record) return;
          if (lines.some(function (line) { return String(value(line.record)) === String(value(record)); })) return;
          var values = {}; fields.forEach(function (field) { values[field.name] = field.defaultValue == null ? "" : String(field.defaultValue); });
          lines.push({ record: record, values: values }); paintLines(); results.innerHTML = ""; input.value = "";
        });
        wrap.appendChild(input); wrap.appendChild(lineList); wrap.appendChild(results); mount.appendChild(wrap);
      },
      getValue: function () {
        if (!lines.length) return {};
        var output = {}; output[options.name] = lines.map(function (line) {
          var mapped = {}; mapped[options.itemKey || "item_id"] = value(line.record);
          fields.forEach(function (field) { mapped[field.name] = line.values[field.name]; }); return mapped;
        }); return output;
      },
      validate: function () {
        if (options.required && !lines.length) return "Add at least one " + (options.label || "line").toLowerCase() + ".";
        for (var i = 0; i < lines.length; i += 1) {
          for (var j = 0; j < fields.length; j += 1) {
            var field = fields[j], raw = lines[i].values[field.name];
            if (field.required && (raw === "" || raw == null)) return "Enter " + (field.label || field.name) + " for every line.";
            if (field.min != null && raw !== "" && Number(raw) < Number(field.min)) return (field.label || field.name) + " must be at least " + field.min + ".";
          }
        }
        return "";
      }
    };
  }

  OPS.newIdempotencyKey = randomKey;
  OPS.listItems = listItems;
  OPS.label = label;
  OPS.omitEmpty = omitEmpty;
  OPS.searchPicker = searchPicker;
  OPS.multiSearchPicker = multiSearchPicker;
  OPS.linePicker = linePicker;
})();
