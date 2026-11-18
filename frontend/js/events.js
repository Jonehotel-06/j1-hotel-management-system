/* js/events.js */
/* ==========================================================================
   Reception Event Management.

   The server owns availability, pricing, payment verification and every
   financial posting. This script only renders, validates what the operator
   typed and forwards the request. Amounts shown come from the API response —
   never from a browser-side calculation of what is owed.
   ========================================================================== */
(function () {
  "use strict";

  var S = window.ReceptionServices;
  if (!S) return;
  var API = window.API;
  var esc = (window.JONE && window.JONE.esc) || function (v) { return String(v == null ? "" : v); };
  var money = (window.JONE && window.JONE.formatNaira) || function (v) { return "₦" + v; };
  /* Events live under their own staff namespace (/api/admin/events/), NOT
     under the service-requests base (S.base). Deriving "events/" from S.base
     produced /api/admin/service-requests/events/... which the backend does
     not serve: the locations call 404'd (Django HTML) and the list call was
     captured by the service-request detail route as reference "events",
     surfacing as "Service request not found". Resolve from the single
     endpoint map with a literal fallback so the path can never drift again. */
  var EVENTS_EP = (window.APP_CONFIG && window.APP_CONFIG.API_ENDPOINTS && window.APP_CONFIG.API_ENDPOINTS.events) || "/api/admin/events/";
  var base = String(EVENTS_EP).replace(/\/?$/, "/");
  var $ = function (sel) { return document.querySelector(sel); };

  var locations = [];
  var guests = [];
  var currentEvents = [];
  var submitting = false;
  var searchTimer = null;

  function message(text, failed) {
    var box = $("[data-event-form-message]");
    if (!box) return;
    box.innerHTML = text ? '<div class="alert' + (failed ? " alert-error" : "") + '" role="status">' + esc(text) + "</div>" : "";
  }

  function statusPill(rows) {
    var map = {
      UNPAID: "out", PARTIALLY_PAID: "low", PAID: "ok",
      UPCOMING: "ok", ONGOING: "low", COMPLETED: "ok", CANCELLED: "out",
    };
    return function (value) {
      var info = map[value] || "ok";
      return '<span class="inv-state inv-state-' + info + '">' + esc(String(value || "—").replace(/_/g, " ")) + "</span>";
    };
  }

  /* --------------------------------- Tabs --------------------------------- */

  function wireTabs() {
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
        if (name === "events") loadEvents();
      });
    });
  }

  /* ------------------------------- Locations ------------------------------ */

  function loadLocations() {
    return API.get(base + "locations/").then(function (res) {
      locations = S.listOf(res);
      var filter = $("#ev-location");
      var form = $("#ev-space");
      if (filter) {
        filter.innerHTML = '<option value="">All locations</option>';
        locations.forEach(function (loc) {
          filter.appendChild(new Option(loc.name, loc.id));
        });
      }
      if (form) {
        form.innerHTML = '<option value="">— Select event space —</option>';
        locations.forEach(function (loc) {
          form.appendChild(new Option(loc.name, loc.id));
        });
        if (!locations.length) {
          form.innerHTML = '<option value="">No active event spaces — ask an administrator to configure one</option>';
        }
      }
      return locations;
    }).catch(function (err) {
      var form = $("#ev-space");
      if (form) form.innerHTML = '<option value="">Event spaces could not be loaded</option>';
      var filter = $("#ev-location");
      if (filter) filter.innerHTML = '<option value="">Event spaces could not be loaded</option>';
      /* The message carries a working retry: clicking it re-issues the exact
         failed request instead of asking for a full page refresh. */
      var box = $("[data-event-form-message]");
      if (box) {
        box.innerHTML = '<div class="alert alert-error" role="status">' + esc(err.message || "Event locations could not be loaded.") +
          ' <button class="btn btn-sm btn-outline" type="button" data-event-locations-retry>Retry</button></div>';
        var retry = box.querySelector("[data-event-locations-retry]");
        if (retry) retry.addEventListener("click", function () { message(""); loadLocations(); });
      }
    });
  }

  /* --------------------------------- Guests ------------------------------- */

  function loadGuests(term) {
    var select = $("#ev-guest");
    if (!select) return Promise.resolve();
    var query = term ? "?search=" + encodeURIComponent(term) : "";
    return API.get(S.base + "reception/guests/" + query).then(function (res) {
      var data = S.unwrap(res) || {};
      guests = data.results || [];
      var keep = select.value;
      select.innerHTML = '<option value="">— Register a new guest below —</option>';
      guests.forEach(function (guest) {
        var stays = (guest.bookings || []).map(function (b) { return b.rooms.join(", "); }).filter(Boolean);
        select.appendChild(new Option(
          guest.full_name + (guest.phone ? " · " + guest.phone : "") + (guests.length && stays.length ? " · Room " + stays[0] : ""),
          guest.id
        ));
      });
      if (!guests.length && term) {
        select.innerHTML = '<option value="">No guest matches "' + esc(term) + '" — register a new guest below</option>';
      }
      if (keep && select.querySelector('option[value="' + keep + '"]')) select.value = keep;
      return guests;
    }).catch(function () {
      select.innerHTML = '<option value="">Guests could not be loaded — type a name to search again</option>';
    });
  }

  function guestById(id) {
    for (var i = 0; i < guests.length; i += 1) {
      if (String(guests[i].id) === String(id)) return guests[i];
    }
    return null;
  }

  function applyGuest(guest) {
    var first = $("#ev-first"), last = $("#ev-last"), phone = $("#ev-phone"), email = $("#ev-email");
    if (!guest) {
      [first, last, phone, email].forEach(function (input) { if (input) input.readOnly = false; });
      var note = $("[data-event-guest-note]");
      if (note) note.textContent = "No existing guest selected: the fields below create a new guest record.";
      return;
    }
    var parts = String(guest.full_name || "").split(" ");
    if (first) first.value = parts.shift() || "";
    if (last) last.value = parts.join(" ");
    if (phone) phone.value = guest.phone || "";
    if (email) email.value = guest.email || "";
    [first, last, phone, email].forEach(function (input) { if (input) input.readOnly = false; });
    var hint = $("[data-event-guest-note]");
    if (hint) hint.textContent = "Using existing guest " + (guest.full_name || "") + " (record reused, no duplicate created). Contact details may still be corrected.";
  }

  /* ------------------------------- Event list ----------------------------- */

  function renderEvents(items) {
    var box = $("[data-events-container]");
    if (!box) return;
    if (!items.length) {
      box.innerHTML = '<div class="empty-state"><p>No events match these filters.</p><p class="caption">Register an event, or widen the date range and clear the search.</p></div>';
      return;
    }
    var pill = statusPill();
    box.innerHTML = '<div class="table-wrap"><table><thead><tr>' +
      "<th>Reference</th><th>Guest</th><th>Location</th><th>Date</th><th>Time</th>" +
      '<th class="num">Charged</th><th class="num">Paid</th><th class="num">Outstanding</th>' +
      "<th>Payment</th><th>Status</th><th></th></tr></thead><tbody>" +
      items.map(function (row) {
        return "<tr>" +
          "<td><strong>" + esc(row.reference) + "</strong></td>" +
          "<td>" + esc(row.guest_name) + "<br><small class=\"caption\">" + esc(row.guest_phone || "") + "</small></td>" +
          "<td>" + esc(row.location_name) + "</td>" +
          "<td>" + esc(row.event_date) + "</td>" +
          "<td>" + esc(String(row.start_time || "").slice(0, 5)) + " – " + esc(String(row.end_time || "").slice(0, 5)) + "</td>" +
          '<td class="num">' + money(row.total_charged) + "</td>" +
          '<td class="num">' + money(row.amount_paid) + "</td>" +
          '<td class="num">' + money(row.outstanding_balance) + "</td>" +
          "<td>" + pill(row.payment_status) + "</td>" +
          "<td>" + pill(row.status) + "</td>" +
          '<td><div class="row-actions">' +
            '<button class="btn btn-sm btn-outline" type="button" data-event-view="' + esc(row.reference) + '">View</button>' +
            (Number(row.outstanding_balance) > 0 && row.status !== "CANCELLED"
              ? '<button class="btn btn-sm" type="button" data-event-pay="' + esc(row.reference) + '">Record payment</button>' : "") +
            '<button class="btn btn-sm btn-outline" type="button" data-event-receipt="' + esc(row.reference) + '">Receipt</button>' +
            (row.status !== "CANCELLED"
              ? '<button class="btn btn-sm btn-outline" type="button" data-event-cancel="' + esc(row.reference) + '">Cancel</button>' : "") +
          "</div></td></tr>";
      }).join("") + "</tbody></table></div>";
  }

  function loadEvents() {
    var box = $("[data-events-container]");
    if (!box) return Promise.resolve();
    var params = [];
    var form = $("[data-events-filter]");
    if (form) {
      Array.prototype.forEach.call(form.elements, function (field) {
        if (field.name && String(field.value || "").trim()) {
          params.push(encodeURIComponent(field.name) + "=" + encodeURIComponent(String(field.value).trim()));
        }
      });
    }
    var url = base + (params.length ? "?" + params.join("&") : "");
    if (!currentEvents.length) box.innerHTML = S.loading();
    return API.get(url).then(function (res) {
      currentEvents = S.listOf(res);
      renderEvents(currentEvents);
    }).catch(function (err) {
      box.innerHTML = '<div class="alert alert-error" role="status">' + esc(err.message || "Events could not be loaded.") +
        ' <button class="btn btn-sm btn-outline" type="button" data-events-retry>Retry</button></div>';
      var retry = box.querySelector("[data-events-retry]");
      if (retry) retry.addEventListener("click", loadEvents);
    });
  }

  function eventByReference(reference) {
    for (var i = 0; i < currentEvents.length; i += 1) {
      if (currentEvents[i].reference === reference) return currentEvents[i];
    }
    return null;
  }

  /* --------------------------- Detail / payment --------------------------- */

  function viewEvent(reference) {
    return API.get(base + encodeURIComponent(reference) + "/").then(function (res) {
      var event = S.unwrap(res) || {};
      var payments = event.payments || [];
      var body = '<div class="table-wrap"><table><tbody>' +
        "<tr><th>Reference</th><td>" + esc(event.reference) + "</td></tr>" +
        "<tr><th>Guest</th><td>" + esc(event.guest_name) + (event.guest_phone ? " · " + esc(event.guest_phone) : "") + (event.guest_email ? " · " + esc(event.guest_email) : "") + "</td></tr>" +
        "<tr><th>Event type</th><td>" + esc(event.event_type || "—") + "</td></tr>" +
        "<tr><th>Location</th><td>" + esc(event.location_name) + "</td></tr>" +
        "<tr><th>Date &amp; time</th><td>" + esc(event.event_date) + " · " + esc(String(event.start_time || "").slice(0, 5)) + " – " + esc(String(event.end_time || "").slice(0, 5)) + "</td></tr>" +
        "<tr><th>Expected guests</th><td>" + esc(event.expected_guests) + "</td></tr>" +
        "<tr><th>Charged</th><td>" + money(event.total_charged) + "</td></tr>" +
        "<tr><th>Paid</th><td>" + money(event.amount_paid) + "</td></tr>" +
        "<tr><th>Outstanding</th><td>" + money(event.outstanding_balance) + "</td></tr>" +
        "<tr><th>Payment status</th><td>" + esc(event.payment_status) + "</td></tr>" +
        "<tr><th>Event status</th><td>" + esc(event.status) + "</td></tr>" +
        "<tr><th>Registered by</th><td>" + esc(event.created_by_name || "—") + "</td></tr>" +
        "<tr><th>Registered at</th><td>" + esc(window.JONE.formatDateTime ? window.JONE.formatDateTime(event.created_at) : event.created_at) + "</td></tr>" +
        (event.cancellation_reason ? "<tr><th>Cancellation reason</th><td>" + esc(event.cancellation_reason) + "</td></tr>" : "") +
        "</tbody></table></div>" +
        "<h3>Payment history</h3>" +
        (payments.length
          ? '<div class="table-wrap"><table><thead><tr><th>Receipt</th><th class="num">Amount</th><th>Method</th><th>Reference</th><th>Received by</th><th>When</th><th>Status</th></tr></thead><tbody>' +
            payments.map(function (p) {
              return "<tr><td>" + esc(p.reference) + "</td><td class=\"num\">" + money(p.amount) + "</td><td>" + esc(p.method) +
                "</td><td>" + esc(p.external_reference || "—") + "</td><td>" + esc(p.received_by_name || "—") +
                "</td><td>" + esc(window.JONE.formatDateTime ? window.JONE.formatDateTime(p.paid_at || p.created_at) : (p.paid_at || p.created_at)) +
                "</td><td>" + esc(p.status) + "</td></tr>";
            }).join("") + "</tbody></table></div>"
          : '<p class="caption">No payment has been received for this event yet.</p>');
      if (window.JONE.dashboard && window.JONE.dashboard.panel) {
        window.JONE.dashboard.panel.open("Event " + event.reference, body,
          '<button class="btn btn-outline" type="button" data-panel-close>Close</button>', "modal-lg");
        return event;
      }
      window.alert(body.replace(/<[^>]+>/g, " ").replace(/\s+/g, " "));
      return event;
    }).catch(function (err) { message(err.message || "The event could not be loaded.", true); });
  }

  function recordPayment(reference) {
    var event = eventByReference(reference);
    var outstanding = event ? Number(event.outstanding_balance) : 0;
    var slate = {};
    return window.JONE.dashboard.formModal({
      title: "Record payment — " + reference,
      submitText: "Record payment",
      fields: [
        { name: "amount", label: "Amount received (₦)", type: "number", step: "0.01", min: "0.01", required: true,
          value: outstanding ? outstanding.toFixed(2) : "",
          help: "Outstanding balance on this event is " + (event ? money(event.outstanding_balance) : "unknown") + "." },
        { name: "method", label: "Payment method", type: "select", required: true, options: [
          { value: "CASH", label: "Cash" },
          { value: "BANK_TRANSFER", label: "Bank Transfer" },
          { value: "POS", label: "POS / Card" },
          { value: "PAYSTACK", label: "Paystack" },
          { value: "OTHER", label: "Other approved method" },
        ] },
        { name: "external_reference", label: "Transaction reference", type: "text",
          help: "Required for transfer, POS and Paystack. A Paystack reference is verified with the gateway before the payment counts." },
        { name: "notes", label: "Notes", type: "textarea", rows: 2 },
      ],
      onSubmit: function (values) {
        if (slate.key === undefined) slate.key = S.key();
        values.idempotency_key = slate.key;
        if (["BANK_TRANSFER", "POS", "PAYSTACK"].indexOf(values.method) !== -1 && !values.external_reference) {
          return Promise.reject(new Error("A transaction reference is required for this payment method."));
        }
        return API.post(base + encodeURIComponent(reference) + "/payments/", values).then(function (res) {
          var data = S.unwrap(res) || {};
          return { message: "Payment " + (data.reference || "") + " recorded." };
        });
      },
    }).then(function (result) {
      if (result) {
        window.JONE.ui.toast(result.message || "Payment recorded.", "success");
        loadEvents();
      }
    });
  }

  function cancelEvent(reference) {
    return window.JONE.dashboard.formModal({
      title: "Cancel event " + reference,
      submitText: "Cancel event",
      fields: [
        { name: "reason", label: "Reason for cancellation", type: "text", required: true,
          help: "Financial records are preserved. Money already received follows the existing refund process." },
      ],
      onSubmit: function (values) {
        return API.post(base + encodeURIComponent(reference) + "/cancel/", values).then(function () {
          return { message: "Event cancelled. Financial records were preserved." };
        });
      },
    }).then(function (result) {
      if (result) {
        window.JONE.ui.toast(result.message, "success");
        loadEvents();
      }
    });
  }

  function printReceipt(reference) {
    return API.get(base + encodeURIComponent(reference) + "/receipt/").then(function (res) {
      var data = S.unwrap(res) || {};
      var receipt = {
        hotel: {
          name: "J-ONE HOTEL & LODGE",
          phone: "+234 803 211 2874",
          address: "Plot 566 Mgbowo Street, off Ezike Street, Enugu State.",
        },
        reference: data.receipt_number || data.event_reference,
        issued_at: data.date_issued,
        staff_label: "Registered by",
        staff_name: "",
        mode: "Event space",
        items: [{
          name: (data.event_location || "Event space") + (data.event_type ? " — " + data.event_type : ""),
          quantity: 1,
          price: Number(data.total_charged || 0),
          total: Number(data.total_charged || 0),
        }],
        subtotal: Number(data.total_charged || 0),
        total: Number(data.total_charged || 0),
        paid: Number(data.amount_paid || 0),
        balance_due: Number(data.outstanding_balance || 0),
        payment_method: data.payment_method || "",
        note: "Event " + (data.event_reference || "") + " · " + (data.guest_name || "") + " · " + (data.event_date || "") + " " + (data.event_time || ""),
      };
      if (window.JONE.ThermalReceipt) {
        window.JONE.ThermalReceipt.print(receipt, { width: window.JONE.ThermalReceipt.getWidth() });
      } else {
        window.print();
      }
    }).catch(function (err) { message(err.message || "The receipt could not be produced.", true); });
  }

  /* ------------------------------ Registration ---------------------------- */

  function submitEvent(event) {
    event.preventDefault();
    if (submitting) return;
    message("");
    var form = event.target;
    var values = {};
    Array.prototype.forEach.call(form.elements, function (field) {
      if (field.name) values[field.name] = String(field.value || "").trim();
    });

    if (!values.guest_id) {
      if (!values.first_name || !values.last_name) return message("Enter the guest's first and last name, or select an existing guest.", true);
      if (!values.phone) return message("A phone number is required for a new guest.", true);
    }
    if (!values.location_id) return message("Choose the event space.", true);
    if (!values.event_type) return message("Enter the event type.", true);
    if (!values.event_date) return message("Choose the event date.", true);
    if (!values.start_time || !values.end_time) return message("Enter the start and end time.", true);
    if (values.end_time <= values.start_time) return message("The end time must be after the start time.", true);
    if (values.total_charged === "" || Number(values.total_charged) < 0) return message("Enter the amount the hotel charges for this event.", true);

    var payload = {
      location_id: Number(values.location_id),
      event_type: values.event_type,
      event_date: values.event_date,
      start_time: values.start_time,
      end_time: values.end_time,
      expected_guests: Number(values.expected_guests || 1),
      total_charged: Number(values.total_charged).toFixed(2),
      notes: values.notes || "",
      idempotency_key: S.key(),
    };
    if (values.guest_id) {
      payload.guest_id = Number(values.guest_id);
    } else {
      payload.first_name = values.first_name;
      payload.last_name = values.last_name;
      payload.phone = values.phone;
      payload.email = values.email || "";
    }
    var paid = Number(values.amount_paid || 0);
    if (paid > 0) {
      payload.amount_paid = paid.toFixed(2);
      payload.payment_method = values.payment_method || "CASH";
      if (values.payment_reference) payload.payment_reference = values.payment_reference;
      if (["BANK_TRANSFER", "POS", "PAYSTACK"].indexOf(payload.payment_method) !== -1 && !values.payment_reference) {
        return message("A payment reference is required for this payment method.", true);
      }
      payload.payment_idempotency_key = "init-" + payload.idempotency_key;
    }
    if (values.override_reason) {
      payload.override = true;
      payload.override_reason = values.override_reason;
    }

    var button = $("[data-event-submit]");
    submitting = true;
    if (button) button.disabled = true;
    API.post(base, payload).then(function (res) {
      var data = S.unwrap(res) || {};
      window.JONE.ui.toast("Event " + (data.reference || "") + " registered.", "success");
      form.reset();
      var select = $("#ev-guest");
      if (select) select.value = "";
      applyGuest(null);
      var typeField = $("#ev-type");
      if (typeField) typeField.value = "";
      var paidField = $("#ev-paid");
      if (paidField) paidField.value = "0";
      currentEvents = [];
      loadEvents();
      document.querySelectorAll("[data-tab]").forEach(function (tab) {
        if (tab.getAttribute("data-tab") === "events") tab.click();
      });
    }).catch(function (err) {
      message(err.message || "The event could not be registered.", true);
    }).then(function () {
      submitting = false;
      if (button) button.disabled = false;
    });
  }

  /* --------------------------------- Boot --------------------------------- */

  function init() {
    if (!API || !S) return;
    wireTabs();
    loadLocations();
    loadGuests();
    loadEvents();

    var search = $("[data-event-guest-search]");
    if (search) {
      search.addEventListener("input", function () {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(function () { loadGuests(search.value.trim()); }, 250);
      });
      search.addEventListener("search", function () { loadGuests(search.value.trim()); });
    }
    var guestSelect = $("#ev-guest");
    if (guestSelect) {
      guestSelect.addEventListener("change", function () { applyGuest(guestById(guestSelect.value)); });
    }
    var form = $("[data-event-form]");
    if (form) form.addEventListener("submit", submitEvent);
    var filter = $("[data-events-filter]");
    if (filter) filter.addEventListener("submit", function (e) { e.preventDefault(); loadEvents(); });
    var refresh = $("[data-events-refresh]");
    if (refresh) refresh.addEventListener("click", function () { loadLocations(); loadEvents(); });

    var container = $("[data-events-container]");
    if (container) {
      container.addEventListener("click", function (e) {
        var view = e.target.closest("[data-event-view]");
        var pay = e.target.closest("[data-event-pay]");
        var cancel = e.target.closest("[data-event-cancel]");
        var receipt = e.target.closest("[data-event-receipt]");
        if (view) viewEvent(view.getAttribute("data-event-view"));
        else if (pay) recordPayment(pay.getAttribute("data-event-pay"));
        else if (cancel) cancelEvent(cancel.getAttribute("data-event-cancel"));
        else if (receipt) printReceipt(receipt.getAttribute("data-event-receipt"));
      });
    }
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
