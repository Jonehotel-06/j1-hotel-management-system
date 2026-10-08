/* js/dashboard.js */
/* ==========================================================================
   dashboard.js — shared staff dashboard behaviours.
   Nav rendering, sidebar, role-aware menus, KPI/data rendering helpers,
   reusable list + table renderers for bookings/guests/rooms.
   ========================================================================== */

(function () {
  "use strict";

  /* --------------------------- Sidebar rendering --------------------------- */
  const NAV = [
    { group: "Overview", items: [
      { label: "Dashboard", href: "index.html", icon: "layout", view: "dashboard" },
    ]},
    { group: "Operations", items: [
      { label: "Bookings", href: "bookings.html", icon: "calendar", view: "bookings" },
      { label: "Availability", href: "availability.html", icon: "grid", view: "availability" },
      { label: "Occupancy", href: "occupancy.html", icon: "calendar", view: "occupancy" },
      { label: "Check-in", href: "check-in.html", icon: "logOut", view: "check-in" },
      { label: "Check-out", href: "check-out.html", icon: "logOut", view: "check-out" },
      { label: "Missed bookings", href: "missed-bookings.html", icon: "userX", view: "missed-bookings" },
      { label: "Guests", href: "guests.html", icon: "users", view: "guests" },
      { label: "Rooms", href: "rooms.html", icon: "bed", view: "rooms" },
      { label: "POS & Room Service", href: "pos.html", icon: "utensils", view: "pos", roles: ["admin", "manager", "cashier"] },
      { label: "Workforce", href: "workforce.html", icon: "clock", view: "workforce", roles: ["admin", "manager", "receptionist", "cashier", "housekeeping", "maintenance", "inventory_clerk"] },
      { label: "Guest Requests", href: "service-requests.html", icon: "messageCircle", view: "service-requests", roles: ["admin", "manager", "receptionist", "housekeeping", "maintenance"] },
      { label: "Housekeeping", href: "housekeeping.html", icon: "bed", view: "housekeeping", roles: ["admin", "manager", "receptionist", "housekeeping"] },
      { label: "Maintenance", href: "maintenance.html", icon: "settings", view: "maintenance", roles: ["admin", "manager", "receptionist", "maintenance"] },
      { label: "Inventory", href: "inventory.html", icon: "inbox", view: "inventory", roles: ["admin", "manager", "inventory_clerk"] },
      { label: "Procurement", href: "procurement.html", icon: "receipt", view: "procurement", roles: ["admin", "manager", "inventory_clerk"] },
    ]},
    { group: "Finance", items: [
      { label: "Payments", href: "payments.html", icon: "creditCard", view: "payments" },
      { label: "Receipts", href: "receipts.html", icon: "receipt", view: "receipts" },
      { label: "Reports", href: "reports.html", icon: "barChart", view: "reports", minRole: "manager" },
    ]},
    { group: "Content", items: [
      { label: "Facilities", href: "facilities.html", icon: "building", view: "facilities", minRole: "manager" },
      { label: "Gallery", href: "gallery.html", icon: "image", view: "gallery", minRole: "manager" },
      { label: "Offers", href: "offers.html", icon: "tag", view: "offers", minRole: "manager" },
    ]},
    { group: "Communication", items: [
      { label: "Enquiries", href: "enquiries.html", icon: "message", view: "enquiries" },
      // Guest reviews are private to the top role — the menu item is a UX
      // convenience only; the backend rejects every other role regardless.
      { label: "Reviews", href: "reviews.html", icon: "star", view: "reviews", role: "admin" },
      { label: "Notifications", href: "notifications.html", icon: "bell", view: "notifications" },
    ]},
    { group: "Administration", items: [
      { label: "Staff", href: "staff.html", icon: "users", view: "staff", role: "admin" },
      { label: "Audit Logs", href: "audit-logs.html", icon: "fileText", view: "audit-logs", role: "admin" },
      { label: "Settings", href: "settings.html", icon: "settings", view: "settings", role: "admin" },
    ]},
  ];

  function renderSidebar(containerSel) {
    const container = document.querySelector(containerSel);
    if (!container) return;
    const current = location.pathname;
    let html = "";
    NAV.forEach((g) => {
      const items = g.items.filter((it) => {
        if (it.role && !(window.Auth && window.Auth.hasRole && window.Auth.hasRole(it.role))) return false;
        if (it.minRole && !(window.Auth && window.Auth.hasRole && window.Auth.hasRole(it.minRole))) return false;
        if (it.roles && !(window.Auth && window.Auth.hasAnyRole && window.Auth.hasAnyRole(it.roles))) return false;
        return true;
      });
      if (!items.length) return;
      html += '<div class="dash-nav-group"><div class="dash-nav-label">' + JONE.esc(g.group).toUpperCase() + '</div>';
      items.forEach((it) => {
        let cls = "dash-nav-item";
        const active = current.endsWith("/" + it.href);
        if (active) cls += " is-active";
        const badge = it.view === "notifications"
          ? '<span class="count notif-count" data-notif-badge hidden></span>' : "";
        html += '<a class="' + cls + '" href="' + it.href + '"' +
          (active ? ' aria-current="page"' : "") +
          ' data-base-label="' + JONE.esc(it.label) + '">' +
          '<span data-icon="' + it.icon + '"></span>' + JONE.esc(it.label) + badge + "</a>";
      });
      html += "</div>";
    });
    container.innerHTML = html;
    JONE.icons.inject(container);
  }

  /* ------------------------- Role / user header ---------------------------- */
  function renderUser(containerSel) {
    const c = document.querySelector(containerSel);
    if (!c) return;
    const user = (window.Auth && window.Auth.state && window.Auth.state.user) || {};
    const name = user.full_name || user.name || user.username || user.email || "Staff";
    const roleRaw = String(user.role || "staff").toLowerCase().replace(/_/g, " ");
    const role = roleRaw.charAt(0).toUpperCase() + roleRaw.slice(1);
    c.innerHTML =
      (user.profile_image_url
        ? '<img class="dash-avatar" src="' + JONE.esc(user.profile_image_url) + '" alt="" loading="lazy" onerror="this.style.display=\'none\';this.nextElementSibling.style.display=\'inline-flex\';">' : '') +
      '<div class="dash-avatar"' + (user.profile_image_url ? ' style="display:none;"' : '') + '>' + JONE.esc(JONE.initials(name)) + "</div>" +
      '<div><div class="dash-user-name">' + JONE.esc(name) + '</div><div class="dash-user-role">' + JONE.esc(role) + "</div></div>";
  }

  /* ------------------------------- Sidebar toggle --------------------------
     THE single dashboard drawer initialization path (spec §3). Idempotent:
     calling it twice never attaches duplicate listeners, so the hamburger
     can never double-toggle. Listeners are delegated on document so they
     survive the icon injector rewriting the toggle button's children, and the
     backdrop element is created here if the page shell didn't include one. */
  let sidebarWired = false;
  /* Sticky topbar elevation.
     IMPORTANT: the dashboard scrolls inside .dash-main (the .dash grid is
     height:100dvh + overflow:hidden), so a window scroll listener would never
     fire here. We observe the real scroll container instead, and fall back to
     the window for any page that is not inside the dashboard shell. */
  let topbarWired = false;
  function setupStickyTopbar() {
    if (topbarWired) return;
    const topbar = document.querySelector(".dash-topbar");
    if (!topbar) return;
    topbarWired = true;

    const scroller = document.querySelector(".dash-main");
    const readTop = () => (scroller ? scroller.scrollTop : window.scrollY);

    const apply = () => topbar.classList.toggle("scrolled", readTop() > 4);
    const onScroll = JONE.throttle(apply, 80);

    (scroller || window).addEventListener("scroll", onScroll, { passive: true });
    apply();   // correct state on load (e.g. restored scroll position)
  }

  function setupSidebar() {
    const sidebar = document.querySelector(".dash-sidebar");
    if (!sidebar) return;

    let backdrop = document.querySelector(".dash-sidebar-backdrop");
    if (!backdrop) {
      backdrop = document.createElement("div");
      backdrop.className = "dash-sidebar-backdrop";
      backdrop.setAttribute("aria-hidden", "true");
      document.body.appendChild(backdrop);
    }
    const toggleBtn = () => document.querySelector(".dash-menu-toggle");
    const t0 = toggleBtn();
    if (t0) {
      t0.setAttribute("aria-expanded", "false");
      t0.setAttribute("aria-controls", "dash-sidebar");
      if (!t0.getAttribute("aria-label")) t0.setAttribute("aria-label", "Open menu");
    }
    if (!sidebar.id) sidebar.id = "dash-sidebar";

    // Desktop collapse control + persisted state (independent of the mobile
    // drawer; styled desktop-only so mobile is never affected by it).
    setupSidebarCollapse();

    if (sidebarWired) return;   // never attach the document listeners twice
    sidebarWired = true;

    function setOpen(open) {
      const bd = document.querySelector(".dash-sidebar-backdrop") || backdrop;
      sidebar.classList.toggle("open", open);
      bd.classList.toggle("open", open);
      document.body.classList.toggle("drawer-open", open);
      const t = toggleBtn();
      if (t) {
        t.setAttribute("aria-expanded", open ? "true" : "false");
        t.setAttribute("aria-label", open ? "Close menu" : "Open menu");
      }
      if (open) {
        const first = sidebar.querySelector("a, button");
        if (first) { try { first.focus({ preventScroll: true }); } catch (_) { first.focus(); } }
      } else if (t && document.activeElement && sidebar.contains(document.activeElement)) {
        try { t.focus({ preventScroll: true }); } catch (_) { t.focus(); }
      }
    }
    const isOpen = () => sidebar.classList.contains("open");

    document.addEventListener("click", (e) => {
      if (e.target.closest(".dash-menu-toggle")) {
        e.preventDefault();
        setOpen(!isOpen());
        return;
      }
      // Tap backdrop or a nav link closes the drawer.
      if (e.target.closest(".dash-sidebar-backdrop") || e.target.closest(".dash-sidebar a")) {
        if (isOpen()) setOpen(false);
      }
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && isOpen()) setOpen(false);
    });
  }

  /* ===================== Desktop sidebar collapse =========================
     The ONLY desktop collapse manager — deliberately separate from the
     mobile drawer state in setupSidebar() (.open). Two independent states:

       desktop  body[data-sidebar="expanded"|"collapsed"]  (CSS min-width:1025px)
       mobile   sidebar.open + backdrop                     (CSS max-width:1024px)

     The collapsed styling is fully gated behind the desktop breakpoint, so a
     saved "collapsed" preference can NEVER affect the mobile drawer. The
     preference is a plain UI key — it never touches auth/booking storage. */
  const SIDEBAR_PREF_KEY = "jone.dashboard.sidebar";

  function sidebarPref() {
    const v = JONE.storage.get(SIDEBAR_PREF_KEY, null);
    return v === "collapsed" ? "collapsed" : "expanded";
  }

  function paintSidebarCollapse(state) {
    const collapsed = state === "collapsed";
    document.body.setAttribute("data-sidebar", collapsed ? "collapsed" : "expanded");
    const btn = document.querySelector("[data-sidebar-collapse]");
    if (btn) {
      btn.setAttribute("aria-expanded", collapsed ? "false" : "true");
      const label = collapsed ? "Expand sidebar" : "Collapse sidebar";
      btn.setAttribute("aria-label", label);
      btn.setAttribute("title", label);
      btn.innerHTML = '<span data-icon="' + (collapsed ? "chevronRight" : "chevronLeft") + '" data-size="18"></span>';
      JONE.icons.inject(btn);
    }
  }

  let collapseWired = false;
  function setupSidebarCollapse() {
    const sidebar = document.querySelector(".dash-sidebar");
    if (!sidebar) return;

    // Wrap the "Sign out" text once so collapsed CSS can show the icon alone;
    // the accessible name stays complete either way.
    const logout = sidebar.querySelector("[data-logout]");
    if (logout && !logout.querySelector(".dash-logout-label")) {
      const label = document.createElement("span");
      label.className = "dash-logout-label";
      [...logout.childNodes].forEach((n) => {
        if (n.nodeType === 3 && n.textContent.trim()) label.appendChild(n);
      });
      if (label.textContent.trim()) {
        label.textContent = label.textContent.trim();
        logout.appendChild(label);
      }
      if (!logout.getAttribute("aria-label")) {
        logout.setAttribute("aria-label", logout.textContent.replace(/\s+/g, " ").trim() || "Sign out");
      }
    }

    // One shared control for every dashboard page — injected here so the
    // checked-in shells never duplicate it. Hidden on mobile by CSS.
    if (!sidebar.querySelector("[data-sidebar-collapse]")) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "dash-sidebar-collapse";
      btn.setAttribute("data-sidebar-collapse", "");
      btn.setAttribute("aria-expanded", "true");
      btn.setAttribute("aria-label", "Collapse sidebar");
      btn.setAttribute("aria-controls", "dash-sidebar");
      btn.innerHTML = '<span data-icon="chevronLeft" data-size="18"></span>';
      // Sits top-right of the brand/logo header, opposite the wordmark.
      const brand = sidebar.querySelector(".dash-sidebar-brand");
      if (brand) {
        brand.appendChild(btn);
      } else {
        const footer = sidebar.querySelector(".dash-sidebar-footer");
        sidebar.insertBefore(btn, footer || null);
      }
      JONE.icons.inject(btn);
    }

    paintSidebarCollapse(sidebarPref());

    if (collapseWired) return;  // one delegated listener, never duplicated
    collapseWired = true;
    document.addEventListener("click", (e) => {
      const t = e.target.closest("[data-sidebar-collapse]");
      if (!t) return;
      e.preventDefault();
      const next = sidebarPref() === "collapsed" ? "expanded" : "collapsed";
      JONE.storage.set(SIDEBAR_PREF_KEY, next);
      paintSidebarCollapse(next);
    });
  }

  /* ---------------------- Unread notification badge -----------------------
     One centralized refresh path (spec §21): a single request per page load
     (plus explicit refreshes after read/mark-all actions), fanned out to
     every badge target — sidebar item, bottom-nav item, topbar control. */
  const notifBadge = (() => {
    let lastCount = null;
    let inflight = null;

    /* Mirror the unread count onto the installed app icon (Badging API).
       Feature-detected and failure-tolerant: unsupported browsers (all of
       iOS Safari, Firefox) simply keep the in-page badge.
       LIMITATION: this only runs while the app is open. Badging a CLOSED app
       needs a push subscription + service-worker push handler, which this
       project deliberately does not run. */
    function paintAppBadge(count) {
      try {
        if (count > 0 && typeof navigator.setAppBadge === "function") {
          navigator.setAppBadge(count).catch(() => {});
        } else if (typeof navigator.clearAppBadge === "function") {
          navigator.clearAppBadge().catch(() => {});
        }
      } catch (_) { /* never let badging break the dashboard */ }
    }

    function clearAppBadge() { paintAppBadge(0); }

    function paint(count) {
      lastCount = count;
      paintAppBadge(count);
      const label = count > 99 ? "99+" : String(count);
      document.querySelectorAll("[data-notif-badge]").forEach((el) => {
        el.textContent = label;
        el.hidden = !(count > 0);
        const host = el.closest("a, button");
        if (host) {
          const base = host.getAttribute("data-base-label") || "Notifications";
          host.setAttribute("aria-label", count > 0 ? base + " (" + count + " unread)" : base);
        }
      });
    }

    async function refresh(force) {
      if (!window.Auth || !window.Auth.isAuthenticated()) return 0;
      if (inflight && !force) return inflight;   // de-duplicate concurrent callers
      inflight = (async () => {
        try {
          const res = await window.API.getUnreadCount();
          const count = (res.data && Number(res.data.unread_count)) || 0;
          paint(count);
          return count;
        } catch (_) {
          return lastCount || 0;   // keep last honest value; no retry storm
        } finally {
          inflight = null;
        }
      })();
      return inflight;
    }

    // Pages that already know the fresh value (notifications page) push it
    // here instead of causing another request.
    function set(count) { paint(Math.max(0, Number(count) || 0)); }

    return { refresh, set, get: () => lastCount, paintAppBadge, clearAppBadge };
  })();

  /* -------------------------- Mobile bottom nav ---------------------------
     Fixed bottom navigation for phones (spec §8). Settings is appended ONLY
     for admins — role comes from the authenticated profile and the backend
     enforces authorization regardless. */
  const BOTTOM_NAV = [
    { label: "Dashboard", href: "index.html", icon: "layout" },
    { label: "Bookings", href: "bookings.html", icon: "calendar" },
    { label: "Availability", href: "availability.html", icon: "grid" },
    { label: "Notifications", href: "notifications.html", icon: "bell", badge: true },
    { label: "Settings", href: "settings.html", icon: "settings", role: "admin" },
  ];

  /* Topbar bell with unread badge — same centralized badge fan-out. */
  function renderTopbarBell() {
    const right = document.querySelector(".dash-topbar-right");
    if (!right || right.querySelector("[data-topbar-bell]")) return;
    const a = document.createElement("a");
    a.href = "notifications.html";
    a.className = "btn-icon btn-ghost dash-topbar-bell";
    a.setAttribute("data-topbar-bell", "");
    a.setAttribute("data-base-label", "Notifications");
    a.setAttribute("aria-label", "Notifications");
    a.innerHTML = '<span data-icon="bell" data-size="19"></span><span class="notif-count" data-notif-badge hidden></span>';
    right.insertBefore(a, right.firstChild);
    JONE.icons.inject(a);
  }

  function renderBottomNav() {
    if (document.querySelector(".dash-bottom-nav")) return;   // once per page
    const page = (location.pathname.split("/").pop() || "index.html");
    const items = BOTTOM_NAV.filter((it) =>
      !it.role || (window.Auth && window.Auth.hasRole && window.Auth.hasRole(it.role))
    );
    const nav = document.createElement("nav");
    nav.className = "dash-bottom-nav";
    nav.setAttribute("aria-label", "Dashboard quick navigation");
    nav.innerHTML = items.map((it) => {
      const active = page === it.href;
      return '<a class="dash-bottom-item' + (active ? " is-active" : "") + '" href="' + it.href + '"' +
        (active ? ' aria-current="page"' : "") +
        ' data-base-label="' + JONE.esc(it.label) + '" aria-label="' + JONE.esc(it.label) + '">' +
        '<span class="dash-bottom-icon"><span data-icon="' + it.icon + '" data-size="22"></span>' +
        (it.badge ? '<span class="notif-count" data-notif-badge hidden></span>' : "") +
        '</span><span class="dash-bottom-label">' + JONE.esc(it.label) + "</span></a>";
    }).join("");
    document.body.appendChild(nav);
    document.body.classList.add("has-bottom-nav");
    JONE.icons.inject(nav);
  }

  /* ------------------------ Data-state renderers -------------------------- */
  // Central loading / empty / error states so dashboard pages never show fake
  // records or blank space. States only appear for genuine API outcomes.
  // Detect whether a target is a table/tbody (row-based) or a generic container.
  function isTable(el) {
    return el && (el.tagName === "TBODY" || el.tagName === "TABLE" || el.tagName === "TR");
  }
  function stateBody(icon, title, msg, extra) {
    const ic = icon ? '<span data-icon="' + JONE.esc(icon) + '" data-size="42"></span>' : '';
    const m = msg ? '<p>' + JONE.esc(msg) + '</p>' : '';
    return '<div class="empty-state">' + ic + '<h3>' + JONE.esc(title) + '</h3>' + m + (extra || '') + '</div>';
  }

  function genericSkeleton() {
    return '<div class="loading-skeleton" role="status" aria-live="polite">' +
      '<span class="visually-hidden">Loading</span>' +
      '<div class="loading-skeleton-lines" aria-hidden="true">' +
        '<span class="skeleton skeleton-line loading-skeleton-line-wide"></span>' +
        '<span class="skeleton skeleton-line loading-skeleton-line-mid"></span>' +
        '<span class="skeleton skeleton-line loading-skeleton-line-short"></span>' +
      '</div>' +
    '</div>';
  }

  function tableSkeleton(colspan) {
    const widths = ["88%", "66%", "78%", "52%", "70%", "60%", "82%", "48%"];
    let rows = '<tr class="visually-hidden"><td colspan="' + colspan + '" role="status" aria-live="polite">Loading</td></tr>';
    for (let row = 0; row < 3; row += 1) {
      rows += '<tr class="table-skeleton-row" aria-hidden="true">';
      for (let col = 0; col < colspan; col += 1) {
        rows += '<td><span class="skeleton skeleton-line" style="width:' + widths[(row + col) % widths.length] + '"></span></td>';
      }
      rows += '</tr>';
    }
    return rows;
  }

  const DATA = {
    apiBaseConfigured() {
      return !!(window.APP_CONFIG && window.APP_CONFIG.API_BASE_URL);
    },
    // Real column count from the table's own header, so loading/empty/error
    // rows always span the full width regardless of the table layout.
    colCount(el) {
      const table = el && el.closest ? el.closest("table") : null;
      const headRow = table && table.querySelector("thead tr");
      const n = headRow ? headRow.children.length : 0;
      return n || 8;
    },
    loading(el, kind = "table") {
      if (!el) return;
      const tbl = isTable(el);
      // Skeletons preserve the destination layout while data is in flight;
      // errors and empty responses below replace them with honest, retryable
      // states. ``kind`` stays accepted for existing dashboard callers.
      el.innerHTML = tbl ? tableSkeleton(DATA.colCount(el)) : genericSkeleton(kind);
    },
    empty(el, icon, title, msg, colspan) {
      if (!el) return;
      const tbl = isTable(el);
      el.innerHTML = tbl
        ? '<tr><td colspan="' + (colspan || DATA.colCount(el)) + '">' + stateBody(icon, title, msg) + '</td></tr>'
        : stateBody(icon, title, msg);
      if (icon || title) JONE.icons.inject(el);
    },
    error(el, title, msg, colspan, showRetry = true) {
      if (!el) return;
      const retry = showRetry ? '<button class="btn btn-sm btn-outline" data-retry style="margin-top:0.75rem;">Try again</button>' : '';
      const tbl = isTable(el);
      el.innerHTML = tbl
        ? '<tr><td colspan="' + (colspan || DATA.colCount(el)) + '">' + stateBody("alertTriangle", title, msg, retry) + '</td></tr>'
        : stateBody("alertTriangle", title, msg, retry);
      if (showRetry) {
        const btn = el.querySelector("[data-retry]");
        if (btn) btn.addEventListener("click", () => { location.reload(); });
      }
    },
    // Wrap a list target: show loading, then either rows / empty / error.
    async loadList(container, cells, opts) {
      const { icon, emptyTitle, emptyMsg, errorTitle, errorMsg, loadFn, mapRow, colspan } = opts;
      DATA.loading(container, "table");
      try {
        const res = await loadFn();
        const list = (res && (res.results || (Array.isArray(res) ? res : res.items))) || [];
        if (!list.length) {
          DATA.empty(container, icon, emptyTitle, emptyMsg, colspan || cells);
          return [];
        }
        container.innerHTML = list.map(mapRow).join("");
        JONE.icons.inject(container);
        return list;
      } catch (err) {
        DATA.error(container, errorTitle || "We couldn't load this data.", (err && err.message) || errorMsg || "", colspan || cells);
        return null;
      }
    }
  };

  /* ======================================================================
     REUSABLE SERVER-SIDE PAGINATION
     Exactly ONE pager for every dashboard list. A page calls
       JONE.dashboard.renderPagination(container, meta, { onPage })
     after each API load; the control renders only the UI and reports the
     requested page back through onPage — the page then re-requests the same
     endpoint with the new `page` (the database paginates, never the browser).

       meta — the envelope pagination block
              { count, page, page_size, total_pages, next, previous }
              (res.pagination) or the normalizeList() result
              ({ pageSize, totalPages, … }).

     idempotent: re-render freely after each load; the click listener is
     delegated and attached exactly once per container.
     ====================================================================== */

  /* The rows-per-page every dashboard list requests. Single source of truth:
     APP_CONFIG.PAGE_SIZE, which matches the backend's default page size. */
  function pageSize() {
    const configured = parseInt(
      (window.APP_CONFIG && window.APP_CONFIG.PAGE_SIZE) || 10, 10
    );
    return configured > 0 ? configured : 10;
  }

  /* Accepts either pagination shape and derives anything missing. */
  function pagerMeta(meta) {
    if (!meta || typeof meta !== "object") return null;
    const page = Math.max(1, parseInt(meta.page, 10) || 1);
    const size = Math.max(
      1, parseInt(meta.page_size != null ? meta.page_size : meta.pageSize, 10) || pageSize()
    );
    let totalPages = meta.total_pages != null ? meta.total_pages : meta.totalPages;
    totalPages = totalPages == null ? null : Math.max(1, parseInt(totalPages, 10) || 1);
    const count = meta.count != null && !isNaN(parseInt(meta.count, 10))
      ? Math.max(0, parseInt(meta.count, 10)) : null;
    if (totalPages == null && count != null) totalPages = Math.max(1, Math.ceil(count / size));
    if (totalPages == null) totalPages = page;
    return { page: Math.min(page, totalPages), pageSize: size, totalPages, count };
  }

  /* Compact numbered window: page ±1 plus both ends, nulls = ellipses. */
  function pagerPageNumbers(page, totalPages) {
    if (totalPages <= 7) return Array.from({ length: totalPages }, (_, i) => i + 1);
    const seq = [1, 2];
    const start = Math.max(3, page - 1);
    const end = Math.min(totalPages - 2, page + 1);
    if (start > 3) seq.push(null);
    for (let p = start; p <= end; p++) seq.push(p);
    if (end < totalPages - 2) seq.push(null);
    seq.push(totalPages - 1, totalPages);
    return seq;
  }

  /* Pure HTML builder (unit-testable without a live DOM). */
  function paginationHTML(meta) {
    const m = pagerMeta(meta);
    if (!m || m.totalPages <= 1) return "";
    let range;
    if (m.count != null) {
      const first = m.count === 0 ? 0 : (m.page - 1) * m.pageSize + 1;
      const last = Math.min(m.count, m.page * m.pageSize);
      range = "Showing " + first + "–" + last + " of " + m.count;
    } else {
      range = "Page " + m.page + " of " + m.totalPages;
    }
    let html = '<span class="muted" aria-live="polite">' + JONE.esc(range) + "</span>";
    html += '<button type="button" data-page="' + (m.page - 1) + '" aria-label="Previous page"' +
      (m.page <= 1 ? " disabled" : "") + '>' +
      '<span data-icon="chevronLeft" data-size="15"></span><span class="page-label">Previous</span></button>';
    html += '<span class="page-nums">';
    pagerPageNumbers(m.page, m.totalPages).forEach(function (n) {
      if (n == null) { html += '<span class="page-ellipsis" aria-hidden="true">\u2026</span>'; return; }
      const active = n === m.page;
      html += '<button type="button" class="page-num' + (active ? " is-active" : "") + '" data-page="' + n + '"' +
        (active ? ' aria-current="page" disabled' : "") +
        ' aria-label="Page ' + n + (active ? " (current)" : "") + '">' + n + "</button>";
    });
    html += "</span>";
    html += '<button type="button" data-page="' + (m.page + 1) + '" aria-label="Next page"' +
      (m.page >= m.totalPages ? " disabled" : "") + '>' +
      '<span class="page-label">Next</span><span data-icon="chevronRight" data-size="15"></span></button>';
    return html;
  }

  function renderPagination(container, meta, opts) {
    if (!container) return;
    opts = opts || {};
    if (typeof opts.onPage === "function") container.__jonePagerOnPage = opts.onPage;
    container.innerHTML = paginationHTML(meta);
    JONE.icons.inject(container);
    if (container.__jonePagerWired) return;
    container.__jonePagerWired = true;
    container.addEventListener("click", function (e) {
      const btn = e.target && e.target.closest ? e.target.closest("[data-page]") : null;
      if (!btn || btn.disabled || !container.contains(btn)) return;
      const next = parseInt(btn.getAttribute("data-page"), 10);
      if (!next || next < 1 || typeof container.__jonePagerOnPage !== "function") return;
      container.__jonePagerOnPage(next);
    });
  }

  /* ----------------------- Generic helpers for pages ----------------------- */
  function statusPill(status, textOrOverride) {
    const s = (status || "").toLowerCase().replace(/[\s_]+/g, "_");
    const label = textOrOverride || String(status || "").replace(/[_-]+/g, " ").toUpperCase();
    return '<span class="status-pill status-' + JONE.esc(s) + '">' + JONE.esc(label) + "</span>";
  }

  function badge(text) {
    return '<span class="badge">' + JONE.esc(text) + "</span>";
  }

  /* Render bookmarks/counts for topbar notice of low stock etc. */
  function topbar(title, subtitle, actionsHTML) {
    const t = document.querySelector("[data-dash-title]");
    if (t) t.innerHTML = "<h1>" + JONE.esc(title) + "</h1>" + (subtitle ? "<p>" + JONE.esc(subtitle) + "</p>" : "");
    const a = document.querySelector("[data-topbar-actions]");
    if (a && actionsHTML) a.innerHTML = actionsHTML;
  }

  /* Boot a dashboard page: guard auth, render sidebar + user, mark active.
     Production ALWAYS requires real authentication — there is no demo bypass.
     If a backend-less visual preview is ever required during development it must
     be enabled explicitly through window.APP_CONFIG.DEV_PREVIEW (default off)
     and never surfaces in a deployed build. */
  function boot(minRole, options) {
    options = options || {};
    if (!window.Auth.guard(minRole)) return false;
    renderSidebar("[data-dash-nav]");
    renderUser("[data-dash-user]");
    setupSidebar();
    setupStickyTopbar();
    renderTopbarBell();
    renderBottomNav();
    window.JONE.nav && window.JONE.nav.initDashboardNav();
    if (window.JONE.nav) window.JONE.nav.markActive(document);
    // Most pages need one badge request, fanned out to every badge target.
    // A page that already fetches an authoritative unread count can defer this
    // and call notifBadge.set(count), avoiding a duplicate API round trip.
    if (!options.deferNotificationBadge) notifBadge.refresh();
    return true;
  }

  /* ------------------------- Image upload field ----------------------------
     Premium drop-zone used by any dashboard form that uploads a picture
     (field type "imagefile"). Shows the CURRENT image when editing, previews a
     newly chosen file immediately, validates type/size client-side for fast
     feedback (the backend stays authoritative) and supports removing the
     existing image where the API allows it.

     Contract with formModal():
       - the chosen File is appended to FormData under the field's own name
       - when the user removes an existing image, `removeName` is appended as
         "true" (the backend field that deletes it, e.g. remove_image)
       - choosing a new file always supersedes a pending removal, so an old
         image can never be submitted by accident. */
  const IMAGE_TYPES = ["image/jpeg", "image/png", "image/webp"];
  const IMAGE_EXT_LABEL = "JPG, PNG or WebP";

  function formatBytes(bytes) {
    if (!bytes && bytes !== 0) return "";
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(0) + " KB";
    return (bytes / (1024 * 1024)).toFixed(1) + " MB";
  }

  function buildImageField(f, currentUrl) {
    const maxMB = f.maxMB || 5;
    const wrap = document.createElement("div");
    wrap.className = "field img-field";
    wrap.dataset.fmField = f.name;

    const label = document.createElement("label");
    label.className = "field-label";
    label.setAttribute("for", "fm-" + f.name);
    label.textContent = f.label + (f.required ? " *" : "");
    wrap.appendChild(label);

    const zone = document.createElement("div");
    zone.className = "img-drop";

    const input = document.createElement("input");
    input.type = "file";
    input.id = "fm-" + f.name;
    input.name = f.name;
    input.accept = f.accept || IMAGE_TYPES.join(",");
    input.className = "img-drop-input";
    if (f.required && !currentUrl) input.required = true;

    const preview = document.createElement("div");
    preview.className = "img-drop-preview";
    const thumb = document.createElement("img");
    thumb.alt = "";
    thumb.loading = "lazy";
    const meta = document.createElement("div");
    meta.className = "img-drop-meta";
    const metaName = document.createElement("div");
    metaName.className = "img-drop-name";
    const metaInfo = document.createElement("div");
    metaInfo.className = "caption img-drop-info";
    meta.appendChild(metaName);
    meta.appendChild(metaInfo);
    const actions = document.createElement("div");
    actions.className = "img-drop-actions";
    const changeBtn = document.createElement("button");
    changeBtn.type = "button";
    changeBtn.className = "btn btn-sm btn-outline";
    changeBtn.textContent = "Change";
    const removeBtn = document.createElement("button");
    removeBtn.type = "button";
    removeBtn.className = "btn btn-sm btn-outline";
    removeBtn.textContent = "Remove";
    actions.appendChild(changeBtn);
    if (f.removeName) actions.appendChild(removeBtn);
    preview.appendChild(thumb);
    preview.appendChild(meta);
    preview.appendChild(actions);

    const empty = document.createElement("div");
    empty.className = "img-drop-empty";
    empty.innerHTML =
      '<span class="img-drop-icon" data-icon="image" data-size="22"></span>' +
      '<div class="img-drop-cta"><strong>Select an image</strong>' +
      '<span class="caption">' + IMAGE_EXT_LABEL + " &middot; up to " + maxMB + " MB</span></div>";

    const err = document.createElement("div");
    err.className = "field-error";

    zone.appendChild(input);
    zone.appendChild(empty);
    zone.appendChild(preview);
    wrap.appendChild(zone);
    if (f.help) {
      const help = document.createElement("div");
      help.className = "hint";
      help.textContent = f.help;
      wrap.appendChild(help);
    }
    wrap.appendChild(err);

    // state: "empty" | "current" (server image) | "selected" (new file) | "removed"
    const state = { mode: currentUrl ? "current" : "empty", file: null, objectUrl: null };

    function releaseObjectUrl() {
      if (state.objectUrl) { URL.revokeObjectURL(state.objectUrl); state.objectUrl = null; }
    }
    function paint() {
      const showPreview = state.mode === "current" || state.mode === "selected";
      zone.classList.toggle("has-image", showPreview);
      preview.style.display = showPreview ? "" : "none";
      empty.style.display = showPreview ? "none" : "";
      removeBtn.textContent = state.mode === "selected" && currentUrl ? "Undo" : "Remove";
      if (state.mode === "current") {
        thumb.src = currentUrl;
        metaName.textContent = "Current image";
        metaInfo.textContent = "Select a new file to replace it.";
      } else if (state.mode === "selected" && state.file) {
        thumb.src = state.objectUrl;
        metaName.textContent = state.file.name;
        metaInfo.textContent = formatBytes(state.file.size) + " · ready to upload";
      } else if (state.mode === "removed") {
        empty.querySelector(".img-drop-cta strong").textContent = "Image will be removed";
      }
    }
    function fail(message) {
      err.textContent = message;
      err.style.display = "block";
      wrap.classList.add("has-error");
    }
    function clearError() {
      err.textContent = "";
      err.style.display = "none";
      wrap.classList.remove("has-error");
    }

    function reject(message) {
      input.value = "";
      releaseObjectUrl();
      state.file = null;
      state.mode = currentUrl ? "current" : "empty";
      paint();
      fail(message);
    }

    function accept(file) {
      clearError();
      if (!file) return;
      // The browser derives file.type from the extension, so these checks are
      // only a fast first pass — the real decode test follows, and the backend
      // validates independently either way.
      if (IMAGE_TYPES.indexOf(file.type) === -1) {
        return reject("Unsupported format. Use " + IMAGE_EXT_LABEL + ".");
      }
      if (!file.size) {
        return reject("That file is empty.");
      }
      if (file.size > maxMB * 1024 * 1024) {
        return reject("That image is " + formatBytes(file.size) + ". The maximum is " + maxMB + " MB.");
      }
      releaseObjectUrl();
      state.file = file;
      state.objectUrl = URL.createObjectURL(file);
      state.mode = "selected";
      paint();

      // Confirm the bytes really are a decodable image (catches a renamed or
      // truncated file before it is ever uploaded).
      const probe = new Image();
      const token = state.objectUrl;
      probe.onload = () => {
        if (state.objectUrl !== token) return;      // superseded by a newer pick
        metaInfo.textContent =
          formatBytes(file.size) + " · " + probe.naturalWidth + "×" + probe.naturalHeight + " · ready to upload";
      };
      probe.onerror = () => {
        if (state.objectUrl !== token) return;
        reject("That file isn't a readable image. Please choose a valid " + IMAGE_EXT_LABEL + " file.");
      };
      probe.src = state.objectUrl;
    }

    input.addEventListener("change", () => accept(input.files && input.files[0]));
    changeBtn.addEventListener("click", () => input.click());
    removeBtn.addEventListener("click", () => {
      clearError();
      if (state.mode === "selected") {
        // Undo the pending selection — restore the server image if there is one.
        releaseObjectUrl();
        state.file = null;
        input.value = "";
        state.mode = currentUrl ? "current" : "empty";
      } else {
        state.mode = currentUrl ? "removed" : "empty";
      }
      paint();
    });
    ["dragenter", "dragover"].forEach((evt) =>
      zone.addEventListener(evt, (e) => { e.preventDefault(); zone.classList.add("is-dragging"); }));
    ["dragleave", "drop"].forEach((evt) =>
      zone.addEventListener(evt, (e) => { e.preventDefault(); zone.classList.remove("is-dragging"); }));
    zone.addEventListener("drop", (e) => {
      const file = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (file) {
        try { input.files = e.dataTransfer.files; } catch (_) {}
        accept(file);
      }
    });

    paint();
    return {
      wrap,
      /* Append this field's contribution to the outgoing FormData. */
      appendTo(formData) {
        if (state.mode === "selected" && state.file) {
          formData.append(f.name, state.file, state.file.name);
        } else if (state.mode === "removed" && f.removeName) {
          formData.append(f.removeName, "true");
        }
      },
      hasFile: () => state.mode === "selected",
      isRequiredMissing: () => !!f.required && state.mode !== "selected" && state.mode !== "current",
      showError: fail,
      dispose: releaseObjectUrl
    };
  }

  /* --------------------------- Modal form builder --------------------------
     Shared create/edit dialog for dashboard CRUD pages.
     opts: {
       title, submitText,
       fields: [{ name, label, type: text|number|date|select|checkbox|textarea|file|imagefile,
                  options:[{value,label}], value, required, placeholder, help, step, accept,
                  // imagefile only:
                  currentUrl,      // existing image shown as "current" when editing
                  removeName,      // backend flag appended as "true" on removal
                  maxMB }],        // client-side size guard (backend is authoritative)
       values: {name: value},           // prefill (edit mode)
       multipart: bool,                 // include file inputs -> FormData
       onSubmit(values, formData) -> Promise   // throw to keep the dialog open
     }
     Field errors from the backend envelope ({errors:{field:[msgs]}}) are shown
     inline; the dialog only closes after onSubmit resolves. */
  function formModal(opts) {
    return new Promise((resolve) => {
      const backdrop = document.createElement("div");
      backdrop.className = "modal";
      backdrop.innerHTML = '<div class="modal-backdrop" data-fm-cancel></div>';

      const panel = document.createElement("div");
      panel.className = "modal-panel" + (opts.wide ? " modal-lg" : "");
      panel.setAttribute("role", "dialog");
      panel.setAttribute("aria-modal", "true");
      panel.setAttribute("aria-label", opts.title || "Form");

      const head = document.createElement("div");
      head.className = "modal-head";
      head.innerHTML = '<h3 style="font-size:var(--fs-md);"></h3>' +
        '<button class="btn-icon btn-ghost modal-close" type="button" data-fm-cancel aria-label="Close">' +
        JONE.icons.get("x") + "</button>";
      head.querySelector("h3").textContent = opts.title || "";

      const form = document.createElement("form");
      form.className = "modal-body";
      form.noValidate = true;

      const errBox = document.createElement("div");
      errBox.className = "field-error";
      errBox.style.cssText = "display:none;margin-bottom:0.75rem;font-weight:500;";

      // Image drop-zones are tracked so their files can be added to FormData.
      const imageFields = {};

      (opts.fields || []).forEach((f) => {
        if (f.type === "imagefile") {
          const current = f.currentUrl || (opts.values ? opts.values[f.name + "_url"] : null) || null;
          const built = buildImageField(f, current);
          imageFields[f.name] = built;
          form.appendChild(built.wrap);
          return;
        }
        if (f.type === "custom") {
          // Caller-rendered widget (e.g. the booking stay calendar). The field
          // owns its own DOM via f.render(mount, form), reports its value via
          // f.getValue() (an object is merged into the submitted values) and
          // may block submission via f.validate() returning an error message.
          const wrap = document.createElement("div");
          wrap.className = "field";
          wrap.dataset.fmField = f.name;
          if (f.label) {
            const label = document.createElement("div");
            label.className = "field-label";
            label.textContent = f.label + (f.required ? " *" : "");
            wrap.appendChild(label);
          }
          const mount = document.createElement("div");
          wrap.appendChild(mount);
          if (f.help) {
            const help = document.createElement("div");
            help.className = "hint";
            help.textContent = f.help;
            wrap.appendChild(help);
          }
          const ferr = document.createElement("div");
          ferr.className = "field-error";
          wrap.appendChild(ferr);
          form.appendChild(wrap);
          if (typeof f.render === "function") {
            try { f.render(mount, form); } catch (_) {}
          }
          return;
        }
        const wrap = document.createElement("div");
        wrap.className = "field";
        wrap.dataset.fmField = f.name;
        const pre = opts.values ? opts.values[f.name] : f.value;   // prefill (edit mode)
        const label = document.createElement("label");
        label.className = "field-label";
        label.setAttribute("for", "fm-" + f.name);
        label.textContent = f.label + (f.required ? " *" : "");
        wrap.appendChild(label);

        if (f.type === "checks") {
          // Checkbox group — values[f.name] ends up as an array of selected values.
          const group = document.createElement("div");
          group.className = "check-grid";
          group.style.cssText = "display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:0.4rem 0.9rem;padding-top:0.2rem;";
          const preArr = Array.isArray(pre) ? pre.map(String) : [];
          (f.options || []).forEach((o) => {
            const row = document.createElement("label");
            row.style.cssText = "display:flex;align-items:center;gap:0.5rem;font-size:var(--fs-sm);cursor:pointer;";
            const cb = document.createElement("input");
            cb.type = "checkbox";
            cb.dataset.fmCheck = f.name;
            cb.value = o.value;
            cb.checked = preArr.indexOf(String(o.value)) !== -1;
            const sp = document.createElement("span");
            sp.textContent = o.label;
            row.appendChild(cb);
            row.appendChild(sp);
            group.appendChild(row);
          });
          wrap.innerHTML = "";
          wrap.appendChild(label);
          wrap.appendChild(group);
          const ferr2 = document.createElement("div");
          ferr2.className = "field-error";
          wrap.appendChild(ferr2);
          form.appendChild(wrap);
          return;
        }

        let input;
        if (f.type === "select") {
          input = document.createElement("select");
          input.className = "select";
          (f.options || []).forEach((o) => {
            const op = document.createElement("option");
            op.value = o.value;
            op.textContent = o.label;
            input.appendChild(op);
          });
        } else if (f.type === "textarea") {
          input = document.createElement("textarea");
          input.className = "textarea";
          input.rows = f.rows || 3;
        } else if (f.type === "checkbox") {
          input = document.createElement("input");
          input.type = "checkbox";
          input.style.width = "auto";
          input.style.marginRight = "0.5rem";
        } else {
          input = document.createElement("input");
          input.className = "input";
          input.type = f.type || "text";
          if (f.step) input.step = f.step;
          if (f.accept) input.accept = f.accept;
        }
        input.id = "fm-" + f.name;
        input.name = f.name;
        if (f.placeholder) input.placeholder = f.placeholder;
        if (f.required) input.required = true;
        if (f.type === "checkbox") input.checked = pre === true || pre === "true";
        else if (pre != null && pre !== "" && f.type !== "file") input.value = pre;

        if (f.type === "checkbox") {
          const row = document.createElement("div");
          row.style.cssText = "display:flex;align-items:center;padding:0.45em 0;";
          row.appendChild(input);
          row.appendChild(label);
          label.style.marginBottom = "0";
          label.style.cssText = "font-size:var(--fs-sm);font-weight:500;letter-spacing:0;text-transform:none;margin-bottom:0;cursor:pointer;";
          wrap.innerHTML = "";
          wrap.appendChild(row);
        } else {
          wrap.appendChild(input);
        }

        if (f.help) {
          const help = document.createElement("div");
          help.className = "hint";
          help.textContent = f.help;
          wrap.appendChild(help);
        }
        const ferr = document.createElement("div");
        ferr.className = "field-error";
        wrap.appendChild(ferr);
        form.appendChild(wrap);
      });

      form.appendChild(errBox);

      const foot = document.createElement("div");
      foot.style.cssText = "display:flex;gap:0.6rem;justify-content:flex-end;padding-top:0.5rem;";
      const cancel = document.createElement("button");
      cancel.type = "button";
      cancel.className = "btn btn-outline";
      cancel.textContent = "Cancel";
      cancel.setAttribute("data-fm-cancel", "");
      const submit = document.createElement("button");
      submit.type = "submit";
      submit.className = "btn btn-accent";
      submit.textContent = opts.submitText || "Save";
      foot.appendChild(cancel);
      foot.appendChild(submit);
      form.appendChild(foot);

      panel.appendChild(head);
      // Optional read-only context (e.g. the payment summary) rendered above
      // the form. `onRender(panel)` lets a caller inject it and wire listeners.
      if (typeof opts.onRender === "function") {
        try { opts.onRender(panel); } catch (_) {}
      }
      panel.appendChild(form);
      backdrop.appendChild(panel);
      document.body.appendChild(backdrop);
      document.body.classList.add("modal-open");
      requestAnimationFrame(() => backdrop.classList.add("open"));
      JONE.icons.inject(panel);
      const first = form.querySelector("input, select, textarea");
      if (first) first.focus();

      function close(result) {
        document.removeEventListener("keydown", onKey);
        // Release any preview object URLs held by image drop-zones.
        Object.keys(imageFields).forEach((k) => imageFields[k].dispose());
        backdrop.classList.remove("open");
        setTimeout(() => {
          backdrop.remove();
          if (!document.querySelector(".modal.open")) document.body.classList.remove("modal-open");
        }, 200);
        resolve(result || null);
      }
      function onKey(e) { if (e.key === "Escape") close(null); }
      document.addEventListener("keydown", onKey);
      backdrop.addEventListener("click", (e) => {
        if (e.target.closest("[data-fm-cancel]")) close(null);
      });

      form.addEventListener("submit", async (e) => {
        e.preventDefault();
        if (!JONE.guardSubmit(submit)) return;
        // Clear old errors
        errBox.style.display = "none";
        form.querySelectorAll("[data-fm-field]").forEach((w) => {
          w.classList.remove("has-error");
          const fe = w.querySelector(".field-error");
          if (fe) fe.style.display = "none";
        });
        if (!form.checkValidity()) { form.reportValidity(); JONE.releaseGuard(submit); return; }

        // Required image fields are validated here (a drop-zone has no native
        // constraint once an existing image is already present).
        let imageMissing = false;
        Object.keys(imageFields).forEach((key) => {
          if (imageFields[key].isRequiredMissing()) {
            imageFields[key].showError("Please select an image.");
            imageMissing = true;
          }
        });
        if (imageMissing) { JONE.releaseGuard(submit); return; }

        // Custom fields validate themselves (e.g. the stay calendar requires
        // both dates). A returned string is shown as that field's error.
        let customInvalid = false;
        (opts.fields || []).forEach((f) => {
          if (f.type !== "custom" || typeof f.validate !== "function") return;
          const problem = f.validate();
          const w = form.querySelector('[data-fm-field="' + f.name + '"]');
          const fe = w && w.querySelector(".field-error");
          if (problem) {
            customInvalid = true;
            if (w) w.classList.add("has-error");
            if (fe) { fe.textContent = problem; fe.style.display = "block"; }
          } else if (fe) {
            if (w) w.classList.remove("has-error");
            fe.style.display = "none";
          }
        });
        if (customInvalid) { JONE.releaseGuard(submit); return; }

        const values = {};
        let formData = null;
        // A picked image forces multipart regardless of the caller's default.
        const hasImagePayload = Object.keys(imageFields).length > 0;
        if (opts.multipart || hasImagePayload) formData = new FormData();
        (opts.fields || []).forEach((f) => {
          if (f.type === "imagefile") {
            if (formData) imageFields[f.name].appendTo(formData);
            return;
          }
          if (f.type === "custom") {
            if (typeof f.getValue === "function") {
              const custom = f.getValue() || {};
              Object.keys(custom).forEach((k) => {
                values[k] = custom[k];
                if (formData && custom[k] !== "" && custom[k] != null) formData.append(k, custom[k]);
              });
            }
            return;
          }
          if (f.type === "checks") {
            const checked = Array.from(form.querySelectorAll('[data-fm-check="' + f.name + '"]:checked')).map((c) => c.value);
            values[f.name] = checked.map(Number);
            if (formData) checked.forEach((v) => formData.append(f.name, v));
            return;
          }
          const el = form.elements[f.name];
          if (!el) return;
          if (f.type === "checkbox") {
            values[f.name] = el.checked;
            if (formData) formData.append(f.name, el.checked);
          } else if (f.type === "file") {
            if (el.files && el.files[0]) {
              values[f.name] = el.files[0];
              formData.append(f.name, el.files[0], el.files[0].name);
            }
          } else {
            values[f.name] = el.value;
            if (formData && el.value !== "") formData.append(f.name, el.value);
          }
        });

        try {
          const out = await opts.onSubmit(values, formData);
          JONE.releaseGuard(submit);
          close(out === undefined ? true : out);
        } catch (err) {
          JONE.releaseGuard(submit);
          // Field-level errors from the backend envelope
          const errs = err && err.data && err.data.errors;
          if (errs && typeof errs === "object") {
            Object.keys(errs).forEach((k) => {
              const w = form.querySelector('[data-fm-field="' + k + '"]');
              const msg = Array.isArray(errs[k]) ? errs[k].join(" ") : String(errs[k]);
              if (w) {
                w.classList.add("has-error");
                const fe = w.querySelector(".field-error");
                if (fe) { fe.textContent = msg; fe.style.display = "block"; }
              } else {
                errBox.textContent = msg;
                errBox.style.display = "block";
              }
            });
            if (!errBox.textContent) { errBox.textContent = "Please review the highlighted fields."; errBox.style.display = "block"; }
          } else {
            const msg = (err && err.message) || "The request failed. Please try again.";
            errBox.textContent = msg;
            errBox.style.display = "block";
            JONE.ui.toast(msg, "error");
          }
        }
      });
    });
  }


  /* ======================================================================
     OPERATIONAL COMPONENTS
     Shared across pages so a behaviour is implemented once and the whole
     console behaves identically. Every component talks to the API layer
     (never raw fetch) and treats the backend response as the only truth.
     ====================================================================== */

  /* --------------------------- Permission helpers -------------------------
     These are UX hints ONLY. They hide controls a role cannot use; the
     backend re-checks every write with its own permission classes, so a
     tampered localStorage role changes what a user SEES, never what they
     can DO. */
  function currentRole() {
    const user = (window.Auth && window.Auth.state && window.Auth.state.user) || {};
    return String(user.role || "").toUpperCase();
  }
  function hasRole(role) {
    return !!(window.Auth && window.Auth.hasRole && window.Auth.hasRole(role));
  }
  /** Rooms / room-types: staff may read, only manager+ may write. */
  function canManageRooms() { return hasRole("manager"); }
  /** Account administration is admin-only. */
  function canManageStaff() { return hasRole("admin"); }

  /* --------------------------- Money formatting --------------------------- */
  function money(value, currency) {
    if (value === null || value === undefined || value === "") return null;
    return JONE.formatNaira(value, currency ? { currency } : undefined);
  }

  /* ======================================================================
     PAYMENT MODAL — the focused "Record payment" dialog.
     Opened directly after a manual booking is created and from any booking
     row. It never reloads the surrounding list: it submits to
     POST /api/admin/payments/record/, refreshes itself from the returned
     booking snapshot and hands the caller the fresh state through
     opts.onRecorded(booking, payment) so the row can be updated in place.
     ====================================================================== */
  const OFFLINE_PROVIDERS = [
    { value: "CASH", label: "Cash" },
    { value: "POS", label: "POS / Card" },
    { value: "BANK_TRANSFER", label: "Bank transfer" },
  ];

  /* Statuses whose booking can still legitimately take money at the desk.
     Mirrors the backend rule in payments.services.record_offline_payment. */
  const PAYABLE_BOOKING_STATUSES = ["PENDING", "CONFIRMED", "CHECKED_IN"];

  /* Whether the "Record payment" control should be offered at all.

     A payment that is already settled must not present the action: the
     backend refuses it (PaymentAlreadyCompletedError / INVALID_BOOKING_STATE),
     so showing the button only invites an error. The rule is derived from the
     authoritative booking snapshot — outstanding balance AND a status that can
     still accept money — never from a client-side flag.
     This is the single place the rule lives; every console page calls it. */
  function canRecordPayment(booking) {
    if (!booking) return false;
    const status = String(booking.status || "").toUpperCase();
    if (status && PAYABLE_BOOKING_STATUSES.indexOf(status) === -1) return false;
    const paymentStatus = String(booking.payment_status || "").toUpperCase();
    if (paymentStatus === "PAID" || paymentStatus === "REFUNDED") return false;
    const due = Number(booking.amount_due != null ? booking.amount_due : 0);
    return Number.isFinite(due) && due > 0;
  }

  function paymentModal(booking, opts = {}) {
    opts = opts || {};
    const b = booking || {};
    const due = Number(b.amount_due != null ? b.amount_due : 0);
    const total = Number(b.total_amount != null ? b.total_amount : 0);
    const paid = Number(b.amount_paid != null ? b.amount_paid : 0);
    const currency = b.currency || "NGN";
    const guest = b.guest || {};
    const guestName = guest.full_name || b.guest_name ||
      ([guest.first_name, guest.last_name].filter(Boolean).join(" ") || "Guest");
    const rooms = (b.room_numbers && b.room_numbers.length)
      ? b.room_numbers.join(", ")
      : (b.room_type_name || "—");

    let latest = b;   // refreshed from every successful response

    return formModal({
      title: "Record payment",
      submitText: "Record payment",
      wide: false,
      fields: [
        {
          name: "amount", label: "Amount received", type: "number",
          required: true, step: "0.01", min: "1",
          value: due > 0 ? String(due) : "",
          placeholder: "0.00",
          help: "Enter exactly what the guest handed over. The server validates it against the outstanding balance.",
        },
        {
          name: "provider", label: "Payment method", type: "select",
          required: true, value: "CASH", options: OFFLINE_PROVIDERS,
        },
        {
          name: "notes", label: "Notes (optional)", type: "textarea",
          placeholder: "e.g. settled at the front desk, receipt issued",
        },
      ],
      onRender(panel) {
        if (!panel) return;
        const summary = document.createElement("div");
        summary.className = "pay-modal-summary";
        summary.innerHTML =
          '<div class="pay-modal-hero">' +
            '<span class="pay-modal-hero-label">Outstanding balance</span>' +
            '<span class="pay-modal-hero-amount" data-pay-due>' + JONE.esc(money(latest.amount_due, currency) || "₦0.00") + '</span>' +
          "</div>" +
          '<dl class="pay-modal-facts">' +
            fact("Booking", latest.booking_reference || "—") +
            fact("Guest", guestName) +
            fact("Contact", guest.phone || b.guest_phone || guest.email || b.guest_email || "—") +
            fact("Room", rooms) +
            fact("Stay", [
              latest.check_in ? JONE.formatDate(latest.check_in, "mid") : null,
              latest.check_out ? JONE.formatDate(latest.check_out, "mid") : null,
            ].filter(Boolean).join(" → ") || "—") +
            fact("Status", String(latest.status || "—").replace(/_/g, " ").toUpperCase()) +
            fact("Total", money(total, currency) || "—") +
            fact("Paid to date", money(paid, currency) || "₦0.00") +
          "</dl>";
        panel.insertBefore(summary, panel.querySelector("form"));
      },
      onSubmit: async (values) => {
        const payload = {
          booking_reference: b.booking_reference || b.reference,
          amount: String(values.amount),
          provider: values.provider,
          notes: values.notes || "",
        };
        if (!payload.booking_reference) throw new Error("This booking has no reference yet.");
        const res = await window.API.recordPayment(payload);
        const data = (res && res.data) || {};
        // The backend snapshot is authoritative — never trust client arithmetic.
        if (data.booking) latest = Object.assign({}, latest, data.booking);
        if (typeof data.amount_paid === "string") latest.amount_paid = data.amount_paid;
        if (opts.onRecorded) {
          try { opts.onRecorded(latest, data); } catch (_) {}
        }
        return {
          message: data.booking && Number(data.booking.amount_due) > 0
            ? "Payment recorded. Balance remaining: " + money(data.booking.amount_due, currency) + "."
            : "Payment recorded — this booking is now fully paid.",
        };
      },
    });
  }

  function fact(label, value) {
    return '<div class="pay-modal-fact"><dt>' + JONE.esc(label) + "</dt><dd>" +
      JONE.esc(value == null || value === "" ? "—" : String(value)) + "</dd></div>";
  }

  /* ======================================================================
     STAFF PROFILE MODAL — complete, credential-free staff card.
     Data: GET /api/admin/users/staff/{id}/ (admins + managers see everyone,
     receptionists only themselves). Nothing is invented client-side.
     ====================================================================== */
  async function staffProfileModal(userId, opts = {}) {
    opts = opts || {};
    openPanel("Loading profile…", '<div class="loading-block"><div class="spinner" role="status"></div></div>');
    let profile;
    try {
      const res = await window.API.getStaffProfile(userId);
      profile = (res && res.data) || {};
    } catch (err) {
      closePanel();
      JONE.ui.toast((err && err.message) || "This staff profile couldn't be loaded.", "error");
      return null;
    }
    const stats = profile.stats || {};
    const fullName = profile.full_name ||
      ([profile.first_name, profile.last_name].filter(Boolean).join(" ") || profile.email || "Staff member");
    const body =
      '<div class="profile-card">' +
        '<div class="profile-identity">' +
          (profile.profile_image_url
            ? '<img class="profile-photo" src="' + JONE.esc(profile.profile_image_url) + '" alt="" loading="lazy">'
            : '<div class="profile-photo profile-photo-fallback">' + JONE.esc(JONE.initials(fullName)) + "</div>") +
          '<div class="profile-identity-text">' +
            '<h3>' + JONE.esc(fullName) + "</h3>" +
            '<p class="muted">' + JONE.esc(profile.email || "—") + "</p>" +
            '<div class="profile-tags">' +
              statusPill(profile.role || "—", String(profile.role_label || profile.role || "").toUpperCase()) +
              statusPill(profile.is_active ? "active" : "inactive",
                         profile.is_active ? "ACTIVE" : "DEACTIVATED") +
              (profile.email_verified ? "" : statusPill("pending", "EMAIL UNVERIFIED")) +
            "</div>" +
          "</div>" +
        "</div>" +
        '<dl class="profile-grid">' +
          row("Phone", profile.phone) +
          row("Role", profile.role_label || profile.role) +
          row("Account status", profile.is_active ? "Active" : "Deactivated") +
          row("Email verified", profile.email_verified ? "Yes" : "No") +
          row("Staff member", profile.is_staff_member ? "Yes" : "No") +
          row("Date joined", profile.date_joined ? JONE.formatDateTime(profile.date_joined) : "—") +
          row("Last login", profile.last_login ? JONE.formatDateTime(profile.last_login) : "—") +
          row("Last updated", profile.updated_at ? JONE.formatDateTime(profile.updated_at) : "—") +
        "</dl>" +
        '<div class="profile-stats">' +
          stat("Bookings created", stats.bookings_created) +
          stat("Payments recorded", stats.payments_recorded) +
          stat("Actions logged", stats.actions_logged) +
          stat("Last action", stats.last_action_at ? JONE.formatDate(stats.last_action_at, "mid") : "—") +
        "</div>" +
      "</div>";

    const actions = [];
    if (opts.canManage && profile.is_active) {
      actions.push('<button class="btn btn-sm btn-outline" data-sp-edit>Edit</button>');
      actions.push('<button class="btn btn-sm btn-danger" data-sp-deactivate>Deactivate</button>');
    } else if (opts.canManage && !profile.is_active) {
      actions.push('<button class="btn btn-sm btn-outline" data-sp-edit>Edit</button>');
      actions.push('<button class="btn btn-sm btn-accent" data-sp-activate>Activate</button>');
    }
    actions.push('<button class="btn btn-sm btn-ghost" data-sp-close>Close</button>');

    openPanel(fullName, body, actions.join(""), "modal-lg");

    const panel = lastPanel;
    if (!panel) return profile;
    const editBtn = panel.querySelector("[data-sp-edit]");
    if (editBtn) {
      editBtn.addEventListener("click", async () => {
        closePanel();
        const ok = await editStaffModal(profile);
        if (ok && opts.onChanged) opts.onChanged();
      });
    }
    const deactivate = panel.querySelector("[data-sp-deactivate]");
    if (deactivate) {
      deactivate.addEventListener("click", async () => {
        const ok = await JONE.ui.confirm({
          title: "Deactivate this account?",
          message: fullName + " will lose dashboard access immediately. Their history is kept.",
          confirmText: "Deactivate", danger: true,
        });
        if (!ok) return;
        await setStaffActive(deactivate, profile, false, opts);
      });
    }
    const activate = panel.querySelector("[data-sp-activate]");
    if (activate) {
      activate.addEventListener("click", () => setStaffActive(activate, profile, true, opts));
    }
    const closeBtn = panel.querySelector("[data-sp-close]");
    if (closeBtn) closeBtn.addEventListener("click", closePanel);
    return profile;
  }

  function row(label, value) {
    return '<div class="profile-row"><dt>' + JONE.esc(label) + "</dt><dd>" +
      JONE.esc(value == null || value === "" ? "—" : String(value)) + "</dd></div>";
  }
  function stat(label, value) {
    return '<div class="profile-stat"><span class="profile-stat-value">' +
      JONE.esc(value == null ? "—" : String(value)) + "</span><span class=\"profile-stat-label\">" +
      JONE.esc(label) + "</span></div>";
  }

  async function setStaffActive(btn, profile, active, opts) {
    if (!JONE.guardSubmit(btn)) return;
    try {
      await window.API.update("users", profile.id, { is_active: active });
      JONE.ui.toast(active ? "Account reactivated." : "Account deactivated.", "success");
      closePanel();
      if (opts && opts.onChanged) opts.onChanged();
    } catch (err) {
      JONE.releaseGuard(btn);
      JONE.ui.toast((err && err.message) || "The account couldn't be updated.", "error");
    }
  }

  /** Edit a staff account (admin only). Reuses formModal so validation and
      backend field errors behave exactly like the rest of the console. */
  function editStaffModal(profile) {
    return formModal({
      title: "Edit staff account",
      submitText: "Save changes",
      values: profile,
      fields: [
        { name: "first_name", label: "First name", type: "text", required: true },
        { name: "last_name", label: "Last name", type: "text", required: true },
        { name: "phone", label: "Phone", type: "tel" },
        {
          name: "role", label: "Role", type: "select", required: true,
          options: [
            { value: "ADMIN", label: "Admin" },
            { value: "MANAGER", label: "Manager" },
            { value: "RECEPTIONIST", label: "Receptionist" },
          ],
        },
      ],
      onSubmit: async (values) => {
        await window.API.update("users", profile.id, values);
        return { message: "Staff account updated." };
      },
    });
  }

  /* ======================================================================
     VIEW ROOMS — the room-type "View Rooms" action.
     Reuses GET /api/admin/rooms/?room_type={id} (staff may read) so no new
     endpoint is needed; the list is the backend's own inventory.
     ====================================================================== */
  async function roomTypeRoomsModal(roomType, opts = {}) {
    const rt = roomType || {};
    openPanel("Rooms — " + (rt.name || "Room type"),
      '<div class="loading-block"><div class="spinner" role="status"></div><p class="muted">Loading rooms…</p></div>');
    let rooms = [];
    try {
      const res = await window.API.list("rooms", {
        room_type: rt.id || rt.slug, page_size: 200,
      });
      rooms = window.API.normalizeList(res.data, res.pagination).items ||
        (res.data && res.data.results) || res.data || [];
    } catch (err) {
      closePanel();
      JONE.ui.toast((err && err.message) || "The rooms for this room type couldn't be loaded.", "error");
      return;
    }
    const body = rooms.length
      ? '<div class="room-board">' + rooms.map((r) => {
          const status = String(r.status || "").toLowerCase().replace(/_/g, " ");
          const hk = r.housekeeping_status ? String(r.housekeeping_status).toLowerCase().replace(/_/g, " ") : "";
          return '<div class="room-tile">' +
            '<div class="room-no">' + JONE.esc(r.room_number) + "</div>" +
            '<div class="room-type">' + (r.floor ? "Floor " + JONE.esc(r.floor) : "") + "</div>" +
            statusPill(r.status || "unknown", status.toUpperCase()) +
            (hk ? '<div class="room-type" style="margin-top:0.3rem;">Housekeeping: ' + JONE.esc(hk) + "</div>" : "") +
            (r.is_active ? "" : '<div class="room-type" style="color:var(--color-danger);">Retired</div>') +
            "</div>";
        }).join("") + "</div>"
      : '<div class="empty-state"><span data-icon="bed"></span><h3>No rooms yet</h3>' +
        "<p>No physical rooms have been added to this room type yet.</p></div>";
    openPanel("Rooms — " + (rt.name || "Room type"),
      '<p class="muted" style="margin-bottom:0.9rem;">' + rooms.length + " room" +
      (rooms.length === 1 ? "" : "s") + " in " + JONE.esc(rt.name || "this type") + ".</p>" + body,
      '<button class="btn btn-sm btn-ghost" data-rt-rooms-close>Close</button>', "modal-lg");
    const panel = lastPanel;
    const closeBtn = panel && panel.querySelector("[data-rt-rooms-close]");
    if (closeBtn) closeBtn.addEventListener("click", closePanel);
    if (panel) JONE.icons.inject(panel);
  }

  /* ======================================================================
     GUEST PROFILE MODAL — complete guest record + stay history.
     Uses GET /api/admin/guests/{id}/ (role-aware: the ID number is masked
     for receptionists by the backend; the frontend never un-masks it).
     ====================================================================== */
  async function guestProfileModal(guestId, opts = {}) {
    opts = opts || {};
    openPanel("Guest profile", '<div class="loading-block"><div class="spinner" role="status"></div></div>');
    let g;
    try {
      const res = await window.API.getOne("guests", guestId);
      g = (res && res.data) || {};
    } catch (err) {
      closePanel();
      JONE.ui.toast((err && err.message) || "This guest profile couldn't be loaded.", "error");
      return null;
    }
    const name = g.full_name || ([g.first_name, g.last_name].filter(Boolean).join(" ") || "Guest");
    const location = [g.city, g.state, g.country].filter(Boolean).join(", ");
    const history = g.bookings || [];
    const body =
      '<div class="profile-card">' +
        '<div class="profile-identity">' +
          '<div class="profile-photo profile-photo-fallback">' + JONE.esc(JONE.initials(name)) + "</div>" +
          '<div class="profile-identity-text"><h3>' + JONE.esc(name) + "</h3>" +
            '<p class="muted">' + JONE.esc([g.email, g.phone].filter(Boolean).join(" · ") || "—") + "</p>" +
            '<div class="profile-tags">' +
              statusPill("checked_in", (g.bookings_count != null ? g.bookings_count : history.length) +
                " booking" + ((g.bookings_count != null ? g.bookings_count : history.length) === 1 ? "" : "s")) +
              (g.has_account ? statusPill("confirmed", "HAS ACCOUNT") : statusPill("pending", "NO ACCOUNT")) +
            "</div>" +
          "</div>" +
        "</div>" +
        '<dl class="profile-grid">' +
          row("Email", g.email) +
          row("Phone", g.phone) +
          row("Address", [g.address, location].filter(Boolean).join(", ")) +
          row("ID type", g.identification_type_label || g.identification_type) +
          row("ID number", g.identification_number || "—") +
          row("Guest since", g.created_at ? JONE.formatDate(g.created_at, "mid") : "—") +
          row("Last booking", g.last_booking_at ? JONE.formatDateTime(g.last_booking_at) : "—") +
          row("Special requests", g.special_requests) +
          row("Total paid", money(g.total_spent, "NGN")) +
          row("Outstanding", money(g.outstanding_balance, "NGN")) +
        "</dl>" +
      "</div>" +
      '<h4 class="profile-section-title">Booking &amp; stay history</h4>' +
      (history.length
        ? '<div class="table-wrap"><table class="table"><thead><tr>' +
            "<th>Reference</th><th>Room</th><th>Stay</th><th>Total</th><th>Paid</th><th>Status</th>" +
          "</tr></thead><tbody>" + history.map((b) =>
            "<tr>" +
              '<td><a href="booking-details.html?ref=' + encodeURIComponent(b.booking_reference) + '">' +
                JONE.esc(b.booking_reference) + "</a></td>" +
              "<td>" + JONE.esc((b.room_numbers && b.room_numbers.length)
                ? b.room_numbers.join(", ") : (b.room_type_name || "—")) +
                (b.room_type_name ? '<div class="caption">' + JONE.esc(b.room_type_name) + "</div>" : "") +
              "</td>" +
              "<td>" + JONE.esc([
                b.check_in ? JONE.formatDate(b.check_in, "mid") : "",
                b.check_out ? JONE.formatDate(b.check_out, "mid") : "",
              ].filter(Boolean).join(" → ")) +
              (b.nights ? '<div class="caption">' + b.nights + " night" + (b.nights === 1 ? "" : "s") + "</div>" : "") +
              "</td>" +
              '<td class="num">' + JONE.esc(money(b.total_amount, b.currency) || "—") + "</td>" +
              '<td class="num">' + JONE.esc(money(b.amount_paid, b.currency) || "₦0.00") + "</td>" +
              "<td>" + statusPill(b.status) + "</td>" +
            "</tr>").join("") + "</tbody></table></div>"
        : '<div class="empty-state"><span data-icon="calendar"></span><h3>No bookings yet</h3>' +
          "<p>This guest has no recorded stays.</p></div>");
    openPanel(name, body, '<button class="btn btn-sm btn-ghost" data-gp-close>Close</button>', "modal-lg");
    const panel = lastPanel;
    const closeBtn = panel && panel.querySelector("[data-gp-close]");
    if (closeBtn) closeBtn.addEventListener("click", closePanel);
    if (panel) JONE.icons.inject(panel);
    return g;
  }

  /* ----------------------------------------------------------------------
     Payment detail panel (the payments table's "View" action).

     Shows the complete payment record the backend returns — reference,
     booking, guest, method, channel, status, gateway response, transaction id,
     timestamps, staff member and notes. No card details exist in the API and
     none are displayed. The list row is shown immediately so the panel is
     never blank, then refreshed from the detail endpoint.
     ---------------------------------------------------------------------- */
  function paymentDetailBody(p) {
    p = p || {};
    const provider = String(p.provider || "").replace(/_/g, " ").toLowerCase();
    const title = p.reference || "Payment";
    return (
      '<div class="profile-card">' +
        '<div class="profile-identity">' +
          '<div class="profile-identity-text">' +
            "<h3>" + JONE.esc(money(p.amount, p.currency) || "—") + "</h3>" +
            '<p class="muted">' + JONE.esc(title) + "</p>" +
            '<div class="profile-tags">' + statusPill(p.status) +
              (provider ? statusPill("confirmed", provider.toUpperCase()) : "") +
            "</div>" +
          "</div>" +
        "</div>" +
        '<dl class="profile-grid">' +
          row("Guest", p.guest_name) +
          row("Booking", p.booking_reference) +
          row("Method", provider) +
          row("Channel", p.channel) +
          row("Status", p.status) +
          row("Amount", money(p.amount, p.currency)) +
          row("Paid at", p.paid_at ? JONE.formatDateTime(p.paid_at) : "—") +
          row("Recorded", p.created_at ? JONE.formatDateTime(p.created_at) : "—") +
          row("Transaction ID", p.transaction_id) +
          row("Recorded by", p.staff_email) +
          row("Gateway response", p.gateway_response) +
          row("Notes", p.notes) +
        "</dl>" +
      "</div>"
    );
  }

  async function paymentDetailPanel(paymentId, rowData) {
    const initial = rowData || {};
    const heading = "Payment " + (initial.reference || "");
    openPanel(
      heading.trim(),
      rowData
        ? paymentDetailBody(initial)
        : '<div class="loading-block"><div class="spinner" role="status"></div></div>',
      '<button class="btn btn-sm btn-ghost" data-pd-close>Close</button>' +
      (initial.booking_reference
        ? ' <a class="btn btn-sm btn-outline" href="booking-details.html?ref=' +
          encodeURIComponent(initial.booking_reference) + '">Open booking</a>'
        : "")
    );
    const wire = () => {
      const panel = lastPanel;
      if (!panel) return;
      const close = panel.querySelector("[data-pd-close]");
      if (close) close.addEventListener("click", closePanel);
      JONE.icons.inject(panel);
    };
    wire();

    let payment = initial;
    try {
      const res = await window.API.getOne("payments", paymentId);
      payment = Object.assign({}, initial, (res && res.data) || {});
    } catch (err) {
      if (!rowData) {
        closePanel();
        JONE.ui.toast((err && err.message) || "This payment couldn't be loaded.", "error");
        return null;
      }
      // The row we already have is authoritative enough to keep on screen.
    }
    const body = lastPanel && lastPanel.querySelector("[data-panel-body]");
    if (body) {
      body.innerHTML = paymentDetailBody(payment);
      JONE.icons.inject(body);
    }
    return payment;
  }

  /* ======================================================================
     Minimal generic panel host for the read-only dialogs above.
     Uses the same .modal markup + focus handling as formModal so the look,
     the escape/backdrop behaviour and mobile rendering stay identical.
     ====================================================================== */
  let lastPanel = null;
  function closePanel() {
    if (panelHost) {
      panelHost.remove();
      panelHost = null;
      lastPanel = null;
      document.body.classList.remove("modal-open");
      document.removeEventListener("keydown", panelKeydown, true);
    }
  }
  let panelHost = null;
  function panelKeydown(e) { if (e.key === "Escape") closePanel(); }

  function openPanel(title, html, actionsHTML, sizeClass) {
    closePanel();
    panelHost = document.createElement("div");
    panelHost.className = "modal open";
    panelHost.innerHTML =
      '<div class="modal-backdrop" data-panel-close></div>' +
      '<div class="modal-panel ' + (sizeClass || "") + '" role="dialog" aria-modal="true" aria-label="' +
      JONE.esc(title) + '">' +
        '<div class="modal-head"><h3 style="font-size:var(--fs-md);">' + JONE.esc(title) + "</h3>" +
        '<button class="btn-icon btn-ghost modal-close" type="button" data-panel-close aria-label="Close">' +
        JONE.icons.get("x") + "</button></div>" +
        '<div class="modal-body" data-panel-body>' + html + "</div>" +
        (actionsHTML ? '<div class="modal-foot" data-panel-foot>' + actionsHTML + "</div>" : "") +
      "</div>";
    document.body.appendChild(panelHost);
    document.body.classList.add("modal-open");
    lastPanel = panelHost.querySelector(".modal-panel");
    panelHost.querySelectorAll("[data-panel-close]").forEach((n) =>
      n.addEventListener("click", closePanel));
    document.addEventListener("keydown", panelKeydown, true);
    const closeBtn = panelHost.querySelector(".modal-close");
    if (closeBtn) closeBtn.focus({ preventScroll: true });
    JONE.icons.inject(panelHost);
    return lastPanel;
  }

  window.JONE = window.JONE || {};
  window.JONE.dashboard = {
    renderSidebar, renderUser, setupSidebar, setupStickyTopbar, renderTopbarBell, renderBottomNav,
    notifBadge, statusPill, boot, topbar, NAV, badge, DATA, formModal,
    /* Test-only seeds: pure sidebar-collapse state mappers (Node harness). */
    _test: {
      sidebarPrefState: (v) => (v === "collapsed" ? "collapsed" : "expanded"),
      sidebarNext: (state) => (state === "collapsed" ? "expanded" : "collapsed"),
      sidebarLabelFor: (collapsed) => ({
        aria: collapsed ? "Expand sidebar" : "Collapse sidebar",
        icon: collapsed ? "chevronRight" : "chevronLeft",
        expanded: collapsed ? "false" : "true",
      }),
    },
    // List pagination (requests pages from the API; never slices in-browser)
    renderPagination, paginationHTML, pageSize,
    // Operational components
    currentRole, hasRole, canManageRooms, canManageStaff, money,
    paymentModal, canRecordPayment, staffProfileModal, editStaffModal, roomTypeRoomsModal,
    guestProfileModal, paymentDetailPanel, openPanel, closePanel,
  };
})();
