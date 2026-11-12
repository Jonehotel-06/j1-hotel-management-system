/* js/breakfast.js */
/* ==========================================================================
   Complementary Breakfast dashboard page — record issuances, check eligibility,
   view history, and manage configuration.

   Tabs:
   - Record Breakfast: record a breakfast served to an eligible guest
   - Check Eligibility: verify a guest can receive breakfast on a date
   - History: audit trail of all issuances
   - Configuration: enable/disable, set service window, add notes
   ========================================================================== */
(function () {
  "use strict";

  var API = window.JONE && window.JONE.api;
  var UI = window.JONE && window.JONE.ui;
  var esc = window.JONE && window.JONE.esc;

  function init() {
    if (!API || !UI) return;

    // Set today's date as default
    var todayInput = document.querySelector('[data-breakfast-issue-form] [name="service_date"]');
    if (todayInput) todayInput.value = new Date().toISOString().split("T")[0];
    var eligDateInput = document.querySelector('[data-breakfast-eligibility-form] [name="service_date"]');
    if (eligDateInput) eligDateInput.value = new Date().toISOString().split("T")[0];

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
        if (name === "history") loadHistory();
        if (name === "config") loadConfig();
      });
    });

    // Load guest selects
    loadGuestSelects();

    // Load config banner
    loadConfigBanner();

    // Form handlers
    var issueForm = document.querySelector("[data-breakfast-issue-form]");
    if (issueForm) issueForm.addEventListener("submit", onIssue);
    var eligForm = document.querySelector("[data-breakfast-eligibility-form]");
    if (eligForm) eligForm.addEventListener("submit", onCheckEligibility);
    var configForm = document.querySelector("[data-breakfast-config-form]");
    if (configForm) configForm.addEventListener("submit", onSaveConfig);

    // Refresh
    var refreshHistory = document.querySelector("[data-breakfast-history-refresh]");
    if (refreshHistory) refreshHistory.addEventListener("click", loadHistory);
  }

  /* ------------------------------ Dropdowns -------------------------------- */

  function loadGuestSelects() {
    var selects = document.querySelectorAll("[data-breakfast-guest-select], [data-breakfast-elig-guest-select]");
    if (!selects.length) return;
    API.get("/api/bookings/guests/?page_size=500").then(function (data) {
      var guests = data.results || data.items || [];
      selects.forEach(function (sel) {
        sel.innerHTML = '<option value="">— Select —</option>';
        guests.forEach(function (g) {
          var opt = document.createElement("option");
          opt.value = g.id;
          opt.textContent = (g.first_name || "") + " " + (g.last_name || "") + " (" + (g.email || "") + ")";
          sel.appendChild(opt);
        });
      });
    }).catch(function () {});
  }

  /* ------------------------------ Config ---------------------------------- */

  function loadConfigBanner() {
    var status = document.querySelector("[data-breakfast-config-status]");
    if (!status) return;
    API.get("/api/admin/service-requests/breakfast/config/").then(function (cfg) {
      status.textContent = cfg.is_enabled ? "ENABLED" : "DISABLED";
      if (cfg.is_enabled && cfg.service_start && cfg.service_end) {
        status.textContent += " (" + cfg.service_start + " – " + cfg.service_end + ")";
      }
    }).catch(function () {
      status.textContent = "Configuration not found";
    });
  }

  function loadConfig() {
    API.get("/api/admin/service-requests/breakfast/config/").then(function (cfg) {
      var form = document.querySelector("[data-breakfast-config-form]");
      if (!form) return;
      var enabled = form.querySelector('[name="is_enabled"]');
      if (enabled) enabled.checked = !!cfg.is_enabled;
      var start = form.querySelector('[name="service_start"]');
      if (start) start.value = cfg.service_start || "";
      var end = form.querySelector('[name="service_end"]');
      if (end) end.value = cfg.service_end || "";
      var notes = form.querySelector('[name="notes"]');
      if (notes) notes.value = cfg.notes || "";
    }).catch(function () {});
  }

  function onSaveConfig(e) {
    e.preventDefault();
    var form = e.target;
    var payload = {
      is_enabled: form.is_enabled.checked,
      service_start: form.service_start.value,
      service_end: form.service_end.value,
      notes: form.notes.value,
    };
    API.put("/api/admin/service-requests/breakfast/config/", payload).then(function () {
      UI.toast("Breakfast configuration saved.", "success");
      loadConfigBanner();
    }).catch(function (err) {
      UI.toast("Failed to save: " + (err.message || ""), "error");
    });
  }

  /* ------------------------------ Issue ---------------------------------- */

  function onIssue(e) {
    e.preventDefault();
    var form = e.target;
    var payload = {
      guest_id: parseInt(form.guest_id.value),
      service_date: form.service_date.value,
      notes: form.notes.value,
    };
    if (!payload.guest_id || !payload.service_date) {
      UI.toast("Please fill in all required fields.", "warning");
      return;
    }
    API.post("/api/admin/service-requests/breakfast/issue/", payload).then(function (data) {
      UI.toast(data.created ? "Breakfast recorded." : "Already recorded for this date.", "success");
      form.reset();
      var today = new Date().toISOString().split("T")[0];
      form.service_date.value = today;
    }).catch(function (err) {
      UI.toast("Failed to record: " + (err.message || ""), "error");
    });
  }

  /* -------------------------- Eligibility -------------------------------- */

  function onCheckEligibility(e) {
    e.preventDefault();
    var form = e.target;
    var guestId = form.guest_id.value;
    var serviceDate = form.service_date.value;
    if (!guestId || !serviceDate) {
      UI.toast("Please fill in all required fields.", "warning");
      return;
    }
    var result = document.querySelector("[data-breakfast-eligibility-result]");
    result.innerHTML = '<p class="text-muted">Checking…</p>';
    API.get("/api/admin/service-requests/breakfast/eligibility/?guest_id=" + guestId + "&service_date=" + serviceDate)
      .then(function (data) {
        if (data.eligible) {
          result.innerHTML = '<div class="alert alert-success"><strong>Eligible.</strong> This guest can receive breakfast on ' + esc(serviceDate) + ". Eligible guests: " + data.eligible_guests + ".</div>";
        } else {
          result.innerHTML = '<div class="alert alert-warning"><strong>Not eligible.</strong> ' + esc(data.reason || "") + "</div>";
        }
      })
      .catch(function (err) {
        result.innerHTML = '<div class="alert alert-error">Failed to check: ' + esc(err.message || "") + "</div>";
      });
  }

  /* ------------------------------ History -------------------------------- */

  function loadHistory() {
    var container = document.querySelector("[data-breakfast-history-container]");
    if (!container) return;
    container.innerHTML = '<p class="text-muted">Loading…</p>';
    API.get("/api/admin/service-requests/breakfast/issues/?page_size=100").then(function (data) {
      var issues = data.results || [];
      if (!issues.length) {
        container.innerHTML = '<p class="text-muted">No breakfast issuances recorded yet.</p>';
        return;
      }
      var html = '<table class="table"><thead><tr><th>Reference</th><th>Guest</th><th>Booking</th><th>Date</th><th>Eligible Guests</th><th>Issued By</th></tr></thead><tbody>';
      issues.forEach(function (i) {
        html += "<tr><td><code>" + esc(i.reference) + "</code></td>";
        html += "<td>" + esc(i.guest_name || i.guest) + "</td>";
        html += "<td>" + esc(i.booking_reference || "—") + "</td>";
        html += "<td>" + esc(i.service_date) + "</td>";
        html += "<td>" + esc(i.eligible_guests) + "</td>";
        html += "<td>" + esc(i.issued_by_name || "—") + "</td>";
        html += "</tr>";
      });
      html += "</tbody></table>";
      container.innerHTML = html;
    }).catch(function (err) {
      container.innerHTML = '<div class="alert alert-error">Failed to load history: ' + esc(err.message || "") + "</div>";
    });
  }

  /* -------------------------------- Boot ---------------------------------- */
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
