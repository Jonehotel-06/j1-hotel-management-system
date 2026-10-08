/* js/utils.js */
/* ==========================================================================
   shared utilities — formatting, dates, DOM helpers, events, debounce.
   ========================================================================== */

const JONE = (() => {
  "use strict";

  /* ----------------------------- Currency ---------------------------------- */
  function formatNaira(amount, opts = {}) {
    if (amount == null || isNaN(amount)) return "--";
    const n = Number(amount);
    const abs = Math.abs(n);
    const s = abs.toLocaleString("en-NG", { maximumFractionDigits: 0 });
    return `${n < 0 ? "-" : ""}₦${s}`;
  }

  /* ------------------------------- Dates ----------------------------------- */
  // Format ISO date (YYYY-MM-DD) into friendly text. e.g. "10 September 2026"
  function parseISO(str) {
    if (!str || typeof str !== "string") return null;
    const m = str.match(/(\d{4})-(\d{2})-(\d{2})/);
    if (!m) return null;
    const d = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3]));
    return Number.isNaN(d.getTime()) ? null : d;
  }

  function formatDate(str, mode = "long") {
    const d = typeof str === "string" ? parseISO(str) : str;
    if (!d) return "--";
    const long = { day: "numeric", month: "long", year: "numeric" };
    const mid = { day: "numeric", month: "short", year: "numeric" };
    const short = { day: "2-digit", month: "2-digit", year: "numeric" };
    const opts = mode === "short" ? short : mode === "mid" ? mid : long;
    return new Intl.DateTimeFormat("en-GB", opts).format(d);
  }


  /* Offer validity window, rendered the same way everywhere (public pages and
     the staff console) so an offer's START date is never hidden from a guest
     deciding whether a deal applies to their stay. */
  function offerPeriod(offer, mode = "mid") {
    if (!offer) return "";
    const start = offer.start_date ? formatDate(offer.start_date, mode) : "";
    const end = offer.end_date ? formatDate(offer.end_date, mode) : "";
    if (start && end) return start + " \u2013 " + end;
    if (start) return "From " + start;
    if (end) return "Until " + end;
    return "";
  }

  /* "Starts in 3 days" / "Ends today" style status for an offer, derived from
     the offer's own dates. Returns "" when there is nothing useful to say. */
  function offerStatusNote(offer, today = null) {
    if (!offer || !offer.start_date) return "";
    const ref = parseISO(today || hotelTodayISO());
    const start = parseISO(offer.start_date);
    const end = offer.end_date ? parseISO(offer.end_date) : null;
    if (!ref || !start) return "";
    const day = 86400000;
    const toStart = Math.round((start - ref) / day);
    if (toStart > 0) return toStart === 1 ? "Starts tomorrow" : "Starts in " + toStart + " days";
    if (end) {
      const toEnd = Math.round((end - ref) / day);
      if (toEnd < 0) return "Ended";
      if (toEnd === 0) return "Ends today";
      if (toEnd === 1) return "Ends tomorrow";
      if (toEnd <= 7) return "Ends in " + toEnd + " days";
    }
    return toStart === 0 ? "Starts today" : "";
  }

  /* Discount as a short badge label: "20% OFF" / "₦5,000 OFF".
     Trailing zeros are dropped — the API sends "20.00". */
  function offerDiscountLabel(offer) {
    if (!offer) return "";
    const value = Number(offer.discount_value);
    if (!Number.isFinite(value) || value <= 0) return "";
    if (offer.discount_type === "FIXED_AMOUNT") return formatNaira(value) + " OFF";
    const pct = Number.isInteger(value) ? String(value) : String(parseFloat(value.toFixed(2)));
    return pct + "% OFF";
  }

  /* The offer's eligibility rules as short display lines, so a guest can tell
     whether a deal applies BEFORE reaching the quote step. */
  function offerCriteria(offer) {
    if (!offer) return [];
    const lines = [];
    const min = Number(offer.min_nights) || 0;
    const max = Number(offer.max_nights) || 0;
    if (min > 1) lines.push("Minimum stay: " + min + " nights");
    else if (min === 1) lines.push("Minimum stay: 1 night");
    if (max > 0) lines.push("Maximum stay: " + max + (max === 1 ? " night" : " nights"));
    const types = offer.applicable_room_types || [];
    lines.push(types.length
      ? "Applies to: " + types.map(function (t) { return t.name; }).join(", ")
      : "Applies to all room types");
    return lines;
  }

  function formatDateTime(str) {
    if (!str) return "--";
    // Full ISO datetimes must keep their time component (parseISO reads the
    // date only, which silently rendered every timestamp as midnight).
    let d = null;
    if (typeof str === "string" && str.indexOf("T") !== -1) {
      d = new Date(str);                      // Safari parses ISO 8601 w/ offset
      if (Number.isNaN(d.getTime())) d = null;
    }
    if (!d) d = parseISO(str);
    if (!d) return "--";
    return new Intl.DateTimeFormat("en-GB", {
      day: "2-digit", month: "short", year: "numeric",
      hour: "2-digit", minute: "2-digit"
    }).format(d);
  }

  // Local date string YYYY-MM-DD for input[type=date]
  function todayISO(offsetDays = 0) {
    const d = new Date();
    d.setDate(d.getDate() + offsetDays);
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    return `${y}-${m}-${day}`;
  }

  // "Today" in the HOTEL's timezone (exposed by /api/hotel/ as settings.timezone).
  // The booking API judges dates by the hotel's business day; a browser in a
  // different timezone would otherwise offer check-in dates the API rejects.
  function hotelTodayISO(offsetDays = 0) {
    const tz = (window.JONE && JONE.hotelTimezone) || null;
    let iso;
    if (tz) {
      try {
        iso = new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date());
      } catch (_) { iso = null; }
    }
    if (!iso) return todayISO(offsetDays);
    if (!offsetDays) return iso;
    const [y, m, d] = iso.split("-").map(Number);
    const dt = new Date(Date.UTC(y, m - 1, d));
    dt.setUTCDate(dt.getUTCDate() + offsetDays);
    return dt.toISOString().slice(0, 10);
  }

  function nightsBetween(checkIn, checkOut) {
    const a = parseISO(checkIn), b = parseISO(checkOut);
    if (!a || !b || b <= a) return 0;
    return Math.round((b - a) / 86400000);
  }

  /* ----------------------------- DOM helpers ------------------------------- */
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") node.className = v;
      else if (k === "dataset") Object.assign(node.dataset, v);
      else if (k === "innerHTML" || k === "textContent") node[k] = v;   // content properties, not attributes
      else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
      else if (v === true) node.setAttribute(k, "");
      else if (v != null && v !== false) node.setAttribute(k, v);
    }
    for (const c of children.flat()) {
      if (c == null) continue;
      node.appendChild(c instanceof Node ? c : document.createTextNode(c));
    }
    return node;
  }

  // Escape HTML to prevent injection when rendering API/user content.
  function esc(str) {
    if (str == null) return "";
    return String(str)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  /* ------------------------- Event handling helpers ------------------------ */
  function debounce(fn, wait = 300) {
    let t;
    return function (...args) {
      clearTimeout(t);
      t = setTimeout(() => fn.apply(this, args), wait);
    };
  }

  function throttle(fn, limit = 300) {
    let last;
    return function (...args) {
      const now = Date.now();
      if (last && now < last + limit) return;
      last = now;
      fn.apply(this, args);
    };
  }

  /* ------------------------------ Storage ---------------------------------- */
  const storage = {
    get(key, fallback = null) {
      try {
        const raw = localStorage.getItem(key);
        return raw == null ? fallback : JSON.parse(raw);
      } catch (_) { return fallback; }
    },
    set(key, value) {
      try { localStorage.setItem(key, JSON.stringify(value)); } catch (_) {}
    },
    remove(key) { try { localStorage.removeItem(key); } catch (_) {} }
  };

  /* -------------------------- Data attribute binding ----------------------- */
  // Set text/innerHTML of elements matching [data-bind] or [data-html].
  function bindData(mapping) {
    for (const [key, value] of Object.entries(mapping)) {
      const $text = $(`[data-bind="${key}"]`);
      if ($text) $text.textContent = value;
      const $html = $(`[data-html="${key}"]`);
      if ($html) $html.innerHTML = esc(value);
    }
  }

  /* ------------------------------ List / grid ------------------------------ */
  function paginate(items, page, perPage) {
    const total = items.length;
    const totalPages = Math.max(1, Math.ceil(total / perPage));
    const p = Math.min(Math.max(1, page || 1), totalPages);
    const start = (p - 1) * perPage;
    return { page: p, totalPages, total, items: items.slice(start, start + perPage), start };
  }

  /* ------------------------------- Misc ------------------------------------ */
  function initials(name) {
    return String(name || "?").trim().split(/\s+/).map(w => w[0]).slice(0, 2).join("").toUpperCase();
  }

  function scrollTop() {
    window.scrollTo({ top: 0, behavior: "auto" });
  }

  /* ------------------------ Reusable action guard -------------------------- */
  // prevent double submit on buttons marked [data-submit-guard]
  function guardSubmit(btn) {
    if (btn.dataset.busy === "true") return false;
    btn.dataset.busy = "true";
    btn.setAttribute("aria-busy", "true");
    btn.classList.add("is-loading");
    return true;
  }
  function releaseGuard(btn) {
    btn.dataset.busy = "false";
    btn.removeAttribute("aria-busy");
    btn.classList.remove("is-loading");
  }

  /* Generic check-in / check-out date constraints.
     Recognizes ANY start/end date pair by naming convention instead of a
     hardcoded id map (the old version missed av-from/av-to on the staff
     availability page, letting past dates through to the API):
       start:  *check_in*, *checkin*, *-in, *-from, *_from
       end:    *check_out*, *checkout*, *-out, *-to, *_to
     Pairs are matched by shared prefix (hp-in/hp-out, av-from/av-to,
     b-checkin/b-checkout, r-in/r-out, ...). Constraints re-apply when the
     hotel timezone arrives so "today" is always hotel-local. */
  function dateKey(input) { return String(input.name || input.id || "").toLowerCase(); }
  function startPrefix(k) {
    let m = k.match(/^(.*?)(?:[-_]?check[-_]?in|[-_]in|[-_]from)$/);
    return m ? m[1] : null;
  }
  function endPrefix(k) {
    let m = k.match(/^(.*?)(?:[-_]?check[-_]?out|[-_]out|[-_]to)$/);
    return m ? m[1] : null;
  }
  function setupDateConstraints(root) {
    const scope = root || document;
    const today = hotelTodayISO(0);
    // Inputs marked data-allow-past (e.g. report date ranges) are exempt.
    const inputs = Array.prototype.slice.call(scope.querySelectorAll('input[type="date"]'))
      .filter((i) => !i.hasAttribute("data-allow-past"));
    const ends = {};
    inputs.forEach((input) => {
      const p = endPrefix(dateKey(input));
      if (p != null) { ends[p] = input; if (!input.min) input.min = hotelTodayISO(1); }
    });
    inputs.forEach((input) => {
      const p = startPrefix(dateKey(input));
      if (p == null) return;
      input.min = today;
      const checkout = ends[p] || null;
      const updateCheckout = () => {
        if (!checkout) return;
        checkout.min = input.value
          ? new Date(new Date(input.value + "T00:00:00").getTime() + 86400000).toISOString().slice(0, 10)
          : hotelTodayISO(1);
        if (checkout.value && checkout.value < checkout.min) checkout.value = "";
      };
      if (input.dataset.jonePaired !== "1") {
        input.dataset.jonePaired = "1";
        input.addEventListener("change", updateCheckout);
      }
      updateCheckout();
    });
  }
  setupDateConstraints();
  // Re-apply once the authoritative hotel timezone is known.
  document.addEventListener("jone:hotel", () => setupDateConstraints());

  /* --------------------- Backend-generated deep links ---------------------- */
  /* The backend stores notification links ROOT-relative, e.g.
     "/dashboard/booking-details.html?ref=J1-…" or "/my-bookings.html?ref=…".
     Naively stripping the leading slash (or leaving it relative) breaks them
     from inside /dashboard/ — "dashboard/booking-details.html" would resolve
     to /dashboard/dashboard/booking-details.html. Resolve against the REAL
     frontend root instead, which is one level up inside /dashboard/. */
  function appUrl(link) {
    if (!link) return "";
    const s = String(link).trim();
    if (!s) return "";
    if (/^(https?:)?\/\//i.test(s) || /^(mailto:|tel:|#)/i.test(s)) return s;
    const absolute = s.charAt(0) === "/" ? s : "/" + s;
    let base = (location.pathname || "/").replace(/[^/]*$/, "");   // current dir
    if (/\/dashboard\/$/.test(base)) base = base.replace(/dashboard\/$/, "");
    return base + absolute.slice(1);
  }

  return {
    formatNaira, formatDate, formatDateTime, offerPeriod, offerStatusNote,
    offerCriteria, offerDiscountLabel, parseISO, hotelTodayISO, todayISO, nightsBetween, setupDateConstraints,
    $, $$, el, esc, debounce, throttle, storage, bindData, paginate, initials,
    scrollTop, guardSubmit, releaseGuard, appUrl
  };
})();

window.JONE = JONE;
// Expose app config on JONE (config.js sets window.APP_CONFIG) so page code can
// read JONE.APP_CONFIG consistently. See js/config.js.
JONE.APP_CONFIG = (typeof window !== "undefined" && window.APP_CONFIG) || {};
window.JONE = JONE;
