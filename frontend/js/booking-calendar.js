/* js/booking-calendar.js */
/* ==========================================================================
   booking-calendar.js — room-type-aware stay calendar (vanilla, accessible).

   Renders the check-in / check-out picker used by booking.html. The page owns
   the DATA (which dates are sold out for the selected room type, fetched via
   the central API client); this component owns the INTERACTION:

   • month navigation (two months on wide screens, one on small screens);
   • sold-out dates are visually disabled, `aria-disabled`, announced when
     clicked and can never be chosen as check-in;
   • once a check-in is chosen, check-out dates that would cross a sold-out
     night are disabled too — an invalid range cannot be selected, not merely
     rejected afterwards (the backend still re-validates authoritatively);
   • a check-out may land on a sold-out date (the guest leaves that morning);
   • keyboard: arrows / Home / End / PageUp / PageDown / Enter / Space with a
     roving tabindex inside role="grid" tables;
   • dates are handled as ISO calendar strings (YYYY-MM-DD) — no local-time
     `new Date("…")` parsing, so no timezone drift between browsers.

   The backend stays the single source of truth: this UI only prevents
   obviously impossible selections; booking creation re-checks everything.
   ========================================================================== */
(function () {
  "use strict";

  /* ------------------------- ISO date math (UTC only) ---------------------- */
  var DAY = 86400000;
  function isoParts(iso) {
    var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(iso || ""));
    return m ? { y: +m[1], m: +m[2], d: +m[3] } : null;
  }
  function toUtc(iso) { var p = isoParts(iso); return p ? Date.UTC(p.y, p.m - 1, p.d) : null; }
  function fromUtc(ms) { return new Date(ms).toISOString().slice(0, 10); }
  function addDays(iso, n) { return fromUtc(toUtc(iso) + n * DAY); }
  function diffDays(a, b) { return Math.round((toUtc(b) - toUtc(a)) / DAY); }
  function cmpIso(a, b) { return String(a) === String(b) ? 0 : (String(a) < String(b) ? -1 : 1); }
  function ymOf(iso) { return String(iso).slice(0, 7); }
  function todayIso() {
    var now = new Date();
    return fromUtc(Date.UTC(now.getFullYear(), now.getMonth(), now.getDate()));
  }

  var MONTHS = ["January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December"];
  var DOW = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"];           // Monday-first
  var DOW_FULL = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

  function monthLabel(ym) {
    var p = ym.split("-");
    return MONTHS[+p[1] - 1] + " " + p[0];
  }
  function daysInMonth(y, m) { return new Date(Date.UTC(y, m, 0)).getUTCDate(); }
  /* Monday-first weekday index of an ISO date. */
  function dowMon(iso) { var d = new Date(toUtc(iso)).getUTCDay(); return (d + 6) % 7; }
  function humanDate(iso) {
    var p = isoParts(iso);
    return p ? DOW_FULL[dowMon(iso)] + " " + p.d + " " + MONTHS[p.m - 1] + " " + p.y : String(iso);
  }
  function shortDate(iso) {
    var p = isoParts(iso);
    return p ? p.d + " " + MONTHS[p.m - 1].slice(0, 3) + " " + p.y : String(iso);
  }
  function nextYm(ym) {
    var p = ym.split("-");
    var y = +p[0], m = +p[1] + 1;
    if (m > 12) { m = 1; y += 1; }
    return y + "-" + String(m).padStart(2, "0");
  }
  function prevYm(ym) {
    var p = ym.split("-");
    var y = +p[0], m = +p[1] - 1;
    if (m < 1) { m = 12; y -= 1; }
    return y + "-" + String(m).padStart(2, "0");
  }

  /* ------------------------------- component ------------------------------- */
  function mount(container, opts) {
    if (!container) return null;
    opts = opts || {};

    var st = {
      min: isoParts(opts.minDate) ? opts.minDate : todayIso(),
      max: isoParts(opts.maxDate) ? opts.maxDate : addDays(todayIso(), 365),
      months: Math.min(Math.max(opts.months || 2, 1), 2),
      unavailable: typeof opts.unavailable === "function" ? opts.unavailable : function () { return false; },
      unavailableScope: opts.unavailableScope || "",      // e.g. " for Superior Room"
      busy: false,
      error: false,
      checkIn: isoParts(opts.checkIn) ? opts.checkIn : null,
      checkOut: isoParts(opts.checkOut) ? opts.checkOut : null,
      viewYm: null,      // first displayed month
      focusDate: null,   // roving-tabindex target
      onChange: typeof opts.onChange === "function" ? opts.onChange : function () {},
      onRetry: typeof opts.onRetry === "function" ? opts.onRetry : null
    };

    /* root = permanent shell (toolbar/months/hint re-rendered inside `body`;
       the live region must survive re-renders so it lives directly on root). */
    var root = document.createElement("div");
    root.className = "cal-root";
    root.setAttribute("data-calendar", "");
    var body = document.createElement("div");
    body.className = "cal-body";
    var live = document.createElement("p");
    live.className = "sr-only";
    live.setAttribute("aria-live", "polite");
    root.appendChild(body);
    root.appendChild(live);
    container.appendChild(root);

    st.viewYm = ymOf(st.checkIn || st.min);
    st.focusDate = st.checkIn || st.min;

    /* ---------------------------- rules ---------------------------------- */
    function outOfWindow(iso) { return cmpIso(iso, st.min) < 0 || cmpIso(iso, st.max) > 0; }
    function isSoldOut(iso) { return !!st.unavailable(iso); }
    function selectableAsCheckin(iso) { return !outOfWindow(iso) && !isSoldOut(iso); }

    /* First sold-out night on/after `from` (null when the horizon is clear). */
    function firstBlockedAfter(from) {
      var cursor = from;
      while (cmpIso(cursor, st.max) <= 0) {
        if (isSoldOut(cursor)) return cursor;
        cursor = addDays(cursor, 1);
      }
      return null;
    }

    /* A stay [ci, e) needs every NIGHT available; the check-out morning
       itself may be sold out (that becomes the next guest's problem). */
    function selectableAsCheckout(e, ci) {
      if (!ci || cmpIso(e, ci) <= 0 || outOfWindow(e)) return false;
      var limit = firstBlockedAfter(ci);
      return !limit || cmpIso(e, limit) <= 0;
    }

    function nights() { return st.checkIn && st.checkOut ? diffDays(st.checkIn, st.checkOut) : 0; }
    function current() { return { checkIn: st.checkIn, checkOut: st.checkOut, nights: nights() }; }

    function announce(text) {
      live.textContent = "";
      window.setTimeout(function () { live.textContent = text; }, 30);
    }

    /* --------------------------- validation ------------------------------- */
    /* Re-check the current selection against (possibly new) availability.
       Clears whatever is no longer possible and reports why via onChange. */
    function revalidate(silent) {
      var reason = null;
      if (st.checkIn && !selectableAsCheckin(st.checkIn)) {
        st.checkIn = null;
        st.checkOut = null;
        reason = "checkin-unavailable";
      } else if (st.checkIn && st.checkOut && !selectableAsCheckout(st.checkOut, st.checkIn)) {
        st.checkOut = null;
        reason = "range-unavailable";
      }
      if (!st.checkIn) st.checkOut = null;
      if (reason && !silent) st.onChange(current(), reason);
      return reason;
    }

    /* ----------------------------- render --------------------------------- */
    function render() {
      var hadFocus = root.contains(document.activeElement);
      body.innerHTML = "";

      /* toolbar: month navigation */
      var toolbar = document.createElement("div");
      toolbar.className = "cal-toolbar";
      var title = document.createElement("span");
      title.className = "cal-title";
      title.textContent = monthLabel(st.viewYm) +
        (st.months > 1 ? " – " + monthLabel(nextYm(st.viewYm)) : "");
      var nav = document.createElement("div");
      nav.className = "cal-nav";
      var prev = document.createElement("button");
      prev.type = "button";
      prev.className = "cal-nav-btn";
      prev.setAttribute("aria-label", "Previous month");
      prev.innerHTML = icon("chevronLeft", 18);
      prev.disabled = st.viewYm <= ymOf(st.min);
      prev.addEventListener("click", function () { moveView(-1); });
      var next = document.createElement("button");
      next.type = "button";
      next.className = "cal-nav-btn";
      next.setAttribute("aria-label", "Next month");
      next.innerHTML = icon("chevronRight", 18);
      var lastViewYm = ymOf(st.max);
      if (st.months > 1) lastViewYm = prevYm(lastViewYm);
      next.disabled = st.viewYm >= lastViewYm;
      next.addEventListener("click", function () { moveView(1); });
      nav.appendChild(prev); nav.appendChild(next);
      toolbar.appendChild(title); toolbar.appendChild(nav);
      body.appendChild(toolbar);

      /* month grids */
      var wrap = document.createElement("div");
      wrap.className = "cal-months";
      var ym = st.viewYm;
      for (var i = 0; i < st.months; i++) {
        wrap.appendChild(monthGrid(ym));
        ym = nextYm(ym);
      }
      body.appendChild(wrap);

      body.appendChild(hintRow());
      body.appendChild(legendRow());
      if (st.busy) body.appendChild(overlay("busy"));
      if (st.error) body.appendChild(overlay("error"));

      if (hadFocus) focusDate(st.focusDate, { preventScroll: true });
    }

    function icon(name, size) {
      if (window.JONE && JONE.icons && JONE.icons.get) {
        return JONE.icons.get(name).replace("<svg ", '<svg width="' + size + '" height="' + size + '" aria-hidden="true" ');
      }
      return "";
    }

    function monthGrid(ym) {
      var p = ym.split("-");
      var y = +p[0], m = +p[1];
      var month = document.createElement("div");
      month.className = "cal-month";

      var table = document.createElement("table");
      table.setAttribute("role", "grid");
      table.setAttribute("aria-label", monthLabel(ym) + " — choose dates");

      var thead = document.createElement("thead");
      var headRow = document.createElement("tr");
      DOW.forEach(function (d, i) {
        var th = document.createElement("th");
        th.setAttribute("scope", "col");
        th.setAttribute("aria-label", DOW_FULL[i]);
        th.textContent = d;
        headRow.appendChild(th);
      });
      thead.appendChild(headRow);
      table.appendChild(thead);

      var tbody = document.createElement("tbody");
      var firstIso = ym + "-01";
      var lead = dowMon(firstIso);
      var total = daysInMonth(y, m);
      var row = document.createElement("tr");
      var day = 1 - lead;
      while (day <= total) {
        var cell = document.createElement("td");
        cell.setAttribute("role", "gridcell");
        if (day < 1 || day > total) {
          cell.className = "cal-pad";
          cell.appendChild(document.createElement("span"));
        } else {
          cell.appendChild(dayCell(ym + "-" + String(day).padStart(2, "0")));
        }
        row.appendChild(cell);
        if (row.children.length === 7) { tbody.appendChild(row); row = document.createElement("tr"); }
        day++;
      }
      if (row.children.length) tbody.appendChild(row);
      table.appendChild(tbody);
      month.appendChild(table);
      return month;
    }

    /* Build one day button with its enable/disable rules + labels.

       Interaction model (strict — impossible selections are prevented):
       • no check-in yet      → a date is pickable iff it is a valid check-in
                                (not sold out, inside the window);
       • check-in chosen      → dates on/before it may restart the selection
                                (when sellable); later dates are pickable iff
                                the WHOLE stay [check-in, date) is available.
                                The first sold-out morning is a valid checkout
                                (the guest leaves that day), so it stays
                                pickable; anything past it is disabled. */
    function dayCell(iso) {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.dataset.date = iso;
      btn.textContent = String(isoParts(iso).d);

      var classes = ["cal-day"];
      var labels = [];
      if (iso === todayIso()) { classes.push("is-today"); labels.push("Today"); }

      var selectable = true;
      var why = "";
      var soldOut = isSoldOut(iso);
      if (outOfWindow(iso)) {
        selectable = false;
        why = cmpIso(iso, st.min) < 0 ? "in the past" : "outside the bookable window";
      } else if (st.checkIn && cmpIso(iso, st.checkIn) <= 0) {
        if (soldOut) { selectable = false; why = "sold out" + st.unavailableScope; }
      } else if (st.checkIn) {
        if (!selectableAsCheckout(iso, st.checkIn)) {
          selectable = false;
          why = soldOut ? "sold out" + st.unavailableScope : "your stay would cross a sold-out date";
        } else if (soldOut) {
          /* the first sold-out morning: a valid checkout, worth explaining */
          why = "checkout day — the room only needs to be free for your nights";
        }
      } else if (soldOut) {
        selectable = false;
        why = "sold out" + st.unavailableScope;
      }

      if (iso === st.checkIn) { classes.push("is-checkin"); labels.push("check-in"); }
      if (iso === st.checkOut) { classes.push("is-checkout"); labels.push("check-out"); }
      if (st.checkIn && st.checkOut && cmpIso(iso, st.checkIn) > 0 && cmpIso(iso, st.checkOut) < 0) {
        classes.push("is-in-range");
      }

      if (!selectable) {
        classes.push("is-disabled");
        btn.setAttribute("aria-disabled", "true");
        if (why) btn.title = humanDate(iso) + " — " + why;
        labels.push(why || "not available");
      } else if (why) {
        btn.title = humanDate(iso) + " — " + why;
      }
      btn.className = classes.join(" ");
      btn.setAttribute("aria-label", humanDate(iso) + (labels.length ? ", " + labels.join(", ") : ""));
      btn.tabIndex = (iso === st.focusDate) ? 0 : -1;

      btn.addEventListener("click", function () { pick(iso); });
      return btn;
    }

    /* ---------------------------- selection -------------------------------- */
    function pick(iso) {
      if (st.busy || st.error) return;
      if (outOfWindow(iso)) return;
      if (!st.checkIn || cmpIso(iso, st.checkIn) <= 0) {
        /* start (or restart) the selection — never on a sold-out night */
        if (isSoldOut(iso)) {
          announce(humanDate(iso) + " is sold out" + st.unavailableScope + ". Please choose another date.");
          return;
        }
        st.checkIn = iso;
        st.checkOut = null;
        announce("Check-in " + humanDate(iso) + ". Now choose your check-out date.");
      } else {
        /* check-out candidate: the whole stay must be available */
        if (!selectableAsCheckout(iso, st.checkIn)) {
          if (isSoldOut(iso)) {
            announce(humanDate(iso) + " is sold out" + st.unavailableScope + ". Please choose another date.");
          } else {
            var limit = firstBlockedAfter(st.checkIn);
            announce("A stay beginning " + shortDate(st.checkIn) + " cannot extend past " +
              shortDate(limit) + " — nights before it are sold out" + st.unavailableScope + ".");
          }
          return;
        }
        st.checkOut = iso;
        var n = diffDays(st.checkIn, st.checkOut);
        announce(n + (n === 1 ? " night" : " nights") + ": " +
          shortDate(st.checkIn) + " to " + shortDate(st.checkOut) + ".");
      }
      st.focusDate = iso;
      st.onChange(current(), "user");
      render();
    }

    /* ------------------------------ hint ----------------------------------- */
    function hintRow() {
      var row = document.createElement("div");
      row.className = "cal-hint";
      var hint = document.createElement("p");
      hint.className = "cal-hint-text";
      hint.id = "cal-hint";
      hint.setAttribute("aria-live", "polite");
      if (st.error) {
        hint.textContent = "Live availability could not be loaded.";
      } else if (!st.checkIn) {
        hint.textContent = "Select your check-in date.";
      } else if (!st.checkOut) {
        hint.textContent = "Check-in " + shortDate(st.checkIn) + " — choose your check-out date.";
      } else {
        var n = diffDays(st.checkIn, st.checkOut);
        hint.textContent = n + (n === 1 ? " night" : " nights") + ": " +
          shortDate(st.checkIn) + " → " + shortDate(st.checkOut) + ".";
      }
      row.appendChild(hint);
      if (st.checkIn) {
        var clear = document.createElement("button");
        clear.type = "button";
        clear.className = "cal-clear";
        clear.textContent = "Clear dates";
        clear.addEventListener("click", function () {
          st.checkIn = st.checkOut = null;
          st.onChange(current(), "user");
          render();
        });
        row.appendChild(clear);
      }
      return row;
    }

    function legendRow() {
      var row = document.createElement("div");
      row.className = "cal-legend";
      row.setAttribute("aria-hidden", "true");
      [
        ["cal-swatch cal-sw-free", "Available"],
        ["cal-swatch cal-sw-sold", "Sold out"],
        ["cal-swatch cal-sw-in", "Check-in"],
        ["cal-swatch cal-sw-out", "Check-out"],
        ["cal-swatch cal-sw-range", "Your stay"]
      ].forEach(function (it) {
        var item = document.createElement("span");
        item.className = "cal-legend-item";
        var sw = document.createElement("span");
        sw.className = it[0];
        var label = document.createElement("span");
        label.textContent = it[1];
        item.appendChild(sw); item.appendChild(label);
        row.appendChild(item);
      });
      return row;
    }

    /* ----------------------------- overlays --------------------------------- */
    function overlay(kind) {
      var layer = document.createElement("div");
      layer.className = "cal-overlay is-" + kind;
      layer.setAttribute("role", "status");
      if (kind === "busy") {
        layer.innerHTML = '<div class="spinner" aria-hidden="true"></div>' +
          '<p>Checking live availability…</p>';
      } else {
        var msg = document.createElement("p");
        msg.textContent = "We couldn't load live availability for this room type right now.";
        var retry = document.createElement("button");
        retry.type = "button";
        retry.className = "btn btn-outline btn-sm";
        retry.textContent = "Try again";
        retry.addEventListener("click", function () {
          if (st.onRetry) st.onRetry();
        });
        layer.appendChild(msg);
        layer.appendChild(retry);
      }
      return layer;
    }

    /* ------------------------- keyboard support ----------------------------- */
    function clampNav(iso) {
      if (cmpIso(iso, st.min) < 0) return st.min;
      if (cmpIso(iso, st.max) > 0) return st.max;
      return iso;
    }
    function weekEdge(iso, dir) {
      var dow = dowMon(iso);
      return clampNav(addDays(iso, dir > 0 ? (6 - dow) : -dow));
    }
    function monthJump(iso, dir) {
      var p = isoParts(iso);
      var y = p.y, m = p.m + dir;
      if (m < 1) { m = 12; y -= 1; }
      if (m > 12) { m = 1; y += 1; }
      var d = Math.min(p.d, daysInMonth(y, m));
      return clampNav(y + "-" + String(m).padStart(2, "0") + "-" + String(d).padStart(2, "0"));
    }

    function onKey(e) {
      if (st.busy || st.error) return;
      var btn = e.target && e.target.closest ? e.target.closest(".cal-day") : null;
      if (!btn || !root.contains(btn)) return;
      var iso = btn.dataset.date;
      if (!iso) return;
      var target = null;
      switch (e.key) {
        case "ArrowLeft": target = clampNav(addDays(iso, -1)); break;
        case "ArrowRight": target = clampNav(addDays(iso, 1)); break;
        case "ArrowUp": target = clampNav(addDays(iso, -7)); break;
        case "ArrowDown": target = clampNav(addDays(iso, 7)); break;
        case "Home": target = weekEdge(iso, -1); break;
        case "End": target = weekEdge(iso, 1); break;
        case "PageUp": target = monthJump(iso, -1); break;
        case "PageDown": target = monthJump(iso, 1); break;
        case "Enter":
        case " ":
          e.preventDefault();
          pick(iso);
          return;
        default: return;
      }
      if (target) {
        e.preventDefault();
        ensureVisible(target);
        st.focusDate = target;
        focusDate(target);
      }
    }

    function ensureVisible(iso) {
      var ym = ymOf(iso);
      var viewEnd = st.viewYm;
      for (var i = 1; i < st.months; i++) viewEnd = nextYm(viewEnd);
      if (ym < st.viewYm || ym > viewEnd) { st.viewYm = ym; render(); }
    }

    function focusDate(iso, focusOpts) {
      if (!iso) return;
      var btn = root.querySelector('[data-date="' + iso + '"]');
      if (btn) btn.focus(focusOpts || {});
    }

    function moveView(dir) {
      st.viewYm = dir > 0 ? nextYm(st.viewYm) : prevYm(st.viewYm);
      var lastViewYm = ymOf(st.max);
      if (st.months > 1) lastViewYm = prevYm(lastViewYm);
      if (st.viewYm < ymOf(st.min)) st.viewYm = ymOf(st.min);
      if (st.viewYm > lastViewYm) st.viewYm = lastViewYm;
      render();
    }

    /* ------------------------------ public API ------------------------------ */
    root.addEventListener("keydown", onKey);
    render();

    return {
      /* Swap the sold-out lookup (e.g. the room type changed). Revalidates
         the current selection, clears what is no longer possible and
         reports the reason through onChange. */
      setUnavailable: function (fn, scopeLabel) {
        st.unavailable = typeof fn === "function" ? fn : function () { return false; };
        st.unavailableScope = scopeLabel || "";
        var reason = revalidate(false);
        render();
        return reason;
      },
      setBounds: function (bounds) {
        if (!bounds) return;
        if (isoParts(bounds.min)) st.min = bounds.min;
        if (isoParts(bounds.max)) st.max = bounds.max;
        revalidate(false);
        render();
      },
      setBusy: function (busy) { st.busy = !!busy; render(); },
      setError: function (error) { st.error = !!error; render(); },
      setSelection: function (checkIn, checkOut, silent) {
        st.checkIn = (isoParts(checkIn) && cmpIso(checkIn, st.min) >= 0 && cmpIso(checkIn, st.max) <= 0) ? checkIn : null;
        st.checkOut = (isoParts(checkOut) && st.checkIn && cmpIso(checkOut, st.checkIn) > 0 && cmpIso(checkOut, st.max) <= 0) ? checkOut : null;
        if (st.checkIn) st.viewYm = ymOf(st.checkIn);
        revalidate(silent === true);
        render();
      },
      getSelection: current,
      focus: function () {
        var btn = root.querySelector('.cal-day[tabindex="0"]') ||
                  root.querySelector(".cal-day:not(.is-disabled)");
        if (btn) btn.focus();
      },
      destroy: function () { root.remove(); }
    };
  }

  window.JONE = window.JONE || {};
  window.JONE.stayCalendar = { mount: mount };
})();
