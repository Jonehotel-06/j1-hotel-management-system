/* js/api.js */
/* ==========================================================================
   Centralized API layer.
   All HTTP to the Django REST backend flows through here. Pages never build
   their own fetch(). The backend remains the single source of truth.

   Endpoint paths below are taken VERBATIM from the verified backend contract
   (backend/docs/FRONTEND_CONTRACT.md + live API inspection). Every response
   uses the envelope { success, message, data, pagination? } — errors use
   { success: false, code, message, errors? }. Money values are STRINGS
   ("25000.00") and are formatted for display only, never recomputed.
   ========================================================================== */

const API = (() => {
  "use strict";

  const BASE = (window.APP_CONFIG && window.APP_CONFIG.API_BASE_URL || "").replace(/\/$/, "");
  const DEFAULT_TIMEOUT = 20000;

  const AUTH_KEY = (window.APP_CONFIG && window.APP_CONFIG.STORAGE.AUTH) || "jone.auth";
  const SESSION_KEY = (window.APP_CONFIG && window.APP_CONFIG.STORAGE.SESSION) || "jone.session";
  const TERMINAL_KEY = (window.APP_CONFIG && window.APP_CONFIG.STORAGE.TERMINAL) || "jone.terminal.reference";

  /* Envelope messages that carry no information ("Validation failed."): when one
     of those arrives, the per-field errors underneath it are the useful part. */
  const GENERIC_ENVELOPE_MESSAGE = /^(validation failed[.]?|validation error[.]?)$/i;

  const TOKEN_RE = /token|jwt|authorization|bearer/i;

  function newRequestId() {
    try { if (window.crypto && typeof window.crypto.randomUUID === "function") return window.crypto.randomUUID(); } catch (_) {}
    return "req-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 14);
  }

  function getWorkstationReference() {
    try { return String(localStorage.getItem(TERMINAL_KEY) || "").trim().slice(0, 48); } catch (_) { return ""; }
  }

  function setWorkstationReference(value) {
    const reference = String(value || "").trim().slice(0, 48);
    try {
      if (reference) localStorage.setItem(TERMINAL_KEY, reference);
      else localStorage.removeItem(TERMINAL_KEY);
    } catch (_) {}
    return reference;
  }

  /* Grab the stored access token (kept outside localStorage entirely if possible). */
  function getToken() {
    try {
      const raw = sessionStorage.getItem(SESSION_KEY);
      if (raw) {
        const parsed = JSON.parse(raw);
        return parsed.access || parsed.token || null;
      }
    } catch (_) {}
    return null;
  }

  /* Refresh-before-use: hook for auth.js to wire re-auth. */
  let tokenProvider = getToken;
  function setTokenProvider(fn) { tokenProvider = fn; }

  /* One-time refresh hook (wired by auth.js). Returns true if a new access
     token was obtained. Set to null to disable refresh (HttpOnly cookies). */
  let refreshProvider = null;
  function setRefreshProvider(fn) { refreshProvider = fn || null; }

  /* ------------------------------ Errors ----------------------------------- */
  /* APIError carries everything callers need to react PRECISELY:
       status — HTTP status code (0 when no response arrived)
       kind   — machine-readable failure class:
                  "http"     a real HTTP error response (400/401/403/409/429/500/…)
                  "timeout"  the request exceeded its timeout and was aborted
                  "abort"    aborted by the CALLER (its own AbortSignal)
                  "offline"  the browser reports no network connection
                  "network"  request never reached the server / response lost
                             (DNS failure, connection refused, CORS block, …)
                  "parse"    the server answered but the body was malformed
       code   — backend contract code (e.g. ROOM_UNAVAILABLE) when present.
     `kind` is what lets the booking flow say "your request may have succeeded,
     press retry" (network/timeout) vs "the room is gone" (http 409). */
  class APIError extends Error {
    constructor(status, message, data, kind = "http") {
      super(message);
      this.name = "APIError";
      this.status = status;
      this.data = data;
      this.kind = kind;
      // Contract error code (e.g. ROOM_UNAVAILABLE) when present.
      this.code = (data && data.code) || null;
    }
  }

  function firstErrorString(node) {
    if (!node) return "";
    if (typeof node === "string") return node.trim();
    if (Array.isArray(node)) {
      for (let i = 0; i < node.length; i++) {
        const found = firstErrorString(node[i]);
        if (found) return found;
      }
      return "";
    }
    if (typeof node === "object") {
      const vals = Object.values(node);
      for (let i = 0; i < vals.length; i++) {
        const found = firstErrorString(vals[i]);
        if (found) return found;
      }
    }
    return "";
  }

  // Map HTTP status codes to human-friendly, non-technical messages.
  // Backend-provided messages (envelope `message`) win when they are
  // user-safe; technical/token messages are replaced.
  function friendlyMessage(status, data) {
    const dmsg = data && (data.message || data.detail || data.error);
    // Field-level errors are more useful than the generic envelope message
    // ("Validation failed.") — surface the first one when present.
    //
    // But they must never displace a message the API authored for the user: a
    // refusal such as STAFF_SIGN_IN_RESTRICTED also carries machine-readable
    // context in `errors` (the address the request came from), and a bare
    // "102.89.34.7" is not a sentence a staff member can act on. So the field
    // error only wins while the envelope message is the generic one.
    const errs = data && data.errors;
    const genericEnvelopeMessage = !dmsg || GENERIC_ENVELOPE_MESSAGE.test(String(dmsg).trim());
    if (errs && typeof errs === "object" && genericEnvelopeMessage) {
      const first = firstErrorString(errs);
      if (first && !TOKEN_RE.test(first)) return first;
    }
    if (dmsg && typeof dmsg === "string" && !TOKEN_RE.test(dmsg)) return dmsg;
    switch (status) {
      case 400: {
        // Field-level errors from the contract: { errors: { field: [msgs] } }
        const errs = data && data.errors;
        if (errs && typeof errs === "object") {
          const first = firstErrorString(errs);
          if (first) return first;
        }
        return "We couldn't process that request. Please check your details and try again.";
      }
      case 401: return "Your session has expired. Please sign in again.";
      case 403: return "You don't have permission to perform this action.";
      case 404: return "The requested item could not be found.";
      case 409: return "The selected room is no longer available. Please choose another room.";
      case 422: return "Some of the information provided is invalid. Please review and try again.";
      case 429: return "Too many requests. Please wait a moment and try again.";
      case 500: return "We're having trouble completing this request. Please try again.";
      case 502:
      case 503:
      case 504: return "Our services are temporarily unavailable. Please try again shortly.";
      default: return "Something went wrong. Please try again.";
    }
  }

  /* --------------------------- Diagnostics (dev only) -----------------------
     Local development origins get one concise, secret-free console line per
     failed request: method, path (never the query's tokens), status/kind and
     a bounded body excerpt. Authorization headers/tokens are NEVER logged,
     and production origins log nothing. */
  const IS_DEV_HOST = ["localhost", "127.0.0.1", "[::1]", ""].indexOf(
    (typeof location !== "undefined" && location.hostname) || ""
  ) !== -1 || /\.e2b\.app$/.test(typeof location !== "undefined" ? location.hostname : "");

  function debugLog(method, url, detail) {
    if (!IS_DEV_HOST || !window.console) return;
    try {
      const pathOnly = String(url || "").replace(/\?.*$/, "");
      const body = detail.bodyExcerpt === undefined ? "" : " body=" + String(detail.bodyExcerpt).slice(0, 300);
      console.warn(
        `[J-ONE API] ${method} ${pathOnly} -> ${detail.label}${body}`,
        detail.errorName || ""
      );
    } catch (_) { /* diagnostics must never throw */ }
  }

  /* ---------------------- Endpoint validation (safe-by-default) ------------ */
  /* A path must be a real, non-empty string. Historically a missing
     API_ENDPOINTS key produced a request to ".../undefined", which the backend
     answered with a 404 and the page rendered a permanently blank panel (D1).
     Building such a URL is now impossible: every request fails fast, in the
     shared layer, with an actionable configuration error. */
  const BAD_PATH_SEGMENT = /(^|\/)(undefined|null|NaN)(\/|$)/;

  function endpointConfigError(path) {
    return new APIError(
      0,
      "This part of the dashboard isn't configured correctly, so the request was " +
        "not sent. Please report it to the administrator (missing endpoint configuration).",
      { code: "ENDPOINT_NOT_CONFIGURED", path: path === undefined ? null : String(path) }
    );
  }

  function assertUsablePath(path) {
    if (typeof path !== "string" || !path.trim()) throw endpointConfigError(path);
    const trimmed = path.trim();
    if (BAD_PATH_SEGMENT.test(trimmed)) throw endpointConfigError(trimmed);
    return trimmed;
  }

  /* ----------------------------- Core request (single pass) ---------------- */
  async function requestOnce(path, opts = {}) {
    path = assertUsablePath(path);
    const {
      method = "GET",
      body,
      params,
      headers = {},
      auth = true,
      timeout = DEFAULT_TIMEOUT,
      cache,
      credentials,
      responseType = "json",
      signal
    } = opts;

    let url = BASE + path;
    if (params) {
      const qs = new URLSearchParams();
      for (const [k, v] of Object.entries(params)) {
        if (v != null && v !== "") qs.append(k, v);
      }
      const q = qs.toString();
      if (q) url += (url.includes("?") ? "&" : "?") + q;
    }

    const out = { method, headers: { Accept: "application/json", ...headers } };
    if (cache) out.cache = cache;
    if (credentials) out.credentials = credentials;
    if (body !== undefined) {
      out.body = body instanceof FormData ? body : JSON.stringify(body);
      if (!(body instanceof FormData)) out.headers["Content-Type"] = "application/json";
    }

    const token = tokenProvider();
    if (auth && token) {
      out.headers.Authorization = `Bearer ${token}`;
      out.headers["X-Request-ID"] = out.headers["X-Request-ID"] || newRequestId();
      const terminalReference = getWorkstationReference();
      if (terminalReference) out.headers["X-JONE-Terminal"] = terminalReference;
    }

    const ctrl = new AbortController();
    // Distinguishes OUR timeout abort from a CALLER-initiated abort so the
    // error surfaced to the UI is honest ("timed out" vs "cancelled").
    let timedOut = false;
    const timer = setTimeout(() => { timedOut = true; ctrl.abort(); }, timeout);
    let callerAborted = false;
    const onAbort = () => { callerAborted = true; ctrl.abort(); };
    if (signal) {
      if (signal.aborted) callerAborted = true;
      signal.addEventListener("abort", onAbort);
    }

    try {
      const res = await fetch(url, { ...out, signal: ctrl.signal });
      clearTimeout(timer);
      if (signal) signal.removeEventListener("abort", onAbort);

      // 204 / empty
      if (res.status === 204) return {
        status: 204,
        ok: true,
        data: null,
        pagination: null,
        response: res,
        headers: res.headers
      };

      const contentType = res.headers.get("content-type") || "";
      if (responseType === "blob" && res.ok) {
        /* Reject a "successful" response whose body is not actually binary.
           A JSON error envelope (or an HTML error page) served with HTTP 200
           must never be written to disk as a .xlsx/.pdf file. */
        if (contentType.includes("application/json") || contentType.includes("text/html")) {
          const raw = await res.text().catch(() => "");
          let detail = null;
          try { detail = raw ? JSON.parse(raw) : null; } catch (_) { detail = null; }
          debugLog(method, url, { label: `blob request returned ${contentType}, not a file`, bodyExcerpt: raw });
          throw new APIError(
            res.status,
            (detail && (detail.message || detail.detail)) ||
              "The server did not return a downloadable file. Please try again.",
            detail,
            "parse"
          );
        }
        const blob = await res.blob();
        // `size === 0` is a genuinely empty body. A non-Blob/stream stub has no
        // `size` at all and must not be rejected here.
        if (!blob || blob.size === 0) {
          debugLog(method, url, { label: "empty binary response body" });
          throw new APIError(
            res.status,
            "The server returned an empty file. Please try a different period or try again.",
            null,
            "parse"
          );
        }
        return {
          status: res.status,
          ok: true,
          data: blob,
          pagination: null,
          response: res,
          headers: res.headers
        };
      }
      let data = null;
      let parseFailed = false;
      if (contentType.includes("application/json")) {
        try {
          data = await res.json();
        } catch (_) {
          parseFailed = true; // server answered, body was not valid JSON
        }
      } else {
        data = await res.text().catch(() => null);
      }
      if (parseFailed || (res.ok && contentType.includes("application/json") && data == null)) {
        debugLog(method, url, { label: `parse error (HTTP ${res.status})`, bodyExcerpt: "" });
        throw new APIError(
          res.status,
          "The server sent a response we couldn't read. Please try again.",
          null,
          "parse"
        );
      }

      if (!res.ok) {
        // Error path keeps the FULL envelope (it carries {errors} for forms).
        debugLog(method, url, {
          label: `HTTP ${res.status}`,
          bodyExcerpt: data && typeof data === "object" ? JSON.stringify(data) : data,
        });
        throw new APIError(res.status, friendlyMessage(res.status, data), data);
      }

      /* The backend wraps every response as {success, message?, data, pagination?}.
         Unwrap it ONCE here so every caller receives the actual payload at
         res.data (and pagination at res.pagination) — the documented contract
         of normalizeList(res.data, res.pagination). */
      let pagination = (data && typeof data === "object" && !Array.isArray(data) && data.pagination) || null;
      if (data && typeof data === "object" && !Array.isArray(data) && data.success !== undefined && "data" in data) {
        data = data.data !== undefined ? data.data : null;
      }

      return {
        status: res.status,
        ok: true,
        data,
        pagination,
        response: res,
        headers: res.headers
      };
    } catch (err) {
      clearTimeout(timer);
      if (signal) signal.removeEventListener("abort", onAbort);
      if (err.name === "AbortError" || err.name === "TimeoutError") {
        if (callerAborted) {
          debugLog(method, url, { label: "aborted by caller", errorName: err.name });
          throw new APIError(0, "The request was cancelled.", null, "abort");
        }
        // OUR timeout. The request MAY have been processed by the server —
        // callers with idempotent retries (booking creation) can safely retry.
        debugLog(method, url, {
          label: `timeout after ${timeout}ms (server may have processed the request)`,
          errorName: err.name,
        });
        throw new APIError(
          0,
          "The request is taking longer than expected. It may still complete — please retry in a moment.",
          null,
          "timeout"
        );
      }
      if (err instanceof APIError) throw err;
      // fetch() rejects on DNS failure, refused connections and CORS blocks:
      // the request never completed, so its outcome is genuinely unknown.
      debugLog(method, url, {
        label: "network failure (no response received)",
        errorName: err && err.name,
        bodyExcerpt: err && err.message ? String(err.message).slice(0, 120) : "",
      });
      if (typeof navigator !== "undefined" && navigator.onLine === false) {
        throw new APIError(0, "You appear to be offline. An internet connection is required — please reconnect and try again.", null, "offline");
      }
      throw new APIError(
        0,
        "We couldn't reach our servers. Your connection or our service may be unavailable — please try again.",
        null,
        "network"
      );
    }
  }

  /* ------------------------------ Core request ----------------------------- */
  // `_retry` guards a single refresh-and-retry pass to avoid loops.
  async function request(path, opts = {}, _retry = false) {
    let response;
    try {
      response = await requestOnce(path, opts);
    } catch (err) {
      // On a 401 (expired access token) attempt exactly one refresh, then retry
      // the original request once. Prevents infinite refresh loops.
      // IMPORTANT: only for requests that actually carried a token. A 401 from
      // a public request (e.g. wrong password on /auth/login/) is a real,
      // expected failure and must surface to the caller as-is.
      const wasAuthenticated = opts.auth !== false && !!tokenProvider();
      if (err instanceof APIError && err.status === 401 && !_retry && wasAuthenticated && refreshProvider) {
        const refreshed = await refreshProvider();
        if (refreshed) {
          return request(path, opts, true);
        }
        // Refresh failed -> clear session and notify, preserving the original
        // destination for post-login return.
        if (window.Auth && typeof window.Auth.onUnauthorized === "function") {
          window.Auth.onUnauthorized();
        }
      }
      throw err;
    }
    return response;
  }

  /* ------------------------------ Verbs ------------------------------------ */
  const get = (path, opts = {}) => request(path, { ...opts, method: "GET" });
  const getBlob = (path, opts = {}) => request(path, { ...opts, method: "GET", responseType: "blob" });
  const post = (path, body, opts = {}) => request(path, { ...opts, method: "POST", body });
  const put = (path, body, opts = {}) => request(path, { ...opts, method: "PUT", body });
  const patch = (path, body, opts = {}) => request(path, { ...opts, method: "PATCH", body });
  const del = (path, opts = {}) => request(path, { ...opts, method: "DELETE" });

  /* --------------------------- Binary downloads ----------------------------
     getBlob resolves to the SAME response envelope as every other verb, so the
     payload is `res.data` — NOT the response object. Passing the envelope to
     URL.createObjectURL() throws and the download silently fails (D14), so
     browsers must never call createObjectURL themselves. Use this helper. */
  const MIME_BY_EXTENSION = {
    csv: "text/csv",
    xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    pdf: "application/pdf"
  };

  function extensionOf(filename) {
    const match = /\.([a-z0-9]+)$/i.exec(String(filename || ""));
    return match ? match[1].toLowerCase() : "";
  }

  async function downloadFile(path, { filename, params, mimeType, expectedExtensions, ...opts } = {}) {
    const response = await getBlob(path, { params, cache: "no-store", ...opts });
    const payload = response && response.data;
    if (!(typeof Blob !== "undefined" && payload instanceof Blob) || !payload.size) {
      throw new APIError(0, "The server did not return a downloadable file. Please try again.", null, "parse");
    }
    const extension = extensionOf(filename);
    /* Validate the declared response type against the requested file: a
       mislabelled body would still "download" and then fail to open. */
    const headerType = (response.headers && response.headers.get("content-type") || "").split(";")[0].trim().toLowerCase();
    const expected = expectedExtensions || (extension ? [extension] : []);
    if (headerType && headerType !== "application/octet-stream") {
      const wanted = mimeType || MIME_BY_EXTENSION[extension] || "";
      if (wanted && headerType !== wanted) {
        throw new APIError(
          0,
          `The server returned "${headerType}" instead of a ${extension.toUpperCase()} file. The export was not saved.`,
          { code: "UNEXPECTED_EXPORT_TYPE", contentType: headerType, expected: wanted }
        );
      }
    }
    if (!expected.length) {
      throw new APIError(0, "An export file name with an extension is required.", { code: "MISSING_FILENAME" });
    }
    const blob = mimeType ? new Blob([payload], { type: mimeType }) : payload;
    const url = URL.createObjectURL(blob);
    try {
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      link.rel = "noopener";
      link.style.display = "none";
      document.body.appendChild(link);
      link.click();
      link.remove();
    } finally {
      /* Revoke on the next macrotask so the browser has started the download
         (an immediate revoke cancels it in some Safari/Firefox builds). */
      setTimeout(() => { try { URL.revokeObjectURL(url); } catch (_) {} }, 0);
    }
    return { filename, size: blob.size, contentType: blob.type || headerType };
  }

  function downloadReport(path, kind, { start, end, filename, ...opts } = {}) {
    const extension = String(kind || "").toLowerCase().replace(/^\./, "");
    if (!MIME_BY_EXTENSION[extension]) {
      return Promise.reject(
        new APIError(0, `"${kind}" is not a supported export format.`, { code: "UNSUPPORTED_EXPORT_FORMAT" })
      );
    }
    return downloadFile(path, {
      filename: filename || `report-${start}_${end}.${extension}`,
      mimeType: MIME_BY_EXTENSION[extension],
      params: { ...(opts.params || {}), start, end, export: extension },
      ...opts
    });
  }

  /* ------------------------- Response helpers ------------------------------ */

  /* Normalize a list response to { items, count, next, previous, page, pageSize, totalPages }.
     Accepts the verified contract envelope (data array + pagination object),
     a bare array, or a legacy {results} shape — all without ever inventing rows. */
  // The fallback page size mirrors APP_CONFIG.PAGE_SIZE (and the backend's
  // StandardPagination.page_size) so a response without a pagination block is
  // still paged consistently with every other list in the app.
  const DEFAULT_PAGE_SIZE =
    (window.APP_CONFIG && window.APP_CONFIG.PAGE_SIZE) || 10;

  function normalizeList(payload, pagination, defaultPageSize = DEFAULT_PAGE_SIZE) {
    // Contract shape: API layer passes (res.data, res.pagination).
    if (Array.isArray(payload)) {
      const pg = pagination || {};
      const count = pg.count != null ? pg.count : payload.length;
      const pageSize = pg.page_size || defaultPageSize;
      return {
        items: payload,
        count,
        next: pg.next || null,
        previous: pg.previous || null,
        page: pg.page || 1,
        pageSize,
        totalPages: pg.total_pages != null ? pg.total_pages : Math.max(1, Math.ceil(count / pageSize) || 1)
      };
    }
    if (payload && typeof payload === "object") {
      // Legacy/defensive: {results, count} or response object with .data
      if (Array.isArray(payload.results)) {
        const count = payload.count != null ? payload.count : payload.results.length;
        return {
          items: payload.results,
          count,
          next: payload.next || null,
          previous: payload.previous || null,
          page: 1,
          pageSize: defaultPageSize,
          totalPages: Math.max(1, Math.ceil(count / defaultPageSize) || 1)
        };
      }
      if (payload.data !== undefined) return normalizeList(payload.data, payload.pagination, defaultPageSize);
    }
    return { items: [], count: 0, next: null, previous: null, page: 1, pageSize: defaultPageSize, totalPages: 1 };
  }

  /* Unwrap helper: contract responses are { success, message, data } — return data. */
  function unwrap(res) {
    // res.data is already the unwrapped payload (envelope is stripped in
    // requestOnce). Returns the payload, or the raw value for bare inputs.
    return res && res.data !== undefined ? res.data : res;
  }

  /* Assert a raw value is safe to render (never undefined/null/NaN). */
  function safe(value, fallback = "") {
    if (value === undefined || value === null) return fallback;
    if (typeof value === "number" && Number.isNaN(value)) return fallback;
    return value;
  }

  /* --------------------------- PUBLIC ENDPOINTS ----------------------------
     Paths verified against the live backend (see config.js for base URL). */

  /* Hotel info & policies */
  function getHotelInfo(opts = {}) { return get("/api/hotel/", { auth: false, ...opts }); }
  function getPolicies(opts = {}) { return get("/api/hotel/policies/", { auth: false, ...opts }); }

  /* Rooms catalog (room types) */
  function getRooms(params, opts = {}) { return get("/api/rooms/", { params, auth: false, ...opts }); }
  function getRoom(slugOrId, opts = {}) { return get(`/api/rooms/${encodeURIComponent(slugOrId)}/`, { auth: false, ...opts }); }

  /* The physical rooms behind one room type — what "Book this room — Room 203"
     needs. Verified: GET /api/rooms/{slug}/rooms/?check_in=&check_out=
     Returns [{ id, room_number, floor, available|null }]. Dates are optional:
     without them `available` is null and the room is simply listed. */
  function getRoomTypeRooms(slugOrId, params, opts = {}) {
    return get(`/api/rooms/${encodeURIComponent(slugOrId)}/rooms/`, { params, auth: false, ...opts });
  }

  /* Availability — the AUTHORITATIVE search (backend computes everything). */
  function checkAvailability(params, opts = {}) {
    return get("/api/rooms/availability/", { params, auth: false, ...opts });
  }
  function getUnavailableDates(roomType, params = {}, opts = {}) {
    return get(`/api/rooms/${encodeURIComponent(roomType)}/unavailable-dates/`, { params, auth: false, ...opts });
  }

  /* Offers / facilities / gallery */
  function getOffers(params, opts = {}) { return get("/api/offers/", { params, auth: false, ...opts }); }
  function getFacilities(params, opts = {}) { return get("/api/facilities/", { params, auth: false, ...opts }); }
  function getGallery(params, opts = {}) { return get("/api/gallery/", { params, auth: false, ...opts }); }

  /* Enquiries / contact (honeypot `website` field must stay blank). */
  function submitEnquiry(payload, opts = {}) { return post("/api/enquiries/", payload, { auth: false, ...opts }); }

  /* ------------------------------ REVIEWS ----------------------------------
     Guest review flow (verified completed stays only). Both endpoints are
     POST so the reference/email pair never lands in access logs. Submitted
     reviews are PRIVATE — there is no public listing endpoint; management is
     administrator-only under /api/admin/reviews/ (via API.list/getOne/...). */
  function verifyReviewStay(payload, opts = {}) { return post("/api/reviews/verify/", payload, { auth: false, ...opts }); }
  function submitReview(payload, opts = {}) { return post("/api/reviews/", payload, { auth: false, ...opts }); }
  function getReviewStats(opts = {}) {
    const base = resourceEp("reviews");
    if (!base) throw notConfigured("reviews");
    return get(base + "/stats/", opts);
  }
  function submitCancellationRequest(payload, opts = {}) {
    return post("/api/enquiries/", { ...payload, enquiry_type: "CANCELLATION", subject: "Cancellation / refund request" }, { auth: false, ...opts });
  }
  function getCancellationStatus(reference, token, opts = {}) {
    const headers = token ? { "X-Cancellation-Access-Token": token, ...(opts.headers || {}) } : (opts.headers || {});
    const params = { ...(opts.params || {}) };
    if (token) params.token = token;
    return get(`/api/enquiries/cancellation-status/${encodeURIComponent(reference)}/`, {
      ...opts,
      auth: false,
      params,
      headers
    });
  }

  /* ------------------------ BOOKING FLOW (guest) ---------------------------
     Quote and creation are public. Subsequent booking/payment operations use
     either staff/owner JWT or the booking-scoped guest access token. */

  /* Quote — authoritative price preview. Nothing is persisted. */
  function quoteBooking(payload, opts = {}) { return post("/api/bookings/quote/", payload, { auth: false, ...opts }); }

  /* Create booking (guest checkout supported). The backend validates availability and
     computes every amount; the response is the authoritative booking detail. */
  function createBooking(payload, opts = {}) { return post("/api/bookings/", payload, { auth: false, ...opts }); }

  /* My bookings (auth required, paginated, ?status= filter supported). */
  function myBookings(params, opts = {}) { return get("/api/bookings/", { params, ...opts }); }

  /* Booking detail by id or booking_reference (auth + owner). */
  /* A guest request must NEVER carry an account session. Without this, a stale
     dashboard JWT left in the same browser (a receptionist's phone, a guest who
     also has a staff login) is attached to a public booking/payment call and the
     API answers 401 before the booking token is even read — which the page can
     only report as "this device is no longer recognised". `auth:false` is set
     even when there is no token, so the outcome cannot depend on one. */
  function guestAccessOpts(token) {
    token = token ? String(token) : "";
    return token ? { auth: false, headers: { "X-Guest-Access-Token": token } } : { auth: false };
  }

  /* Emailed secure links.
     A guest token lives in one browser's storage. The link in every booking
     email carries a signed `access` value instead, which this call exchanges
     for a normal token — so a guest on a phone, or one who cleared site data,
     can still open and pay for their booking. The value is bound to the
     reference and expires; it is never stored, only exchanged. */
  /* Reclaim a booking this browser created, using the random key it generated
     for that creation (no email round-trip). Same endpoint, same 404. */
  function reclaimBookingAccess(lookup, idempotencyKey, opts = {}) {
    return post(
      `/api/bookings/${encodeURIComponent(lookup)}/access/`,
      { idempotency_key: String(idempotencyKey || "") },
      { auth: false, ...opts }
    );
  }

  function exchangeBookingAccess(lookup, access, opts = {}) {
    return post(
      `/api/bookings/${encodeURIComponent(lookup)}/access/`,
      { access: String(access || "") },
      { auth: false, ...opts }
    );
  }

  /* Ask the hotel system to email a fresh secure link. The response is
     deliberately identical whether or not the reference exists, so this can
     never be used to discover bookings. */
  function resendBookingAccessLink(lookup, opts = {}) {
    return post(`/api/bookings/${encodeURIComponent(lookup)}/access-link/`, {}, { auth: false, ...opts });
  }
  function getBooking(lookup, opts = {}) { return get(`/api/bookings/${encodeURIComponent(lookup)}/`, opts); }

  /* Receipt for a booking (auth + owner) — renders the confirmation page. */
  function getBookingReceipt(lookup, opts = {}) {
    return get(`/api/bookings/${encodeURIComponent(lookup)}/receipt/`, opts);
  }

  /* Staff payment workspace: separate capability-gated route. This does not
     weaken the guest-owned public booking/receipt endpoint above. */
  function getStaffBookingReceipt(lookup, opts = {}) {
    return get(`/api/admin/bookings/${encodeURIComponent(lookup)}/receipt/`, opts);
  }

  /* --------------------------- PAYMENTS (Paystack) -------------------------
     The frontend never holds a Paystack secret. Initialize returns the
     backend-generated payment reference + authorization_url; success is ONLY
     ever confirmed by verifyPayment (or the server-side webhook). */

  function initPayment(bookingReference, opts = {}) {
    return post("/api/payments/initialize/", { booking_reference: bookingReference }, opts);
  }
  function verifyPayment(paymentReference, opts = {}) {
    return get(`/api/payments/verify/${encodeURIComponent(paymentReference)}/`, opts);
  }

  /* ------------------------------ AUTH ------------------------------------- */

  function login(payload, opts = {}) { return post("/api/auth/login/", payload, { auth: false, ...opts }); }
  /* Logout needs an authenticated call (the backend requires a valid JWT to
     blacklist the refresh token). The access token is attached when one is
     stored — and the standard 401→refresh→retry pass in request() covers an
     expired access token, so a normal logout no longer trips a pointless 401.
     With no token at all (already signed out) we still call so the backend
     blacklist gets a chance; a 401 there is swallowed by Auth.logout(). */
  function logout(refreshToken, opts = {}) {
    return post("/api/auth/logout/", refreshToken ? { refresh: refreshToken } : {}, opts);
  }
  function me(opts = {}) { return get("/api/auth/profile/", opts); }
  function refreshTokenCall(refresh, opts = {}) {
    return post("/api/auth/token/refresh/", { refresh }, { auth: false, ...opts });
  }

  /* --------------------- NOTIFICATIONS (authenticated) --------------------- */

  function getNotifications(params, opts = {}) { return get("/api/notifications/", { params, ...opts }); }
  function getUnreadCount(opts = {}) { return get("/api/notifications/unread-count/", opts); }
  function markNotificationRead(id, opts = {}) { return post(`/api/notifications/${encodeURIComponent(id)}/read/`, {}, opts); }
  /* Full notification for the details page. Verified:
     GET /api/notifications/{id}/ — the backend scopes the queryset to the
     signed-in recipient, so another user's id is a 404. Opening the detail
     marks it read and returns the fresh unread_count. */
  function getNotification(id, opts = {}) { return get(`/api/notifications/${encodeURIComponent(id)}/`, opts); }
  function markAllNotificationsRead(opts = {}) { return post("/api/notifications/read-all/", {}, opts); }

  /* ======================================================================
     STAFF/DASHBOARD RESOURCES — resolved from APP_CONFIG.API_ENDPOINTS
     (the single place staff paths are declared; verified against the real
     backend). A resource that is not configured fails with a clear
     APIError so the page shows an honest error state.
     ====================================================================== */
  const EPS = (window.APP_CONFIG && window.APP_CONFIG.API_ENDPOINTS) || {};
  function resourceEp(name) {
    const p = EPS[name];
    return p ? p.replace(/\/$/, "") : "";
  }

  function notConfigured(name) {
    return new APIError(0, `The "${name}" module isn't configured. Please contact the administrator.`);
  }

  // List a staff resource. Throws an APIError(0) if not configured.
  async function list(name, params, opts = {}) {
    const base = resourceEp(name);
    if (!base) throw notConfigured(name);
    return get(base + "/", { params, ...opts });
  }
  // Get a single staff resource by id/slug/reference.
  async function getOne(name, id, opts = {}) {
    const base = resourceEp(name);
    if (!base) throw notConfigured(name);
    return get(`${base}/${encodeURIComponent(id)}/`, opts);
  }
  // Create a staff resource. Returns backend-created record; success only on ok.
  async function create(name, payload, opts = {}) {
    const base = resourceEp(name);
    if (!base) throw notConfigured(name);
    return post(base + "/", payload, opts);
  }
  async function update(name, id, payload, opts = {}) {
    const base = resourceEp(name);
    if (!base) throw notConfigured(name);
    return patch(`${base}/${encodeURIComponent(id)}/`, payload, opts);
  }
  /* Batch 01 reorder dialog: everything the screen needs about ONE stock item
     (identity, purchasing references, per-store balances, last actual purchase
     cost, quantity already on order). One bounded read — the browser never
     copies master data or guesses a price. */
  async function inventoryReorderContext(itemId, opts = {}) {
    const base = resourceEp("procurementItems") || resourceEp("inventoryItems");
    if (!base) throw notConfigured("procurementItems");
    return get(`${base}/${encodeURIComponent(itemId)}/reorder-context/`, opts);
  }

  async function remove(name, id, opts = {}) {
    const base = resourceEp(name);
    if (!base) throw notConfigured(name);
    return del(`${base}/${encodeURIComponent(id)}/`, opts);
  }
  /* Controlled operation endpoints consistently use /{reference}/{action}/.
     Keep URL construction here so pages never hard-code deployment paths. */
  function resourceAction(name, id, action, payload, opts = {}) {
    const base = resourceEp(name);
    if (!base) throw notConfigured(name);
    // Some controlled actions have a nested static segment (for example
    // outage/start). Escape each segment while preserving the route separator.
    const safeAction = String(action || "").split("/").filter(Boolean).map(encodeURIComponent).join("/");
    if (!safeAction) throw new APIError(0, "An operation action is required.");
    return post(`${base}/${encodeURIComponent(id)}/${safeAction}/`, payload || {}, opts);
  }

  /* -------------------- STAFF BOOKING ACTIONS (verified) -------------------
     All are POST /api/admin/bookings/{lookup}/<action>/ — lookup is the
     booking id or booking_reference. Bodies: {} or { reason? }; check-out
     also accepts { allow_balance_due? }. */

  const BOOKINGS_EP = () => resourceEp("bookings");
  function bookingAction(action, lookup, payload, opts = {}) {
    const base = BOOKINGS_EP();
    if (!base) throw notConfigured("bookings");
    return post(`${base}/${encodeURIComponent(lookup)}/${action}/`, payload || {}, opts);
  }
  const confirmBooking   = (lookup, opts = {}) => bookingAction("confirm", lookup, {}, opts);
  const staffCancelBooking = (lookup, reason, opts = {}) => bookingAction("cancel", lookup, reason ? { reason } : {}, opts);
  const checkInBooking   = (lookup, opts = {}) => bookingAction("check-in", lookup, {}, opts);
  const checkOutBooking  = (lookup, payload, opts = {}) => bookingAction("check-out", lookup, payload || {}, opts);
  /* Checkout-desk folio & gate-clearance read (folio.view capability):
     GET /api/admin/bookings/{lookup}/folio-clearance/

     Money is stated, never implied. `charges` / `charges_total` are the guest's
     purchases and service charges; `credits` / `credits_total` are the
     discounts, adjustments, reversals and folio payments that settle THEM; and
     `accommodation_settlements` / `accommodation_settlements_total` are the
     payments that settle the ROOM. Accommodation money is listed separately on
     purpose: it is already counted in `accommodation.amount_paid`, so netting it
     against the guest's purchases made a fully paid room hide an unpaid laundry
     bill ("purchases and charges −416,800", "amount to settle 0", gate pass
     cleared). `outstanding_balance` / `amount_to_settle` and the
     `gate_pass_allowed` decision are the server's and must never be re-derived
     in the browser. */
  const bookingFolioClearance = (lookup, opts = {}) => {
    const base = BOOKINGS_EP();
    if (!base) throw notConfigured("bookings");
    return get(`${base}/${encodeURIComponent(lookup)}/folio-clearance/`, opts);
  };
  /* Desk settlement of the guest's WHOLE bill (stay balance + folio charges),
     the money the staff member just received:
         POST /api/admin/bookings/{lookup}/settle-balance/
             { amount, provider, notes?, include_unbilled?, cash_session_reference? }
     It answers with the payment AND the refreshed folio read, so the check-out
     dialog and the gate-pass decision update from the same transaction. */
  const settleBookingBalance = (lookup, payload, opts = {}) => {
    const base = BOOKINGS_EP();
    if (!base) throw notConfigured("bookings");
    return post(`${base}/${encodeURIComponent(lookup)}/settle-balance/`, payload || {}, opts);
  };
  const noShowBooking    = (lookup, opts = {}) => bookingAction("no-show", lookup, {}, opts);
  /* Room guests + per-room check-in (multi-room bookings).
       PUT  /api/admin/bookings/{lookup}/room-guests/
            { rooms: [{ assignment_id, guests: [{email, full_name?, phone?}] }] }
       POST /api/admin/bookings/{lookup}/rooms/{assignment_id}/check-in/
     The PUT is a REPLACE of the whole room list, so callers always send every
     room they want to keep. Both answer with the refreshed booking detail. */
  const saveBookingRoomGuests = (lookup, rooms, opts = {}) => {
    const base = BOOKINGS_EP();
    if (!base) throw notConfigured("bookings");
    return put(`${base}/${encodeURIComponent(lookup)}/room-guests/`, { rooms: rooms || [] }, opts);
  };
  const checkInBookingRoom = (lookup, assignmentId, opts = {}) => {
    const base = BOOKINGS_EP();
    if (!base) throw notConfigured("bookings");
    const id = Number.parseInt(String(assignmentId ?? ""), 10);
    if (!Number.isInteger(id) || id < 1) {
      throw new APIError(0, "A valid room assignment is required to check a room in.", {
        code: "INVALID_ROOM_ASSIGNMENT",
      });
    }
    return post(`${base}/${encodeURIComponent(lookup)}/rooms/${id}/check-in/`, {}, opts);
  };
  /* Re-point one existing reservation row at a different physical room.
     Contract (apps/bookings/serializers_admin.py → AssignRoomSerializer):
         { assignment_id, room_id }   → POST /api/admin/bookings/{lookup}/assign-room/
     The booking must already have the target assignment row; the backend
     re-validates room type, operational readiness, dates and overlapping
     reservations under a lock. Sending a bare { room } used to be accepted as
     a no-op success (D7) — both identifiers are now required and validated
     here, before the request leaves the browser. */
  const assignRoom = (lookup, assignmentId, roomId, opts = {}) => {
    const asPositiveInt = (value, label) => {
      const numeric = typeof value === "number" ? value : Number.parseInt(String(value ?? ""), 10);
      if (!Number.isInteger(numeric) || numeric < 1) {
        throw new APIError(0, `A valid ${label} is required to change a room assignment.`, {
          code: "INVALID_ROOM_ASSIGNMENT",
        });
      }
      return numeric;
    };
    return bookingAction(
      "assign-room",
      lookup,
      { assignment_id: asPositiveInt(assignmentId, "assignment id"), room_id: asPositiveInt(roomId, "room id") },
      opts
    );
  };
  /* Move an existing booking to new dates. The backend re-checks availability
     and re-prices the stay; the response carries the authoritative booking. */
  const rescheduleBooking = (lookup, payload, opts = {}) =>
    bookingAction("reschedule", lookup, payload || {}, opts);

  /* Month occupancy grid: { year, month, start_date, end_date,
     days: { "YYYY-MM-DD": [ { booking_id, booking_reference, room_number,
     room_type_name, guest_name, status, check_in, check_out } ] } }. */
  function getOccupancyCalendar(params, opts = {}) {
    const base = resourceEp("occupancy");
    if (!base) throw notConfigured("occupancy");
    return get(base, { params, ...opts });
  }

  /* Bookings whose guest never arrived (derived server-side). */
  function listMissedBookings(params, opts = {}) {
    const base = resourceEp("missedBookings");
    if (!base) throw notConfigured("missedBookings");
    return get(base, { params, ...opts });
  }

  /* Confirmed guests who missed the first night but are still inside their
     booked range, so the desk can still check them in (derived server-side). */
  function listLateArrivals(params, opts = {}) {
    const base = resourceEp("lateArrivals");
    if (!base) throw notConfigured("lateArrivals");
    return get(base, { params, ...opts });
  }

  /* Individual guest discounts (manager/admin write, all staff read). */
  function listGuestDiscounts(params, opts = {}) { return list("guestDiscounts", params, opts); }
  function createGuestDiscount(payload, opts = {}) { return create("guestDiscounts", payload, opts); }
  function updateGuestDiscount(id, payload, opts = {}) { return update("guestDiscounts", id, payload, opts); }
  function deactivateGuestDiscount(id, opts = {}) { return remove("guestDiscounts", id, opts); }

  /* Record an offline payment (CASH / POS / BANK_TRANSFER).
     The response carries the payment plus the authoritative booking snapshot
     (total / paid / due / status) so the payment modal never needs a second
     request and never has to reload the whole bookings table. */
  function recordPayment(payload, opts = {}) {
    const base = resourceEp("payments");
    if (!base) throw notConfigured("payments");
    return post(base + "/record/", payload, opts);
  }

  /* Payroll salary movements are manual records, never payout instructions. */
  function payrollLinePaymentsPath(periodReference, lineId) {
    const base = resourceEp("payrollPeriods");
    if (!base) throw notConfigured("payrollPeriods");
    return `${base}/${encodeURIComponent(periodReference)}/lines/${encodeURIComponent(lineId)}/payments/`;
  }
  function listPayrollSalaryPayments(periodReference, lineId, params = {}, opts = {}) {
    return get(payrollLinePaymentsPath(periodReference, lineId), { params, ...opts });
  }
  function recordPayrollSalaryPayment(periodReference, lineId, payload, opts = {}) {
    return post(payrollLinePaymentsPath(periodReference, lineId), payload || {}, opts);
  }
  function reversePayrollSalaryPayment(periodReference, lineId, paymentReference, payload, opts = {}) {
    return post(
      `${payrollLinePaymentsPath(periodReference, lineId)}${encodeURIComponent(paymentReference)}/reverse/`,
      payload || {},
      opts,
    );
  }

  /* ----------------------- POS / room-service actions ---------------------- */
  const POS_ORDERS_EP = () => resourceEp("posOrders");
  function posOrderAction(reference, action, payload, opts = {}) {
    const base = POS_ORDERS_EP();
    if (!base) throw notConfigured("posOrders");
    return post(`${base}/${encodeURIComponent(reference)}/${action}/`, payload || {}, opts);
  }
  const submitPosOrder = (reference, payload, opts = {}) => posOrderAction(reference, "submit", payload || {}, opts);
  const updatePosOrderStatus = (reference, status, opts = {}) => posOrderAction(reference, "status", { status }, opts);
  const capturePosTender = (reference, payload, opts = {}) => posOrderAction(reference, "tenders", payload, opts);
  /* --- Bar/restaurant unopened-item returns ------------------------------ */
  function posReturnAction(reference, action, payload, opts = {}) {
    const base = resourceEp("posOrders");
    if (!base) throw notConfigured("posOrders");
    return post(`${base}/${encodeURIComponent(reference)}/${action}/`, payload || {}, opts);
  }
  /* Orders that can still take an unopened-item return (delivered, bar or
     restaurant, charged, within the caller's service area). */
  function listReturnablePosOrders(params = {}, opts = {}) {
    const base = POS_ORDERS_EP();
    if (!base) throw notConfigured("posOrders");
    return get(`${base}/returns/eligible/`, { params, ...opts });
  }
  function listPosOrderReturns(params = {}, opts = {}) {
    const base = POS_ORDERS_EP();
    if (!base) throw notConfigured("posOrders");
    return get(`${base}/returns/`, { params, ...opts });
  }
  function previewPosOrderReturn(reference, opts = {}) {
    return posReturnAction(reference, "return-preview", {}, opts);
  }
  function recordPosOrderReturn(reference, payload, opts = {}) {
    return posReturnAction(reference, "returns", payload, opts);
  }
  function confirmPosReturnRefund(returnReference, payload, opts = {}) {
    const base = POS_ORDERS_EP();
    if (!base) throw notConfigured("posOrders");
    return post(`${base}/returns/${encodeURIComponent(returnReference)}/refund/`, payload || {}, opts);
  }
  function updatePosKitchenTicketStatus(id, status, opts = {}) {
    const base = resourceEp("posKitchenTickets");
    if (!base) throw notConfigured("posKitchenTickets");
    return post(`${base}/${encodeURIComponent(id)}/status/`, { status }, opts);
  }
  function openPosCashSession(payload, opts = {}) {
    const base = resourceEp("posCashSessions");
    if (!base) throw notConfigured("posCashSessions");
    return post(base + "/open/", payload || {}, opts);
  }
  function closePosCashSession(reference, payload, opts = {}) {
    const base = resourceEp("posCashSessions");
    if (!base) throw notConfigured("posCashSessions");
    return post(`${base}/${encodeURIComponent(reference)}/close/`, payload || {}, opts);
  }

  function enquiryAction(id, action, payload, opts = {}) {
    const base = resourceEp("enquiries");
    if (!base) throw notConfigured("enquiries");
    return post(`${base}/${encodeURIComponent(id)}/${action}/`, payload || {}, opts);
  }
  const reviewCancellationRequest = (id, payload, opts = {}) => enquiryAction(id, "review", payload, opts);
  const approveCancellationRequest = (id, payload, opts = {}) => enquiryAction(id, "approve-cancellation", payload, opts);
  const rejectCancellationRequest = (id, payload, opts = {}) => enquiryAction(id, "reject-cancellation", payload, opts);
  const processCancellationRefund = (id, payload, opts = {}) => enquiryAction(id, "process-refund", payload, opts);
  const closeCancellationRequest = (id, payload, opts = {}) => enquiryAction(id, "close", payload, opts);
  function listRefunds(params, opts = {}) { return list("refunds", params, opts); }

  /* --- Operational search ------------------------------------------------
     Every search below runs SERVER-SIDE: the backend matches booking
     reference, guest name/email/phone, physical room number, room type and
     payment/receipt reference (partial + case-insensitive). The frontend
     never downloads a full list to filter it locally. */
  function searchBookings(params, opts = {}) { return list("bookings", params, opts); }

  /* Checkout desk search: only bookings that can actually be checked out
     (in-house or confirmed) and only `page_size` rows back. */
  function searchCheckout(term, opts = {}) {
    return list("bookings", {
      status: "CHECKED_IN,CONFIRMED",
      search: term,
      page_size: 25,
      ...(opts.params || {}),
    }, opts);
  }

  /* The address the SERVER sees this browser arriving from.
     Verified: GET /api/auth/client-address/ (any signed-in account; returns
     only the caller's own address). Used when registering a front desk
     desktop, because the address that matters is the one the server observes:
     behind the deployment's proxy or the hotel's carrier NAT that is NOT what
     the desk sees on "what is my IP", and the two differ by exactly the reason
     sign-in kept being refused. */
  function getClientAddress(opts = {}) {
    const path = EPS.clientAddress;
    if (!path) throw endpointConfigError("clientAddress");
    return get(path, opts);
  }

  /* Staff profile card. Verified: GET /api/admin/users/staff/{id}/ — admins
     and managers may open any card, receptionists only their own. */
  function getStaffProfile(id, opts = {}) {
    const base = resourceEp("users");
    if (!base) throw notConfigured("users");
    return get(`${base}/staff/${encodeURIComponent(id)}/`, opts);
  }

  /* Email the receipt for a booking to the guest (staff action).
     The backend delivers SYNCHRONOUSLY: it performs the SMTP send during the
     request and only then answers — 200 with data.status === "SENT" means the
     mail server actually accepted the message; a 502 throws an APIError
     carrying a safe version of the real failure reason. There is no queued
     state in this flow. */
  function sendReceipt(lookup, payloadOrOpts = {}, opts = {}) {
    const base = BOOKINGS_EP();
    if (!base) throw notConfigured("bookings");
    let body = {};
    let reqOpts = { ...opts };
    if (payloadOrOpts && typeof payloadOrOpts === "object") {
      const { recipient_email, idempotency_key, force, force_resend, ...rest } = payloadOrOpts;
      if (recipient_email !== undefined) body.recipient_email = recipient_email;
      if (idempotency_key !== undefined) body.idempotency_key = idempotency_key;
      if (force !== undefined) body.force = force;
      if (force_resend !== undefined) body.force_resend = force_resend;
      reqOpts = { ...rest, ...reqOpts };
    }
    const headers = { ...(reqOpts.headers || {}) };
    if (body.idempotency_key && !headers["Idempotency-Key"]) {
      headers["Idempotency-Key"] = String(body.idempotency_key);
    }
    // Receipt delivery involves PDF generation and a real SMTP handshake,
    // which legitimately takes longer than a normal API call. Use a timeout
    // that matches the server's request timeout so a slow-but-successful send
    // is not aborted as a false "timeout" on the client. Callers may still
    // override via opts.timeout.
    return post(`${base}/${encodeURIComponent(lookup)}/send-receipt/`, body, { timeout: 60000, ...reqOpts, headers });
  }

  /* Read the recorded delivery state of a transactional email (EmailLog). */
  function getEmailLog(id, opts = {}) {
    return get(`/api/notifications/emails/${encodeURIComponent(id)}/`, opts);
  }

  /* Staff email delivery log (optionally filtered ?status=FAILED / ?booking=). */
  function listEmailLogs(params = {}, opts = {}) {
    return get(`/api/notifications/emails/`, { ...opts, params });
  }

  return {
    get, getBlob, post, put, patch, del, request,
    downloadFile, downloadReport,
    // Public site
    getHotelInfo, getPolicies, getRooms, getRoom, checkAvailability, getUnavailableDates,
    getOffers, getFacilities, getGallery, submitEnquiry, submitCancellationRequest, getCancellationStatus,
    verifyReviewStay, submitReview, getReviewStats,
    // Booking flow (guest)
    quoteBooking, createBooking, myBookings, getBooking, guestAccessOpts, getBookingReceipt,
    exchangeBookingAccess, reclaimBookingAccess, resendBookingAccessLink,
    // Payments
    initPayment, verifyPayment,
    // Auth
    login, logout, me, refreshTokenCall, getClientAddress,
    // Notifications
    getNotifications, getUnreadCount, markNotificationRead, markAllNotificationsRead, getNotification,
    // Staff resources + actions
    list, getOne, getStaffBookingReceipt, create, update, remove, resourceAction, inventoryReorderContext,
    rescheduleBooking, getOccupancyCalendar, listMissedBookings, listLateArrivals,
    listGuestDiscounts, createGuestDiscount, updateGuestDiscount, deactivateGuestDiscount,
    confirmBooking, staffCancelBooking, checkInBooking, checkOutBooking, bookingFolioClearance,
    saveBookingRoomGuests, checkInBookingRoom,
    settleBookingBalance,
    noShowBooking, assignRoom, recordPayment, listPayrollSalaryPayments,
    recordPayrollSalaryPayment, reversePayrollSalaryPayment, searchBookings, searchCheckout,
    submitPosOrder, updatePosOrderStatus, capturePosTender, updatePosKitchenTicketStatus, openPosCashSession, closePosCashSession,
    listReturnablePosOrders, listPosOrderReturns, previewPosOrderReturn, recordPosOrderReturn, confirmPosReturnRefund,
    reviewCancellationRequest, approveCancellationRequest, rejectCancellationRequest, processCancellationRefund, closeCancellationRequest, listRefunds,
    getStaffProfile, sendReceipt, getEmailLog, listEmailLogs, getRoomTypeRooms,
    // Helpers
    setTokenProvider, setRefreshProvider, setWorkstationReference, getWorkstationReference, APIError, BASE,
    normalizeList, unwrap, safe
  };
})();

window.API = API;
