/* js/amenities.js */
/* ==========================================================================
   Guest Amenities dashboard page — Reception stock allocation & issuance.

   Tabs:
   - Reception Stock: shows the Reception location's current amenity balances
   - Allocate: Store Keeper transfers amenity stock into the Reception location
   - Issue to Guest: Receptionist issues amenities to a checking-in guest
   - History: audit trail of all issuances
   ========================================================================== */
(function () {
  "use strict";

  /* The API object: JONE.api when something exposes it under that
     convention, and window.API (always set by js/api.js) as the reliable
     fallback. Reading only JONE.api left API undefined, so init() returned
     before the page did anything. */
  var API = (window.JONE && window.JONE.api) || window.API;
  var UI = window.JONE && window.JONE.ui;
  var esc = window.JONE && window.JONE.esc;

  /* API.get resolves to the response envelope {status, ok, data, message};
     every read below needs the PAYLOAD, so unwrap it exactly the way
     API.unwrap does. Reading the envelope as if it were the payload is how
     the guest picker, the stock table and the deep link stayed empty on this
     page. */
  function unwrap(res) { return (res && res.data !== undefined) ? res.data : res; }

  /* Payload -> array of records.
     The paginated list endpoints answer { success, message, data: [...],
     pagination: {...} }, so after unwrap() the payload is ALREADY an array.
     Only endpoints that name a key (`{results: [...]}`) or a legacy client
     that hands over the whole envelope still need unwrapping. Reading
     `.results` first meant every dropdown on this page was built from an
     ARRAY value `.results` (undefined) and therefore stayed empty: stock items
     could not be selected, and the store-location list never appeared. */
  function listOf(res) {
    var data = unwrap(res);
    if (Array.isArray(data)) return data;
    if (data && Array.isArray(data.results)) return data.results;
    if (data && Array.isArray(data.items)) return data.items;
    if (res && Array.isArray(res.results)) return res.results;
    return [];
  }

  function init() {
    if (!API || !UI) return;

    // Tab switching
    document.querySelectorAll("[data-tab]").forEach(function (tab) {
      tab.addEventListener("click", function () {
        var name = tab.getAttribute("data-tab");
        document.querySelectorAll("[data-tab]").forEach(function (t) {
          t.classList.toggle("active", t === tab);
          t.setAttribute("aria-selected", t === tab ? "true" : "false");
        });
        document.querySelectorAll("[data-panel]").forEach(function (p) {
          p.classList.toggle("hidden", p.getAttribute("data-panel") !== name);
        });
        if (name === "stock") loadStock();
        if (name === "history") loadHistory();
      });
    });

    // Load dropdowns
    var user = window.Auth && window.Auth.state && window.Auth.state.user;
    var canAllocate = user && ["ADMIN", "GENERAL_MANAGER", "MANAGER", "STOREKEEPER"].indexOf(String(user.role).toUpperCase()) !== -1;
    document.querySelector('[data-tab="allocate"]').hidden = !canAllocate;
    if (canAllocate) { loadItemSelects(); loadLocationSelect(); }
    var canIssue = !user || String(user.role).toUpperCase() !== "STOREKEEPER";
    if (canIssue) {
      ReceptionServices.guests(document.querySelector("[data-amenity-issue-form]"), {deepLink:true});
      loadIssueItemSelect();
    } else {
      document.querySelector('[data-tab="issue"]').hidden = true;
      document.querySelector('[data-tab="history"]').hidden = true;
    }

    // Refresh buttons
    var refreshStock = document.querySelector("[data-amenity-refresh]");
    if (refreshStock) refreshStock.addEventListener("click", loadStock);

    /* Stock search: filters the already-loaded shelf, so it is instant and
       costs no API traffic. Debounced to one re-render per pause, and the
       container is delegated so the "Clear search" button added by
       renderStock() works without re-binding after every render. */
    var stockSearch = document.querySelector("[data-amenity-stock-search]");
    if (stockSearch) {
      stockSearch.addEventListener("input", function () {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(renderStock, 200);
      });
      stockSearch.addEventListener("search", function () {
        // Native clear ("x") in a type=search input.
        clearTimeout(searchTimer);
        renderStock();
      });
      stockSearch.addEventListener("keydown", function (event) {
        if (event.key === "Escape") {
          stockSearch.value = "";
          renderStock();
        }
      });
    }
    var stockContainer = document.querySelector("[data-amenity-stock-container]");
    if (stockContainer) {
      stockContainer.addEventListener("click", function (event) {
        if (!event.target.closest("[data-amenity-stock-clear]")) return;
        if (stockSearch) { stockSearch.value = ""; stockSearch.focus(); }
        renderStock();
      });
    }
    var refreshHistory = document.querySelector("[data-amenity-history-refresh]");
    if (refreshHistory) refreshHistory.addEventListener("click", loadHistory);

    // Allocate form
    var allocForm = document.querySelector("[data-amenity-allocate-form]");
    if (allocForm) allocForm.addEventListener("submit", onAllocate);

    // Issue form
    var issueForm = document.querySelector("[data-amenity-issue-form]");
    if (issueForm) issueForm.addEventListener("submit", onIssue);

    departmentSelects().forEach(function (sel) {
      sel.addEventListener("change", function () {
        departmentSelects().forEach(function (other) { if (other !== sel) other.value = sel.value; });
        loadStock();
        if (canIssue) loadIssueItemSelect();
      });
    });

    // Load the configured department shelf, then refresh the stock and issue
    // picker against exactly that location.
    loadDepartmentLocationSelect().then(function () {
      loadStock();
      if (canIssue) loadIssueItemSelect();
    }).catch(function () { loadStock(); });
  }

  /* ---------------------- Configured department store --------------------- */

  function departmentSelects() {
    return document.querySelectorAll("[data-amenity-shelf-select],[data-amenity-department-location-select]");
  }
  function departmentLocationId() {
    var selects = departmentSelects();
    for (var i = 0; i < selects.length; i += 1) {
      if (selects[i].value) return selects[i].value;
    }
    return "";
  }
  function withLocation(url) {
    var id = departmentLocationId();
    return id ? url + (url.indexOf("?") === -1 ? "?" : "&") + "location_id=" + encodeURIComponent(id) : url;
  }
  function loadDepartmentLocationSelect() {
    var selects = departmentSelects();
    if (!selects.length || !API) return Promise.resolve();
    return API.get("/api/admin/inventory/locations/?page_size=200&active=true").then(function (res) {
      var locs = listOf(res).filter(function (loc) { return loc.is_active !== false; });
      var configured = locs.filter(function (loc) { return loc.is_reception; })[0] ||
        locs.filter(function (loc) { return String(loc.code || "").toUpperCase() === "RECEPTION"; })[0];
      selects.forEach(function (sel) {
        var keep = sel.value;
        sel.innerHTML = '<option value="">Use configured Reception store</option>';
        locs.forEach(function (loc) {
          var opt = document.createElement("option");
          opt.value = loc.id;
          opt.textContent = loc.code + " — " + loc.name + (loc.is_reception ? " (configured)" : "");
          sel.appendChild(opt);
        });
        sel.value = keep || (configured ? String(configured.id) : "");
      });
    });
  }

  /* ------------------------------ Dropdowns -------------------------------- */

  /* The ALLOCATE dropdown needs the whole catalogue (the store keeper is
     choosing what to send to Reception). The ISSUE dropdown must NOT: it can
     only offer what Reception actually holds, otherwise the desk picks a soap
     and gets a 400 after the guest has been promised it. */
  function loadItemSelects() {
    var selects = document.querySelectorAll("[data-amenity-item-select]");
    if (!selects.length) return;
    API.get("/api/admin/inventory/items/?page_size=500").then(function (res) {
      var items = listOf(res).filter(function (i) {
        return i.is_active !== false;
      });
      selects.forEach(function (sel) {
        sel.innerHTML = '<option value="">— Select —</option>';
        items.forEach(function (item) {
          var opt = document.createElement("option");
          opt.value = item.id;
          opt.textContent = item.sku + " — " + item.name + " (" + item.base_unit + ")";
          sel.appendChild(opt);
        });
      });
    }).catch(function (err) { UI.toast(err.message || "Unable to load selection. Refresh to retry.", "error"); });
  }

  /* Issue dropdown = the Reception Department's own shelf. */
  function loadIssueItemSelect() {
    var sel = document.querySelector("[data-amenity-issue-item-select]");
    if (!sel) return;
    var keep = sel.value;
    API.get(withLocation("/api/admin/service-requests/reception/amenities/stock/")).then(function (res) {
      var data = unwrap(res);
      var rows = (data && data.balances) || [];
      var available = rows.filter(function (r) { return r.is_active !== false && Number(r.quantity) > 0; });
      sel.innerHTML = '<option value="">— Select —</option>';
      available.forEach(function (row) {
        var opt = document.createElement("option");
        opt.value = row.item_id;
        opt.textContent = row.name + " — " + row.quantity + " " + row.base_unit + " on the Reception shelf";
        sel.appendChild(opt);
      });
      if (!available.length) {
        sel.innerHTML = '<option value="">Nothing on the Reception shelf yet</option>';
        sel.disabled = true;
      } else {
        sel.disabled = false;
        if (keep) sel.value = keep;
      }
    }).catch(function (err) { UI.toast(err.message || "Unable to load selection. Refresh to retry.", "error"); });
  }

  function loadLocationSelect() {
    var sel = document.querySelector("[data-amenity-location-select]");
    if (!sel) return;
    API.get("/api/admin/inventory/locations/?page_size=200").then(function (res) {
      var locs = listOf(res);
      sel.innerHTML = '<option value="">— Select —</option>';
      locs.forEach(function (loc) {
        if (loc.is_reception || (loc.code && loc.code.toUpperCase() === "RECEPTION")) return; // don't allocate from the configured Reception store
        var opt = document.createElement("option");
        opt.value = loc.id;
        opt.textContent = loc.code + " — " + loc.name;
        sel.appendChild(opt);
      });
    }).catch(function (err) { UI.toast(err.message || "Unable to load selection. Refresh to retry.", "error"); });
  }

  /* ------------------------------ Load data ------------------------------- */

  /* --------------------------- Stock + search ----------------------------- */

  /* Last successful shelf payload. Search re-renders from this snapshot so
     typing never re-hits the API (debounced or not) and the balances on screen
     always come from the server, never from a client-side recomputation. */
  var shelf = null;
  var searchTimer = null;

  function stockMatches(row, term) {
    if (!term) return true;
    var haystack = [row.name, row.sku, row.base_unit].join(" ").toLowerCase();
    return haystack.indexOf(term) !== -1;
  }

  function renderStock() {
    var container = document.querySelector("[data-amenity-stock-container]");
    if (!container || !shelf) return;
    if (!shelf.location) {
      container.innerHTML = '<div class="alert alert-warning">No department store is configured. Ask an administrator to mark an active stock location as the Reception store in Inventory.</div>';
      return;
    }
    var searchInput = document.querySelector("[data-amenity-stock-search]");
    /* Case-insensitive and whitespace-tolerant: a clerk pasting " amn-soap "
       must still find AMN-SOAP. */
    var term = searchInput ? String(searchInput.value || "").trim().toLowerCase() : "";
    var balances = (shelf.balances || []).filter(function (row) { return stockMatches(row, term); });

    var head = '<p class="caption">Location: ' + esc(shelf.location.name) + ' (' + esc(shelf.location.code) + ')</p>';
    if (!shelf.balances.length) {
      container.innerHTML = head + '<p class="text-muted">No amenity stock allocated to Reception yet.</p>';
      return;
    }
    if (!balances.length) {
      container.innerHTML = head + '<p class="text-muted" data-amenity-stock-empty>No stock items match “' + esc(term) + '”. <button type="button" class="btn btn-sm btn-outline" data-amenity-stock-clear>Clear search</button></p>';
      return;
    }
    var html = head + '<table class="table"><thead><tr><th>SKU</th><th>Name</th><th>Base Unit</th><th>Quantity</th><th>Status</th></tr></thead><tbody>';
    balances.forEach(function (b) {
      html += "<tr><td><code>" + esc(b.sku) + "</code></td><td>" + esc(b.name) + "</td><td>" + esc(b.base_unit) + "</td><td><strong>" + esc(b.quantity) + "</strong></td><td>" + (b.is_active === false ? "Inactive" : Number(b.quantity) <= 0 ? "Unavailable" : b.low_stock ? "Low stock" : "Available") + "</td></tr>";
    });
    html += "</tbody></table>";
    if (term) {
      html += '<p class="caption">Showing ' + balances.length + " of " + shelf.balances.length + ' item(s). <button type="button" class="btn btn-sm btn-outline" data-amenity-stock-clear>Clear search</button></p>';
    }
    container.innerHTML = html;
  }

  function loadStock() {
    var container = document.querySelector("[data-amenity-stock-container]");
    if (!container) return;
    container.innerHTML = ReceptionServices.loading();
    API.get(withLocation("/api/admin/service-requests/reception/amenities/stock/")).then(function (res) {
      var data = unwrap(res);
      shelf = { location: data.location || null, balances: (data && data.balances) || [] };
      renderStock();
    }).catch(function (err) {
      shelf = null;
      container.innerHTML = '<div class="alert alert-error">Failed to load stock: ' + esc(err.message || "") + "</div>";
    });
  }

  function loadHistory() {
    var container = document.querySelector("[data-amenity-history-container]");
    if (!container) return;
    container.innerHTML = ReceptionServices.loading();
    API.get("/api/admin/service-requests/reception/amenities/issues/?page_size=100").then(function (res) {
      var data = unwrap(res);
      var issues = data.results || [];
      if (!issues.length) {
        container.innerHTML = '<p class="text-muted">No amenity issuances recorded yet.</p>';
        return;
      }
      var html = '<table class="table"><thead><tr><th>Reference</th><th>Guest</th><th>Room</th><th>Item</th><th>Qty</th><th>Balance After</th><th>Date</th></tr></thead><tbody>';
      issues.forEach(function (i) {
        html += "<tr><td><code>" + esc(i.reference) + "</code></td>";
        html += "<td>" + esc(i.guest_name || "Guest name unavailable") + "</td>";
        html += "<td>" + esc(i.room_number || "—") + "</td>";
        html += "<td><code>" + esc(i.item_name || i.item_sku || "—") + "</code></td>";
        html += "<td>" + esc(i.quantity) + "</td>";
        html += "<td>" + esc(i.departmental_balance_after) + "</td>";
        html += "<td>" + esc(i.occurred_at || "") + "</td>";
        html += "</tr>";
      });
      html += "</tbody></table>";
      container.innerHTML = html;
    }).catch(function (err) {
      container.innerHTML = '<div class="alert alert-error">Failed to load history: ' + esc(err.message || "") + "</div>";
    });
  }

  /* ------------------------------ Actions -------------------------------- */

  function onAllocate(e) {
    e.preventDefault();
    var form = e.target;
    var payload = {
      item_id: parseInt(fieldValue(form, "item_id"), 10),
      quantity: fieldValue(form, "quantity"),
      from_location_id: parseInt(fieldValue(form, "from_location_id"), 10),
      notes: fieldValue(form, "notes"),
    };
    var locationId = departmentLocationId();
    if (locationId) payload.location_id = parseInt(locationId);
    if (!payload.item_id || !payload.quantity || !payload.from_location_id) {
      UI.toast("Please fill in all required fields.", "warning");
      return;
    }
    UI.confirm({title:"Allocate stock", message:"Allocate this stock to the Reception Department?"}).then(function (ok) {
      if (!ok) return;
      ReceptionServices.postOnce(form, "/api/admin/service-requests/reception/amenities/allocate/", payload).then(function (res) {
        var data = unwrap(res);
        UI.toast(data.created ? "Stock allocated to Reception." : "Allocation already exists.", "success");
        form.reset();
        loadStock();
      }).catch(function (err) {
        UI.toast("Allocation failed: " + (err.message || ""), "error");
      });
    });
  }

  /* Every read goes through the form's element map so an optional field that a
     future markup change removes (or a cached older page) cannot throw and
     strand the clerk with a dialog that silently does nothing. */
  function fieldValue(form, name) {
    var field = form && (form.elements ? form.elements[name] : form[name]);
    return field && field.value != null ? field.value : "";
  }

  function onIssue(e) {
    e.preventDefault();
    var form = e.target;
    var payload = {
      guest_id: parseInt(fieldValue(form, "guest_id"), 10),
      room_number: fieldValue(form, "room_number"),
      item_id: parseInt(fieldValue(form, "item_id"), 10),
      quantity: fieldValue(form, "quantity"),
      notes: fieldValue(form, "notes"),
    };
    var locationId = departmentLocationId();
    if (locationId) payload.location_id = parseInt(locationId);
    /* Attach the issuance to the stay when we know it (deep link / check-in). */
    var rawBooking = fieldValue(form, "booking_id");
    var bookingId = rawBooking ? parseInt(rawBooking, 10) : null;
    if (bookingId) payload.booking_id = bookingId;
    if (!payload.guest_id || !payload.item_id || !payload.quantity) {
      UI.toast("Please fill in all required fields.", "warning");
      return;
    }
    ReceptionServices.postOnce(form, "/api/admin/service-requests/reception/amenities/issue/", payload).then(function (res) {
      var data = unwrap(res);
      UI.toast(data.created ? "Amenity issued to guest." : "Issuance already recorded.", "success");
      form.reset();
      loadStock();
      loadIssueItemSelect();
      loadHistory();
    }).catch(function (err) {
      UI.toast("Issuance failed: " + (err.message || ""), "error");
    });
  }

  /* -------------------------------- Boot ---------------------------------- */
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
