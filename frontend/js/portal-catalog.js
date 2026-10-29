/* js/portal-catalog.js
   Guest portal catalogue: restaurant & bar menu and guest-facing services.
   Data comes only from the server (portalMenu / portalServices endpoints).
   Availability is an advisory snapshot; the order API validates it again.

   Pure helpers (filtering, search, money formatting, action mapping) are kept
   free of DOM access so they can be unit-tested directly. */
(function () {
  "use strict";

  var J = window.JONE = window.JONE || {};
  var catalog = J.portalCatalog = J.portalCatalog || {};

  /* ---------- pure helpers (no DOM) ---------- */

  // Naira display without locale dependence: "₦3,500.00".
  function formatNaira(value) {
    var amount = Number(value);
    if (value === null || value === undefined || value === "" || !isFinite(amount)) return "Price on request";
    var fixed = Math.abs(amount).toFixed(2);
    var parts = fixed.split(".");
    var whole = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    return (amount < 0 ? "-" : "") + "\u20A6" + whole + "." + parts[1];
  }

  function formatCurrency(value, currency) {
    var code = String(currency || "NGN").toUpperCase();
    if (code === "NGN") return formatNaira(value);
    var amount = Number(value);
    if (value === null || value === undefined || value === "" || !isFinite(amount)) return "Price on request";
    try { return new Intl.NumberFormat("en-NG", { style: "currency", currency: code }).format(amount); }
    catch (_) { return code + " " + amount.toFixed(2); }
  }

  function normalizeQuery(value) {
    return String(value || "").trim().toLowerCase().replace(/\s+/g, " ");
  }

  function itemMatches(item, query) {
    var q = normalizeQuery(query);
    if (!q) return true;
    var haystack = (String(item.name || "") + " " + String(item.description || "")).toLowerCase();
    return haystack.indexOf(q) !== -1;
  }

  // Filter the server's category list. `area` is ALL, RESTAURANT or BAR;
  // `categoryId` is "ALL" or a category id. Empty categories are removed.
  function filterMenu(categories, options) {
    var opts = options || {};
    var area = String(opts.area || "ALL").toUpperCase();
    var categoryId = opts.categoryId == null ? "ALL" : String(opts.categoryId);
    var query = opts.query || "";
    return (categories || []).reduce(function (out, category) {
      if (area !== "ALL" && category.service_area !== area) return out;
      if (categoryId !== "ALL" && String(category.id) !== categoryId) return out;
      var items = (category.items || []).filter(function (item) { return itemMatches(item, query); });
      if (items.length) out.push(Object.assign({}, category, { items: items }));
      return out;
    }, []);
  }

  function categoryOptions(categories, area) {
    var wanted = String(area || "ALL").toUpperCase();
    return (categories || [])
      .filter(function (category) { return wanted === "ALL" || category.service_area === wanted; })
      .map(function (category) { return { value: String(category.id), label: category.name }; });
  }

  function areaLabel(area) {
    return String(area).toUpperCase() === "BAR" ? "Bar" : "Restaurant";
  }

  function availabilityBadge(item) {
    var available = !!(item && item.is_available);
    var quantity = item && Number(item.available_quantity);
    return {
      label: available ? (Number.isInteger(quantity) && quantity > 0 ? "Available · up to " + quantity : "Available") : "Currently unavailable",
      unavailable: !available,
    };
  }

  // Map a server service entry to the action the guest can take. Requests route
  // to the existing portal request flow; facilities only show details.
  function serviceAction(service) {
    if (service && service.kind === "request") {
      return { kind: "request", label: "Request Service", category: service.category };
    }
    return { kind: "details", label: "View Details", category: null };
  }

  // A menu item is ordered as a portal request routed to the department the
  // server names (ROOM_SERVICE for restaurant, FOOD_BEVERAGE for bar).
  function orderFor(item, category) {
    var summary = "Order: " + (item && item.name ? item.name : "menu item");
    return {
      category: (item && item.order_category) || (category && category.service_area === "BAR" ? "FOOD_BEVERAGE" : "ROOM_SERVICE"),
      summary: summary.slice(0, 255),
    };
  }

  function filterServices(services, query) {
    return (services || []).filter(function (service) {
      var q = normalizeQuery(query);
      if (!q) return true;
      return (String(service.name || "") + " " + String(service.description || "")).toLowerCase().indexOf(q) !== -1;
    });
  }

  catalog.formatNaira = formatNaira;
  catalog.formatCurrency = formatCurrency;
  catalog.normalizeQuery = normalizeQuery;
  catalog.itemMatches = itemMatches;
  catalog.filterMenu = filterMenu;
  catalog.categoryOptions = categoryOptions;
  catalog.areaLabel = areaLabel;
  catalog.availabilityBadge = availabilityBadge;
  catalog.serviceAction = serviceAction;
  catalog.orderFor = orderFor;
  catalog.filterServices = filterServices;

  /* ---------- rendering ---------- */

  function esc(value) {
    return J.esc ? J.esc(value == null ? "" : String(value)) : String(value == null ? "" : value);
  }

  function stateBox(root, html) {
    root.innerHTML = '<div class="empty-state">' + html + "</div>";
  }

  function errorBox(root, message, retryAttr) {
    stateBox(root,
      "<h3>Unavailable right now</h3><p>" + esc(message) + "</p>" +
      '<button class="btn btn-sm btn-outline" type="button" ' + retryAttr + ">Try again</button>");
  }

  function renderMenuItem(item, category) {
    var badge = availabilityBadge(item);
    var order = item.is_available
      ? '<button class="btn btn-sm btn-accent" type="button" data-portal-order="' + esc(item.id) + '" data-portal-order-category="' + esc(category.id) + '">Add to order</button>'
      : '<span class="badge badge-warning">Currently unavailable</span>';
    var options = (item.modifiers && item.modifiers.length)
      ? '<p class="caption">For custom options, please contact the hotel.</p>'
      : "";
    return (
      '<article class="card card-pad portal-menu-item" data-portal-menu-item="' + esc(item.id) + '">' +
        '<div style="display:flex;justify-content:space-between;gap:.75rem;align-items:flex-start">' +
          '<div><h3 style="margin:0">' + esc(item.name) + "</h3>" +
          '<span class="badge">' + esc(areaLabel(category.service_area)) + "</span> " +
          '<span class="badge ' + (badge.unavailable ? "badge-warning" : "badge-success") + '">' + esc(badge.label) + "</span></div>" +
          '<strong style="white-space:nowrap">' + esc(formatCurrency(item.price, item.currency)) + "</strong>" +
        "</div>" +
        (item.description ? '<p class="muted" style="margin:.5rem 0">' + esc(item.description) + "</p>" : "") +
        options +
        '<div class="card-actions" style="margin-top:.75rem">' + order + "</div>" +
      "</article>"
    );
  }

  // Menu panel. `api.load()` returns {categories:[...]};
  // `api.onOrder(item, category)` adds a server-listed item to the guest cart.
  catalog.mountMenu = function mountMenu(root, api) {
    var state = { area: "ALL", categoryId: "ALL", query: "", data: null };
    root.innerHTML =
      '<div class="filter-bar" style="flex-wrap:wrap;gap:.75rem">' +
        '<div class="portal-tabs" role="tablist" aria-label="Menu area" style="margin:0">' +
          '<button class="btn btn-sm is-active" type="button" role="tab" aria-selected="true" data-menu-area="ALL">All</button>' +
          '<button class="btn btn-sm btn-outline" type="button" role="tab" aria-selected="false" data-menu-area="RESTAURANT">Restaurant</button>' +
          '<button class="btn btn-sm btn-outline" type="button" role="tab" aria-selected="false" data-menu-area="BAR">Bar</button>' +
        "</div>" +
        '<div class="field" style="margin:0"><label class="field-label" for="portal-menu-category">Category</label>' +
          '<select class="select" id="portal-menu-category"></select></div>' +
        '<div class="field" style="margin:0"><label class="field-label" for="portal-menu-search">Search</label>' +
          '<input class="input" id="portal-menu-search" type="search" maxlength="80" placeholder="Search the menu"></div>' +
      "</div>" +
      '<p class="caption" data-menu-status aria-live="polite"></p>' +
      '<div data-menu-list class="portal-card-grid" aria-live="polite"></div>';

    var list = root.querySelector("[data-menu-list]");
    var status = root.querySelector("[data-menu-status]");
    var select = root.querySelector("#portal-menu-category");
    var search = root.querySelector("#portal-menu-search");

    function paint() {
      if (!state.data) return;
      var categories = filterMenu(state.data.categories, state);
      var options = categoryOptions(state.data.categories, state.area);
      select.innerHTML = '<option value="ALL">All categories</option>' + options.map(function (o) {
        return '<option value="' + esc(o.value) + '"' + (o.value === state.categoryId ? " selected" : "") + ">" + esc(o.label) + "</option>";
      }).join("");
      if (!categories.length) {
        status.textContent = "";
        stateBox(list, state.query
          ? "<h3>No matching items</h3><p>Try a different search or category.</p>"
          : "<h3>Nothing is listed here yet</h3><p>Check back later or ask the front desk.</p>");
        return;
      }
      var count = categories.reduce(function (n, c) { return n + c.items.length; }, 0);
      status.textContent = count + (count === 1 ? " item" : " items") + " shown. Availability may change; stock and prices are checked when you send your order.";
      list.innerHTML = categories.map(function (category) {
        return '<section style="grid-column:1/-1"><h3 style="margin:.5rem 0 0">' + esc(category.name) + "</h3>" +
          (category.description ? '<p class="caption">' + esc(category.description) + "</p>" : "") +
          '<div class="portal-card-grid" style="margin-top:.75rem">' +
          category.items.map(function (item) { return renderMenuItem(item, category); }).join("") +
          "</div></section>";
      }).join("");
    }

    function load() {
      stateBox(list, '<div class="loading-skeleton"><span></span><span></span><span></span></div>');
      status.textContent = "";
      return Promise.resolve(api.load()).then(function (payload) {
        state.data = payload || { categories: [] };
        paint();
      }).catch(function (err) {
        if (api.onAuthError && err && err.status === 401) { api.onAuthError(err); return; }
        errorBox(list, (err && err.message) || "The menu could not be loaded.", 'data-menu-retry');
      });
    }

    root.addEventListener("click", function (event) {
      var area = event.target.closest("[data-menu-area]");
      if (area) {
        state.area = area.getAttribute("data-menu-area");
        state.categoryId = "ALL";
        root.querySelectorAll("[data-menu-area]").forEach(function (b) {
          var active = b === area;
          b.classList.toggle("is-active", active);
          b.classList.toggle("btn-outline", !active);
          b.setAttribute("aria-selected", active ? "true" : "false");
        });
        return paint();
      }
      if (event.target.closest("[data-menu-retry]")) return load();
      var order = event.target.closest("[data-portal-order]");
      if (order && api.onOrder) {
        var id = order.getAttribute("data-portal-order");
        var categoryId = order.getAttribute("data-portal-order-category");
        var found = null, owner = null;
        (state.data && state.data.categories || []).forEach(function (c) {
          if (String(c.id) !== categoryId) return;
          owner = c;
          (c.items || []).forEach(function (i) { if (String(i.id) === id) found = i; });
        });
        if (found) api.onOrder(found, owner);
      }
    });
    select.addEventListener("change", function () { state.categoryId = select.value; paint(); });
    search.addEventListener("input", function () { state.query = search.value; paint(); });

    return { load: load, state: state };
  };

  function renderService(service) {
    var action = serviceAction(service);
    var price = service.price === null || service.price === undefined
      ? '<span class="caption">Pricing: ask the front desk</span>'
      : '<strong>' + esc(formatNaira(service.price)) + "</strong>";
    var button = action.kind === "request"
      ? '<button class="btn btn-sm btn-accent" type="button" data-service-action="request" data-service-key="' + esc(service.key) + '">Request Service</button>'
      : '<button class="btn btn-sm btn-outline" type="button" data-service-action="details" data-service-key="' + esc(service.key) + '">View Details</button>';
    return (
      '<article class="card card-pad portal-card" data-service-key="' + esc(service.key) + '">' +
        "<h3 style=\"margin:0\">" + esc(service.name) + "</h3>" +
        (service.department_label ? '<p class="caption">Handled by ' + esc(service.department_label) + "</p>" : "") +
        (service.description ? '<p class="muted" style="margin:.5rem 0">' + esc(service.description) + "</p>" : "") +
        '<p style="margin:.25rem 0">' + price + "</p>" +
        '<p class="caption">' + esc(service.availability || "") + "</p>" +
        '<div class="card-actions">' + button + "</div>" +
      "</article>"
    );
  }

  // Services panel. `api.load()` resolves to {services:[...]};
  // `api.onRequest(service)` opens the request form; `api.onDetails(service)` shows details.
  catalog.mountServices = function mountServices(root, api) {
    var state = { query: "", services: [] };
    root.innerHTML =
      '<div class="filter-bar"><div class="field" style="margin:0;flex:1"><label class="field-label" for="portal-service-search">Search services</label>' +
      '<input class="input" id="portal-service-search" type="search" maxlength="80" placeholder="Search services"></div></div>' +
      '<p class="caption" data-service-status aria-live="polite"></p>' +
      '<div data-service-list class="portal-card-grid" aria-live="polite"></div>';
    var list = root.querySelector("[data-service-list]");
    var status = root.querySelector("[data-service-status]");
    var search = root.querySelector("#portal-service-search");

    function paint() {
      var services = filterServices(state.services, state.query);
      if (!services.length) {
        status.textContent = "";
        stateBox(list, state.query
          ? "<h3>No matching services</h3><p>Try a different search.</p>"
          : "<h3>No services are listed yet</h3><p>Ask the front desk for help.</p>");
        return;
      }
      status.textContent = "";
      list.innerHTML = services.map(renderService).join("");
    }

    function load() {
      stateBox(list, '<div class="loading-skeleton"><span></span><span></span><span></span></div>');
      return Promise.resolve(api.load()).then(function (payload) {
        state.services = (payload && payload.services) || [];
        paint();
      }).catch(function (err) {
        if (api.onAuthError && err && err.status === 401) { api.onAuthError(err); return; }
        errorBox(list, (err && err.message) || "Services could not be loaded.", 'data-service-retry');
      });
    }

    root.addEventListener("click", function (event) {
      if (event.target.closest("[data-service-retry]")) return load();
      var button = event.target.closest("[data-service-action]");
      if (!button) return;
      var key = button.getAttribute("data-service-key");
      var service = state.services.filter(function (s) { return s.key === key; })[0];
      if (!service) return;
      if (button.getAttribute("data-service-action") === "request") { if (api.onRequest) api.onRequest(service); }
      else if (api.onDetails) api.onDetails(service);
    });
    search.addEventListener("input", function () { state.query = search.value; paint(); });

    return { load: load, state: state };
  };
})();
