/* js/utils.js */
/* ==========================================================================
   shared utilities — formatting, dates, DOM helpers, events, debounce.
   ========================================================================== */

const JONE = (() => {
  "use strict";

  /* ----------------------------- Currency ---------------------------------- */
  /* ONE currency formatter for the whole application. Every financial table,
     KPI card, modal, receipt and export renders money through this function —
     which is why "no thousands separators" was a single-point defect and is
     fixed in a single place.

     Rules enforced here:
     * thousands separators and the currency's decimal precision, always
       (`₦165,000.00`, not `₦165000` and not `₦165,000`);
     * the value is formatted as a NUMBER — callers pass numeric fields, never
       pre-built strings containing a currency symbol;
     * negative amounts, zero and reversals keep their sign;
     * `null`/`undefined`/non-numeric render an explicit placeholder instead of
       a misleading `₦0.00`;
     * the underlying amount is never altered, rounded for storage, or coerced to
       a different precision than the caller asked for.

     Options: `{ currency, decimals, symbol, compact }`.
     `compact:true` drops the decimal places (used for badges such as
     "₦5,000 OFF" where trailing zeros are noise), never for money columns. */
  const CURRENCY_SYMBOLS = { NGN: "\u20a6", USD: "$", GBP: "\u00a3", EUR: "\u20ac" };
  const CURRENCY_DECIMALS = { NGN: 2, USD: 2, GBP: 2, EUR: 2 };
  const CURRENCY_LOCALES = { NGN: "en-NG", USD: "en-US", GBP: "en-GB", EUR: "en-IE" };

  function formatMoney(amount, opts = {}) {
    if (amount === null || amount === undefined || amount === "") return "--";
    const n = typeof amount === "number" ? amount : Number(String(amount).trim());
    if (!Number.isFinite(n)) return "--";
    const currency = String(opts.currency || "NGN").toUpperCase();
    const decimals = opts.compact
      ? 0
      : (Number.isInteger(opts.decimals) ? opts.decimals : (CURRENCY_DECIMALS[currency] ?? 2));
    const symbol = opts.symbol != null ? opts.symbol : (CURRENCY_SYMBOLS[currency] || `${currency} `);
    // en-* grouping is the standard 3-digit separator the hotel's invoices use.
    const body = Math.abs(n).toLocaleString(CURRENCY_LOCALES[currency] || "en-NG", {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals,
    });
    return `${n < 0 ? "\u2212" : ""}${symbol}${body}`;
  }

  /* Back-compat alias: every existing call site keeps working and now inherits
     the separator/precision rules above. */
  function formatNaira(amount, opts = {}) {
    return formatMoney(amount, { currency: "NGN", ...(opts || {}) });
  }

  /* --------------------------- Quantities ---------------------------------- */
  /* One quantity formatter for the whole application.

     The defect this replaces: every stock table printed the raw four-decimal
     column value, so a delivery of twenty whole bottles read "+20.0000".

     The rule is driven by the PRODUCT'S OWN UNIT, never by blanket rounding:
     * whole-count units (bottle, pack, piece, crate, plate, ...) show no
       decimals at all — `20`, not `20.0000`;
     * measured units keep the configured precision, and trailing zeros are
       PRESERVED there — `1.250 kg` must never collapse to `1.25 kg`, let alone
       `1 kg`;
     * an unknown unit is treated as measured-but-trimmed, so nothing is
       silently rounded down;
     * the underlying value is never modified — this is presentation only.

     Options: `{ unit, precision, signed, showUnit, sign }`. */
  const WHOLE_COUNT_UNITS = new Set([
    "PIECE", "PIECES", "PCS", "PC", "UNIT", "UNITS", "EA", "EACH", "ITEM", "ITEMS",
    "BOTTLE", "BOTTLES", "PACK", "PACKS", "PACKET", "PACKETS", "CRATE", "CRATES",
    "CAN", "CANS", "TIN", "TINS", "BOX", "BOXES", "CARTON", "CARTONS", "BAG", "BAGS",
    "SACHET", "SACHETS", "PLATE", "PLATES", "PORTION", "PORTIONS", "SERVING", "SERVINGS",
    "ROLL", "ROLLS", "TUBE", "TUBES", "BAR", "BARS", "DOZEN", "DOZENS", "CUP", "CUPS",
    "GLASS", "GLASSES", "SHOT", "SHOTS", "TABLESPOON", "TABLESPOONS", "TEASPOON", "TEASPOONS",
  ]);
  /* Measured units keep fixed precision so `2.500 L` reads as a measured amount. */
  const MEASURED_UNIT_PRECISION = {
    L: 3, LITRE: 3, LITRES: 3, LITER: 3, LITERS: 3, LTR: 3,
    KG: 3, KILOGRAM: 3, KILOGRAMS: 3, KILO: 3, KILOS: 3,
    G: 1, GRAM: 1, GRAMS: 1,
    ML: 1, MILLILITRE: 1, MILLILITRES: 1, MILLILITER: 1, MILLILITERS: 1, CL: 1,
    M: 2, METRE: 2, METRES: 2, METER: 2, METERS: 2,
    TBSP: 1, TSP: 1,
  };
  const MAX_QUANTITY_DECIMALS = 4;

  function unitKey(unit) {
    return String(unit == null ? "" : unit).trim().toUpperCase().replace(/\.$/, "");
  }

  /* Which unit labels are displayed with a space ("2.500 L") vs. tight ("12 pcs"). */
  function unitSpacing(unit) {
    const key = unitKey(unit);
    return /^[A-Z]{1,4}$/.test(key) ? " " : " ";
  }

  function quantityPrecision(unit, explicit) {
    if (Number.isInteger(explicit)) return Math.max(0, Math.min(explicit, MAX_QUANTITY_DECIMALS));
    const key = unitKey(unit);
    if (WHOLE_COUNT_UNITS.has(key)) return 0;
    if (Object.prototype.hasOwnProperty.call(MEASURED_UNIT_PRECISION, key)) {
      return MEASURED_UNIT_PRECISION[key];
    }
    return null;   // unknown unit: decide from the value itself
  }

  function formatQuantity(value, opts = {}) {
    if (value === null || value === undefined || value === "") return "--";
    const raw = typeof value === "number" ? String(value) : String(value).trim();
    const n = Number(raw);
    if (!Number.isFinite(n)) return "--";
    const unit = opts.unit || "";
    let decimals = quantityPrecision(unit, opts.precision);
    if (decimals === null || !Number.isInteger(opts.precision)) {
      // Display rule (staff request): quantities read the way people count —
      // `20`, never `20.0000`. Keep every significant decimal the value
      // actually carries (`1.25`, `0.5`) but drop trailing zeros for every
      // unit, whole-count or measured. An explicit `precision` option still
      // wins for callers that must show a fixed number of places.
      const fraction = raw.split(".")[1] || "";
      const trimmed = fraction.replace(/0+$/, "");
      decimals = Math.min(trimmed.length, MAX_QUANTITY_DECIMALS);
    }
    const body = Math.abs(n).toLocaleString("en-NG", {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals,
    });
    const negative = n < 0;
    // Stock movements show direction explicitly: +20 received, −4 issued.
    const showSign = opts.signed != null ? opts.signed : !!opts.sign;
    const prefix = negative ? "\u2212" : (showSign && n > 0 ? "+" : "");
    const label = opts.showUnit === false || !unit ? "" : unitSpacing(unit) + unit;
    return `${prefix}${body}${label}`;
  }

  /* Plain trimmed quantity text for form inputs: `20`, never `20.0000`.
     String-based so no significant decimal is ever lost to float rounding. */
  function trimQuantityValue(value) {
    if (value === null || value === undefined || value === "") return "";
    const raw = String(value).trim();
    if (!/^-?\d+(\.\d+)?$/.test(raw)) return raw;
    const trimmed = raw.replace(/\.?0+$/, "");
    return trimmed === "-0" ? "0" : trimmed;
  }

  /* Convenience wrapper for a stock API row ({ quantity_on_hand, base_unit }). */
  function formatStockQuantity(value, item, opts = {}) {
    const unit = (item && (item.base_unit || item.unit)) || opts.unit || "";
    return formatQuantity(value, { ...opts, unit });
  }

  /* Presentational precision for a stock item, so a table cell and its
     tooltip/detail view can never disagree about the same number. */
  function quantityPrecisionFor(unit) {
    return quantityPrecision(unit);
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
    formatNaira, formatMoney, CURRENCY_SYMBOLS,
    formatQuantity, formatStockQuantity, trimQuantityValue, quantityPrecisionFor, WHOLE_COUNT_UNITS,
    formatDate, formatDateTime, offerPeriod, offerStatusNote,
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
