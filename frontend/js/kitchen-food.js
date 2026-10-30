/* Prepared food only. Central API configuration, existing modal and capabilities. */
(function () {
  "use strict";
  var J = window.JONE = window.JONE || {};
  function key() {
    return window.crypto && window.crypto.randomUUID ? window.crypto.randomUUID() : "food-" + Date.now() + "-" + Math.random().toString(36).slice(2);
  }
  function quantity(value) {
    var number = Number(value);
    if (!Number.isInteger(number) || number < 1 || number > 1000000) throw new Error("Enter a whole prepared quantity from 1 to 1000000.");
    return number;
  }
  function canChange() {
    return ["kitchen.ticket.manage", "pos.menu.manage", "restaurant.menu.manage"].some(function (code) { return window.Auth.hasCapability(code); });
  }
  J.kitchenFood = { quantity: quantity, mount: function (root, onChange) {
    var rows = [], page = 1, busy = false, loading = false, pending = null, query = "";
    root.innerHTML = '<p class="caption">Ready / Not Available works without recipes. Portions are cooked food, not raw ingredients. Drafts do not reserve portions; submitted orders do. Advanced ingredient tracking is optional: authorized inventory staff configure or retire recipes in Inventory → Recipes.</p><label class="field-label">Find food<input class="input" type="search" maxlength="80" data-food-search></label><p data-food-error role="status" aria-live="polite"></p><div class="pos-ticket-list" data-food-list></div><div class="pos-action-stack"><button class="btn btn-sm" type="button" data-food-prev>Previous</button><button class="btn btn-sm" type="button" data-food-next>Next</button></div>';
    var list = root.querySelector("[data-food-list]"), error = root.querySelector("[data-food-error]");
    function endpoint() {
      var path = window.APP_CONFIG.API_ENDPOINTS.posKitchenFood;
      if (!path) throw new Error("Kitchen food API is not configured.");
      return path;
    }
    function paint() {
      list.innerHTML = rows.length ? rows.map(function (item) {
        var id = J.esc(item.id);
        function button(action, text) { return '<button class="btn btn-sm btn-outline" type="button" data-food-id="' + id + '" data-food-action="' + action + '">' + text + '</button>'; }
        var actions = canChange() ? button(item.is_available ? "NOT_AVAILABLE" : "READY", item.is_available ? "Mark Not Available" : "Mark Ready") + button(item.portion_tracking ? "DISABLE_PORTIONS" : "ENABLE_PORTIONS", item.portion_tracking ? "Disable portions" : "Track portions") : "";
        if (canChange() && item.portion_tracking) actions += button("REPLENISH", "Record prepared quantity") + button("WASTE", "Record wastage");
        return '<div class="pos-ticket"><strong>' + J.esc(item.name) + '</strong><p><span class="badge">' + J.esc(item.availability_label) + '</span> · ' + (item.portion_tracking ? J.esc(item.prepared_portions) + ' unreserved prepared portions' : 'No portion tracking') + '</p><div class="pos-action-stack">' + actions + (canChange() ? button("HISTORY", "Change history") : "") + '</div></div>';
      }).join("") : '<p>No matching food items.</p>';
    }
    async function load(silent) {
      if (loading || busy) return true;
      loading = true;
      try {
        var result = await window.API.list("posKitchenFood", { page: page, q: query });
        rows = Array.isArray(result.data) ? result.data : (result.data && result.data.results) || [];
        paint();
        var pagination = result.pagination || {};
        root.querySelector("[data-food-prev]").disabled = page <= 1;
        root.querySelector("[data-food-next]").disabled = !pagination.next;
        error.textContent = "";
        return true;
      } catch (err) {
        error.textContent = (err && err.message) || "Food availability could not be loaded. Try again.";
        return false;
      } finally { loading = false; }
    }
    root.addEventListener("click", async function (event) {
      if (busy) return;
      if (event.target.closest("[data-food-prev]")) { page = Math.max(1, page - 1); return load(); }
      if (event.target.closest("[data-food-next]")) { page += 1; return load(); }
      var button = event.target.closest("[data-food-action]");
      if (!button) return;
      var id = button.getAttribute("data-food-id"), action = button.getAttribute("data-food-action");
      var item = rows.find(function (row) { return String(row.id) === id; });
      if (!item) return;
      try {
        if (action === "HISTORY") {
          var history = await window.API.get(endpoint() + id + "/");
          var events = Array.isArray(history.data) ? history.data : [];
          await J.ui.confirm({ title: item.name + " · recent changes", message: events.map(function (row) { return row.created_at + " · " + row.action + " · staff #" + row.actor_id + " · balance " + row.portions_after + (row.reason ? " · " + row.reason : ""); }).join("\n") || "No changes recorded.", confirmText: "Close" });
          return;
        }
        var needsQuantity = action === "REPLENISH" || action === "WASTE";
        var requestKey = pending && pending.id === id && pending.action === action ? pending.key : key();
        var fields = needsQuantity ? [{ name: "quantity", label: "Prepared portions", type: "number", required: true, min: 1 }] : [];
        fields.push({ name: "reason", label: action === "WASTE" ? "Wastage reason (required)" : "Reason / note", type: "text", required: action === "WASTE" });
        var result = await J.dashboard.formModal({ title: item.name + " · " + action.replace(/_/g, " "), fields: fields, onSubmit: async function (values) {
          var payload = { action: action, quantity: needsQuantity ? quantity(values.quantity) : 0, reason: values.reason || "", idempotency_key: requestKey };
          // Keep key + exact payload for uncertain-network retries. Editing a failed
          // request gets a new key rather than silently applying it twice.
          if (pending && pending.id === id && pending.action === action && pending.body !== JSON.stringify(payload)) payload.idempotency_key = key();
          requestKey = payload.idempotency_key;
          pending = { id: id, action: action, key: payload.idempotency_key, body: JSON.stringify(payload) };
          busy = true;
          try {
            await window.API.post(endpoint() + id + "/", payload);
            pending = null;
            return { message: "Prepared food updated." };
          } finally { busy = false; }
        } });
        if (result) { await load(); if (onChange) onChange(); }
      } catch (err) { error.textContent = (err && err.message) || "Food change could not be saved."; }
    });
    var searchTimer;
    root.querySelector("[data-food-search]").addEventListener("input", function (event) {
      query = event.target.value; page = 1; window.clearTimeout(searchTimer); searchTimer = window.setTimeout(load, 250);
    });
    return { load: load };
  } };
})();
