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
    var refreshHistory = document.querySelector("[data-amenity-history-refresh]");
    if (refreshHistory) refreshHistory.addEventListener("click", loadHistory);

    // Allocate form
    var allocForm = document.querySelector("[data-amenity-allocate-form]");
    if (allocForm) allocForm.addEventListener("submit", onAllocate);

    // Issue form
    var issueForm = document.querySelector("[data-amenity-issue-form]");
    if (issueForm) issueForm.addEventListener("submit", onIssue);

    // Load stock on init
    loadStock();
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
      var data = unwrap(res);
      var items = (data.results || data.items || []).filter(function (i) {
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
    API.get("/api/admin/service-requests/reception/amenities/stock/").then(function (res) {
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
      var data = unwrap(res);
      var locs = data.results || data.items || [];
      sel.innerHTML = '<option value="">— Select —</option>';
      locs.forEach(function (loc) {
        if (loc.code && loc.code.toUpperCase() === "RECEPTION") return; // don't allocate from Reception
        var opt = document.createElement("option");
        opt.value = loc.id;
        opt.textContent = loc.code + " — " + loc.name;
        sel.appendChild(opt);
      });
    }).catch(function (err) { UI.toast(err.message || "Unable to load selection. Refresh to retry.", "error"); });
  }

  /* ------------------------------ Load data ------------------------------- */

  function loadStock() {
    var container = document.querySelector("[data-amenity-stock-container]");
    if (!container) return;
    container.innerHTML = ReceptionServices.loading();
    API.get("/api/admin/service-requests/reception/amenities/stock/").then(function (res) {
      var data = unwrap(res);
      if (!data.location) {
        container.innerHTML = '<div class="alert alert-warning">No Reception stock location found. Ask the Store Keeper to create one with code <strong>RECEPTION</strong>.</div>';
        return;
      }
      var balances = data.balances || [];
      if (!balances.length) {
        container.innerHTML = '<p class="text-muted">No amenity stock allocated to Reception yet.</p>';
        return;
      }
      var html = '<p class="caption">Location: ' + esc(data.location.name) + ' (' + esc(data.location.code) + ')</p><table class="table"><thead><tr><th>SKU</th><th>Name</th><th>Base Unit</th><th>Quantity</th><th>Status</th></tr></thead><tbody>';
      balances.forEach(function (b) {
        html += "<tr><td><code>" + esc(b.sku) + "</code></td><td>" + esc(b.name) + "</td><td>" + esc(b.base_unit) + "</td><td><strong>" + esc(b.quantity) + "</strong></td><td>" + (b.is_active === false ? "Inactive" : Number(b.quantity) <= 0 ? "Unavailable" : b.low_stock ? "Low stock" : "Available") + "</td></tr>";
      });
      html += "</tbody></table>";
      container.innerHTML = html;
    }).catch(function (err) {
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
      item_id: parseInt(form.item_id.value),
      quantity: form.quantity.value,
      from_location_id: parseInt(form.from_location_id.value),
      notes: form.notes.value,
    };
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

  function onIssue(e) {
    e.preventDefault();
    var form = e.target;
    var payload = {
      guest_id: parseInt(form.guest_id.value),
      room_number: form.room_number.value,
      item_id: parseInt(form.item_id.value),
      quantity: form.quantity.value,
      notes: form.notes.value,
    };
    /* Attach the issuance to the stay when we know it (deep link / check-in). */
    var bookingId = form.booking_id && form.booking_id.value ? parseInt(form.booking_id.value) : null;
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
