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

  var API = window.JONE && window.JONE.api;
  var UI = window.JONE && window.JONE.ui;
  var esc = window.JONE && window.JONE.esc;

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
    loadItemSelects();
    loadLocationSelect();
    loadGuestSelect();
    loadIssueItemSelect();

    // Guest search (server-side): the desk types a name instead of scrolling
    // through every guest the hotel has ever had.
    var guestSearch = document.querySelector("[data-amenity-guest-search]");
    if (guestSearch) {
      guestSearch.addEventListener("input", function () {
        if (guestSearchTimer) clearTimeout(guestSearchTimer);
        guestSearchTimer = setTimeout(function () { loadGuestSelect(guestSearch.value); }, 250);
      });
    }

    // ?booking=REF & friends: preselect the stay being recorded against.
    applyDeepLink();

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
    API.get("/api/admin/inventory/items/?page_size=500").then(function (data) {
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
    }).catch(function () {});
  }

  /* Issue dropdown = the Reception Department's own shelf. */
  function loadIssueItemSelect() {
    var sel = document.querySelector("[data-amenity-issue-item-select]");
    if (!sel) return;
    var keep = sel.value;
    API.get("/api/admin/service-requests/reception/amenities/stock/").then(function (data) {
      var rows = (data && data.balances) || [];
      var available = rows.filter(function (r) { return Number(r.quantity) > 0; });
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
    }).catch(function () {});
  }

  function loadLocationSelect() {
    var sel = document.querySelector("[data-amenity-location-select]");
    if (!sel) return;
    API.get("/api/admin/inventory/locations/?page_size=200").then(function (data) {
      var locs = data.results || data.items || [];
      sel.innerHTML = '<option value="">— Select —</option>';
      locs.forEach(function (loc) {
        if (loc.code && loc.code.toUpperCase() === "RECEPTION") return; // don't allocate from Reception
        var opt = document.createElement("option");
        opt.value = loc.id;
        opt.textContent = loc.code + " — " + loc.name;
        sel.appendChild(opt);
      });
    }).catch(function () {});
  }

  /* Guest picker.

     It used to load 500 guests and hope the right one was in the first page,
     with no search and no tie to the stay being checked in. Now:
       · the list is a SEARCH result (name/email/phone, debounced), not a dumb
         500-row page;
       · a deep link (?booking=REF, as the check-in screen and the booking page
         pass it) preselects the guest, fills the room and keeps the booking id,
         so the issuance is attached to the stay it belongs to. */
  var guestSearchTimer = null;
  var linkedBooking = null;

  function guestLabel(g) {
    var name = ((g.first_name || "") + " " + (g.last_name || "")).trim() || g.full_name || "Guest";
    return name + (g.email ? " (" + g.email + ")" : (g.phone ? " (" + g.phone + ")" : ""));
  }

  function loadGuestSelect(term) {
    var sel = document.querySelector("[data-amenity-guest-select]");
    if (!sel) return;
    var q = String(term || "").trim();
    /* /api/admin/guests/ is the staff guest list (searchable). The old path,
       /api/bookings/guests/, does not exist: it falls through the guest booking
       route as <lookup>="guests" and 404s, which is why this dropdown used to
       stay empty and the Issue form could never be submitted. */
    var url = "/api/admin/guests/?page_size=50" + (q ? "&search=" + encodeURIComponent(q) : "");
    API.get(url).then(function (data) {
      var guests = data.results || data.items || [];
      var keep = sel.value;
      sel.innerHTML = '<option value="">' + (q ? "— No match; refine the search —" : "— Search above, then select —") + "</option>";
      guests.forEach(function (g) {
        var opt = document.createElement("option");
        opt.value = g.id;
        opt.textContent = guestLabel(g);
        sel.appendChild(opt);
      });
      if (keep) sel.value = keep;
      if (linkedBooking && linkedBooking.guest && linkedBooking.guest.id) sel.value = linkedBooking.guest.id;
    }).catch(function () {});
  }

  /* ?booking=REF / ?guest=ID / ?room=N — the "record it later" path, and the
     link the check-in screen can hand over when the desk skipped the prompt. */
  function applyDeepLink() {
    var params = new URLSearchParams(window.location.search);
    var bookingRef = (params.get("booking") || params.get("ref") || "").trim();
    var guestId = (params.get("guest") || "").trim();
    var room = (params.get("room") || "").trim();
    var context = document.querySelector("[data-amenity-issue-context]");
    var form = document.querySelector("[data-amenity-issue-form]");
    if (room && form && form.room_number) form.room_number.value = room;
    if (!bookingRef) {
      if (guestId) loadGuestSelect("");
      return;
    }
    API.get("/api/admin/bookings/" + encodeURIComponent(bookingRef) + "/").then(function (booking) {
      linkedBooking = booking || null;
      if (!booking || !form) return;
      if (form.booking_id) form.booking_id.value = booking.id || "";
      if (form.guest_id && booking.guest && booking.guest.id) {
        // Keep the guest in the list even when the search box is untouched.
        var sel = form.guest_id;
        if (!Array.prototype.some.call(sel.options, function (o) { return String(o.value) === String(booking.guest.id); })) {
          var opt = document.createElement("option");
          opt.value = booking.guest.id;
          opt.textContent = guestLabel(booking.guest);
          sel.appendChild(opt);
        }
        sel.value = booking.guest.id;
      }
      var rooms = (booking.room_assignments || []).map(function (a) { return a.room_number; }).filter(Boolean);
      if (!room && rooms.length && form.room_number) form.room_number.value = rooms.join(", ");
      if (context) {
        var guestName = booking.guest_name || (booking.guest && booking.guest.full_name) || "";
        context.textContent = "Recording amenities against " + (booking.booking_reference || bookingRef) +
          (guestName ? " — " + guestName : "") +
          (rooms.length ? " (room " + rooms.join(", ") + ")" : "") + ".";
      }
    }).catch(function () {
      if (context) context.textContent = "That booking could not be loaded; pick the guest manually.";
    });
  }

  /* ------------------------------ Load data ------------------------------- */

  function loadStock() {
    var container = document.querySelector("[data-amenity-stock-container]");
    if (!container) return;
    container.innerHTML = '<p class="text-muted">Loading…</p>';
    API.get("/api/admin/service-requests/reception/amenities/stock/").then(function (data) {
      if (!data.location) {
        container.innerHTML = '<div class="alert alert-warning">No Reception stock location found. Ask the Store Keeper to create one with code <strong>RECEPTION</strong>.</div>';
        return;
      }
      var balances = data.balances || [];
      if (!balances.length) {
        container.innerHTML = '<p class="text-muted">No amenity stock allocated to Reception yet.</p>';
        return;
      }
      var html = '<table class="table"><thead><tr><th>SKU</th><th>Name</th><th>Base Unit</th><th>Quantity</th></tr></thead><tbody>';
      balances.forEach(function (b) {
        html += "<tr><td><code>" + esc(b.sku) + "</code></td><td>" + esc(b.name) + "</td><td>" + esc(b.base_unit) + "</td><td><strong>" + esc(b.quantity) + "</strong></td></tr>";
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
    container.innerHTML = '<p class="text-muted">Loading…</p>';
    API.get("/api/admin/service-requests/reception/amenities/issues/?page_size=100").then(function (data) {
      var issues = data.results || [];
      if (!issues.length) {
        container.innerHTML = '<p class="text-muted">No amenity issuances recorded yet.</p>';
        return;
      }
      var html = '<table class="table"><thead><tr><th>Reference</th><th>Guest</th><th>Room</th><th>Item</th><th>Qty</th><th>Balance After</th><th>Date</th></tr></thead><tbody>';
      issues.forEach(function (i) {
        html += "<tr><td><code>" + esc(i.reference) + "</code></td>";
        html += "<td>" + esc(i.guest_name || i.guest) + "</td>";
        html += "<td>" + esc(i.room_number || "—") + "</td>";
        html += "<td><code>" + esc(i.item_sku || i.item) + "</code></td>";
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
    UI.confirm("Allocate this stock to the Reception Department?").then(function (ok) {
      if (!ok) return;
      API.post("/api/admin/service-requests/reception/amenities/allocate/", payload).then(function (data) {
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
    API.post("/api/admin/service-requests/reception/amenities/issue/", payload).then(function (data) {
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
