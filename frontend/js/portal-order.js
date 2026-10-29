/* Guest room-service cart. The server owns prices, stock, stay eligibility and
   billing. The catalogue is advisory: it is not a stock reservation.

   The pending payload and retry key live only in this page's memory. A timeout
   or unreadable response freezes the exact payload; retries reuse its key.
   No portal credential or order is written to web storage. */
(function () {
  "use strict";

  var J = window.JONE = window.JONE || {};
  var orders = J.portalOrder = J.portalOrder || {};

  function escapeHTML(value) {
    var text = String(value == null ? "" : value);
    if (J.esc) return J.esc(text);
    return text.replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }

  // Integer minor units prevent floating-point totals from drifting by a kobo.
  function priceInCents(value) {
    var match = /^(\d{1,10})(?:\.(\d{1,2}))?$/.exec(String(value == null ? "" : value).trim());
    if (!match) return null;
    var cents = Number(match[1]) * 100 + Number((match[2] || "").padEnd(2, "0"));
    return Number.isSafeInteger(cents) && cents > 0 ? cents : null;
  }

  function formatCents(cents, currency) {
    var code = String(currency || "NGN").toUpperCase();
    if (code === "NGN" && J.portalCatalog) return J.portalCatalog.formatNaira(cents / 100);
    try { return new Intl.NumberFormat("en-NG", { style: "currency", currency: code }).format(cents / 100); }
    catch (_) { return code + " " + (cents / 100).toFixed(2); }
  }

  function countAvailable(item) {
    var n = Number(item && item.available_quantity);
    return Number.isInteger(n) && n > 0 ? Math.min(100, n) : 0;
  }

  function lineIssue(line) {
    if (line.priceChanged) return "The price changed. Remove this item and add it again to review the new price.";
    if (!line.available || !line.max) return "This item is currently unavailable. Remove it or refresh availability.";
    if (line.quantity > line.max) return "Only " + line.max + " currently shown as available. Reduce the quantity or refresh.";
    return line.serverError || "";
  }

  function clone(value) { return JSON.parse(JSON.stringify(value)); }
  function freezePayload(value) {
    value.lines.forEach(function (line) { Object.freeze(line); });
    Object.freeze(value.lines);
    return Object.freeze(value);
  }

  // The controller is also usable without a DOM, so retry and cart semantics
  // can be tested against real transport responses without a browser.
  orders.createController = function createController(options) {
    var opts = options || {};
    var state = {
      lines: [], stayReference: "", roomId: "", notes: "", pending: null,
      submitting: false, message: "", messageKind: "", confirmed: null,
    };
    function changed() { if (opts.onChange) opts.onChange(state); }
    function setMessage(text, kind) { state.message = text || ""; state.messageKind = kind || ""; }
    function clearServerErrors() { state.lines.forEach(function (line) { line.serverError = ""; }); }
    function blocked() {
      if (!state.pending && !state.submitting) return false;
      setMessage("The earlier order may have been received. Retry that same order or check Service requests before changing it.", "warning");
      changed();
      return true;
    }
    function stays() { return (opts.getStays && opts.getStays()) || []; }
    function currentStay() {
      return stays().filter(function (entry) { return String(entry.reference) === state.stayReference; })[0] || null;
    }
    function syncStays() {
      if (state.pending || state.submitting) return;
      var eligible = stays().filter(function (entry) { return entry && entry.reference && (entry.rooms || []).length; });
      if (!eligible.some(function (entry) { return String(entry.reference) === state.stayReference; })) {
        state.stayReference = eligible.length === 1 ? String(eligible[0].reference) : "";
        state.roomId = "";
      }
      var stay = currentStay();
      var rooms = stay ? (stay.rooms || []) : [];
      if (!rooms.some(function (room) { return String(room.id) === String(state.roomId); })) {
        state.roomId = rooms.length === 1 ? String(rooms[0].id) : "";
      }
      changed();
    }
    function add(item) {
      if (blocked()) return false;
      var id = Number(item && item.id), price = priceInCents(item && item.price), max = countAvailable(item);
      if (!Number.isInteger(id) || id < 1 || !item || !item.is_available || !max || !price) {
        setMessage("This item cannot be ordered right now. Refresh the menu or contact the hotel.", "error"); changed(); return false;
      }
      var currency = String(item.currency || "NGN").toUpperCase();
      if (state.lines.length && state.lines[0].currency !== currency) {
        setMessage("Items with different currencies cannot be ordered together. Contact the hotel.", "error"); changed(); return false;
      }
      var line = state.lines.filter(function (entry) { return entry.id === id; })[0];
      if (line && (line.cents !== price || line.currency !== currency)) {
        line.priceChanged = true;
        setMessage("A menu price changed. Review the affected item before ordering.", "error"); changed(); return false;
      }
      if (!line && state.lines.length >= 20) {
        setMessage("An order can contain at most 20 different items.", "error"); changed(); return false;
      }
      if (line && line.quantity >= max) {
        setMessage("Only " + max + " of " + line.name + " currently shown as available.", "error"); changed(); return false;
      }
      if (!line) {
        line = { id: id, name: String(item.name || "Menu item"), price: String(item.price),
          cents: price, currency: currency, quantity: 0, max: max, available: true,
          priceChanged: false, serverError: "" };
        state.lines.push(line);
      }
      line.quantity += 1;
      line.max = max; line.available = true; line.priceChanged = false;
      clearServerErrors(); state.confirmed = null; setMessage("", ""); changed(); return true;
    }
    function setQuantity(id, value) {
      if (blocked()) return false;
      var line = state.lines.filter(function (entry) { return entry.id === Number(id); })[0];
      var raw = String(value == null ? "" : value).trim();
      var quantity = /^\d+$/.test(raw) ? Number(raw) : 0;
      if (!line || !Number.isInteger(quantity) || quantity < 1 || quantity > 100) {
        setMessage("Quantity must be a whole number from 1 to 100.", "error"); changed(); return false;
      }
      line.quantity = quantity; clearServerErrors(); setMessage("", ""); changed(); return true;
    }
    function remove(id) {
      if (blocked()) return false;
      state.lines = state.lines.filter(function (entry) { return entry.id !== Number(id); });
      clearServerErrors(); setMessage("", ""); changed(); return true;
    }
    function reconcile(categories) {
      if (state.pending || state.submitting) return;
      var items = {};
      (categories || []).forEach(function (category) {
        (category.items || []).forEach(function (item) { items[String(item.id)] = item; });
      });
      state.lines.forEach(function (line) {
        var item = items[String(line.id)];
        line.available = !!(item && item.is_available);
        line.max = countAvailable(item);
        if (item && (priceInCents(item.price) !== line.cents || String(item.currency || "NGN").toUpperCase() !== line.currency)) {
          line.priceChanged = true;
        }
        line.serverError = "";
      });
      changed();
    }
    function chooseStay(reference) {
      if (blocked()) return;
      state.stayReference = String(reference || ""); state.roomId = "";
      var stay = currentStay(), rooms = stay ? (stay.rooms || []) : [];
      if (rooms.length === 1) state.roomId = String(rooms[0].id);
      setMessage("", ""); changed();
    }
    function chooseRoom(id) {
      if (blocked()) return;
      state.roomId = String(id || ""); setMessage("", ""); changed();
    }
    function setNotes(value) {
      if (state.pending || state.submitting) return;
      state.notes = String(value || "").slice(0, 500);
    }
    function total() { return state.lines.reduce(function (sum, line) { return sum + line.cents * line.quantity; }, 0); }
    function payload() {
      var stay = currentStay();
      if (!state.lines.length) throw new Error("Add at least one menu item to your order.");
      if (!stay || !(stay.rooms || []).some(function (room) { return String(room.id) === String(state.roomId); })) {
        throw new Error("Select an active in-house stay and the room for delivery.");
      }
      if (state.lines.some(function (line) { return !!lineIssue(line); })) {
        throw new Error("Review the highlighted items before sending your order.");
      }
      return {
        stay_reference: state.stayReference, room_id: Number(state.roomId),
        lines: state.lines.map(function (line) {
          return { menu_item_id: line.id, quantity: line.quantity, expected_unit_price: line.price };
        }),
        notes: state.notes.trim(), payment_method: "FOLIO", idempotency_key: opts.newKey(),
      };
    }
    function recordProductErrors(err) {
      var details = err && err.data && err.data.errors;
      var affected = details && Array.isArray(details.items) ? details.items : [];
      affected.forEach(function (item) {
        state.lines.forEach(function (line) {
          if (line.id === Number(item.menu_item_id)) {
            line.serverError = "This item cannot be ordered in the requested quantity. Adjust it or refresh availability.";
          }
        });
      });
    }
    async function submit() {
      if (state.submitting) return false;
      if (!state.pending) {
        try { state.pending = freezePayload(payload()); }
        catch (err) { setMessage(err.message, "error"); changed(); return false; }
      }
      state.submitting = true; setMessage("Sending your order…", "info"); changed();
      var response;
      try {
        // Clone so a transport or caller cannot mutate the frozen retry intent.
        response = await opts.submit(clone(state.pending));
        if (!response || !response.data || !response.data.reference || !response.data.pos_order) {
          throw { kind: "parse", message: "The response did not confirm your order." };
        }
      } catch (err) {
        state.submitting = false;
        if (err && err.status === 401 && opts.onAuthError) {
          setMessage("Your session ended. Check Service requests after signing in again.", "warning"); changed(); opts.onAuthError(err); return false;
        }
        // A structured 400/409 from this endpoint is rejected inside the
        // transaction. An unknown, timed-out, unreadable, 404 or 5xx response
        // can follow a commit: never generate a new key automatically.
        var rejected = err && (err.status === 400 || err.status === 409) && err.kind !== "parse" && err.data && err.data.success === false;
        if (rejected) {
          state.pending = null; recordProductErrors(err);
          setMessage((err.message || "The order was not accepted.") + " Check Service requests before placing a similar order.", "error");
        } else {
          setMessage("We could not confirm whether the order was received. Do not start another order: retry this same order or check Service requests. If you refresh, check requests first.", "warning");
        }
        changed(); return false;
      }
      state.submitting = false;
      state.confirmed = clone(response.data);
      state.pending = null; state.lines = []; state.notes = "";
      setMessage("", ""); changed();
      if (opts.onSuccess) opts.onSuccess(state.confirmed);
      return true;
    }
    function clearFound() {
      if (state.submitting || !state.pending) return false;
      state.pending = null; state.lines = []; state.notes = "";
      state.confirmed = null; setMessage("Cart cleared. Check the request's status in Service requests.", "info"); changed(); return true;
    }
    return { state: state, add: add, setQuantity: setQuantity, remove: remove, reconcile: reconcile,
      chooseStay: chooseStay, chooseRoom: chooseRoom, setNotes: setNotes, syncStays: syncStays,
      lineIssue: lineIssue, total: total, submit: submit, clearFound: clearFound };
  };

  orders.mount = function mount(root, options) {
    var opts = options || {}, controller;
    function disabled(value) { return value ? " disabled" : ""; }
    function render() {
      var state = controller.state, busy = !!(state.pending || state.submitting);
      var stayList = (opts.getStays && opts.getStays() || []).filter(function (s) { return s && s.reference && (s.rooms || []).length; });
      var stay = stayList.filter(function (s) { return String(s.reference) === state.stayReference; })[0];
      var rooms = stay ? (stay.rooms || []) : [];
      var heading = '<div class="portal-order-head"><div><span class="eyebrow">Room service</span><h3 id="portal-order-heading">Your order</h3></div>' +
        '<button class="btn btn-sm btn-outline" type="button" data-cart-refresh' + disabled(state.submitting) + '>Refresh availability</button></div>';
      var intro = '<p class="caption">Availability is a snapshot, not a reservation. The hotel checks stock and price again when you send your order. ' +
        'If an earlier order did not confirm, check Service requests before placing another.</p>';
      var confirmation = "";
      if (state.confirmed) {
        var order = state.confirmed.pos_order || {};
        var cancelled = String(order.status || "").toUpperCase() === "CANCELLED";
        var delivered = String(order.status || "").toUpperCase() === "DELIVERED";
        var title = cancelled ? 'Order cancelled' : delivered ? 'Order delivered' : 'Order received for staff review';
        var charge = order.charge_posted ? 'The charge has been posted to your folio.'
          : cancelled ? 'No charge was posted for this cancelled order.'
          : 'No payment has been taken. The order is charged to your folio on delivery.';
        confirmation = '<div class="portal-order-confirm" role="status"><strong>' + title + '</strong>' +
          '<p>Request ' + escapeHTML(state.confirmed.reference) + ' · Order status: ' + escapeHTML(String(order.status || "received").toLowerCase().replace(/_/g, " ")) +
          ' · Total: ' + escapeHTML(formatCents(Math.round(Number(order.total_amount || 0) * 100), order.currency)) + '</p>' +
          '<p>' + charge + '</p>' +
          '<button class="btn btn-sm btn-outline" type="button" data-cart-requests>View Service requests</button></div>';
      }
      var list = state.lines.length ? '<ul class="portal-order-lines">' + state.lines.map(function (line) {
        var issue = controller.lineIssue(line);
        return '<li class="portal-order-line"><div class="portal-order-line-main"><strong>' + escapeHTML(line.name) + '</strong><span class="caption">' +
          escapeHTML(formatCents(line.cents, line.currency)) + ' each · ' + (line.available ? 'up to ' + escapeHTML(line.max) + ' shown available' : 'currently unavailable') + '</span>' +
          (issue ? '<span class="field-error" role="alert">' + escapeHTML(issue) + '</span>' : '') + '</div>' +
          '<div class="portal-order-line-controls"><label class="field-label" for="cart-qty-' + line.id + '">Quantity</label>' +
          '<input class="input" id="cart-qty-' + line.id + '" type="number" min="1" max="100" step="1" inputmode="numeric" value="' + line.quantity + '" data-cart-quantity="' + line.id + '"' + disabled(busy) + '>' +
          '<strong>' + escapeHTML(formatCents(line.cents * line.quantity, line.currency)) + '</strong>' +
          '<button class="btn btn-sm btn-outline" type="button" data-cart-remove="' + line.id + '" aria-label="Remove ' + escapeHTML(line.name) + '"' + disabled(busy) + '>Remove</button></div></li>';
      }).join("") + '</ul>' : '<p class="muted">Your order is empty. Add items from the menu below.</p>';
      var form = "";
      if (state.lines.length) {
        var stayOptions = '<option value="">Select a stay</option>' + stayList.map(function (s) {
          return '<option value="' + escapeHTML(s.reference) + '"' + (String(s.reference) === state.stayReference ? ' selected' : '') + '>' +
            escapeHTML((s.booking_reference || "Stay") + ' · ' + s.reference) + '</option>';
        }).join("");
        var roomOptions = '<option value="">Select a room</option>' + rooms.map(function (r) {
          return '<option value="' + escapeHTML(r.id) + '"' + (String(r.id) === String(state.roomId) ? ' selected' : '') + '>Room ' + escapeHTML(r.room_number) + '</option>';
        }).join("");
        form = '<div class="portal-order-fields"><div class="field"><label class="field-label" for="cart-stay">In-house stay</label>' +
          '<select class="select" id="cart-stay" data-cart-stay' + disabled(busy) + '>' + stayOptions + '</select></div>' +
          '<div class="field"><label class="field-label" for="cart-room">Deliver to room</label>' +
          '<select class="select" id="cart-room" data-cart-room' + disabled(busy) + '>' + roomOptions + '</select></div></div>' +
          (!stayList.length ? '<p class="field-error">An active in-house stay and assigned room are required. Contact the front desk.</p>' : '') +
          '<div class="field"><label class="field-label" for="cart-notes">Delivery notes (optional)</label>' +
          '<textarea class="textarea" id="cart-notes" data-cart-notes rows="2" maxlength="500"' + disabled(busy) + '>' + escapeHTML(state.notes) + '</textarea></div>' +
          '<div class="portal-order-total"><span>Order total</span><strong>' + escapeHTML(formatCents(controller.total(), state.lines[0].currency)) + '</strong></div>' +
          '<p class="caption">Payment: charge to your room folio on delivery. Menu options and special changes must be discussed with the hotel.</p>';
      }
      var message = state.message ? '<p class="portal-order-message ' + (state.messageKind === 'error' ? 'is-error' : '') + '" role="status">' + escapeHTML(state.message) + '</p>' : '';
      var actions = state.lines.length ? '<div class="portal-order-actions">' +
        '<button class="btn btn-accent" type="button" data-cart-submit' + disabled(state.submitting) + '>' +
        (state.submitting ? 'Sending…' : state.pending ? 'Retry same order' : 'Send order') + '</button>' +
        (state.pending ? '<button class="btn btn-sm btn-outline" type="button" data-cart-requests>Check Service requests</button>' +
          '<button class="btn btn-sm btn-outline" type="button" data-cart-found' + disabled(state.submitting) + '>I found this order; clear cart</button>' : '') + '</div>' : '';
      root.innerHTML = '<section class="card card-pad portal-order-cart" aria-labelledby="portal-order-heading">' + heading + intro + confirmation + list + form + message + actions + '</section>';
    }
    controller = orders.createController(Object.assign({}, opts, { onChange: render }));
    root.addEventListener("click", function (event) {
      var button = event.target.closest("button");
      if (!button) return;
      if (button.hasAttribute("data-cart-remove")) controller.remove(button.getAttribute("data-cart-remove"));
      else if (button.hasAttribute("data-cart-submit")) controller.submit();
      else if (button.hasAttribute("data-cart-refresh") && opts.refreshMenu) opts.refreshMenu();
      else if (button.hasAttribute("data-cart-requests") && opts.onViewRequests) opts.onViewRequests();
      else if (button.hasAttribute("data-cart-found") && opts.confirmFound) {
        Promise.resolve(opts.confirmFound()).then(function (confirmed) { if (confirmed) controller.clearFound(); });
      }
    });
    root.addEventListener("change", function (event) {
      var target = event.target;
      if (target.hasAttribute("data-cart-quantity")) controller.setQuantity(target.getAttribute("data-cart-quantity"), target.value);
      else if (target.hasAttribute("data-cart-stay")) controller.chooseStay(target.value);
      else if (target.hasAttribute("data-cart-room")) controller.chooseRoom(target.value);
    });
    root.addEventListener("input", function (event) {
      if (event.target.hasAttribute("data-cart-notes")) controller.setNotes(event.target.value);
    });
    controller.syncStays();
    return controller;
  };

  orders.priceInCents = priceInCents;
  orders.formatCents = formatCents;
})();
